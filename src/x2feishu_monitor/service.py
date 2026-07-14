"""监控业务流程与消息格式化。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from x2feishu_monitor.clients import FeishuClient, XClient
from x2feishu_monitor.config import Settings
from x2feishu_monitor.models import Post
from x2feishu_monitor.state import StateStore
from x2feishu_monitor.translation import TranslationClient, TranslationError

LOGGER = logging.getLogger(__name__)


def _format_created_at(raw_value: str | None, utc_offset: str) -> str | None:
    """将 X 的 UTC 时间转换为配置的固定 UTC 偏移。"""
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        sign = 1 if utc_offset.startswith("+") else -1
        hours, minutes = (int(part) for part in utc_offset[1:].split(":"))
        target_timezone = timezone(sign * timedelta(hours=hours, minutes=minutes))
        local_time = parsed.astimezone(target_timezone).strftime("%Y-%m-%d %H:%M:%S")
        return f"{local_time} UTC{utc_offset}"
    except ValueError:
        LOGGER.warning("无法转换帖子时间，保留原始值：%s", raw_value)
        return raw_value


def _escape_lark_markdown(text: str) -> str:
    """转义帖子正文中的飞书 Markdown 控制字符。"""
    escaped = text.replace("\\", "\\\\")
    for character in ("`", "*", "_", "~", "[", "]"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _truncate_card_text(text: str, maximum_length: int, post_id: str) -> str:
    """按卡片内容预算截断文本。"""
    if len(text) <= maximum_length:
        return text
    LOGGER.warning("帖子卡片内容超过飞书消息上限，已截断：post_id=%s", post_id)
    return f"{text[: maximum_length - 1]}…"


def build_post_card(
    post: Post,
    settings: Settings,
    translation: str | None = None,
    translation_failed: bool = False,
) -> dict[str, Any]:
    """生成包含原文、译文和原文按钮的飞书交互式卡片。"""
    is_reply = post.reply_to_post_id is not None
    section_count = (
        1 + int(is_reply) + int(translation is not None or translation_failed)
    )
    available_length = settings.feishu_message_max_length - 300
    if available_length < 100:
        raise ValueError("FEISHU_MESSAGE_MAX_LENGTH 过小，无法容纳卡片固定内容")
    section_length = max(50, available_length // section_count)

    elements: list[dict[str, Any]] = []
    if is_reply:
        reply_target = (
            f"@{post.reply_to_username}"
            if post.reply_to_username
            else post.reply_to_name or "原作者"
        )
        replied_to_text = (
            post.reply_to_text or "原推文内容不可用，可通过下方按钮尝试查看。"
        )
        replied_to_text = _truncate_card_text(
            _escape_lark_markdown(replied_to_text), section_length, post.id
        )
        elements.extend(
            [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**被回复的推文 · {reply_target}**\n{replied_to_text}",
                    },
                },
                {"tag": "hr"},
            ]
        )

    original_text = _truncate_card_text(
        _escape_lark_markdown(post.text), section_length, post.id
    )
    original_label = "回复内容" if is_reply else "原文"
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{original_label}**\n{original_text}",
            },
        }
    )

    if translation is not None or translation_failed:
        if translation is not None:
            translated_text = _truncate_card_text(
                _escape_lark_markdown(translation), section_length, post.id
            )
        else:
            translated_text = "翻译服务暂时不可用，本次仅推送原文。"
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{settings.translation_target_language}译文**\n"
                            f"{translated_text}"
                        ),
                    },
                },
            ]
        )

    created_at = _format_created_at(post.created_at, settings.display_utc_offset)
    if created_at:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"发布时间：{created_at}"}
                ],
            }
        )

    actions: list[dict[str, Any]] = []
    if post.reply_to_post_id:
        if post.reply_to_username:
            replied_to_url = (
                f"https://x.com/{post.reply_to_username}/status/{post.reply_to_post_id}"
            )
        else:
            replied_to_url = f"https://x.com/i/web/status/{post.reply_to_post_id}"
        actions.append(
            {
                "tag": "button",
                "type": "default",
                "text": {"tag": "plain_text", "content": "查看被回复推文"},
                "url": replied_to_url,
            }
        )

    post_url = f"https://x.com/{settings.x_username}/status/{post.id}"
    actions.append(
        {
            "tag": "button",
            "type": "primary",
            "text": {
                "tag": "plain_text",
                "content": "查看回复原文" if is_reply else "查看 X 原文",
            },
            "url": post_url,
        }
    )
    elements.append(
        {
            "tag": "action",
            "actions": actions,
        }
    )
    if is_reply:
        header_text = (
            f"【{settings.feishu_keyword}】@{settings.x_username} 回复了 "
            f"@{post.reply_to_username}"
            if post.reply_to_username
            else f"【{settings.feishu_keyword}】@{settings.x_username} 发布了回复"
        )
    else:
        header_text = (
            f"【{settings.feishu_keyword}】@{settings.x_username} 发布了新帖子"
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": header_text,
            },
        },
        "elements": elements,
    }


def build_test_card(settings: Settings) -> dict[str, Any]:
    """生成用于验证飞书 Webhook 的测试卡片。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {
                "tag": "plain_text",
                "content": f"【{settings.feishu_keyword}】连接测试成功",
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "X2Feishu Monitor 已成功连接飞书机器人。",
                },
            }
        ],
    }


