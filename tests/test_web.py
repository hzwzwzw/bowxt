import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import base64
import io
import json
from pathlib import Path

from PIL import Image

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
        self.assertNotIn('id="show-logs"', body)
        self.assertIn('id="show-agents"', body)
        self.assertIn('id="agent-panel"', body)
        self.assertIn('id="agent-log-dialog"', body)
        self.assertIn('id="agent-custom-panel-dialog"', body)
        self.assertIn("会话权限", body)
        self.assertIn("reconnect=1&amp;reconnect_delay=1000", body)
        self.assertIn('id="simulate-receive"', body)
        self.assertIn('id="simulation-dialog"', body)
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

    def test_simulated_chat_injection_wakes_agent_without_wechat_login(self):
        create = urllib.request.Request(
            self.base + "/api/simulated-chats",
            data=json.dumps({
                "name": "Web 模拟群",
                "chat_type": "group",
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(create, timeout=2) as response:
            chat = json.loads(response.read())["chat"]
        self.assertEqual(chat["source"], "simulation")

        self.assertEqual(
            self.service.claim_agent_messages(
                "web-simulation-agent", chat_ids=[chat["id"]], timeout=0
            ),
            [],
        )
        claimed = []

        def claim():
            claimed.extend(self.service.claim_agent_messages(
                "web-simulation-agent",
                chat_ids=[chat["id"]],
                timeout=2,
                require_sender=True,
                require_at_me=True,
            ))

        waiter = threading.Thread(target=claim)
        waiter.start()
        inject = urllib.request.Request(
            self.base + f"/api/chats/{chat['id']}/simulate",
            data=json.dumps({
                "text": "@机器人 测试问题",
                "sender": "模拟群员",
                "sender_organization": "模拟组织",
                "is_at_me": True,
            }, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(inject, timeout=2) as response:
            incoming = json.loads(response.read())["message"]
        waiter.join(3)

        self.assertFalse(self.service.status()["wechat_connected"])
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].message.seq, incoming["seq"])
        self.assertEqual(claimed[0].message.sender, "模拟群员")
        self.assertEqual(claimed[0].message.sender_organization, "模拟组织")

        outgoing_request = urllib.request.Request(
            self.base + f"/api/chats/{chat['id']}/messages",
            data=b'{"text":"agent reply","client_id":"simulation-web-reply"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(outgoing_request, timeout=2) as response:
            outgoing = json.loads(response.read())["message"]
        self.assertEqual(outgoing["delivery_status"], "sent")
        self.assertTrue(outgoing["verified"])

    def test_simulated_image_is_normalized_and_served_as_png(self):
        create = urllib.request.Request(
            self.base + "/api/simulated-chats",
            data=b'{"name":"image simulator","chat_type":"contact"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(create, timeout=2) as response:
            chat = json.loads(response.read())["chat"]
        source = io.BytesIO()
        Image.new("RGB", (7, 5), (30, 120, 220)).save(source, format="JPEG")
        inject = urllib.request.Request(
            self.base + f"/api/chats/{chat['id']}/simulate",
            data=json.dumps({
                "image": {
                    "data": base64.b64encode(source.getvalue()).decode(),
                    "mime_type": "image/jpeg",
                    "name": "debug.jpg",
                },
                "is_at_me": False,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(inject, timeout=2) as response:
            message = json.loads(response.read())["message"]
        self.assertEqual(message["message_type"], "image")
        self.assertEqual(message["image_mime_type"], "image/png")
        self.assertEqual((message["image_width"], message["image_height"]), (7, 5))
        with urllib.request.urlopen(self.base + message["image_url"], timeout=2) as response:
            self.assertEqual(response.read()[:8], b"\x89PNG\r\n\x1a\n")

    def test_agent_identity_cannot_create_or_inject_simulated_messages(self):
        create_as_agent = urllib.request.Request(
            self.base + "/api/simulated-chats",
            data=b'{"name":"forbidden","chat_type":"contact"}',
            headers={"Content-Type": "application/json", "X-Bowxt-Agent": "debug-agent"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(create_as_agent, timeout=2)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()

        chat = self.service.add_simulated_chat("human-created", ChatType.CONTACT)
        inject_as_agent = urllib.request.Request(
            self.base + f"/api/chats/{chat.id}/simulate",
            data=b'{"text":"fabricated"}',
            headers={"Content-Type": "application/json", "X-Bowxt-Agent": "debug-agent"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(inject_as_agent, timeout=2)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        self.assertEqual(self.store.get_messages(chat.id), [])

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

    def test_agent_instance_api_is_available_without_installed_plugins(self):
        with urllib.request.urlopen(self.base + "/api/agent/plugins", timeout=2) as response:
            plugins = json.loads(response.read())
        with urllib.request.urlopen(self.base + "/api/agent/instances", timeout=2) as response:
            instances = json.loads(response.read())

        self.assertEqual(plugins, {"plugins": []})
        self.assertEqual(instances, {"instances": []})

    def test_managed_agent_read_and_write_permissions_are_enforced(self):
        readable = self.store.upsert_chat("可读群", ChatType.GROUP)
        writable = self.store.upsert_chat("可写群", ChatType.GROUP)
        self.store.create_agent_instance(
            "restricted-agent",
            "missing-plugin",
            "Restricted",
            permissions={
                "read": {"mode": "selected", "chat_ids": [readable.id], "patterns": []},
                "write": {"mode": "selected", "chat_ids": [writable.id], "patterns": []},
            },
        )
        self.store.save_message(Message(
            id="allowed-message",
            chat=readable.name,
            content="allowed",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            chat_type=ChatType.GROUP,
        ))
        self.store.save_message(Message(
            id="blocked-message",
            chat=writable.name,
            content="blocked",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            chat_type=ChatType.GROUP,
        ))
        headers = {"Content-Type": "application/json", "X-Bowxt-Agent": "restricted-agent"}
        claim = urllib.request.Request(
            self.base + "/api/agents/restricted-agent/claim",
            data=json.dumps({"chat_ids": [], "replay_existing": True}).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(claim, timeout=2) as response:
            deliveries = json.loads(response.read())["deliveries"]
        self.assertEqual([item["message"]["content"] for item in deliveries], ["allowed"])

        with urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/chats",
            headers={"X-Bowxt-Agent": "restricted-agent"},
        ), timeout=2) as response:
            chats = json.loads(response.read())["chats"]
        self.assertEqual([chat["id"] for chat in chats], [readable.id])

        denied = urllib.request.Request(
            self.base + f"/api/chats/{readable.id}/messages",
            data=b'{"text":"denied"}',
            headers=headers,
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(denied, timeout=2)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

        self.service.start()
        allowed = urllib.request.Request(
            self.base + f"/api/chats/{writable.id}/messages",
            data=b'{"text":"allowed"}',
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=2) as response:
            self.assertEqual(response.status, 202)
        self.service.stop()

    def test_custom_panel_is_scoped_to_authenticated_managed_agent(self):
        self.store.create_agent_instance("panel-agent", "missing-plugin", "Panel Agent")
        request = urllib.request.Request(
            self.base + "/api/agent/panels/status",
            data=json.dumps({
                "title": "状态",
                "document": {
                    "version": 1,
                    "type": "tree",
                    "nodes": [{"label": "任务", "value": "运行中"}],
                },
            }).encode(),
            headers={"Content-Type": "application/json", "X-Bowxt-Agent": "panel-agent"},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            panel = json.loads(response.read())["panel"]
        self.assertEqual(panel["agent"], "panel-agent")

        with urllib.request.urlopen(
            self.base + "/api/agent/instances/panel-agent/panels/status", timeout=2
        ) as response:
            fetched = json.loads(response.read())["panel"]
        self.assertEqual(fetched["document"]["nodes"][0]["value"], "运行中")

        anonymous = urllib.request.Request(
            self.base + "/api/agent/panels/status",
            data=b'{"title":"x","document":{"version":1,"type":"tree","nodes":[]}}',
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(anonymous, timeout=2)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_history_endpoint(self):
        stored, _ = self.store.save_message(Message(
            id="history-web",
            chat="历史接口群",
            content="历史正文",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            chat_type=ChatType.GROUP,
        ))
        with urllib.request.urlopen(
            self.base
            + f"/api/chats/{stored.chat_id}/history"
            + "?since=2026-08-01T00%3A00%3A00%2B00%3A00"
            + "&until=2026-08-31T00%3A00%3A00%2B00%3A00",
            timeout=2,
        ) as response:
            history = json.loads(response.read())
        self.assertEqual(history["messages"][0]["content"], "历史正文")
