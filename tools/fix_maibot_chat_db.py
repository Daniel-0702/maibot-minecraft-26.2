"""修复 MaiBot WebUI 聊天页 React #185：清理 mai_messages 里的重复 message_id。

背景：MaiBot 1.3.x 的 /chat 页面在历史消息出现重复 ``message_id`` 时会进入
React 更新死循环（MaiBot issue #2292）。本脚本只做两件事：

* 默认 dry-run：列出重复的 (platform, session_id, message_id) 组合；
* ``--apply``：备份数据库后，每个组合只保留 id 最小的一条。

请先停止 MaiBot 再执行，避免 SQLite 被占用。

用法::

    python tools/fix_maibot_chat_db.py --db "C:\\MaiBot\\data\\MaiBot.db"
    python tools/fix_maibot_chat_db.py --db "C:\\MaiBot\\data\\MaiBot.db" --apply
    python tools/fix_maibot_chat_db.py --db "...\\MaiBot.db" --session <session_id> --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

TABLE = "mai_messages"


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def find_duplicates(connection: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    cursor = connection.execute(
        f"""
        SELECT platform, session_id, message_id, COUNT(*) AS copies, MIN(id) AS keep_id
        FROM {TABLE}
        GROUP BY platform, session_id, message_id
        HAVING COUNT(*) > 1
        ORDER BY copies DESC, keep_id ASC
        LIMIT ?
        """,
        (limit,),
    )
    return list(cursor.fetchall())


def count_duplicates(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        f"""
        SELECT COALESCE(SUM(copies - 1), 0) AS extra
        FROM (
            SELECT COUNT(*) AS copies
            FROM {TABLE}
            GROUP BY platform, session_id, message_id
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    return int(row["extra"] if row else 0)


def delete_duplicates(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        f"""
        DELETE FROM {TABLE}
        WHERE id NOT IN (
            SELECT MIN(id) FROM {TABLE} GROUP BY platform, session_id, message_id
        )
        """
    )
    connection.commit()
    return int(cursor.rowcount or 0)


def clear_session(connection: sqlite3.Connection, session_id: str) -> int:
    cursor = connection.execute(f"DELETE FROM {TABLE} WHERE session_id = ?", (session_id,))
    connection.commit()
    return int(cursor.rowcount or 0)


def backup(db_path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    shutil.copy2(db_path, target)
    for suffix in ("-wal", "-shm"):
        extra = Path(str(db_path) + suffix)
        if extra.exists():
            shutil.copy2(extra, Path(str(target) + suffix))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 MaiBot 重复 message_id（修复 /chat React #185）")
    parser.add_argument("--db", required=True, help="MaiBot 数据库路径，一般是 data/MaiBot.db")
    parser.add_argument("--apply", action="store_true", help="实际执行清理（默认只检查）")
    parser.add_argument("--vacuum", action="store_true", help="清理后执行 VACUUM")
    parser.add_argument("--session", default="", help="只清空指定 session_id 的全部消息")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"数据库不存在：{db_path}")
        return 1

    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        if row is None:
            print(f"数据库里没有 {TABLE} 表，确认 --db 指向 MaiBot 的数据库")
            return 1

        if args.session:
            total = connection.execute(
                f"SELECT COUNT(*) AS n FROM {TABLE} WHERE session_id = ?", (args.session,)
            ).fetchone()["n"]
            print(f"会话 {args.session} 共 {total} 条消息")
            if not args.apply:
                print("dry-run：加 --apply 才会删除")
                return 0
            target = backup(db_path)
            print(f"已备份到 {target}")
            deleted = clear_session(connection, args.session)
            print(f"已删除 {deleted} 条会话消息")
            if args.vacuum:
                connection.execute("VACUUM")
            return 0

        extra = count_duplicates(connection)
        rows = find_duplicates(connection)
        print(f"重复 message_id 的额外副本：{extra}")
        for item in rows:
            print(
                f"  platform={item['platform']} session={item['session_id']} "
                f"message_id={item['message_id']} 份数={item['copies']}"
            )
        if extra == 0:
            print("没有发现重复 message_id；React #185 可能来自其他历史问题或旧版 WebUI bug")
            return 0
        if not args.apply:
            print("dry-run：确认后加 --apply 执行清理（会先备份数据库）")
            return 0

        target = backup(db_path)
        print(f"已备份到 {target}")
        deleted = delete_duplicates(connection)
        print(f"已删除 {deleted} 条重复消息；剩余额外副本：{count_duplicates(connection)}")
        if args.vacuum:
            connection.execute("VACUUM")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
