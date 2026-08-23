from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .service import EventBroker
from .panels import validate_panel_document
from .store import AgentInstance, AgentLog, SQLiteStore


@dataclass(frozen=True, slots=True)
class AgentPlugin:
    id: str
    name: str
    version: str
    description: str
    root: Path
    entrypoint: tuple[str, ...]
    default_config: dict[str, Any]
    config_schema: dict[str, Any]
    secret_schema: tuple[dict[str, Any], ...]
    resources: tuple[str, ...]
    lifecycle: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "default_config": self.default_config,
            "config_schema": self.config_schema,
            "secret_schema": list(self.secret_schema),
            "lifecycle": self.lifecycle,
        }


@dataclass(slots=True)
class _ProcessRecord:
    process: subprocess.Popen[str]
    started_at: float
    requested_stop: bool = False
    done: threading.Event = field(default_factory=threading.Event)


class AgentPluginRegistry:
    """Discover executable Agent packages from administrator-controlled paths."""

    def __init__(self, directories: tuple[Path, ...] | None = None):
        self.directories = directories or self._default_directories()
        self._plugins: dict[str, AgentPlugin] = {}
        self.reload()

    @staticmethod
    def _default_directories() -> tuple[Path, ...]:
        configured = os.environ.get("BOWXT_AGENT_PLUGIN_DIRS", "").strip()
        values = configured.split(os.pathsep) if configured else [
            "/opt/bowxt-agents",
            "/home/wechat/.local/share/bowxt/plugins",
        ]
        return tuple(Path(value).expanduser().resolve() for value in values if value)

    def reload(self) -> None:
        found: dict[str, AgentPlugin] = {}
        for directory in self.directories:
            if not directory.is_dir():
                continue
            candidates = [directory / "bowxt-agent.json"]
            candidates.extend(directory.glob("*/bowxt-agent.json"))
            for manifest_path in candidates:
                if not manifest_path.is_file():
                    continue
                plugin = self._load_manifest(manifest_path)
                if plugin.id in found:
                    # Directory order is an administrator-controlled precedence
                    # list. A read-only host mount may intentionally shadow the
                    # persisted fallback copy after an upgrade or migration.
                    continue
                found[plugin.id] = plugin
        self._plugins = found

    def list(self) -> list[AgentPlugin]:
        return sorted(self._plugins.values(), key=lambda item: (item.name, item.id))

    def get(self, plugin_id: str) -> AgentPlugin:
        try:
            return self._plugins[str(plugin_id)]
        except KeyError as exc:
            raise KeyError(f"Agent plugin is not installed: {plugin_id}") from exc

    @staticmethod
    def _load_manifest(path: Path) -> AgentPlugin:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError(f"unsupported Agent manifest: {path}")
        plugin_id = SQLiteStore._validate_agent_name(value.get("id", ""), field="plugin id")
        entrypoint = value.get("entrypoint")
        if not isinstance(entrypoint, list) or not entrypoint or not all(
            isinstance(item, str) and item for item in entrypoint
        ):
            raise ValueError(f"Agent plugin {plugin_id} has an invalid entrypoint")
        default_config = value.get("default_config", {})
        default_config_file = value.get("default_config_file")
        if default_config_file:
            default_path = (path.parent / str(default_config_file)).resolve()
            if path.parent.resolve() not in default_path.parents or not default_path.is_file():
                raise ValueError(f"Agent plugin {plugin_id} default config file is invalid")
            default_config = json.loads(default_path.read_text(encoding="utf-8"))
        config_schema = value.get("config_schema", {})
        secret_schema = value.get("secret_schema", [])
        resources = value.get("resources", [])
        lifecycle = value.get("lifecycle", {"owner": "bowxt"})
        if not isinstance(default_config, dict) or not isinstance(config_schema, dict):
            raise ValueError(f"Agent plugin {plugin_id} config metadata must be objects")
        if not isinstance(secret_schema, list) or not all(isinstance(item, dict) for item in secret_schema):
            raise ValueError(f"Agent plugin {plugin_id} secret_schema must be an array")
        if not isinstance(resources, list) or not all(isinstance(item, str) for item in resources):
            raise ValueError(f"Agent plugin {plugin_id} resources must be an array")
        if not isinstance(lifecycle, dict) or lifecycle.get("owner", "bowxt") != "bowxt":
            raise ValueError(f"Agent plugin {plugin_id} lifecycle owner must be bowxt")
        root = path.parent.resolve()
        for resource in resources:
            source = (root / resource).resolve()
            if root not in source.parents or not source.exists():
                raise ValueError(f"Agent plugin {plugin_id} resource is invalid: {resource}")
        return AgentPlugin(
            id=plugin_id,
            name=str(value.get("name", plugin_id)).strip() or plugin_id,
            version=str(value.get("version", "0")),
            description=str(value.get("description", "")),
            root=root,
            entrypoint=tuple(entrypoint),
            default_config=default_config,
            config_schema=config_schema,
            secret_schema=tuple(secret_schema),
            resources=tuple(resources),
            lifecycle=lifecycle,
        )


