"""
对话记忆模块 — SQLite 存储
v0.2.2 优化：彻底去除数据库路径的硬编码，必须由外部实例化时传入或从环境变量读取。
"""
import os
import sqlite3
import asyncio
from pathlib import Path

class Memory:
    def __init__(self, db_path: str = None):
        resolved_path = db_path or os.getenv("DATABASE_PATH")
        if not resolved_path:
            raise ValueError("Database path must be configured via parameter or DATABASE_PATH env.")
        
        self.db_path = Path(resolved_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._sync_init_db()

    def _sync_init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    session_id TEXT DEFAULT 'default'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_created
                ON conversations(user_id, created_at DESC)
            """)
            conn.commit()

    def _sync_save(self, user_id: str, role: str, content: str, session_id: str):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO conversations (user_id, role, content, session_id) VALUES (?, ?, ?, ?)",
                (user_id, role, content, session_id)
            )
            conn.commit()

    async def save(self, user_id: str, role: str, content: str, session_id: str = "default"):
        await asyncio.to_thread(self._sync_save, user_id, role, content, session_id)

    def _sync_get_recent(self, user_id: str, n: int, session_id: str) -> list[dict]:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT role, content FROM conversations
                   WHERE user_id = ? AND session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, session_id, n * 2)
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def get_recent(self, user_id: str, n: int = 20, session_id: str = "default") -> list[dict]:
        return await asyncio.to_thread(self._sync_get_recent, user_id, n, session_id)

    def _sync_clear(self, user_id: str, session_id: str):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "DELETE FROM conversations WHERE user_id = ? AND session_id = ?",
                (user_id, session_id)
            )
            conn.commit()

    async def clear(self, user_id: str, session_id: str = "default"):
        await asyncio.to_thread(self._sync_clear, user_id, session_id)