class MonitorService:
    """协调 X 查询、飞书推送与状态持久化。"""

    def __init__(
        self,
        settings: Settings,
        x_client: XClient,
        feishu_client: FeishuClient,
        state_store: StateStore,
        translation_client: TranslationClient | None = None,
    ) -> None:
        """初始化监控服务。

        功能说明：注入配置、外部客户端和状态存储，形成可测试的主流程对象。
        参数 settings：服务运行配置。
        参数 x_client：X API 客户端。
        参数 feishu_client：飞书 Webhook 客户端。
        参数 state_store：SQLite 状态存储。
        参数 translation_client：可选的帖子翻译客户端。
        返回值：无。
        """
        self.settings = settings
        self.x_client = x_client
        self.feishu_client = feishu_client
        self.state_store = state_store
        self.translation_client = translation_client

    def initialize(self) -> None:
        """建立首次运行基线。

        功能说明：首次启动时使用指定游标或当前最新帖子作为基线，不推送历史内容。
        参数：无。
        返回值：无。
        """
        if self.state_store.get("initialized") == "1":
            return

        LOGGER.info("开始初始化监控基线")
        baseline_id = self.settings.initial_since_id
        if baseline_id is None:
            latest_posts = self.x_client.fetch_posts(
                since_id=None,
                max_pages=1,
                max_results=5,
                require_complete=False,
            )
            if latest_posts:
                baseline_id = max(latest_posts, key=lambda post: int(post.id)).id

        if baseline_id:
            self.state_store.set("last_seen_id", baseline_id)
            LOGGER.info("监控基线已保存：post_id=%s", baseline_id)
        else:
            LOGGER.info("目标用户暂无帖子，后续出现的首批帖子将正常推送")
        self.state_store.set("initialized", "1")
        LOGGER.info("监控基线初始化完成")

    def run_cycle(self) -> int:
        """执行一轮查询与推送。

        功能说明：读取上次游标、获取新增帖子、按时间正序推送，并在每条成功后保存游标。
        参数：无。
        返回值：本轮成功推送的帖子数量。
        """
        if self.state_store.get("initialized") != "1":
            self.initialize()
            return 0

        last_seen_id = self.state_store.get("last_seen_id")
        LOGGER.info("开始轮询 X：last_seen_id=%s", last_seen_id or "无")
        posts = self.x_client.fetch_posts(since_id=last_seen_id)
        if last_seen_id:
            posts = [post for post in posts if int(post.id) > int(last_seen_id)]
        ordered_posts = sorted(posts, key=lambda post: int(post.id))
        if not ordered_posts:
            LOGGER.info("本轮未发现新帖子")
            return 0

        pushed_count = 0
        for post in ordered_posts:
            LOGGER.info("开始处理新帖子：post_id=%s", post.id)
            translation: str | None = None
            translation_failed = False
            translation_cache_key = f"translation:{post.id}"
            if self.translation_client is not None:
                translation = self.state_store.get(translation_cache_key)
                if translation is None:
                    try:
                        translation = self.translation_client.translate(post.text)
                        if translation:
                            self.state_store.set(translation_cache_key, translation)
                    except TranslationError as exc:
                        translation_failed = True
                        LOGGER.warning(
                            "帖子翻译失败，将仅推送原文：post_id=%s, error=%s",
                            post.id,
                            exc,
                        )

            card = build_post_card(
                post,
                self.settings,
                translation=translation,
                translation_failed=translation_failed,
            )
            self.feishu_client.send_card(card)
            self.state_store.set("last_seen_id", post.id)
            self.state_store.delete(translation_cache_key)
            pushed_count += 1
            LOGGER.info("新帖子处理完成：post_id=%s", post.id)

        LOGGER.info("本轮轮询完成：pushed_count=%s", pushed_count)
        return pushed_count
