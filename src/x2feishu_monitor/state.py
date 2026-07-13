"""使用 SQLite 持久化监控游标与心跳。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


class StateStore:
    """以键值形式管理监控运行状态。"""

    def __init__(self, database_path: Path) -> None:
        """初始化 SQLite 状态库。

        功能说明：创建数据目录、数据库和状态表，并启用 WAL 模式。
        参数 database_path：SQLite 数据库文件路径。
        返回值：无。
        """
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS monitor_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        """创建一个短生命周期数据库连接。"""
        return sqlite3.connect(self.database_path, timeout=10)

    def get(self, key: str) -> str | None:
        """读取指定状态值。"""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM monitor_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    def set(self, key: str, value: str) -> None:
        """原子写入指定状态值。"""
        updated_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO monitor_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, updated_at),
                )

    def touch_heartbeat(self) -> None:
        """记录主循环最近一次完成时间。"""
        self.set("heartbeat_at", datetime.now(UTC).isoformat())

    def heartbeat_age_seconds(self) -> float | None:
        """计算最近心跳距现在的秒数。"""
        raw_value = self.get("heartbeat_at")
        if raw_value is None:
            return None
        heartbeat_at = datetime.fromisoformat(raw_value)
        return max(0.0, (datetime.now(UTC) - heartbeat_at).total_seconds())
