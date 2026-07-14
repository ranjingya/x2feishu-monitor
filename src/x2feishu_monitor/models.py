"""监控服务使用的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _extract_post_text(payload: Mapping[str, Any]) -> str:
    """优先读取 Note Tweet 完整正文，并回退到顶层兼容文本。"""
    note_tweet = payload.get("note_tweet")
    if isinstance(note_tweet, Mapping):
        note_text = note_tweet.get("text")
        if isinstance(note_text, str) and note_text.strip():
            return note_text
    return str(payload.get("text", ""))


@dataclass(frozen=True, slots=True)
class Post:
    """表示从 X API 读取到的一条帖子。"""

    id: str
    text: str
    created_at: str | None
    reply_to_post_id: str | None = None
    reply_to_text: str | None = None
    reply_to_username: str | None = None
    reply_to_name: str | None = None

    @classmethod
    def from_api(
        cls,
        payload: Mapping[str, Any],
        included_posts: Mapping[str, Mapping[str, Any]] | None = None,
        included_users: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Post:
        """将 X API 数据转换为帖子模型。

        功能说明：校验必要字段并保留推送所需的帖子信息。
        参数 payload：X API 返回的单条帖子对象。
        参数 included_posts：按 ID 索引的扩展帖子对象。
        参数 included_users：按 ID 索引的扩展用户对象。
        返回值：规范化后的 Post 对象。
        """
        post_id = str(payload.get("id", "")).strip()
        if not post_id.isdigit():
            raise ValueError("X API 返回了无效的帖子 ID")
        text = _extract_post_text(payload)
        created_at_raw = payload.get("created_at")
        created_at = str(created_at_raw) if created_at_raw else None

        reply_to_post_id: str | None = None
        referenced_posts = payload.get("referenced_tweets")
        if isinstance(referenced_posts, list):
            for reference in referenced_posts:
                if (
                    not isinstance(reference, Mapping)
                    or reference.get("type") != "replied_to"
                ):
                    continue
                candidate_id = str(reference.get("id", "")).strip()
                if candidate_id.isdigit():
                    reply_to_post_id = candidate_id
                    break

        reply_to_text: str | None = None
        reply_to_username: str | None = None
        reply_to_name: str | None = None
        reply_author_id: str | None = None
        if reply_to_post_id and included_posts:
            replied_to_post = included_posts.get(reply_to_post_id)
            if replied_to_post:
                reply_to_text = _extract_post_text(replied_to_post) or None
                raw_reply_author_id = replied_to_post.get("author_id")
                reply_author_id = (
                    str(raw_reply_author_id).strip() if raw_reply_author_id else None
                )

        if reply_author_id is None:
            raw_reply_user_id = payload.get("in_reply_to_user_id")
            reply_author_id = (
                str(raw_reply_user_id).strip() if raw_reply_user_id else None
            )
        if reply_author_id and included_users:
            replied_to_user = included_users.get(reply_author_id)
            if replied_to_user:
                raw_username = replied_to_user.get("username")
                raw_name = replied_to_user.get("name")
                reply_to_username = str(raw_username).strip() if raw_username else None
                reply_to_name = str(raw_name).strip() if raw_name else None

        return cls(
            id=post_id,
            text=text,
            created_at=created_at,
            reply_to_post_id=reply_to_post_id,
            reply_to_text=reply_to_text,
            reply_to_username=reply_to_username,
            reply_to_name=reply_to_name,
        )
