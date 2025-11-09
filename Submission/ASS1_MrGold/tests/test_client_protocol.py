import os
import tempfile
import unittest
from unittest import mock

import client


class ClientProtocolTests(unittest.TestCase):
    def test_download_from_peer_streams_chunks_to_disk(self):
        cli = client.Client("127.0.0.1", 9999, 5000, hostname="alice")
        chosen_peer = {"hostname": "beta", "ip": "192.168.1.10", "port": 4100, "lname": "/data/report.bin"}

        fake_socket = mock.MagicMock()
        fake_socket.recv.side_effect = [b"chunk1", b"chunk2", b""]

        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = os.path.join(tmpdir, "report.bin")
            with mock.patch("client.socket.socket", return_value=fake_socket):
                with mock.patch("client.protocol.send_message") as send_mock:
                    cli._download_from_peer(chosen_peer, target_path)

            fake_socket.settimeout.assert_called_once_with(10)
            fake_socket.connect.assert_called_once_with(("192.168.1.10", 4100))
            send_mock.assert_called_once_with(fake_socket, {"action": "get_file", "lname": "/data/report.bin"})
            with open(target_path, "rb") as handle:
                self.assertEqual(handle.read(), b"chunk1chunk2")

    def test_do_fetch_selects_first_peer_by_default(self):
        cli = client.Client("127.0.0.1", 9999, 5000, hostname="alice")

        peer_list = [
            {"hostname": "beta", "ip": "10.0.0.2", "port": 4100, "lname": "/data/A"},
            {"hostname": "gamma", "ip": "10.0.0.3", "port": 4200, "lname": "/data/B"},
        ]

        with mock.patch("client.protocol.send_message", return_value=True):
            with mock.patch(
                "client.protocol.receive_message",
                return_value={"status": "success", "peer_list": peer_list},
            ):
                with mock.patch("builtins.input", return_value=""):
                    download_mock = mock.Mock()
                    cli._download_from_peer = download_mock
                    cli._do_fetch("file.txt")

        download_mock.assert_called_once_with(peer_list[0], "file.txt")

    def test_do_fetch_honours_user_choice(self):
        cli = client.Client("127.0.0.1", 9999, 5000, hostname="alice")
        peer_list = [
            {"hostname": "beta", "ip": "10.0.0.2", "port": 4100, "lname": "/data/A"},
            {"hostname": "gamma", "ip": "10.0.0.3", "port": 4200, "lname": "/data/B"},
        ]

        with mock.patch("client.protocol.send_message", return_value=True):
            with mock.patch(
                "client.protocol.receive_message",
                return_value={"status": "success", "peer_list": peer_list},
            ):
                with mock.patch("builtins.input", return_value="2"):
                    download_mock = mock.Mock()
                    cli._download_from_peer = download_mock
                    cli._do_fetch("file.txt")

        download_mock.assert_called_once_with(peer_list[1], "file.txt")


if __name__ == "__main__":
    unittest.main()
