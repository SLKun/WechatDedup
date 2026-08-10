"""SQLite 持久化：当前文件清单与追加式操作历史。

线程安全：用 threading.Lock 串行化所有 DB 操作，允许 Textual @work(thread=True)
跨线程共享同一连接（check_same_thread=False + 锁）。
数据库版本迁移由独立工具负责。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import (
    CurrentFile,
    FileResultRecord,
    OperationGroupRecord,
    OperationGroupWrite,
    OperationRecord,
    PhysicalIdentity,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS current_file (
    rel_path      TEXT PRIMARY KEY,
    account       TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         INTEGER NOT NULL,
    content_hash  TEXT,
    volume_id     TEXT NOT NULL,
    file_id       TEXT NOT NULL,
    link_count    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_current_file_account
    ON current_file(account);
CREATE INDEX IF NOT EXISTS idx_current_file_account_hash
    ON current_file(account, content_hash);

CREATE TABLE IF NOT EXISTS operation_record (
    id              INTEGER PRIMARY KEY,
    operation_type  TEXT NOT NULL,
    scope           TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL,
    account         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operation_record_account
    ON operation_record(account, id DESC);

CREATE TABLE IF NOT EXISTS operation_group (
    id               INTEGER PRIMARY KEY,
    operation_id     INTEGER NOT NULL REFERENCES operation_record(id),
    content_hash     TEXT NOT NULL,
    source_rel_path  TEXT
);
CREATE INDEX IF NOT EXISTS idx_operation_group_operation
    ON operation_group(operation_id);

CREATE TABLE IF NOT EXISTS file_result (
    id                 INTEGER PRIMARY KEY,
    operation_group_id INTEGER NOT NULL REFERENCES operation_group(id),
    rel_path            TEXT NOT NULL,
    role                TEXT NOT NULL,
    action              TEXT NOT NULL,
    status              TEXT NOT NULL,
    message             TEXT NOT NULL,
    before_volume_id    TEXT,
    before_file_id      TEXT,
    after_volume_id     TEXT,
    after_file_id       TEXT,
    size                INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_result_group
    ON file_result(operation_group_id);
CREATE INDEX IF NOT EXISTS idx_file_result_path
    ON file_result(rel_path, id DESC);
"""


