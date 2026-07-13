"""监控服务的核心行为测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from x2feishu_monitor.clients import FeishuClient, XClient
from x2feishu_monitor.config import ConfigError, Settings
from x2feishu_monitor.models import Post
from x2feishu_monitor.service import MonitorService, format_post_message
from x2feishu_monitor.state import StateStore


def make_settings(database_path: Path) -> Settings:
    """创建不包含真实密钥的测试配置。"""
    return Settings.from_env(
        {
            "X_BEARER_TOKEN": "test-token",
            "X_USER_ID": "123456789",
            "X_USERNAME": "example_user",
            "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
            "FEISHU_KEYWORD": "X 更新",
            "STATE_DB_PATH": str(database_path),
            "POLL_INTERVAL_SECONDS": "300",
        }
    )


class FakeResponse:
    """模拟 requests 响应。"""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        """返回预设 JSON。"""
        return self.payload

    def raise_for_status(self) -> None:
        """测试仅覆盖成功响应。"""


class FakeSession:
    """记录请求并按顺序返回预设响应。"""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        """记录 GET 请求并返回下一响应。"""
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """记录 POST 请求并返回下一响应。"""
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class FakeXClient:
    """为业务流程提供预设帖子。"""

    def __init__(self, batches: list[list[Post]]) -> None:
        self.batches = batches
        self.since_ids: list[str | None] = []

    def fetch_posts(
        self,
        since_id: str | None,
        max_pages: int | None = None,
        max_results: int | None = None,
        require_complete: bool = True,
    ) -> list[Post]:
        """记录游标并返回下一批帖子。"""
        self.since_ids.append(since_id)
        return self.batches.pop(0)


class FakeFeishuClient:
    """记录业务流程生成的飞书消息。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_text(self, text: str) -> None:
        """记录文本消息。"""
        self.messages.append(text)


class ConfigTests(unittest.TestCase):
    """配置加载测试。"""

    def test_missing_required_value_is_rejected(self) -> None:
        """缺少密钥时应明确失败。"""
        with self.assertRaises(ConfigError):
            Settings.from_env({})


class ClientTests(unittest.TestCase):
    """外部客户端测试。"""

    def test_x_client_uses_since_id_and_pagination(self) -> None:
        """X 客户端应携带游标并读取下一页。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            session = FakeSession(
                [
                    FakeResponse(
                        {
                            "data": [{"id": "102", "text": "新帖二"}],
                            "meta": {"next_token": "next-page"},
                        }
                    ),
                    FakeResponse({"data": [{"id": "101", "text": "新帖一"}], "meta": {}}),
                ]
            )
            posts = XClient(settings, session=session).fetch_posts("100")

        self.assertEqual({post.id for post in posts}, {"101", "102"})
        self.assertEqual(session.calls[0][2]["params"]["since_id"], "100")
        self.assertEqual(session.calls[1][2]["params"]["pagination_token"], "next-page")
        self.assertEqual(session.calls[0][2]["params"]["exclude"], "replies,retweets")

    def test_feishu_client_accepts_v2_success_response(self) -> None:
        """飞书 V2 成功响应应被识别。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            session = FakeSession([FakeResponse({"StatusCode": 0, "StatusMessage": "success"})])
            FeishuClient(settings, session=session).send_text("【X 更新】测试")

        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][2]["json"]["msg_type"], "text")

    def test_x_client_can_read_only_latest_page_for_baseline(self) -> None:
        """初始化基线时应允许忽略历史分页令牌。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            session = FakeSession(
                [
                    FakeResponse(
                        {
                            "data": [{"id": "102", "text": "最新帖子"}],
                            "meta": {"next_token": "older-posts"},
                        }
                    )
                ]
            )
            posts = XClient(settings, session=session).fetch_posts(
                since_id=None,
                max_pages=1,
                max_results=5,
                require_complete=False,
            )

        self.assertEqual([post.id for post in posts], ["102"])
        self.assertEqual(session.calls[0][2]["params"]["max_results"], 5)


class ServiceTests(unittest.TestCase):
    """监控主流程测试。"""

    def test_initialization_saves_latest_post_without_push(self) -> None:
        """首次运行只建立基线，不推送历史帖子。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            state = StateStore(settings.state_db_path)
            x_client = FakeXClient(
                [[Post(id="102", text="较新", created_at=None), Post(id="101", text="较旧", created_at=None)]]
            )
            feishu_client = FakeFeishuClient()
            service = MonitorService(settings, x_client, feishu_client, state)  # type: ignore[arg-type]

            pushed_count = service.run_cycle()

            self.assertEqual(pushed_count, 0)
            self.assertEqual(state.get("last_seen_id"), "102")
            self.assertEqual(feishu_client.messages, [])

    def test_new_posts_are_pushed_oldest_first_and_cursor_advances(self) -> None:
        """新增帖子应从旧到新推送并保存最终游标。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            state = StateStore(settings.state_db_path)
            state.set("initialized", "1")
            state.set("last_seen_id", "100")
            x_client = FakeXClient(
                [[Post(id="102", text="新帖二", created_at=None), Post(id="101", text="新帖一", created_at=None)]]
            )
            feishu_client = FakeFeishuClient()
            service = MonitorService(settings, x_client, feishu_client, state)  # type: ignore[arg-type]

            pushed_count = service.run_cycle()

            self.assertEqual(pushed_count, 2)
            self.assertIn("status/101", feishu_client.messages[0])
            self.assertIn("status/102", feishu_client.messages[1])
            self.assertEqual(state.get("last_seen_id"), "102")

    def test_message_contains_keyword_and_link(self) -> None:
        """飞书消息应包含安全关键词和原文地址。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            message = format_post_message(
                Post(id="123", text="正文", created_at="2026-07-13T02:33:19Z"), settings
            )

        self.assertIn("X 更新", message)
        self.assertIn("@example_user", message)
        self.assertIn("https://x.com/example_user/status/123", message)


if __name__ == "__main__":
    unittest.main()
