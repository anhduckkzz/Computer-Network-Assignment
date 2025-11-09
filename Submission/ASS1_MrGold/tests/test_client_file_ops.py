import os
import tempfile
import unittest
from unittest import mock

import client


class ClientFileOperationTests(unittest.TestCase):
    def test_do_publish_infers_extension_and_metadata(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as temp_file:
            temp_file.write(b"Hello world!")
            temp_file.flush()

            cli = client.Client("127.0.0.1", 9999, 5000, hostname="alice")

            send_payloads = []

            def fake_send(sock, message):
                send_payloads.append(message)
                return True

            with mock.patch("client.protocol.send_message", side_effect=fake_send):
                with mock.patch(
                    "client.protocol.receive_message",
                    return_value={"status": "created"},
                ):
                    response = cli._do_publish(temp_file.name, "report.bin")

        self.assertEqual(response["status"], "created")
        self.assertEqual(len(send_payloads), 1)
        payload = send_payloads[0]
        self.assertEqual(payload["fname"], "report.txt")
        self.assertEqual(payload["file_size"], len(b"Hello world!"))
        self.assertTrue(payload["last_modified"].endswith("Z"))

    def test_do_publish_raises_for_missing_file(self):
        cli = client.Client("127.0.0.1", 9999, 5000, hostname="alice")
        with self.assertRaises(FileNotFoundError):
            cli._do_publish("missing.txt", "alias.txt")


class ClientPathUtilityTests(unittest.TestCase):
    def test_path_existence_and_size(self):
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(b"12345678")
            path = temp_file.name
        try:
            self.assertTrue(os.path.exists(path))
            self.assertEqual(os.path.getsize(path), 8)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
