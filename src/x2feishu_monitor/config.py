"""从环境变量加载服务配置。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


class ConfigError(ValueError):
    """表示环境变量配置不完整或格式不正确。"""


def _required(environ: Mapping[str, str], name: str) -> str:
    """读取必填环境变量并清理首尾空白。"""
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"缺少必填环境变量：{name}")
    return value


def _integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """读取带范围约束的整数环境变量。"""
    raw_value = environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数") from exc
    if value < minimum or (maximum is not None and value > maximum):
        range_text = f"{minimum} 到 {maximum}" if maximum is not None else f"不小于 {minimum}"
        raise ConfigError(f"{name} 必须位于{range_text}之间")
    return value


def _boolean(environ: Mapping[str, str], name: str, default: bool) -> bool:
    """读取布尔环境变量。"""
    raw_value = environ.get(name, str(default)).strip().lower()
    truthy = {"1", "true", "yes", "on"}
    falsy = {"0", "false", "no", "off"}
    if raw_value in truthy:
        return True
    if raw_value in falsy:
        return False
    raise ConfigError(f"{name} 必须是 true 或 false")


@dataclass(frozen=True, slots=True)
class Settings:
    """服务的全部运行配置。"""

    x_bearer_token: str
    x_user_id: str
    x_username: str
    feishu_webhook_url: str
    feishu_keyword: str
    state_db_path: Path
    poll_interval_seconds: int
    request_timeout_seconds: int
    x_max_results: int
    x_max_pages_per_poll: int
    include_replies: bool
    include_retweets: bool
    initial_since_id: str | None
    feishu_message_max_length: int
    display_utc_offset: str
    log_level: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """从环境变量构建配置。

        功能说明：读取、校验并规范化监控服务需要的全部环境变量。
        参数 environ：环境变量映射；为空时读取当前进程环境。
        返回值：校验通过的不可变 Settings 配置对象。
        """
        values = os.environ if environ is None else environ
        user_id = _required(values, "X_USER_ID")
        if not user_id.isdigit():
            raise ConfigError("X_USER_ID 必须是纯数字用户 ID")

        webhook_url = _required(values, "FEISHU_WEBHOOK_URL")
        parsed_webhook = urlparse(webhook_url)
        if parsed_webhook.scheme != "https" or not parsed_webhook.netloc:
            raise ConfigError("FEISHU_WEBHOOK_URL 必须是有效的 HTTPS 地址")

        initial_since_id = values.get("INITIAL_SINCE_ID", "").strip() or None
        if initial_since_id is not None and not initial_since_id.isdigit():
            raise ConfigError("INITIAL_SINCE_ID 必须是纯数字帖子 ID")

        log_level = values.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("LOG_LEVEL 必须是标准日志级别")

        display_utc_offset = values.get("DISPLAY_UTC_OFFSET", "+08:00").strip()
        offset_match = re.fullmatch(r"([+-])(\d{2}):([0-5]\d)", display_utc_offset)
        if offset_match is None:
            raise ConfigError("DISPLAY_UTC_OFFSET 必须使用 +08:00 形式")
        offset_hours = int(offset_match.group(2))
        offset_minutes = int(offset_match.group(3))
        if offset_hours > 14 or (offset_hours == 14 and offset_minutes != 0):
            raise ConfigError("DISPLAY_UTC_OFFSET 必须位于 -14:00 到 +14:00 之间")

        return cls(
            x_bearer_token=_required(values, "X_BEARER_TOKEN"),
            x_user_id=user_id,
            x_username=_required(values, "X_USERNAME").lstrip("@"),
            feishu_webhook_url=webhook_url,
            feishu_keyword=_required(values, "FEISHU_KEYWORD"),
            state_db_path=Path(values.get("STATE_DB_PATH", "/data/state.db").strip()),
            poll_interval_seconds=_integer(values, "POLL_INTERVAL_SECONDS", 300, 30),
            request_timeout_seconds=_integer(values, "REQUEST_TIMEOUT_SECONDS", 20, 1, 120),
            x_max_results=_integer(values, "X_MAX_RESULTS", 100, 5, 100),
            x_max_pages_per_poll=_integer(values, "X_MAX_PAGES_PER_POLL", 10, 1, 100),
            include_replies=_boolean(values, "X_INCLUDE_REPLIES", False),
            include_retweets=_boolean(values, "X_INCLUDE_RETWEETS", False),
            initial_since_id=initial_since_id,
            feishu_message_max_length=_integer(
                values, "FEISHU_MESSAGE_MAX_LENGTH", 19000, 500, 30000
            ),
            display_utc_offset=display_utc_offset,
            log_level=log_level,
        )