class Database:
    """封装当前状态和操作历史的 SQLite 访问。"""

    def __init__(self, path: str | Path) -> None:
        """打开指定路径的 SQLite 数据库并启用外键约束。"""
        self._path = str(path)
        # check_same_thread=False + Lock：允许 Textual @work 跨线程共享连接，
        # 所有操作经 _lock 串行化，避免 SQLite 并发写损坏。
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def init_schema(self) -> None:
        """初始化当前文件清单和追加式操作历史。"""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        """进入数据库上下文。"""
        return self

    def __exit__(self, *exc: object) -> None:
        """退出数据库上下文并关闭连接。"""
        self.close()

    def get_current_files(self, accounts: list[str]) -> list[CurrentFile]:
        """读取指定账号的当前文件清单。

        Args:
            accounts: 要读取的账号名称。

        Returns:
            指定账号当前保存的附件，按相对路径排序。
        """
        if not accounts:
            return []
        placeholders = ", ".join("?" for _ in accounts)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM current_file
                    WHERE account IN ({placeholders})
                    ORDER BY rel_path""",
                accounts,
            ).fetchall()
        return [self._row_to_current_info(row) for row in rows]

    def reconcile_current_files(
        self,
        accounts: list[str],
        files: list[CurrentFile],
    ) -> None:
        """用扫描结果原子替换指定账号的当前文件清单。

        Args:
            accounts: 本次扫描的账号名称。
            files: 本次实际发现的附件。

        Returns:
            无返回值。
        """
        if not accounts:
            return
        placeholders = ", ".join("?" for _ in accounts)
        rows = [
            (
                file.rel_path,
                file.account,
                file.size,
                file.mtime,
                file.content_hash,
                file.volume_id,
                file.file_id,
                file.link_count,
            )
            for file in files
        ]
        with self._lock, self._conn:
            self._conn.execute(
                f"DELETE FROM current_file WHERE account IN ({placeholders})",
                accounts,
            )
            self._conn.executemany(
                """INSERT INTO current_file (
                       rel_path, account, size, mtime, content_hash,
                       volume_id, file_id, link_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def list_scanned_accounts(self, discovered: list[str]) -> list[str]:
        """返回已发现账号中、数据库里存在当前文件清单的账号集合。

        以文件系统发现为账号清单来源，仅把 DB 中仍有扫描记录的账号视为
        已扫描账号，避免文件系统已删除的账号继续显示为已扫描。

        Args:
            discovered: 文件系统当前发现的所有账号名称。

        Returns:
             discovered 与数据库当前文件清单的交集，按账号名排序。
        """
        if not discovered:
            return []
        placeholders = ", ".join("?" for _ in discovered)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT DISTINCT account FROM current_file
                    WHERE account IN ({placeholders})
                    ORDER BY account""",
                discovered,
            ).fetchall()
        return [row["account"] for row in rows]

    def update_current_file(self, file: CurrentFile) -> None:
        """更新一次文件操作后的当前附件状态。"""
        with self._lock:
            self._conn.execute(
                """UPDATE current_file SET
                       size=?, mtime=?, volume_id=?, file_id=?, link_count=?
                   WHERE rel_path=?""",
                (
                    file.size,
                    file.mtime,
                    file.volume_id,
                    file.file_id,
                    file.link_count,
                    file.rel_path,
                ),
            )
            self._conn.commit()

    def append_operation(
        self,
        operation_type: str,
        scope: str,
        confirmed_at: str,
        account: str,
        groups: list[OperationGroupWrite],
    ) -> int:
        """在一个事务中追加完整操作记录和全部文件结果。"""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """INSERT INTO operation_record (
                       operation_type, scope, confirmed_at, account
                   ) VALUES (?, ?, ?, ?)""",
                (operation_type, scope, confirmed_at, account),
            )
            assert cursor.lastrowid is not None
            operation_id = cursor.lastrowid
            for group in groups:
                group_cursor = self._conn.execute(
                    """INSERT INTO operation_group (
                           operation_id, content_hash, source_rel_path
                       ) VALUES (?, ?, ?)""",
                    (operation_id, group.content_hash, group.source_rel_path),
                )
                assert group_cursor.lastrowid is not None
                group_id = group_cursor.lastrowid
                self._conn.executemany(
                    """INSERT INTO file_result (
                           operation_group_id, rel_path, role, action, status,
                           message, before_volume_id, before_file_id,
                           after_volume_id, after_file_id, size
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            group_id,
                            result.rel_path,
                            result.role,
                            result.action,
                            result.status,
                            result.message,
                            result.before_identity.volume_id
                            if result.before_identity else None,
                            result.before_identity.file_id
                            if result.before_identity else None,
                            result.after_identity.volume_id
                            if result.after_identity else None,
                            result.after_identity.file_id
                            if result.after_identity else None,
                            result.size,
                        )
                        for result in group.file_results
                    ],
                )
        return operation_id

    def get_operations(self, accounts: list[str]) -> list[OperationRecord]:
        """按最新优先读取所选账号的操作记录。"""
        if not accounts:
            return []
        placeholders = ", ".join("?" for _ in accounts)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM operation_record
                    WHERE account IN ({placeholders})
                    ORDER BY id DESC""",
                accounts,
            ).fetchall()
        return [self._row_to_operation(row) for row in rows]

    def get_operation_groups(self, operation_id: int) -> list[OperationGroupRecord]:
        """读取一条操作记录中的重复组。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM operation_group WHERE operation_id=? ORDER BY id",
                (operation_id,),
            ).fetchall()
        return [self._row_to_operation_group(row) for row in rows]

    def get_file_results(self, operation_group_id: int) -> list[FileResultRecord]:
        """读取一个历史重复组中的文件结果。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM file_result WHERE operation_group_id=? ORDER BY id",
                (operation_group_id,),
            ).fetchall()
        return [self._row_to_file_result(row) for row in rows]

    def get_successful_results(self, accounts: list[str]) -> list[FileResultRecord]:
        """读取所选账号全部成功文件结果，供当前状态匹配。"""
        if not accounts:
            return []
        placeholders = ", ".join("?" for _ in accounts)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT fr.* FROM file_result AS fr
                    JOIN operation_group AS og ON og.id=fr.operation_group_id
                    JOIN operation_record AS op ON op.id=og.operation_id
                    WHERE op.account IN ({placeholders}) AND fr.status='success'
                    ORDER BY fr.id""",
                accounts,
            ).fetchall()
        return [self._row_to_file_result(row) for row in rows]

    @staticmethod
    def _row_to_current_info(row: sqlite3.Row) -> CurrentFile:
        """把当前文件清单行转换为领域对象。"""
        return CurrentFile(
            rel_path=row["rel_path"],
            account=row["account"],
            size=row["size"],
            mtime=row["mtime"],
            content_hash=row["content_hash"],
            volume_id=row["volume_id"],
            file_id=row["file_id"],
            link_count=row["link_count"],
        )

    @staticmethod
    def _row_to_operation(row: sqlite3.Row) -> OperationRecord:
        """把数据库行转换为操作记录。"""
        return OperationRecord(
            id=row["id"],
            operation_type=row["operation_type"],
            scope=row["scope"],
            confirmed_at=row["confirmed_at"],
            account=row["account"],
        )

    @staticmethod
    def _row_to_operation_group(row: sqlite3.Row) -> OperationGroupRecord:
        """把数据库行转换为历史重复组。"""
        return OperationGroupRecord(
            id=row["id"],
            operation_id=row["operation_id"],
            content_hash=row["content_hash"],
            source_rel_path=row["source_rel_path"],
        )

    @staticmethod
    def _identity_from_row(
        row: sqlite3.Row,
        prefix: str,
    ) -> PhysicalIdentity | None:
        """读取数据库行中的可空物理文件标识。"""
        volume_id = row[f"{prefix}_volume_id"]
        file_id = row[f"{prefix}_file_id"]
        if volume_id is None or file_id is None:
            return None
        return PhysicalIdentity(volume_id, file_id)

    @classmethod
    def _row_to_file_result(cls, row: sqlite3.Row) -> FileResultRecord:
        """把数据库行转换为文件结果。"""
        return FileResultRecord(
            id=row["id"],
            operation_group_id=row["operation_group_id"],
            rel_path=row["rel_path"],
            role=row["role"],
            action=row["action"],
            status=row["status"],
            message=row["message"],
            before_identity=cls._identity_from_row(row, "before"),
            after_identity=cls._identity_from_row(row, "after"),
            size=row["size"],
        )
