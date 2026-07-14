"""通过 OpenAI 兼容接口翻译帖子正文。"""

from __future__ import annotations

import logging
from typing import Any

import requests

from x2feishu_monitor.config import Settings

LOGGER = logging.getLogger(__name__)


class TranslationError(RuntimeError):
    """表示翻译请求失败或返回格式异常。"""


class TranslationClient:
    """调用 OpenAI 兼容的 Chat Completions 接口。"""

    def __init__(
        self, settings: Settings, session: requests.Session | None = None
    ) -> None:
        """初始化翻译客户端。"""
        if not settings.translation_enabled:
            raise ValueError("翻译未启用，不能创建 TranslationClient")
        if settings.translation_api_url is None or settings.translation_model is None:
            raise ValueError("翻译接口地址和模型不能为空")

        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "x2feishu-monitor/0.1",
            }
        )
        if settings.translation_api_key:
            self.session.headers.update(
                {"Authorization": f"Bearer {settings.translation_api_key}"}
            )

    def translate(self, text: str) -> str:
        """将帖子正文翻译为配置的目标语言，只返回译文。"""
        if not text.strip():
            return ""

        payload = {
            "model": self.settings.translation_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是一名专业翻译。将用户提供的 X 帖子翻译为"
                        f"{self.settings.translation_target_language}。保留原有换行、链接、"
                        "@用户名、#话题和专有名词；不要解释，不要添加前言，只返回译文。"
                    ),
                },
                {"role": "user", "content": text},
            ],
            "temperature": 0,
        }
        LOGGER.info("开始翻译帖子正文")
        try:
            response = self.session.post(
                self.settings.translation_api_url,
                json=payload,
                timeout=self.settings.translation_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise TranslationError(f"请求翻译服务失败：{exc}") from exc

        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise TranslationError(
                f"翻译服务返回 HTTP {response.status_code}：{response.text[:500]}"
            ) from exc

        try:
            response_payload = response.json()
        except ValueError as exc:
            raise TranslationError("翻译服务返回了非 JSON 数据") from exc
        translated_text = self._extract_text(response_payload)
        LOGGER.info("帖子正文翻译完成")
        return translated_text

    @staticmethod
    def _extract_text(payload: Any) -> str:
        """从 Chat Completions 响应中提取非空译文。"""
        if not isinstance(payload, dict):
            raise TranslationError("翻译服务返回的 JSON 顶层格式不正确")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise TranslationError("翻译服务响应缺少 choices")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise TranslationError("翻译服务响应中的 choice 格式不正确")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise TranslationError("翻译服务响应缺少 message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise TranslationError("翻译服务返回了空译文")
        return content.strip()
