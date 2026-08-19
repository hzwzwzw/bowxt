from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .client import WeChatClient
from .models import ChatType
from .store import SQLiteStore, StoredChat, StoredMessage


class EventBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


@dataclass(slots=True)
class _SendCommand:
    chat_id: int
    text: str
    mentions: tuple[str, ...]
    completed: threading.Event = field(default_factory=threading.Event)
    result: StoredMessage | None = None
    error: BaseException | None = None


class BowxtService:
    """One safe UI worker exposed as a concurrent multi-chat service.

    HTTP threads never touch WeChat. They enqueue writes while one worker owns
    all focus changes, AT-SPI reads and XTest events. This is the only reliable
    meaning of concurrent multi-chat operation for a single desktop window.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        client_factory: Callable[[], WeChatClient] | None = None,
        poll_gap: float = 2.0,
        discovery_interval: float = 8.0,
    ):
        if poll_gap < 1.5:
            raise ValueError("poll_gap below 1.5s is intentionally rejected")
        self.store = store
        self.poll_gap = float(poll_gap)
        self.discovery_interval = max(float(discovery_interval), 4.0)
        self.events = EventBroker()
        self._client_factory = client_factory or self._default_client
        self._commands: queue.Queue[_SendCommand] = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "running": False,
            "wechat_connected": False,
            "last_error": None,
            "active_chat": None,
        }

    @staticmethod
    def _default_client() -> WeChatClient:
        return WeChatClient(
            visual_direction=True,
            uia_sender=os.environ.get("BOWXT_UIA_SENDER", "0") == "1",
        )

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> "BowxtService":
        if self.is_running:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bowxt-ui-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10.0)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            value = dict(self._status)
        value["running"] = bool(value.get("running") and self.is_running)
        value["chat_count"] = len(self.store.list_chats(enabled_only=True))
        return value

    def add_chat(
        self,
        name: str,
        chat_type: ChatType | str = ChatType.UNKNOWN,
        *,
        source: str = "manual",
    ) -> StoredChat:
        before = {chat.name for chat in self.store.list_chats()}
        chat = self.store.upsert_chat(name, chat_type, source=source)
        if chat.name not in before:
            self.events.publish({"type": "chat", "chat": chat.as_dict()})
        self._wake.set()
        return chat

    def update_chat_type(self, chat_id: int, chat_type: ChatType | str) -> StoredChat:
        chat = self.store.update_chat_type(chat_id, chat_type)
        self.events.publish({"type": "chat", "chat": chat.as_dict()})
        self._wake.set()
        return chat

    def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        mentions: Iterable[str] = (),
        timeout: float = 30.0,
    ) -> StoredMessage:
        if not self.is_running:
            raise RuntimeError("bowxt UI worker is not running")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must not be empty")
        command = _SendCommand(int(chat_id), text, tuple(mentions))
        self._commands.put(command, timeout=min(timeout, 5.0))
        self._wake.set()
        if not command.completed.wait(timeout):
            raise TimeoutError("timed out waiting for the WeChat UI worker")
        if command.error:
            raise command.error
        assert command.result is not None
        return command.result

    def _set_status(self, **changes: Any) -> None:
        with self._status_lock:
            self._status.update(changes)
        self.events.publish({"type": "status", "status": self.status()})

    def _run(self) -> None:
        client: WeChatClient | None = None
        next_poll = 0.0
        next_discovery = 0.0
        cursor = 0
        self._set_status(running=True)
        try:
            while not self._stop.is_set():
                if client is None:
                    try:
                        client = self._client_factory()
                        client.connect()
                        if not client.is_main_ui_ready:
                            raise RuntimeError("WeChat login or phone confirmation is still required")
                        self._set_status(wechat_connected=True, last_error=None)
                    except Exception as exc:
                        client = None
                        self._set_status(wechat_connected=False, last_error=str(exc))
                        self._stop.wait(2.0)
                        continue

                command = self._next_command()
                if command is not None:
                    self._execute_send(client, command)
                    next_poll = time.monotonic() + self.poll_gap
                    continue

                now = time.monotonic()
                if now >= next_discovery:
                    try:
                        for name in client.discover_unread_chats(limit=1):
                            self.add_chat(name, ChatType.UNKNOWN, source="unread")
                        self._set_status(last_error=None)
                    except Exception as exc:
                        self._set_status(last_error=f"discovery: {exc}")
                    next_discovery = now + self.discovery_interval

                # A stable ID order prevents new-message reordering in the UI
                # from starving a quieter conversation.
                chats = sorted(self.store.list_chats(enabled_only=True), key=lambda item: item.id)
                if chats and now >= next_poll:
                    chat = chats[cursor % len(chats)]
                    cursor = (cursor + 1) % max(len(chats), 1)
                    self._poll_chat(client, chat)
                    next_poll = time.monotonic() + self.poll_gap

                wait_for = min(
                    0.5,
                    max(0.05, next_poll - time.monotonic()) if chats else 0.5,
                )
                self._wake.wait(wait_for)
                self._wake.clear()
        finally:
            if client is not None:
                client.disconnect()
            self._set_status(running=False, wechat_connected=False, active_chat=None)

    def _next_command(self) -> _SendCommand | None:
        try:
            return self._commands.get_nowait()
        except queue.Empty:
            return None

    def _execute_send(self, client: WeChatClient, command: _SendCommand) -> None:
        try:
            chat = self.store.get_chat(command.chat_id)
            self._set_status(active_chat=chat.name)
            receipt = client.send_text(
                chat.name,
                command.text,
                chat_type=chat.chat_type,
                mentions=command.mentions,
            )
            stored, _created = self.store.save_receipt(receipt, chat.chat_type)
            command.result = stored
            self.events.publish({"type": "message", "message": stored.as_dict()})
            self.store.set_chat_error(chat.id, None)
            self._set_status(last_error=None)
        except BaseException as exc:
            command.error = exc
            try:
                self.store.set_chat_error(command.chat_id, str(exc))
            except Exception:
                pass
            self._set_status(last_error=str(exc))
        finally:
            self._set_status(active_chat=None)
            command.completed.set()
            self._commands.task_done()

    def _poll_chat(self, client: WeChatClient, chat: StoredChat) -> None:
        self._set_status(active_chat=chat.name)
        try:
            messages = client.get_visible_messages(chat.name, chat_type=chat.chat_type)
            for message in messages:
                stored, created = self.store.save_message(message)
                if created:
                    self.events.publish({"type": "message", "message": stored.as_dict()})
            self.store.set_chat_error(chat.id, None)
            self._set_status(last_error=None)
        except Exception as exc:
            self.store.set_chat_error(chat.id, str(exc))
            self._set_status(last_error=f"{chat.name}: {exc}")
        finally:
            self._set_status(active_chat=None)
