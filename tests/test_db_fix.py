"""MaiBot 聊天数据库去重脚本的单元测试（内存 SQLite，不碰真实数据）。"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("fix_maibot_chat_db", ROOT / "tools" / "fix_maibot_chat_db.py")
assert spec is not None and spec.loader is not None
fixer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixer)


def make_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE mai_messages (
            id INTEGER PRIMARY KEY,
            platform TEXT,
            session_id TEXT,
            message_id TEXT
        )
        """
    )
    connection.executemany(
        "INSERT INTO mai_messages (platform, session_id, message_id) VALUES (?, ?, ?)",
        [
            ("qq", "s1", "m1"),
            ("qq", "s1", "m1"),
            ("qq", "s1", "m1"),
            ("qq", "s1", "m2"),
            ("qq", "s2", "m1"),
        ],
    )
    connection.commit()
    return connection


class ChatDbFixTests(unittest.TestCase):
    def test_find_and_count(self) -> None:
        connection = make_db()
        try:
            self.assertEqual(fixer.count_duplicates(connection), 2)
            rows = fixer.find_duplicates(connection)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["message_id"], "m1")
            self.assertEqual(rows[0]["copies"], 3)
        finally:
            connection.close()

    def test_delete_duplicates_keeps_one_per_session(self) -> None:
        connection = make_db()
        try:
            deleted = fixer.delete_duplicates(connection)
            self.assertEqual(deleted, 2)
            self.assertEqual(fixer.count_duplicates(connection), 0)
            # s2 的同名 message_id 不同会话，必须保留
            remaining = connection.execute(
                "SELECT platform, session_id, message_id FROM mai_messages ORDER BY id"
            ).fetchall()
            self.assertEqual(len(remaining), 3)
            self.assertIn(("qq", "s2", "m1"), [tuple(row) for row in remaining])
        finally:
            connection.close()

    def test_clear_session(self) -> None:
        connection = make_db()
        try:
            deleted = fixer.clear_session(connection, "s1")
            self.assertEqual(deleted, 4)
            count = connection.execute("SELECT COUNT(*) FROM mai_messages").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
