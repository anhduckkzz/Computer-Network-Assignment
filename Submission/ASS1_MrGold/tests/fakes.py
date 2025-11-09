from __future__ import annotations

import socket
import sys
import types


def ensure_psycopg2_stub() -> None:
    """Provide a psycopg2 stub so tests can run without the real dependency."""
    if "psycopg2" in sys.modules:
        return
    fake_psycopg2 = types.ModuleType("psycopg2")

    def _unpatched_connect(*args, **kwargs):
        raise RuntimeError("psycopg2.connect should be patched in tests")

    fake_psycopg2.connect = _unpatched_connect  # type: ignore[attr-defined]
    fake_extras = types.ModuleType("psycopg2.extras")
    fake_extras.RealDictCursor = object  # type: ignore[attr-defined]
    fake_psycopg2.extras = fake_extras  # type: ignore[attr-defined]
    sys.modules["psycopg2"] = fake_psycopg2
    sys.modules["psycopg2.extras"] = fake_extras


def make_socketpair() -> tuple[socket.socket, socket.socket]:
    """Return a portable socket pair usable on Windows as well."""
    if hasattr(socket, "socketpair"):
        return socket.socketpair()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    addr = listener.getsockname()
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(addr)
    server_sock, _ = listener.accept()
    listener.close()
    return server_sock, client_sock


class FakeDatabase:
    """Minimal in-memory stand-in for the production Database class."""

    def __init__(self, dsn=None, **kwargs):
        self.entries: list[dict[str, object]] = []
        self.closed = False
        self.register_history: list[tuple[str, dict[str, object]]] = []
        self.deleted_history: list[tuple[str, str, int, dict[str, int]]] = []

    def fetch_all_entries(self):
        return [entry.copy() for entry in self.entries]

    def list_files_by_hostname(self, hostname: str):
        return sorted({entry["fname"] for entry in self.entries if entry["hostname"] == hostname})

    def list_peers_for_file(self, fname: str):
        return [entry.copy() for entry in self.entries if entry["fname"] == fname]

    def get_entry(self, fname: str, hostname: str, ip: str, port: int):
        for entry in self.entries:
            if (
                entry["fname"] == fname
                and entry["hostname"] == hostname
                and entry["ip"] == ip
                and entry["port"] == port
            ):
                return entry.copy()
        return None

    def register_file(self, entry: dict[str, object]):
        key = (entry["fname"], entry["hostname"], entry["ip"], entry["port"])
        for existing in self.entries:
            existing_key = (existing["fname"], existing["hostname"], existing["ip"], existing["port"])
            if existing_key == key:
                existing.update(entry)
                self.register_history.append(("updated", entry.copy()))
                return "updated"
        self.entries.append(entry.copy())
        self.register_history.append(("inserted", entry.copy()))
        return "inserted"

    def delete_entries_for_peer(self, hostname: str, ip: str, port: int):
        remaining = []
        removed: dict[str, int] = {}
        for entry in self.entries:
            if entry["hostname"] == hostname and entry["ip"] == ip and entry["port"] == port:
                removed.setdefault(entry["fname"], 0)
                removed[entry["fname"]] += 1
            else:
                remaining.append(entry)
        self.entries = remaining
        self.deleted_history.append((hostname, ip, port, removed.copy()))
        return removed

    def close(self):
        self.closed = True
