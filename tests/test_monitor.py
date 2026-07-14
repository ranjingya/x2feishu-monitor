"""监控服务的核心行为测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from x2feishu_monitor.clients import ExternalServiceError, FeishuClient, XClient
from x2feishu_monitor.config import ConfigError, Settings
from x2feishu_monitor.models import Post
from x2feishu_monitor.service import MonitorService, build_post_card
from x2feishu_monitor.state import StateStore
from x2feishu_monitor.translation import TranslationClient, TranslationError


def make_settings(
    database_path: Path, extra_values: dict[str, str] | None = None
) -> Settings:
    """创建不包含真实密钥的测试配置。"""
    values = {
        "X_BEARER_TOKEN": "test-token",
        "X_USER_ID": "123456789",
        "X_USERNAME": "example_user",
        "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
        "FEISHU_KEYWORD": "X 更新",
        "STATE_DB_PATH": str(database_path),
        "POLL_INTERVAL_SECONDS": "300",
    }
    if extra_values:
        values.update(extra_values)
    return Settings.from_env(values)


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
    """记录业务流程生成的飞书卡片。"""

    def __init__(self, failures_remaining: int = 0) -> None:
        self.cards: list[dict[str, Any]] = []
        self.failures_remaining = failures_remaining

    def send_card(self, card: dict[str, Any]) -> None:
        """记录卡片，或按预设次数模拟发送失败。"""
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ExternalServiceError("模拟飞书发送失败")
        self.cards.append(card)


class FakeTranslationClient:
    """记录翻译调用并返回预设译文。"""

    def __init__(
        self, translation: str = "中文译文", should_fail: bool = False
    ) -> None:
        self.translation = translation
        self.should_fail = should_fail
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        """返回译文或模拟翻译失败。"""
        self.calls.append(text)
        if self.should_fail:
            raise TranslationError("模拟翻译失败")
        return self.translation


class ConfigTests(unittest.TestCase):
    """配置加载测试。"""

    def test_missing_required_value_is_rejected(self) -> None:
        """缺少密钥时应明确失败。"""
        with self.assertRaises(ConfigError):
            Settings.from_env({})

    def test_translation_requires_api_url_and_model(self) -> None:
        """启用翻译但缺少接口配置时应明确失败。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                make_settings(
                    Path(directory) / "state.db",
                    {"TRANSLATION_ENABLED": "true"},
                )


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
                    FakeResponse(
                        {"data": [{"id": "101", "text": "新帖一"}], "meta": {}}
                    ),
                ]
            )
            posts = XClient(settings, session=session).fetch_posts("100")

        self.assertEqual({post.id for post in posts}, {"101", "102"})
        self.assertEqual(session.calls[0][2]["params"]["since_id"], "100")
        self.assertEqual(session.calls[1][2]["params"]["pagination_token"], "next-page")
        self.assertEqual(session.calls[0][2]["params"]["exclude"], "replies,retweets")

    def test_x_client_prefers_full_note_tweet_text(self) -> None:
        """Note Tweet 应使用完整正文而不是带短链的顶层兼容文本。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            session = FakeSession(
                [
                    FakeResponse(
                        {
                            "data": [
                                {
                                    "id": "101",
                                    "text": "被截断的正文 https://t.co/example",
                                    "note_tweet": {
                                        "text": "这是 Note Tweet 的完整正文"
                                    },
                                }
                            ],
                            "meta": {},
                        }
                    )
                ]
            )
            posts = XClient(settings, session=session).fetch_posts("100")

        self.assertEqual(posts[0].text, "这是 Note Tweet 的完整正文")
        self.assertIn("note_tweet", session.calls[0][2]["params"]["tweet.fields"])

    def test_feishu_client_sends_interactive_card(self) -> None:
        """飞书卡片应使用 interactive 消息类型。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            session = FakeSession(
                [FakeResponse({"StatusCode": 0, "StatusMessage": "success"})]
            )
            card = {"header": {"title": {"tag": "plain_text", "content": "测试"}}}
            FeishuClient(settings, session=session).send_card(card)

        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][2]["json"]["msg_type"], "interactive")
        self.assertEqual(session.calls[0][2]["json"]["card"], card)

    def test_translation_client_uses_chat_completions_format(self) -> None:
        """翻译客户端应提交模型和正文并提取译文。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(
                Path(directory) / "state.db",
                {
                    "TRANSLATION_ENABLED": "true",
                    "TRANSLATION_API_URL": "https://translation.example/v1/chat/completions",
                    "TRANSLATION_API_KEY": "translation-key",
                    "TRANSLATION_MODEL": "translation-model",
                    "TRANSLATION_TARGET_LANGUAGE": "简体中文",
                },
            )
            session = FakeSession(
                [FakeResponse({"choices": [{"message": {"content": "你好世界"}}]})]
            )
            translated = TranslationClient(settings, session=session).translate(
                "Hello world"
            )

        self.assertEqual(translated, "你好世界")
        request_json = session.calls[0][2]["json"]
        self.assertEqual(request_json["model"], "translation-model")
        self.assertEqual(request_json["messages"][1]["content"], "Hello world")
        self.assertEqual(session.headers["Authorization"], "Bearer translation-key")

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

    def test_x_client_can_include_user_replies(self) -> None:
        """启用回复后应在同一响应中解析被回复推文和作者。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(
                Path(directory) / "state.db", {"X_INCLUDE_REPLIES": "true"}
            )
            session = FakeSession(
                [
                    FakeResponse(
                        {
                            "data": [
                                {
                                    "id": "101",
                                    "text": "被截断的回复",
                                    "note_tweet": {"text": "完整回复内容"},
                                    "in_reply_to_user_id": "900",
                                    "referenced_tweets": [
                                        {"type": "replied_to", "id": "88"}
                                    ],
                                }
                            ],
                            "includes": {
                                "tweets": [
                                    {
                                        "id": "88",
                                        "text": "被截断的原推文",
                                        "note_tweet": {"text": "被回复推文的完整内容"},
                                        "author_id": "900",
                                    }
                                ],
                                "users": [
                                    {
                                        "id": "900",
                                        "username": "original_author",
                                        "name": "Original Author",
                                    }
                                ],
                            },
                            "meta": {},
                        }
                    )
                ]
            )
            posts = XClient(settings, session=session).fetch_posts("100")

        self.assertEqual([post.id for post in posts], ["101"])
        self.assertEqual(posts[0].text, "完整回复内容")
        self.assertEqual(posts[0].reply_to_post_id, "88")
        self.assertEqual(posts[0].reply_to_text, "被回复推文的完整内容")
        self.assertEqual(posts[0].reply_to_username, "original_author")
        self.assertEqual(session.calls[0][2]["params"]["exclude"], "retweets")
        self.assertIn(
            "referenced_tweets.id", session.calls[0][2]["params"]["expansions"]
        )


