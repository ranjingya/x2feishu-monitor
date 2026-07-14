"""X API 与飞书 Webhook 客户端。"""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from x2feishu_monitor.config import Settings
from x2feishu_monitor.models import Post

LOGGER = logging.getLogger(__name__)
X_API_BASE_URL = "https://api.x.com/2"


class ExternalServiceError(RuntimeError):
    """表示 X 或飞书外部服务请求失败。"""


class XClient:
    """读取指定 X 用户帖子。"""

    def __init__(
        self, settings: Settings, session: requests.Session | None = None
    ) -> None:
        """初始化 X API 客户端。

        功能说明：配置认证信息、读取参数和仅用于 GET 请求的安全重试策略。
        参数 settings：服务运行配置。
        参数 session：可选的 requests 会话，主要用于测试注入。
        返回值：无。
        """
        self.settings = settings
        self.session = session or requests.Session()
        if session is None:
            retry = Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=1,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.x_bearer_token}",
                "User-Agent": "x2feishu-monitor/0.1",
            }
        )

    def fetch_posts(
        self,
        since_id: str | None,
        max_pages: int | None = None,
        max_results: int | None = None,
        require_complete: bool = True,
    ) -> list[Post]:
        """读取目标用户的新帖子。

        功能说明：调用用户帖子接口，按需使用 since_id，并处理分页结果。
        参数 since_id：上次成功推送的帖子 ID；为空时读取最新一页。
        参数 max_pages：本次最多读取页数；为空时使用配置值。
        参数 max_results：每页读取数量；为空时使用配置值。
        参数 require_complete：达到分页上限时是否按错误处理，增量轮询应保持为真。
        返回值：去重后的帖子列表，顺序与 API 返回顺序无关。
        """
        page_limit = max_pages or self.settings.x_max_pages_per_poll
        page_size = max_results or self.settings.x_max_results
        if not 5 <= page_size <= 100:
            raise ValueError("X API 每页读取数量必须在 5 到 100 之间")
        next_token: str | None = None
        posts_by_id: dict[str, Post] = {}

        for page_number in range(1, page_limit + 1):
            tweet_fields = ["created_at", "author_id", "note_tweet"]
            if self.settings.include_replies:
                tweet_fields.extend(["referenced_tweets", "in_reply_to_user_id"])
            params: dict[str, str | int] = {
                "max_results": page_size,
                "tweet.fields": ",".join(tweet_fields),
            }
            if self.settings.include_replies:
                params["expansions"] = (
                    "referenced_tweets.id,"
                    "referenced_tweets.id.author_id,"
                    "in_reply_to_user_id"
                )
                params["user.fields"] = "username,name"
            excluded_types: list[str] = []
            if not self.settings.include_replies:
                excluded_types.append("replies")
            if not self.settings.include_retweets:
                excluded_types.append("retweets")
            if excluded_types:
                params["exclude"] = ",".join(excluded_types)
            if since_id:
                params["since_id"] = since_id
            if next_token:
                params["pagination_token"] = next_token

            payload = self._request_page(params)
            raw_posts = payload.get("data") or []
            if not isinstance(raw_posts, list):
                raise ExternalServiceError("X API 返回的 data 字段格式不正确")

            included_posts: dict[str, dict[str, Any]] = {}
            included_users: dict[str, dict[str, Any]] = {}
            includes = payload.get("includes") or {}
            if isinstance(includes, dict):
                for included_post in includes.get("tweets") or []:
                    if not isinstance(included_post, dict):
                        continue
                    included_post_id = str(included_post.get("id", "")).strip()
                    if included_post_id.isdigit():
                        included_posts[included_post_id] = included_post
                for included_user in includes.get("users") or []:
                    if not isinstance(included_user, dict):
                        continue
                    included_user_id = str(included_user.get("id", "")).strip()
                    if included_user_id.isdigit():
                        included_users[included_user_id] = included_user

            for raw_post in raw_posts:
                if not isinstance(raw_post, dict):
                    LOGGER.warning("忽略格式异常的帖子数据")
                    continue
                post = Post.from_api(raw_post, included_posts, included_users)
                posts_by_id[post.id] = post

            meta = payload.get("meta") or {}
            next_token_raw = meta.get("next_token") if isinstance(meta, dict) else None
            next_token = str(next_token_raw) if next_token_raw else None
            LOGGER.info(
                "完成 X 帖子分页读取：page=%s, count=%s, has_next=%s",
                page_number,
                len(raw_posts),
                bool(next_token),
            )
            if not next_token:
                break
        else:
            if next_token:
                if require_complete:
                    raise ExternalServiceError(
                        "本轮新增帖子超过最大分页数，为防止漏帖已停止推进游标；"
                        "请提高 X_MAX_PAGES_PER_POLL"
                    )
                LOGGER.info("已按要求只读取最新分页，忽略历史分页令牌")

        return list(posts_by_id.values())

    def _request_page(self, params: dict[str, str | int]) -> dict[str, Any]:
        """请求一页 X 帖子数据。"""
        url = f"{X_API_BASE_URL}/users/{self.settings.x_user_id}/tweets"
        LOGGER.info("开始请求 X API 用户帖子")
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.settings.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ExternalServiceError(f"请求 X API 失败：{exc}") from exc

        if response.status_code == 402:
            raise ExternalServiceError("X API Credits 已用尽或账户无法计费（HTTP 402）")
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ExternalServiceError(
                f"X API 返回 HTTP {response.status_code}：{response.text[:500]}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalServiceError("X API 返回了非 JSON 数据") from exc
        if not isinstance(payload, dict):
            raise ExternalServiceError("X API 返回的 JSON 顶层格式不正确")
        if payload.get("errors"):
            LOGGER.warning("X API 返回部分错误：%s", payload["errors"])
        return payload


class FeishuClient:
    """通过飞书群自定义机器人发送消息。"""

    def __init__(
        self, settings: Settings, session: requests.Session | None = None
    ) -> None:
        """初始化飞书客户端。

        功能说明：保存 Webhook、超时和会话配置；POST 请求不自动重试以降低重复消息风险。
        参数 settings：服务运行配置。
        参数 session：可选的 requests 会话，主要用于测试注入。
        返回值：无。
        """
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "x2feishu-monitor/0.1"})

    def send_text(self, text: str) -> None:
        """向飞书群发送普通文本消息。"""
        self._send_payload({"msg_type": "text", "content": {"text": text}})

    def send_card(self, card: dict[str, Any]) -> None:
        """向飞书群发送交互式卡片消息。"""
        self._send_payload({"msg_type": "interactive", "card": card})

    def _send_payload(self, payload: dict[str, Any]) -> None:
        """提交 Webhook 消息并校验 HTTP 与业务响应码。

        功能说明：调用自定义机器人 Webhook，并同时校验 HTTP 与业务响应码。
        参数 payload：符合飞书机器人格式的完整消息对象。
        返回值：无；发送失败时抛出 ExternalServiceError。
        """
        LOGGER.info("开始向飞书推送消息")
        try:
            response = self.session.post(
                self.settings.feishu_webhook_url,
                json=payload,
                timeout=self.settings.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ExternalServiceError(f"请求飞书 Webhook 失败：{exc}") from exc
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ExternalServiceError(
                f"飞书 Webhook 返回 HTTP {response.status_code}：{response.text[:500]}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalServiceError("飞书 Webhook 返回了非 JSON 数据") from exc
        if not isinstance(payload, dict):
            raise ExternalServiceError("飞书 Webhook 返回的 JSON 顶层格式不正确")

        code = payload.get("code", payload.get("StatusCode"))
        if code != 0:
            message = payload.get("msg", payload.get("StatusMessage", "未知错误"))
            raise ExternalServiceError(
                f"飞书 Webhook 发送失败：code={code}, message={message}"
            )
        LOGGER.info("飞书消息推送成功")
