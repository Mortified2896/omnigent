"""Bounds for the existing provenance database, without deleting DB/WAL files."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from managed_storage import StoragePaused


class BoundedConnection(sqlite3.Connection):
    def configure(self, path, allocation, record_limit=8 * 1024**2):
        self.storage_path = Path(path)
        self.record_limit = record_limit
        self.page_size = super().execute("PRAGMA page_size").fetchone()[0]
        # One main DB, two full WAL generations and sidecar/rounding headroom.
        self.main_limit = allocation // 4
        self.wal_limit = allocation // 2
        pages = self.main_limit // self.page_size
        count = super().execute("PRAGMA page_count").fetchone()[0]
        if pages < count or pages < 8:
            raise StoragePaused("database_allocation_full")
        super().execute(f"PRAGMA max_page_count={pages}")
        super().execute("PRAGMA busy_timeout=100")
        super().execute("PRAGMA cache_spill=OFF")
        if hasattr(self, "setlimit"):
            self.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, record_limit)
        self.prepare_write()

    def prepare_write(self):
        if self.in_transaction:
            return
        # A pinned reader is backpressure, not permission to accumulate WAL.
        result = super().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0] != 0:
            raise StoragePaused("wal_reader_pinned")
        wal = Path(str(self.storage_path) + "-wal")
        existing = wal.stat().st_size if wal.exists() else 0
        pages = self.main_limit // self.page_size
        maximum_transaction = pages * (self.page_size + 24) + 32
        if existing + maximum_transaction + 65536 > self.wal_limit:
            raise StoragePaused("wal_allocation_full")

    def execute(self, sql, parameters=(), /):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        size = sum(
            len(v.encode()) if isinstance(v, str) else len(v) if isinstance(v, bytes) else 8
            for v in values
        )
        if size > self.record_limit:
            raise StoragePaused("oversized_database_record")
        command = sql.lstrip().split(None, 1)[0].upper()
        if command == "BEGIN" and not self.in_transaction:
            self.prepare_write()
        if command not in {"SELECT", "PRAGMA", "EXPLAIN", "BEGIN", "ROLLBACK", "COMMIT"}:
            if not self.in_transaction:
                self.prepare_write()
                super().execute("BEGIN IMMEDIATE")
        return super().execute(sql, parameters)