class AgentManager:
    """Persist Agent instances and supervise their isolated child processes."""

    def __init__(
        self,
        store: SQLiteStore,
        events: EventBroker,
        *,
        base_url: str,
        registry: AgentPluginRegistry | None = None,
        data_root: Path | None = None,
    ):
        self.store = store
        self.events = events
        self.base_url = base_url.rstrip("/")
        self.registry = registry or AgentPluginRegistry()
        self.data_root = data_root or Path(store.path).parent / "agents"
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._processes: dict[str, _ProcessRecord] = {}

    def start_autostart(self) -> None:
        for instance in self.store.list_agent_instances():
            if instance.autostart:
                try:
                    self.start(instance.id)
                except Exception as exc:
                    self._log(instance.id, "error", f"自动启动失败：{exc}", "process_start_failed")

    def close(self) -> None:
        for instance_id in list(self._processes):
            self.stop(instance_id)

    def plugins(self) -> list[dict[str, Any]]:
        self.registry.reload()
        return [plugin.as_dict() for plugin in self.registry.list()]

    def instances(self) -> list[dict[str, Any]]:
        return [self.describe(item) for item in self.store.list_agent_instances()]

    def describe(self, instance: AgentInstance | str) -> dict[str, Any]:
        value = self.store.get_agent_instance(instance) if isinstance(instance, str) else instance
        status = self.status(value.id)
        result = value.as_dict()
        try:
            result["plugin"] = self.registry.get(value.plugin_id).as_dict()
            result["plugin_available"] = True
        except KeyError:
            result["plugin"] = None
            result["plugin_available"] = False
        result["status"] = status
        activity = self.store.get_agent_consumer_activity(value.id)
        chats_by_id = {chat.id: chat for chat in self.store.list_chats()}
        activity["chats"] = [
            chats_by_id[chat_id].as_dict()
            for chat_id in activity["last_claim_chat_ids"]
            if chat_id in chats_by_id
        ]
        result["consumer_activity"] = activity
        result["access"] = {
            capability: [chat.as_dict() for chat in self.allowed_chats(value.id, capability) or []]
            for capability in ("read", "write")
        }
        result["panels"] = [
            panel.as_dict(include_document=False)
            for panel in self.store.list_agent_panels(value.id)
        ]
        return result

    def publish_panel(
        self,
        consumer: str,
        panel_id: str,
        title: str,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish a code-free panel owned by the authenticated managed Agent."""

        self.store.get_agent_instance(consumer)
        normalized = validate_panel_document(document)
        panel = self.store.upsert_agent_panel(consumer, panel_id, title, normalized)
        summary = panel.as_dict(include_document=False)
        self.events.publish({"type": "agent_panel", "panel": summary})
        return panel.as_dict()

    def delete_panel(self, consumer: str, panel_id: str) -> None:
        self.store.get_agent_instance(consumer)
        self.store.delete_agent_panel(consumer, panel_id)
        self.events.publish(
            {"type": "agent_panel_removed", "agent": consumer, "panel_id": panel_id}
        )

    def create(
        self,
        plugin_id: str,
        instance_id: str,
        name: str,
        *,
        config: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        permissions: dict[str, Any] | None = None,
        autostart: bool = False,
    ) -> dict[str, Any]:
        plugin = self.registry.get(plugin_id)
        value = self.store.create_agent_instance(
            instance_id,
            plugin.id,
            name,
            config=plugin.default_config if config is None else config,
            secrets=secrets,
            permissions=self.normalize_permissions(permissions),
            autostart=autostart,
        )
        self._materialize(value, plugin)
        self._publish(value.id)
        if autostart:
            self.start(value.id)
        return self.describe(value.id)

    def update(
        self,
        instance_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        secrets: dict[str, str | None] | None = None,
        permissions: dict[str, Any] | None = None,
        autostart: bool | None = None,
        restart: bool = False,
    ) -> dict[str, Any]:
        status = self.status(instance_id)["state"]
        if status == "stopping":
            raise ValueError("wait for the Agent to stop before changing its configuration")
        if status == "running" and not restart:
            raise ValueError("stop the Agent before changing its configuration")
        normalized_permissions = (
            None if permissions is None else self.normalize_permissions(permissions)
        )
        if status == "running":
            self.stop(instance_id)
        value = self.store.update_agent_instance(
            instance_id,
            name=name,
            config=config,
            secrets=secrets,
            permissions=normalized_permissions,
            autostart=autostart,
        )
        self._materialize(value, self.registry.get(value.plugin_id))
        self._publish(value.id)
        if status == "running":
            self.start(value.id)
        return self.describe(value)

    @classmethod
    def normalize_permissions(cls, value: dict[str, Any] | None) -> dict[str, Any]:
        source = SQLiteStore.default_agent_permissions() if value is None else value
        if not isinstance(source, dict):
            raise ValueError("permissions must be an object")
        unknown = set(source) - {"read", "write"}
        if unknown:
            raise ValueError("permissions accepts only read and write policies")
        result: dict[str, Any] = {}
        for capability in ("read", "write"):
            policy = source.get(capability, {"mode": "all"})
            if not isinstance(policy, dict):
                raise ValueError(f"permissions.{capability} must be an object")
            policy_unknown = set(policy) - {"mode", "chat_ids", "patterns"}
            if policy_unknown:
                raise ValueError(
                    f"permissions.{capability} accepts only mode, chat_ids and patterns"
                )
            mode = str(policy.get("mode", "all"))
            if mode not in {"all", "selected", "regex_allow", "regex_deny"}:
                raise ValueError(
                    f"permissions.{capability}.mode must be all, selected, regex_allow or regex_deny"
                )
            raw_ids = policy.get("chat_ids", [])
            raw_patterns = policy.get("patterns", [])
            if not isinstance(raw_ids, list) or not isinstance(raw_patterns, list):
                raise ValueError(
                    f"permissions.{capability}.chat_ids and patterns must be arrays"
                )
            chat_ids = tuple(dict.fromkeys(int(item) for item in raw_ids))
            if len(chat_ids) > 1000 or any(item <= 0 for item in chat_ids):
                raise ValueError(
                    f"permissions.{capability}.chat_ids must contain at most 1000 positive ids"
                )
            patterns = tuple(dict.fromkeys(str(item) for item in raw_patterns))
            if len(patterns) > 100 or any(not item or len(item) > 256 for item in patterns):
                raise ValueError(
                    f"permissions.{capability}.patterns must contain 1-256 character expressions"
                )
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(
                        f"permissions.{capability} contains invalid regex {pattern!r}: {exc}"
                    ) from exc
            result[capability] = {
                "mode": mode,
                "chat_ids": list(chat_ids),
                "patterns": list(patterns),
            }
        return result

    def allowed_chats(self, consumer: str, capability: str):
        """Return matching chats for managed consumers, or ``None`` for fallback clients."""

        try:
            instance = self.store.get_agent_instance(consumer)
        except KeyError:
            return None
        if capability not in {"read", "write"}:
            raise ValueError("capability must be read or write")
        policy = self.normalize_permissions(instance.permissions)[capability]
        chats = self.store.list_chats()
        mode = policy["mode"]
        if mode == "all":
            return chats
        if mode == "selected":
            selected = set(policy["chat_ids"])
            return [chat for chat in chats if chat.id in selected]
        patterns = [re.compile(item) for item in policy["patterns"]]
        matched = lambda chat: any(pattern.search(chat.name) for pattern in patterns)
        if mode == "regex_allow":
            return [chat for chat in chats if matched(chat)]
        return [chat for chat in chats if not matched(chat)]

    def filter_read_chat_ids(
        self, consumer: str, requested_chat_ids: tuple[int, ...]
    ) -> tuple[int, ...] | None:
        allowed = self.allowed_chats(consumer, "read")
        if allowed is None:
            return None
        allowed_ids = {chat.id for chat in allowed}
        if requested_chat_ids:
            return tuple(chat_id for chat_id in requested_chat_ids if chat_id in allowed_ids)
        return tuple(chat.id for chat in allowed)

    def permits_chat(self, consumer: str, capability: str, chat_id: int) -> bool | None:
        allowed = self.allowed_chats(consumer, capability)
        if allowed is None:
            return None
        return int(chat_id) in {chat.id for chat in allowed}

    def publish_activity(self, consumer: str) -> None:
        """Refresh a managed instance card after its consumer completes a claim."""

        try:
            self.store.get_agent_instance(consumer)
        except KeyError:
            return
        self._publish(consumer)

    def delete(self, instance_id: str) -> None:
        if self.status(instance_id)["state"] in {"running", "stopping"}:
            raise ValueError("stop the Agent before removing it")
        self.store.delete_agent_instance(instance_id)
        self.events.publish({"type": "agent_instance_removed", "instance_id": instance_id})

    def start(self, instance_id: str) -> dict[str, Any]:
        with self._lock:
            current = self._processes.get(instance_id)
            if current and current.process.poll() is None:
                return self.status(instance_id)
            instance = self.store.get_agent_instance(instance_id)
            plugin = self.registry.get(instance.plugin_id)
            paths = self._materialize(instance, plugin)
            replacements = {
                "python": sys.executable,
                "plugin_dir": str(plugin.root),
                "instance_dir": str(paths["instance_dir"]),
                "config_path": str(paths["config_path"]),
                "env_path": str(paths["env_path"]),
            }
            command = [self._format_argument(item, replacements) for item in plugin.entrypoint]
            environment = os.environ.copy()
            environment.update({
                "BOWXT_MANAGED": "1",
                "BOWXT_BASE_URL": self.base_url,
                "BOWXT_AGENT_ID": instance.id,
                "BOWXT_CONSUMER": instance.id,
                "BOWXT_AGENT_DATA_DIR": str(paths["instance_dir"] / "data"),
                "PYTHONUNBUFFERED": "1",
            })
            environment.update(instance.secrets)
            process = subprocess.Popen(
                command,
                cwd=paths["instance_dir"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            record = _ProcessRecord(process=process, started_at=time.time())
            self._processes[instance.id] = record
            threading.Thread(
                target=self._read_output,
                args=(instance.id, process),
                name=f"bowxt-agent-output-{instance.id}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._wait_process,
                args=(instance.id, record),
                name=f"bowxt-agent-wait-{instance.id}",
                daemon=True,
            ).start()
            self._log(
                instance.id,
                "info",
                f"{instance.name} 已启动",
                "process_started",
                {"pid": process.pid, "plugin": plugin.id},
            )
            self._publish(instance.id)
            return self.status(instance.id)

    def stop(self, instance_id: str) -> dict[str, Any]:
        instance = self.store.get_agent_instance(instance_id)
        try:
            lifecycle = self.registry.get(instance.plugin_id).lifecycle
        except KeyError:
            lifecycle = {}
        stop_timeout = min(
            max(float(lifecycle.get("stop_timeout_seconds", 8.0)), 2.0),
            60.0,
        )
        with self._lock:
            record = self._processes.get(instance_id)
            if not record or record.process.poll() is not None:
                self._processes.pop(instance_id, None)
                return self.status(instance_id)
            record.requested_stop = True
            record.process.terminate()
        try:
            record.process.wait(timeout=stop_timeout)
        except subprocess.TimeoutExpired:
            record.process.kill()
            record.process.wait(timeout=3)
        record.done.wait(timeout=2)
        self._publish(instance_id)
        return self.status(instance_id)

    def restart(self, instance_id: str) -> dict[str, Any]:
        self.stop(instance_id)
        return self.start(instance_id)

    def status(self, instance_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._processes.get(instance_id)
            if not record:
                return {"state": "stopped", "pid": None, "exit_code": None, "started_at": None}
            code = record.process.poll()
            if code is None:
                state = "stopping" if record.requested_stop else "running"
            else:
                state = "stopped" if record.requested_stop else "failed"
            return {
                "state": state,
                "pid": record.process.pid if code is None else None,
                "exit_code": code,
                "started_at": record.started_at,
            }

    def _materialize(self, instance: AgentInstance, plugin: AgentPlugin) -> dict[str, Path]:
        instance_dir = (self.data_root / instance.id).resolve()
        if self.data_root.resolve() not in instance_dir.parents:
            raise ValueError("invalid Agent instance path")
        instance_dir.mkdir(parents=True, exist_ok=True)
        (instance_dir / "data").mkdir(exist_ok=True)
        config_path = instance_dir / "config.json"
        env_path = instance_dir / ".env"
        config_path.write_text(
            json.dumps(instance.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(config_path, 0o600)
        env_path.write_text(
            "".join(f"{key}={self._env_quote(value)}\n" for key, value in instance.secrets.items()),
            encoding="utf-8",
        )
        os.chmod(env_path, 0o600)
        for resource in plugin.resources:
            source = plugin.root / resource
            target = instance_dir / resource
            if target.exists():
                # Declared resources are plugin-owned snapshots, not mutable
                # instance data. Refresh them on every materialization so a
                # plugin upgrade updates prompts/skills together with code.
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        return {"instance_dir": instance_dir, "config_path": config_path, "env_path": env_path}

    @staticmethod
    def _format_argument(value: str, replacements: dict[str, str]) -> str:
        try:
            return value.format_map(replacements)
        except KeyError as exc:
            raise ValueError(f"unknown Agent entrypoint placeholder: {exc.args[0]}") from exc

    @staticmethod
    def _env_quote(value: str) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    def _read_output(self, instance_id: str, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                clean = line.rstrip("\r\n")
                if clean:
                    self._log(instance_id, "info", clean[:20000], "process_output")
        finally:
            process.stdout.close()

    def _wait_process(self, instance_id: str, record: _ProcessRecord) -> None:
        try:
            code = record.process.wait()
            level = "info" if record.requested_stop and code in {0, -15} else "error"
            event = "process_stopped" if record.requested_stop else "process_exited"
            self._log(instance_id, level, f"Agent 进程已退出（code={code}）", event, {"exit_code": code})
            self._publish(instance_id)
        finally:
            record.done.set()

    def _log(
        self,
        agent: str,
        level: str,
        message: str,
        event: str,
        context: dict[str, Any] | None = None,
    ) -> AgentLog:
        log = self.store.append_agent_log(agent, level, message, event=event, context=context)
        self.events.publish({"type": "agent_log", "log": log.as_dict()})
        return log

    def _publish(self, instance_id: str) -> None:
        try:
            value = self.describe(instance_id)
        except KeyError:
            return
        self.events.publish({"type": "agent_instance", "instance": value})
