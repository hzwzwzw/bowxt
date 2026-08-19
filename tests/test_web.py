import tempfile
import threading
import unittest
import urllib.request
import json
from pathlib import Path

from bowxt.models import ChatType, Direction, Message, MessageImage, MessageType
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
        self.assertIn('id="sync-mode"', body)
        self.assertIn("新消息唤醒", body)
        self.assertIn('id="show-logs"', body)
        self.assertIn("Agent 日志", body)
        self.assertIn("reconnect=1&amp;reconnect_delay=1000", body)
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

    def test_captured_message_image_is_served_inline(self):
        payload = b"\x89PNG\r\n\x1a\nweb-image"
        stored, _created = self.store.save_message(Message(
            id="web-picture",
            chat="hzw",
            content="[图片]",
            type=MessageType.IMAGE,
            direction=Direction.INCOMING,
            chat_type=ChatType.CONTACT,
            image=MessageImage(payload, width=90, height=60),
        ))

        with urllib.request.urlopen(self.base + stored.image_url, timeout=2) as response:
            body = response.read()

        self.assertEqual(body, payload)
        self.assertEqual(response.headers.get_content_type(), "image/png")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_control_api_pauses_and_changes_runtime_delays(self):
        request = urllib.request.Request(
            self.base + "/api/control",
            data=json.dumps({"paused": True, "poll_gap": 3.0, "action_delay": 0.08}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            body = json.loads(response.read())
        self.assertTrue(body["paused"])
        self.assertEqual(body["poll_gap"], 3.0)
        self.assertEqual(body["action_delay"], 0.08)

        request = urllib.request.Request(
            self.base + "/api/control",
            data=b'{"mode":"unread"}',
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            body = json.loads(response.read())
        self.assertFalse(body["paused"])
        self.assertEqual(body["mode"], "unread")

    def test_agent_logs_and_global_message_stream_are_exposed(self):
        stored, _ = self.store.save_message(Message(
            id="stream-message",
            chat="stream-chat",
            content="stream body",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            chat_type=ChatType.CONTACT,
        ))
        request = urllib.request.Request(
            self.base + "/api/agent/logs",
            data=json.dumps({
                "agent": "web-test",
                "level": "info",
                "event": "started",
                "message": "agent online",
                "context": {"message_seq": stored.seq},
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            created = json.loads(response.read())
        with urllib.request.urlopen(self.base + "/api/agent/logs?recent=1", timeout=2) as response:
            logs = json.loads(response.read())
        with urllib.request.urlopen(self.base + "/api/messages?after=0", timeout=2) as response:
            messages = json.loads(response.read())
        with urllib.request.urlopen(self.base + "/api/messages?recent=1&limit=1", timeout=2) as response:
            recent = json.loads(response.read())
        with urllib.request.urlopen(self.base + f"/api/messages/{stored.seq}", timeout=2) as response:
            message = json.loads(response.read())

        self.assertEqual(created["log"]["event"], "started")
        self.assertEqual(logs["logs"][0]["context"], {"message_seq": stored.seq})
        self.assertEqual(messages["messages"][0]["message_id"], "stream-message")
        self.assertEqual(recent["messages"][0]["message_id"], "stream-message")
        self.assertEqual(message["message"]["seq"], stored.seq)
