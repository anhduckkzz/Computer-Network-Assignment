import os
import tempfile
import unittest
from unittest import mock

import client


class ClientTransferTests(unittest.TestCase):
    def test_handle_peer_streams_file_chunks(self):
        cli = client.Client("127.0.0.1", 9999, 5000, hostname="alice")

        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(b"ABCDEF")
            temp_path = temp_file.name

        peer_socket = mock.MagicMock()

        try:
            with mock.patch(
                "client.protocol.receive_message",
                return_value={"action": "get_file", "lname": temp_path},
            ):
                cli._handle_peer(peer_socket, ("127.0.0.1", 4000))

            sent_calls = b"".join(call.args[0] for call in peer_socket.sendall.call_args_list)
            self.assertEqual(sent_calls, b"ABCDEF")
            peer_socket.close.assert_called_once()
        finally:
            os.remove(temp_path)

    def test_handle_peer_with_missing_file(self):
        cli = client.Client("127.0.0.1", 9999, 5000, hostname="alice")
        peer_socket = mock.MagicMock()

        with mock.patch(
            "client.protocol.receive_message",
            return_value={"action": "get_file", "lname": "missing.file"},
        ):
            cli._handle_peer(peer_socket, ("127.0.0.1", 4000))

        peer_socket.sendall.assert_not_called()
        peer_socket.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
