"""监控服务使用的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Post:
    """表示从 X API 读取到的一条帖子。"""

    id: str
    text: str
    created_at: str | None

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Post:
        """将 X API 数据转换为帖子模型。

        功能说明：校验必要字段并保留推送所需的帖子信息。
        参数 payload：X API 返回的单条帖子对象。
        返回值：规范化后的 Post 对象。
        """
        post_id = str(payload.get("id", "")).strip()
        if not post_id.isdigit():
            raise ValueError("X API 返回了无效的帖子 ID")
        text = str(payload.get("text", ""))
        created_at_raw = payload.get("created_at")
        created_at = str(created_at_raw) if created_at_raw else None
        return cls(id=post_id, text=text, created_at=created_at)
