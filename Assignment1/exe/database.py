"""SQLite-backed data access layer for the packaged P2P metadata server."""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def _resolve_default_data_dir() -> Path:
    """Return a writable directory regardless of whether we run from source or PyInstaller."""
    if getattr(sys, "frozen", False):  # type: ignore[attr-defined]
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resolve_db_path(dsn: Optional[str]) -> Path:
    if not dsn:
        return _resolve_default_data_dir() / "p2p_metadata.db"
    if dsn.startswith("sqlite:///"):
        raw_path = dsn.split("sqlite:///", 1)[1]
        return Path(raw_path)
    # Accept plain paths for convenience.
    return Path(dsn)


DEFAULT_DB_PATH = _resolve_db_path(None)
DEFAULT_DB_URL = f"sqlite:///{DEFAULT_DB_PATH}"


class Database:
    """Thin helper for executing SQLite statements with the same API as the PostgreSQL layer."""

    def __init__(self, dsn: Optional[str] = None, **_: object):
        self.db_path = _resolve_db_path(dsn)
        if not self.db_path.is_absolute():
            self.db_path = (_resolve_default_data_dir() / self.db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        create_table_stmt = """
            CREATE TABLE IF NOT EXISTS file_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fname TEXT NOT NULL,
                hostname TEXT NOT NULL,
                ip TEXT NOT NULL,
                port INTEGER NOT NULL,
                lname TEXT,
                file_size INTEGER,
                last_modified TEXT,
                UNIQUE(fname, hostname, ip, port)
            )
        """
        with self._connect() as conn:
            conn.execute(create_table_stmt)
        logging.info("SQLite metadata store ready at %s", self.db_path)

    def _fetch_rows(self, query: str, params: Iterable[object] = ()) -> List[Dict[str, object]]:
        with self._connect() as conn:
            cur = conn.execute(query, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def fetch_all_entries(self) -> List[Dict[str, object]]:
        query = """
            SELECT fname, hostname, ip, port, lname, file_size, last_modified
            FROM file_index
            ORDER BY fname, hostname, ip, port
        """
        return self._fetch_rows(query)

    def list_peers_for_file(self, fname: str) -> List[Dict[str, object]]:
        query = """
            SELECT fname, hostname, ip, port, lname, file_size, last_modified
            FROM file_index
            WHERE fname = ?
            ORDER BY hostname, ip, port
        """
        return self._fetch_rows(query, (fname,))

    def list_all_shared_files(self) -> List[Dict[str, object]]:
        query = """
            SELECT
                fname,
                COUNT(*) AS peer_count,
                MAX(file_size) AS file_size,
                MAX(last_modified) AS last_modified
            FROM file_index
            GROUP BY fname
            ORDER BY fname
        """
        return self._fetch_rows(query)

    def get_entry(self, fname: str, hostname: str, ip: str, port: int) -> Optional[Dict[str, object]]:
        query = """
            SELECT fname, hostname, ip, port, lname, file_size, last_modified
            FROM file_index
            WHERE fname = ? AND hostname = ? AND ip = ? AND port = ?
            LIMIT 1
        """
        rows = self._fetch_rows(query, (fname, hostname, ip, port))
        return rows[0] if rows else None

    def register_file(self, entry: Dict[str, object]) -> str:
        select_query = """
            SELECT 1 FROM file_index
            WHERE fname = ? AND hostname = ? AND ip = ? AND port = ?
            LIMIT 1
        """
        insert_stmt = """
            INSERT INTO file_index (fname, hostname, ip, port, lname, file_size, last_modified)
            VALUES (:fname, :hostname, :ip, :port, :lname, :file_size, :last_modified)
            ON CONFLICT(fname, hostname, ip, port) DO UPDATE SET
                lname=excluded.lname,
                file_size=excluded.file_size,
                last_modified=excluded.last_modified
        """
        key = (entry.get("fname"), entry.get("hostname"), entry.get("ip"), entry.get("port"))
        existed = bool(self._fetch_rows(select_query, key))
        with self._connect() as conn:
            conn.execute(insert_stmt, entry)
        return "updated" if existed else "inserted"

    def delete_entries_for_peer(self, hostname: str, ip: str, port: int) -> Dict[str, int]:
        delete_stmt = """
            DELETE FROM file_index
            WHERE hostname = ? AND ip = ? AND port = ?
            RETURNING fname
        """
        removed: Dict[str, int] = {}
        with self._connect() as conn:
            try:
                rows = conn.execute(delete_stmt, (hostname, ip, port)).fetchall()
            except sqlite3.OperationalError:
                # SQLite versions without RETURNING: emulate with manual select/delete.
                select_stmt = """
                    SELECT fname FROM file_index WHERE hostname = ? AND ip = ? AND port = ?
                """
                rows = conn.execute(select_stmt, (hostname, ip, port)).fetchall()
                conn.execute("DELETE FROM file_index WHERE hostname = ? AND ip = ? AND port = ?", (hostname, ip, port))
            for row in rows:
                fname = row[0] if isinstance(row, tuple) else row["fname"]
                removed[fname] = removed.get(fname, 0) + 1
        return removed

    def list_files_by_hostname(self, hostname: str) -> List[str]:
        query = """
            SELECT DISTINCT fname
            FROM file_index
            WHERE hostname = ?
            ORDER BY fname
        """
        rows = self._fetch_rows(query, (hostname,))
        return [row["fname"] for row in rows]

    def close(self) -> None:
        logging.info("SQLite database helper closed. Connections will be reopened lazily.")

