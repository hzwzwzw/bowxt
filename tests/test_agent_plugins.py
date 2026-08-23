import json
import stat
import tempfile
import time
import unittest
from pathlib import Path

from bowxt.agent_plugins import AgentManager, AgentPluginRegistry
from bowxt.service import EventBroker
from bowxt.store import SQLiteStore


class AgentPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        plugin = self.root / "plugins" / "demo"
        plugin.mkdir(parents=True)
        (plugin / "runner.py").write_text(
            "import os, time\n"
            "print('demo ready', flush=True)\n"
            "print('managed=' + os.environ.get('BOWXT_MANAGED', ''), flush=True)\n"
            "print('consumer=' + os.environ.get('BOWXT_CONSUMER', ''), flush=True)\n"
            "while True: time.sleep(.1)\n",
            encoding="utf-8",
        )
        (plugin / "prompts").mkdir()
        (plugin / "prompts" / "system.md").write_text("version one", encoding="utf-8")
        (plugin / "bowxt-agent.json").write_text(json.dumps({
            "schema_version": 1,
            "id": "demo-agent",
            "name": "Demo Agent",
            "version": "1.0",
            "description": "test plugin",
            "entrypoint": ["{python}", "{plugin_dir}/runner.py"],
            "default_config": {"chats": ["a"]},
            "resources": ["prompts"],
            "secret_schema": [{"name": "API_KEY", "label": "key"}],
        }), encoding="utf-8")
        self.store = SQLiteStore(self.root / "messages.db")
        self.manager = AgentManager(
            self.store,
            EventBroker(),
            base_url="http://127.0.0.1:8787",
            registry=AgentPluginRegistry((self.root / "plugins",)),
            data_root=self.root / "instances",
        )

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_instance_config_secrets_and_process_lifecycle(self):
        created = self.manager.create(
            "demo-agent",
            "secretary",
            "Personal Secretary",
            secrets={"API_KEY": "top-secret"},
            autostart=False,
        )
        self.assertEqual(created["config"], {"chats": ["a"]})
        self.assertEqual(created["plugin"]["lifecycle"]["owner"], "bowxt")
        self.assertEqual(created["secrets"], {"API_KEY": {"configured": True}})
        self.assertEqual(created["panels"], [])
        self.assertNotIn("top-secret", json.dumps(created))

        env_path = self.root / "instances" / "secretary" / ".env"
        self.assertIn("top-secret", env_path.read_text())
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

        status = self.manager.start("secretary")
        self.assertEqual(status["state"], "running")
        deadline = time.time() + 2
        while time.time() < deadline:
            logs = self.store.get_agent_logs(agent="secretary", recent=True)
            messages = {item.message for item in logs}
            if {"demo ready", "managed=1", "consumer=secretary"} <= messages:
                break
            time.sleep(0.02)
        self.assertIn("demo ready", messages)
        self.assertIn("managed=1", messages)
        self.assertIn("consumer=secretary", messages)
        self.assertEqual(self.manager.stop("secretary")["state"], "stopped")

    def test_running_instance_cannot_be_reconfigured(self):
        self.manager.create("demo-agent", "worker", "Worker")
        self.manager.start("worker")
        with self.assertRaisesRegex(ValueError, "stop the Agent"):
            self.manager.update("worker", config={"changed": True})

    def test_running_instance_can_save_and_restart_with_permissions(self):
        first = self.store.upsert_chat("客户一群")
        second = self.store.upsert_chat("内部群")
        self.manager.create("demo-agent", "worker", "Worker")
        before = self.manager.start("worker")["pid"]

        updated = self.manager.update(
            "worker",
            config={"changed": True},
            permissions={
                "read": {"mode": "regex_allow", "patterns": ["^客户"], "chat_ids": []},
                "write": {"mode": "selected", "chat_ids": [second.id], "patterns": []},
            },
            restart=True,
        )

        self.assertEqual(updated["status"]["state"], "running")
        self.assertNotEqual(updated["status"]["pid"], before)
        self.assertEqual([chat["id"] for chat in updated["access"]["read"]], [first.id])
        self.assertEqual([chat["id"] for chat in updated["access"]["write"]], [second.id])

    def test_earlier_plugin_directory_shadows_persisted_fallback_copy(self):
        fallback = self.root / "fallback" / "demo"
        fallback.mkdir(parents=True)
        (fallback / "bowxt-agent.json").write_text(json.dumps({
            "schema_version": 1,
            "id": "demo-agent",
            "name": "Fallback copy",
            "version": "0.9",
            "entrypoint": ["{python}", "fallback.py"],
        }), encoding="utf-8")
        registry = AgentPluginRegistry((self.root / "plugins", self.root / "fallback"))
        self.assertEqual(registry.get("demo-agent").name, "Demo Agent")

    def test_materialize_refreshes_plugin_owned_resources(self):
        self.manager.create("demo-agent", "worker", "Worker")
        target = self.root / "instances" / "worker" / "prompts" / "system.md"
        self.assertEqual(target.read_text(encoding="utf-8"), "version one")

        source = self.root / "plugins" / "demo" / "prompts" / "system.md"
        source.write_text("version two", encoding="utf-8")
        self.manager.update("worker", config={"changed": True})

        self.assertEqual(target.read_text(encoding="utf-8"), "version two")

    def test_panel_summary_is_exposed_on_instance_and_removed_with_it(self):
        self.manager.create("demo-agent", "worker", "Worker")
        panel = self.manager.publish_panel(
            "worker",
            "status",
            "运行状态",
            {"version": 1, "type": "tree", "nodes": [{"label": "正常"}]},
        )

        described = self.manager.describe("worker")
        self.assertEqual(described["panels"][0]["id"], "status")
        self.assertNotIn("document", described["panels"][0])
        self.assertEqual(panel["document"]["nodes"][0]["label"], "正常")

        self.manager.delete("worker")
        self.assertEqual(self.store.list_agent_panels("worker"), [])
