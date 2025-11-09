import threading
import unittest
from unittest import mock

import protocol
import server

from tests.fakes import FakeDatabase, make_socketpair


class ServerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fake_db = FakeDatabase()
        self.db_patcher = mock.patch("server.Database", return_value=self.fake_db)
        self.db_patcher.start()
        self.server = server.Server("127.0.0.1", 0)

    def tearDown(self):
        self.db_patcher.stop()

    def test_handle_client_successful_publish_and_fetch(self):
        srv_sock, cli_sock = make_socketpair()
        client_address = ("127.0.0.1", 60000)
        worker = threading.Thread(target=self.server.handle_client, args=(srv_sock, client_address))
        worker.start()

        try:
            protocol.send_message(cli_sock, {"action": "hello", "hostname": "alpha", "p2p_port": 4000})
            ack = protocol.receive_message(cli_sock)
            self.assertEqual(ack["status"], "success")

            publish_payload = {
                "action": "publish",
                "lname": "/tmp/sample.txt",
                "fname": "sample.txt",
                "file_size": 10,
                "last_modified": "2024-11-04T00:00:00Z",
                "allow_overwrite": False,
            }
            protocol.send_message(cli_sock, publish_payload)
            publish_response = protocol.receive_message(cli_sock)
            self.assertEqual(publish_response["status"], "created")

            protocol.send_message(cli_sock, {"action": "fetch", "fname": "sample.txt"})
            fetch_response = protocol.receive_message(cli_sock)
            self.assertEqual(fetch_response["status"], "success")
            self.assertEqual(fetch_response["peer_list"][0]["hostname"], "alpha")
        finally:
            try:
                cli_sock.shutdown(2)
            except OSError:
                pass
            cli_sock.close()
            worker.join(timeout=2)

        self.assertNotIn("alpha", self.server.active_clients)
        self.assertEqual(self.fake_db.entries, [])

    def test_rejects_request_without_handshake(self):
        srv_sock, cli_sock = make_socketpair()
        client_address = ("127.0.0.1", 60001)
        worker = threading.Thread(target=self.server.handle_client, args=(srv_sock, client_address))
        worker.start()

        try:
            protocol.send_message(cli_sock, {"action": "publish", "fname": "sample.txt", "lname": "/tmp/sample.txt"})
            response = protocol.receive_message(cli_sock)
            self.assertEqual(response["status"], "error")
        finally:
            try:
                cli_sock.shutdown(2)
            except OSError:
                pass
            cli_sock.close()
            worker.join(timeout=2)


class ProtocolSerializationTests(unittest.TestCase):
    def test_round_trip_serialization(self):
        srv_sock, cli_sock = make_socketpair()
        try:
            outgoing = {"action": "publish", "payload": {"fname": "demo.txt"}}
            self.assertTrue(protocol.send_message(srv_sock, outgoing))
            incoming = protocol.receive_message(cli_sock)
            self.assertEqual(incoming, outgoing)
        finally:
            srv_sock.close()
            cli_sock.close()

    def test_receive_none_on_disconnect(self):
        srv_sock, cli_sock = make_socketpair()
        srv_sock.close()
        try:
            message = protocol.receive_message(cli_sock)
            self.assertIsNone(message)
        finally:
            cli_sock.close()


if __name__ == "__main__":
    unittest.main()
