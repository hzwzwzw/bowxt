import tempfile
import threading
import unittest
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
            chat_type=ChatType.GROUP,
            is_at_me=True,
        ))

        chat = self.client.list_chats()[0]
        deliveries = self.client.claim(
            chat_ids=[chat.id], timeout=0, replay_existing=True
        )

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].message.sender, "群成员")
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


if __name__ == "__main__":
    unittest.main()
