import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bowxt.agent import AgentClient
from bowxt.models import ChatType, Direction, Message, MessageType
from bowxt.service import BowxtService
from bowxt.store import SQLiteStore
from bowxt.web import BowxtHTTPServer


class AgentClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp.name) / "messages.db")
        self.service = BowxtService(self.store, client_factory=lambda: None, poll_gap=1.5)
        self.server = BowxtHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = AgentClient(
            "test-agent", base_url=f"http://127.0.0.1:{self.server.server_port}"
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_claim_ack_and_structured_group_fields(self):
        self.store.save_message(Message(
            id="agent-http-1",
            chat="Agent 测试群",
            content="@机器人 请处理",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            sender="群成员",
            sender_organization="测试组织",
            chat_type=ChatType.GROUP,
            is_at_me=True,
        ))

        chat = self.client.list_chats()[0]
        deliveries = self.client.claim(
            chat_ids=[chat.id], timeout=0, replay_existing=True
        )

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].message.sender, "群成员")
        self.assertEqual(deliveries[0].message.sender_organization, "测试组织")
        self.assertTrue(deliveries[0].message.is_at_me)
        self.client.ack(deliveries[0])
        self.assertEqual(self.client.claim(timeout=0), [])

    def test_agent_log_round_trip(self):
        value = self.client.log(
            "info", "开始处理", event="run_started", context={"conversation": "群"}
        )
        self.assertEqual(value.agent, "test-agent")
        self.assertEqual(value.context, {"conversation": "群"})
        self.assertEqual(self.store.get_agent_logs(recent=True)[0].event, "run_started")

    def test_managed_agent_can_publish_and_remove_declarative_panel(self):
        self.store.create_agent_instance("test-agent", "missing-plugin", "Test Agent")

        panel = self.client.publish_panel(
            "conversations",
            "会话信息",
            [{"label": "客户群", "meta": "1 个会话", "children": [
                {"label": "conversation-id", "value": "你好"}
            ]}],
        )

        self.assertEqual(panel["agent"], "test-agent")
        self.assertEqual(panel["document"]["version"], 1)
        stored = self.store.get_agent_panel("test-agent", "conversations")
        self.assertEqual(stored.title, "会话信息")
        self.client.delete_panel("conversations")
        self.assertEqual(self.store.list_agent_panels("test-agent"), [])

    def test_history_round_trip(self):
        before = Message(
            id="history-before",
            chat="Agent 历史群",
            content="窗口外",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            sender="甲",
            timestamp=datetime(2026, 8, 20, 9, 59, tzinfo=timezone.utc),
            chat_type=ChatType.GROUP,
        )
        inside = Message(
            id="history-inside",
            chat="Agent 历史群",
            content="窗口内",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            sender="乙",
            timestamp=datetime(2026, 8, 20, 10, 30, tzinfo=timezone.utc),
            chat_type=ChatType.GROUP,
        )
        self.store.save_message(before)
        self.store.save_message(inside)

        history = self.client.get_history(
            "Agent 历史群",
            duration_seconds=3600,
            until=datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc),
        )
        self.assertEqual([item.content for item in history], ["窗口内"])

if __name__ == "__main__":
    unittest.main()
