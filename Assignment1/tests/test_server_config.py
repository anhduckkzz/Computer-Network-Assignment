import socket
import unittest
from unittest import mock

import server


class ServerConfigurationTests(unittest.TestCase):
    def test_initialises_with_provided_endpoint_and_database(self):
        with mock.patch("server.Database") as db_ctor:
            srv = server.Server(ip="0.0.0.0", port=9999, db_url="postgresql://demo")
            db_ctor.assert_called_once_with(dsn="postgresql://demo")
            self.assertEqual(srv.ip, "0.0.0.0")
            self.assertEqual(srv.port, 9999)

    def test_load_data_fetches_all_entries(self):
        fake_db = mock.Mock()
        fake_db.fetch_all_entries.return_value = []
        srv = server.Server(ip="127.0.0.1", port=9000)
        srv.db = fake_db
        srv.load_data()
        fake_db.fetch_all_entries.assert_called_once()

    def test_shutdown_closes_socket_and_database(self):
        fake_db = mock.Mock()
        srv = server.Server(ip="127.0.0.1", port=0)
        srv.db = fake_db
        listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.listening_socket = listening_socket
        try:
            srv.shutdown()
            fake_db.close.assert_called_once()
            self.assertIsNone(srv.listening_socket)
        finally:
            listening_socket.close()


if __name__ == "__main__":
    unittest.main()
