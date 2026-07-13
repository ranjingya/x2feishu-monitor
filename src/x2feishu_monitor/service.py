"""监控业务流程与消息格式化。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from x2feishu_monitor.clients import FeishuClient, XClient
from x2feishu_monitor.config import Settings
from x2feishu_monitor.models import Post
from x2feishu_monitor.state import StateStore

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


def format_post_message(post: Post, settings: Settings) -> str:
    """生成飞书文本消息。

    功能说明：组合安全关键词、作者、正文、时间与原文链接，并限制总长度。
    参数 post：需要推送的 X 帖子。
    参数 settings：包含用户名、关键词、时区和长度限制的运行配置。
    返回值：可直接提交给飞书 Webhook 的文本内容。
    """
    header = f"【{settings.feishu_keyword}】@{settings.x_username} 发布了新帖子\n\n"
    created_at = _format_created_at(post.created_at, settings.display_utc_offset)
    time_line = f"\n\n发布时间：{created_at}" if created_at else ""
    footer = f"{time_line}\n原文：https://x.com/{settings.x_username}/status/{post.id}"
    available_length = settings.feishu_message_max_length - len(header) - len(footer)
    if available_length <= 1:
        raise ValueError("FEISHU_MESSAGE_MAX_LENGTH 过小，无法容纳消息固定内容")
    body = post.text
    if len(body) > available_length:
        body = f"{body[: available_length - 1]}…"
        LOGGER.warning("帖子正文超过飞书消息上限，已截断：post_id=%s", post.id)
    return f"{header}{body}{footer}"


class MonitorService:
    """协调 X 查询、飞书推送与状态持久化。"""

    def __init__(
        self,
        settings: Settings,
        x_client: XClient,
        feishu_client: FeishuClient,
        state_store: StateStore,
    ) -> None:
        """初始化监控服务。

        功能说明：注入配置、外部客户端和状态存储，形成可测试的主流程对象。
        参数 settings：服务运行配置。
        参数 x_client：X API 客户端。
        参数 feishu_client：飞书 Webhook 客户端。
        参数 state_store：SQLite 状态存储。
        返回值：无。
        """
        self.settings = settings
        self.x_client = x_client
        self.feishu_client = feishu_client
        self.state_store = state_store

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
            message = format_post_message(post, self.settings)
            self.feishu_client.send_text(message)
            self.state_store.set("last_seen_id", post.id)
            pushed_count += 1
            LOGGER.info("新帖子处理完成：post_id=%s", post.id)

        LOGGER.info("本轮轮询完成：pushed_count=%s", pushed_count)
        return pushed_count
