import unittest
from unittest import mock

import client


class ClientErrorHandlingTests(unittest.TestCase):
    def setUp(self):
        self.cli = client.Client("127.0.0.1", 9999, 5000, hostname="alice")

    def test_do_fetch_handles_missing_peers(self):
        with mock.patch("client.protocol.send_message", return_value=True):
            with mock.patch(
                "client.protocol.receive_message",
                return_value={"status": "success", "peer_list": []},
            ):
                download_mock = mock.Mock()
                self.cli._download_from_peer = download_mock
                self.cli._do_fetch("file.txt")
        download_mock.assert_not_called()

    def test_do_fetch_handles_error_status(self):
        with mock.patch("client.protocol.send_message", return_value=True):
            with mock.patch(
                "client.protocol.receive_message",
                return_value={"status": "error", "message": "not found"},
            ):
                download_mock = mock.Mock()
                self.cli._download_from_peer = download_mock
                self.cli._do_fetch("file.txt")
        download_mock.assert_not_called()

    def test_publish_failure_raise_runtime_error(self):
        with mock.patch("client.protocol.send_message", return_value=False):
            with self.assertRaises(RuntimeError):
                self.cli._do_publish(__file__, "dummy.txt")


if __name__ == "__main__":
    unittest.main()
