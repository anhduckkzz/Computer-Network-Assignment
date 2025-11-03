"""PostgreSQL data access layer for the P2P metadata server."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor


DEFAULT_DB_CONFIG: Dict[str, object] = {
    "dbname": "p2p_metadata",
    "user": "p2p_server",
    "password": "p2p_pass",
    "host": "127.0.0.1",
    "port": 5432,
}
DEFAULT_DB_URL = (
    "postgresql://{user}:{password}@{host}:{port}/{dbname}".format(**DEFAULT_DB_CONFIG)
)


class Database:
    """Thin helper that executes SQL statements using psycopg2.connect."""

    def __init__(self, dsn: Optional[str] = None, **config):
        if dsn:
            self._conn_kwargs: Dict[str, object] = {"dsn": dsn}
        else:
            merged = DEFAULT_DB_CONFIG.copy()
            merged.update(config)
            self._conn_kwargs = merged
        self._ensure_schema()

    def _connect(self):
        if "dsn" in self._conn_kwargs:
            logging.debug("Opening PostgreSQL connection with DSN.")
            return psycopg2.connect(self._conn_kwargs["dsn"])  # type: ignore[arg-type]
        logging.debug("Opening PostgreSQL connection with params: %s", self._conn_kwargs)
        return psycopg2.connect(**self._conn_kwargs)

    def _ensure_schema(self) -> None:
        """Ensure required tables and indexes exist."""
        create_table_stmt = """
            CREATE TABLE IF NOT EXISTS file_index (
                id SERIAL PRIMARY KEY,
                fname TEXT NOT NULL,
                hostname TEXT NOT NULL,
                ip TEXT NOT NULL,
                port INTEGER NOT NULL,
                lname TEXT,
                file_size BIGINT,
                last_modified TEXT
            );
        """
        create_index_stmt = """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_file_index_unique_peer
                ON file_index (fname, hostname, ip, port, file_size, last_modified);
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(create_table_stmt)
            cur.execute(create_index_stmt)
        logging.info("Database schema verified.")

    def fetch_all_entries(self) -> List[Dict[str, object]]:
        query = """
            SELECT fname, hostname, ip, port, lname, file_size, last_modified
            FROM file_index
        """
        with self._connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
        return list(rows)

    def list_peers_for_file(self, fname: str) -> List[Dict[str, object]]:
        query = """
            SELECT fname, hostname, ip, port, lname, file_size, last_modified
            FROM file_index
            WHERE fname = %s
            ORDER BY hostname, ip, port
        """
        with self._connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (fname,))
            rows = cur.fetchall()
        return list(rows)

    def register_file(self, entry: Dict[str, object]) -> bool:
        insert_stmt = """
            INSERT INTO file_index (fname, hostname, ip, port, lname, file_size, last_modified)
            VALUES (%(fname)s, %(hostname)s, %(ip)s, %(port)s, %(lname)s, %(file_size)s, %(last_modified)s)
            ON CONFLICT (fname, hostname, ip, port, file_size, last_modified)
            DO NOTHING
            RETURNING id
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(insert_stmt, entry)
            inserted = cur.fetchone()
        return inserted is not None

    def delete_entries_for_peer(self, hostname: str, ip: str, port: int) -> Dict[str, int]:
        delete_stmt = """
            DELETE FROM file_index
            WHERE hostname = %s AND ip = %s AND port = %s
            RETURNING fname
        """
        removed: Dict[str, int] = {}
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(delete_stmt, (hostname, ip, port))
            for fname, in cur.fetchall():
                removed[fname] = removed.get(fname, 0) + 1
        return removed

    def list_files_by_hostname(self, hostname: str) -> List[str]:
        query = """
            SELECT DISTINCT fname
            FROM file_index
            WHERE hostname = %s
            ORDER BY fname
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(query, (hostname,))
            rows = cur.fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        # Each operation opens its own connection, so nothing to close.
        logging.info("Database helper shutdown complete.")
