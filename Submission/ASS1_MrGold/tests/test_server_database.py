import unittest
from unittest import mock

import database

from tests.fakes import ensure_psycopg2_stub


ensure_psycopg2_stub()


class DatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dataset = []

        class FakeCursor:
            def __init__(self, storage):
                self.storage = storage
                self.result = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.result = None

            def execute(self, statement, params=None):
                stmt = " ".join(statement.split()).upper()
                if stmt.startswith("CREATE TABLE") or stmt.startswith("DROP INDEX") or stmt.startswith(
                    "CREATE UNIQUE INDEX"
                ):
                    return
                if stmt.startswith("SELECT DISTINCT"):
                    if params:
                        hostname = params[0]
                        names = sorted({row["fname"] for row in self.storage if row["hostname"] == hostname})
                        self.result = [(name,) for name in names]
                    else:
                        self.result = [(row["fname"],) for row in self.storage]
                    return
                if stmt.startswith("SELECT FNAME"):
                    if "WHERE FNAME = %S AND HOSTNAME = %S" in stmt and params and len(params) == 4:
                        fname, hostname, ip, port = params
                        for row in self.storage:
                            if (
                                row["fname"] == fname
                                and row["hostname"] == hostname
                                and row["ip"] == ip
                                and row["port"] == port
                            ):
                                self.result = row.copy()
                                break
                        else:
                            self.result = None
                    elif params:
                        fname = params[0]
                        self.result = [row.copy() for row in self.storage if row["fname"] == fname]
                    else:
                        self.result = [row.copy() for row in self.storage]
                    return
                if stmt.startswith("INSERT INTO FILE_INDEX"):
                    key = (
                        params["fname"],
                        params["hostname"],
                        params["ip"],
                        params["port"],
                    )
                    for idx, row in enumerate(self.storage):
                        existing_key = (row["fname"], row["hostname"], row["ip"], row["port"])
                        if existing_key == key:
                            self.storage[idx] = params.copy()
                            self.result = (idx + 1, False)
                            return
                    self.storage.append(params.copy())
                    self.result = (len(self.storage), True)
                    return
                if stmt.startswith("DELETE FROM FILE_INDEX"):
                    hostname, ip, port = params
                    removed = []
                    kept = []
                    for row in self.storage:
                        if row["hostname"] == hostname and row["ip"] == ip and row["port"] == port:
                            removed.append((row["fname"],))
                        else:
                            kept.append(row)
                    self.storage[:] = kept
                    self.result = removed
                    return

            def fetchall(self):
                return self.result or []

            def fetchone(self):
                return self.result

        class FakeConnection:
            def __init__(self, storage):
                self.storage = storage

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self, cursor_factory=None):
                return FakeCursor(self.storage)

            def close(self):
                return None

        def fake_connect(*args, **kwargs):
            return FakeConnection(self.dataset)

        self.connect_patcher = mock.patch("database.psycopg2.connect", side_effect=fake_connect)
        self.connect_patcher.start()

    def tearDown(self):
        self.connect_patcher.stop()

    def test_register_and_update_metadata(self):
        db = database.Database(dsn="fake")
        entry = {
            "fname": "report.pdf",
            "hostname": "peerA",
            "ip": "10.1.0.10",
            "port": 7000,
            "lname": "/files/report.pdf",
            "file_size": 1024,
            "last_modified": "2024-11-04T01:00:00Z",
        }
        result = db.register_file(entry)
        self.assertEqual(result, "inserted")
        self.assertEqual(len(self.dataset), 1)

        entry["file_size"] = 2048
        result = db.register_file(entry)
        self.assertEqual(result, "updated")
        self.assertEqual(self.dataset[0]["file_size"], 2048)

    def test_fetch_peers_and_discover_host_files(self):
        db = database.Database(dsn="fake")
        sample_rows = [
            {
                "fname": "slides.pptx",
                "hostname": "peerA",
                "ip": "10.1.0.1",
                "port": 8000,
                "lname": "/docs/slides.pptx",
                "file_size": 5120,
                "last_modified": "2024-11-03T00:00:00Z",
            },
            {
                "fname": "slides.pptx",
                "hostname": "peerB",
                "ip": "10.1.0.2",
                "port": 8001,
                "lname": "/docs/slides.pptx",
                "file_size": 5300,
                "last_modified": "2024-11-03T00:10:00Z",
            },
            {
                "fname": "notes.txt",
                "hostname": "peerA",
                "ip": "10.1.0.1",
                "port": 8000,
                "lname": "/docs/notes.txt",
                "file_size": 256,
                "last_modified": "2024-11-02T18:00:00Z",
            },
        ]
        self.dataset[:] = [row.copy() for row in sample_rows]

        peers = db.list_peers_for_file("slides.pptx")
        self.assertEqual(len(peers), 2)
        host_files = db.list_files_by_hostname("peerA")
        self.assertEqual(host_files, ["notes.txt", "slides.pptx"])

    def test_delete_entries_for_peer(self):
        db = database.Database(dsn="fake")
        self.dataset[:] = [
            {
                "fname": "slides.pptx",
                "hostname": "peerA",
                "ip": "10.1.0.1",
                "port": 8000,
                "lname": "/docs/slides.pptx",
                "file_size": 5120,
                "last_modified": "2024-11-03T00:00:00Z",
            },
            {
                "fname": "notes.txt",
                "hostname": "peerA",
                "ip": "10.1.0.1",
                "port": 8000,
                "lname": "/docs/notes.txt",
                "file_size": 256,
                "last_modified": "2024-11-02T18:00:00Z",
            },
            {
                "fname": "slides.pptx",
                "hostname": "peerB",
                "ip": "10.1.0.2",
                "port": 8001,
                "lname": "/docs/slides.pptx",
                "file_size": 5300,
                "last_modified": "2024-11-03T00:10:00Z",
            },
        ]
        removed = db.delete_entries_for_peer("peerA", "10.1.0.1", 8000)
        self.assertEqual(removed, {"slides.pptx": 1, "notes.txt": 1})
        self.assertEqual(len(self.dataset), 1)


if __name__ == "__main__":
    unittest.main()
