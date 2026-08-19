import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from bowxt.service import BowxtService
from bowxt.store import SQLiteStore
from bowxt.web import BowxtHTTPServer


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp.name) / "messages.db")
        self.service = BowxtService(self.store, client_factory=lambda: None, poll_gap=1.5)
        self.server = BowxtHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_static_application_is_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=2) as response:
            body = response.read().decode()
        self.assertIn("微信消息中枢", body)
        self.assertEqual(response.headers.get_content_type(), "text/html")

    def test_chat_api_creates_and_lists_chat(self):
        request = urllib.request.Request(
            self.base + "/api/chats",
            data=b'{"name":"contact-b","chat_type":"contact"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 201)
        with urllib.request.urlopen(self.base + "/api/chats", timeout=2) as response:
            body = response.read().decode()
        self.assertIn('"name":"contact-b"', body)