class ServiceTests(unittest.TestCase):
    """监控主流程测试。"""

    def test_initialization_saves_latest_post_without_push(self) -> None:
        """首次运行只建立基线，不推送历史帖子。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            state = StateStore(settings.state_db_path)
            x_client = FakeXClient(
                [
                    [
                        Post(id="102", text="较新", created_at=None),
                        Post(id="101", text="较旧", created_at=None),
                    ]
                ]
            )
            feishu_client = FakeFeishuClient()
            service = MonitorService(settings, x_client, feishu_client, state)  # type: ignore[arg-type]

            pushed_count = service.run_cycle()

            self.assertEqual(pushed_count, 0)
            self.assertEqual(state.get("last_seen_id"), "102")
            self.assertEqual(feishu_client.cards, [])

    def test_new_posts_are_pushed_oldest_first_and_cursor_advances(self) -> None:
        """新增帖子应从旧到新推送并保存最终游标。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            state = StateStore(settings.state_db_path)
            state.set("initialized", "1")
            state.set("last_seen_id", "100")
            x_client = FakeXClient(
                [
                    [
                        Post(id="102", text="新帖二", created_at=None),
                        Post(id="101", text="新帖一", created_at=None),
                    ]
                ]
            )
            feishu_client = FakeFeishuClient()
            service = MonitorService(settings, x_client, feishu_client, state)  # type: ignore[arg-type]

            pushed_count = service.run_cycle()

            self.assertEqual(pushed_count, 2)
            self.assertIn("status/101", str(feishu_client.cards[0]))
            self.assertIn("status/102", str(feishu_client.cards[1]))
            self.assertEqual(state.get("last_seen_id"), "102")

    def test_card_contains_keyword_translation_and_link(self) -> None:
        """飞书卡片应包含安全关键词、译文和原文地址。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            card = build_post_card(
                Post(id="123", text="Original", created_at="2026-07-13T02:33:19Z"),
                settings,
                translation="中文译文",
            )

        card_text = str(card)
        self.assertIn("X 更新", card_text)
        self.assertIn("@example_user", card_text)
        self.assertIn("中文译文", card_text)
        self.assertIn("https://x.com/example_user/status/123", card_text)

    def test_reply_card_contains_context_and_both_links(self) -> None:
        """回复卡片应展示被回复内容并提供两条原文链接。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory) / "state.db")
            card = build_post_card(
                Post(
                    id="123",
                    text="这是回复内容",
                    created_at="2026-07-13T02:33:19Z",
                    reply_to_post_id="88",
                    reply_to_text="这是被回复的推文",
                    reply_to_username="original_author",
                    reply_to_name="Original Author",
                ),
                settings,
                translation="This is the translated reply",
            )

        card_text = str(card)
        self.assertIn("@example_user 回复了 @original_author", card_text)
        self.assertIn("被回复的推文", card_text)
        self.assertIn("这是回复内容", card_text)
        self.assertIn("This is the translated reply", card_text)
        self.assertIn("https://x.com/original_author/status/88", card_text)
        self.assertIn("https://x.com/example_user/status/123", card_text)

    def test_translation_failure_still_pushes_original_and_advances_cursor(
        self,
    ) -> None:
        """翻译失败不应阻塞原文卡片和游标推进。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(
                Path(directory) / "state.db",
                {
                    "TRANSLATION_ENABLED": "true",
                    "TRANSLATION_API_URL": "https://translation.example/v1/chat/completions",
                    "TRANSLATION_MODEL": "translation-model",
                },
            )
            state = StateStore(settings.state_db_path)
            state.set("initialized", "1")
            state.set("last_seen_id", "100")
            x_client = FakeXClient([[Post(id="101", text="Original", created_at=None)]])
            feishu_client = FakeFeishuClient()
            translation_client = FakeTranslationClient(should_fail=True)
            service = MonitorService(
                settings,
                x_client,  # type: ignore[arg-type]
                feishu_client,  # type: ignore[arg-type]
                state,
                translation_client,  # type: ignore[arg-type]
            )

            pushed_count = service.run_cycle()

            self.assertEqual(pushed_count, 1)
            self.assertEqual(state.get("last_seen_id"), "101")
            self.assertIn("翻译服务暂时不可用", str(feishu_client.cards[0]))

    def test_translation_is_cached_when_feishu_send_retries(self) -> None:
        """飞书失败后再次处理同一帖子时应复用已生成的译文。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(
                Path(directory) / "state.db",
                {
                    "TRANSLATION_ENABLED": "true",
                    "TRANSLATION_API_URL": "https://translation.example/v1/chat/completions",
                    "TRANSLATION_MODEL": "translation-model",
                },
            )
            state = StateStore(settings.state_db_path)
            state.set("initialized", "1")
            state.set("last_seen_id", "100")
            post = Post(id="101", text="Original", created_at=None)
            x_client = FakeXClient([[post], [post]])
            feishu_client = FakeFeishuClient(failures_remaining=1)
            translation_client = FakeTranslationClient()
            service = MonitorService(
                settings,
                x_client,  # type: ignore[arg-type]
                feishu_client,  # type: ignore[arg-type]
                state,
                translation_client,  # type: ignore[arg-type]
            )

            with self.assertRaises(ExternalServiceError):
                service.run_cycle()
            pushed_count = service.run_cycle()

            self.assertEqual(pushed_count, 1)
            self.assertEqual(translation_client.calls, ["Original"])
            self.assertIsNone(state.get("translation:101"))
            self.assertEqual(state.get("last_seen_id"), "101")


if __name__ == "__main__":
    unittest.main()
