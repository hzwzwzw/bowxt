from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable

from .client import WeChatClient
from .errors import ServicePaused
from .models import ChatType, Direction, Message, MessageImage, MessageType, SendReceipt
from .store import AgentDelivery, AgentLog, SQLiteStore, StoredChat, StoredMessage


class SyncMode(str, Enum):
    POLLING = "polling"
    UNREAD = "unread"
    PAUSED = "paused"


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
    pending_seq: int
    queued_at: float = field(default_factory=time.monotonic)
    completed: threading.Event = field(default_factory=threading.Event)
    result: StoredMessage | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _SenderJob:
    chat_id: int
    message_id: str


@dataclass(frozen=True, slots=True)
class _ImageJob:
    chat_id: int
    message_id: str


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
        poll_gap: float = 1.5,
        discovery_interval: float = 4.0,
        action_delay: float = 0.12,
        sync_mode: SyncMode | str = SyncMode.POLLING,
    ):
        if poll_gap < 1.5:
            raise ValueError("poll_gap below 1.5s is intentionally rejected")
        self.store = store
        self.poll_gap = float(poll_gap)
        if not 0.06 <= float(action_delay) <= 0.5:
            raise ValueError("action_delay must be between 0.06 and 0.5 seconds")
        self.action_delay = float(action_delay)
        self.discovery_interval = max(float(discovery_interval), 4.0)
        self._mode = SyncMode(sync_mode)
        self._resume_mode = (
            self._mode if self._mode is not SyncMode.PAUSED else SyncMode.POLLING
        )
        self.events = EventBroker()
        self._client_factory = client_factory or self._default_client
        self._commands: queue.Queue[_SendCommand] = queue.Queue(maxsize=128)
        self._sender_jobs: deque[_SenderJob] = deque()
        self._sender_job_keys: set[tuple[int, str]] = set()
        self._image_jobs: deque[_ImageJob] = deque()
        self._image_job_keys: set[tuple[int, str]] = set()
        self._stop = threading.Event()
        self._paused = threading.Event()
        if self._mode is SyncMode.PAUSED:
            self._paused.set()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._sender_event_seqs: set[int] = set()
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "running": False,
            "wechat_connected": False,
            "last_error": None,
            "active_chat": None,
            "last_send_timings": None,
        }

    def _default_client(self) -> WeChatClient:
        my_names = tuple(
            value.strip()
            for value in os.environ.get("BOWXT_MY_NAMES", "").split(",")
            if value.strip()
        )
        client = WeChatClient(
            visual_direction=True,
            uia_sender=os.environ.get("BOWXT_UIA_SENDER", "1") == "1",
            my_names=my_names,
        )
        client.set_operation_delay(self.action_delay)
        return client

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
        value["paused"] = self._paused.is_set()
        value["mode"] = self._mode.value
        value["poll_gap"] = self.poll_gap
        value["action_delay"] = self.action_delay
        value["queue_depth"] = self._commands.qsize()
        value["sender_queue_depth"] = len(self._sender_jobs)
        value["image_queue_depth"] = len(self._image_jobs)
        return value

    def configure(
        self,
        *,
        paused: bool | None = None,
        mode: SyncMode | str | None = None,
        poll_gap: float | None = None,
        action_delay: float | None = None,
    ) -> dict[str, Any]:
        if paused is not None and mode is not None:
            raise ValueError("configure accepts either mode or paused, not both")
        if poll_gap is not None:
            value = float(poll_gap)
            if not 1.5 <= value <= 30.0:
                raise ValueError("poll_gap must be between 1.5 and 30 seconds")
            self.poll_gap = value
        if action_delay is not None:
            value = float(action_delay)
            if not 0.06 <= value <= 0.5:
                raise ValueError("action_delay must be between 0.06 and 0.5 seconds")
            self.action_delay = value
        if mode is not None:
            selected = SyncMode(mode)
            self._mode = selected
            if selected is SyncMode.PAUSED:
                self._paused.set()
            else:
                self._resume_mode = selected
                self._paused.clear()
        elif paused is not None:
            if not isinstance(paused, bool):
                raise ValueError("paused must be a boolean")
            if paused:
                if self._mode is not SyncMode.PAUSED:
                    self._resume_mode = self._mode
                self._mode = SyncMode.PAUSED
                self._paused.set()
            else:
                self._mode = self._resume_mode
                self._paused.clear()
        self._wake.set()
        self.events.publish({"type": "status", "status": self.status()})
        return self.status()

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

    def add_simulated_chat(
        self, name: str, chat_type: ChatType | str
    ) -> StoredChat:
        chat = self.store.create_simulated_chat(name, chat_type)
        self.events.publish({"type": "chat", "chat": chat.as_dict()})
        return chat

    def inject_simulated_message(
        self,
        chat_id: int,
        *,
        text: str = "",
        sender: str | None = None,
        sender_organization: str | None = None,
        timestamp: datetime | None = None,
        is_at_me: bool = False,
        image: MessageImage | None = None,
    ) -> StoredMessage:
        """Persist a fabricated incoming message and wake normal Agent consumers."""

        if self._paused.is_set():
            raise ServicePaused("bowxt UI worker is paused")
        chat = self.store.get_chat(chat_id)
        if chat.source != "simulation":
            raise ValueError("messages can only be simulated inside a simulated chat")
        clean_text = str(text or "").strip()
        if image is None and not clean_text:
            raise ValueError("simulated text must not be empty")
        if len(clean_text) > 20_000:
            raise ValueError("simulated text must not exceed 20000 characters")
        clean_sender = " ".join(str(sender or "").split())
        clean_organization = " ".join(str(sender_organization or "").split())
        if chat.chat_type is ChatType.GROUP and not clean_sender:
            raise ValueError("simulated group messages require a sender")
        if len(clean_sender) > 128 or len(clean_organization) > 128:
            raise ValueError("sender and organization must not exceed 128 characters")
        if chat.chat_type is ChatType.CONTACT:
            clean_sender = clean_sender or chat.name
            clean_organization = ""
        message_time = timestamp or datetime.now(timezone.utc)
        if message_time.tzinfo is None or message_time.utcoffset() is None:
            raise ValueError("simulated message timestamp must include a timezone")
        message = Message(
            id=f"simulation:{uuid.uuid4().hex}",
            chat=chat.name,
            content=clean_text or "[图片]",
            type=MessageType.IMAGE if image is not None else MessageType.TEXT,
            direction=Direction.INCOMING,
            sender=clean_sender,
            sender_organization=clean_organization or None,
            timestamp=message_time.astimezone(timezone.utc),
            chat_type=chat.chat_type,
            is_at_me=bool(is_at_me),
            image=image,
            raw={"source": "simulation", "sender_source": "simulation"},
        )
        stored, _created = self.store.save_message(message)
        self.events.publish({"type": "message", "message": stored.as_dict()})
        return stored

    def update_chat_type(self, chat_id: int, chat_type: ChatType | str) -> StoredChat:
        chat = self.store.update_chat_type(chat_id, chat_type)
        self.events.publish({"type": "chat", "chat": chat.as_dict()})
        self._wake.set()
        return chat

    def claim_agent_messages(
        self,
        consumer: str,
        *,
        chat_ids: Iterable[int] = (),
        limit: int = 8,
        lease_seconds: float = 60.0,
        timeout: float = 0.0,
        require_sender: bool = False,
        require_at_me: bool = False,
        replay_existing: bool = False,
        deny_all_chats: bool = False,
    ) -> list[AgentDelivery]:
        """Long-poll the durable incoming-message stream for an Agent.

        The store lease is the source of truth; the in-memory broker is used
        only as a wake-up hint, so reconnects and process restarts do not lose
        messages.
        """

        if self._paused.is_set():
            raise ServicePaused("bowxt UI worker is paused")
        selected_chat_ids = tuple(dict.fromkeys(int(item) for item in chat_ids))
        wait_seconds = min(max(float(timeout), 0.0), 25.0)
        deadline = time.monotonic() + wait_seconds
        subscriber = self.events.subscribe() if wait_seconds else None
        try:
            while True:
                if self._paused.is_set():
                    raise ServicePaused("bowxt UI worker is paused")
                deliveries = self.store.claim_agent_messages(
                    consumer,
                    chat_ids=selected_chat_ids,
                    limit=limit,
                    lease_seconds=lease_seconds,
                    require_sender=require_sender,
                    require_at_me=require_at_me,
                    replay_existing=replay_existing,
                    deny_all_chats=deny_all_chats,
                )
                if deliveries or subscriber is None:
                    return deliveries
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                try:
                    subscriber.get(timeout=remaining)
                except queue.Empty:
                    return []
        finally:
            if subscriber is not None:
                self.events.unsubscribe(subscriber)

    def ack_agent_message(self, consumer: str, message_seq: int, lease_token: str) -> None:
        self.store.ack_agent_message(consumer, message_seq, lease_token)

    def nack_agent_message(
        self,
        consumer: str,
        message_seq: int,
        lease_token: str,
        *,
        error: str = "",
        retry_delay: float = 5.0,
    ) -> None:
        self.store.nack_agent_message(
            consumer,
            message_seq,
            lease_token,
            error=error,
            retry_delay=retry_delay,
        )
        self.events.publish({"type": "agent_delivery_retry", "consumer": consumer})

    def log_agent(
        self,
        agent: str,
        level: str,
        message: str,
        *,
        event: str = "log",
        context: dict[str, Any] | None = None,
    ) -> AgentLog:
        value = self.store.append_agent_log(
            agent,
            level,
            message,
            event=event,
            context=context,
        )
        self.events.publish({"type": "agent_log", "log": value.as_dict()})
        return value

    def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        mentions: Iterable[str] = (),
        timeout: float = 30.0,
    ) -> StoredMessage:
        _pending, command = self._enqueue_text(chat_id, text, mentions=mentions)
        if command is None:
            return _pending
        if not command.completed.wait(timeout):
            raise TimeoutError("timed out waiting for the WeChat UI worker")
        if command.error:
            raise command.error
        assert command.result is not None
        return command.result

    def enqueue_text(
        self,
        chat_id: int,
        text: str,
        *,
        mentions: Iterable[str] = (),
        client_id: str | None = None,
    ) -> StoredMessage:
        """Persist and queue a send without waiting for visible UI confirmation."""

        pending, _command = self._enqueue_text(
            chat_id,
            text,
            mentions=mentions,
            client_id=client_id,
        )
        return pending

    def _enqueue_text(
        self,
        chat_id: int,
        text: str,
        *,
        mentions: Iterable[str] = (),
        client_id: str | None = None,
    ) -> tuple[StoredMessage, _SendCommand | None]:
        if self._paused.is_set():
            raise ServicePaused("bowxt UI worker is paused")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must not be empty")
        mention_names = tuple(mentions)
        chat = self.store.get_chat(int(chat_id))
        if chat.source == "simulation":
            pending, created = self.store.queue_send(
                int(chat_id), text, client_id=client_id, mentions=mention_names
            )
            if created:
                pending = self.store.complete_pending_send(
                    pending.seq,
                    SendReceipt(
                        chat=chat.name,
                        content=text,
                        sent_at=datetime.now(timezone.utc),
                        verified=True,
                        matched_message_id=pending.message_id,
                        mentions=mention_names,
                    ),
                )
                self.events.publish({"type": "message", "message": pending.as_dict()})
            return pending, None
        if not self.is_running:
            raise RuntimeError("bowxt UI worker is not running")
        pending, created = self.store.queue_send(
            int(chat_id), text, client_id=client_id, mentions=mention_names
        )
        if not created:
            return pending, None
        command = _SendCommand(int(chat_id), text, mention_names, pending.seq)
        try:
            self._commands.put_nowait(command)
        except queue.Full as exc:
            failed = self.store.fail_pending_send(pending.seq, "send queue is full")
            self.events.publish({"type": "message", "message": failed.as_dict()})
            raise RuntimeError("send queue is full") from exc
        self.events.publish({"type": "message", "message": pending.as_dict()})
        self._wake.set()
        self.events.publish({"type": "status", "status": self.status()})
        return pending, command

    def _set_status(self, **changes: Any) -> None:
        with self._status_lock:
            self._status.update(changes)
        self.events.publish({"type": "status", "status": self.status()})

    def _run(self) -> None:
        client: WeChatClient | None = None
        next_poll = 0.0
        next_discovery = 0.0
        cursor = 0
        applied_action_delay: float | None = None
        applied_mode: SyncMode | None = None
        self._set_status(running=True)
        try:
            while not self._stop.is_set():
                if self._paused.is_set():
                    self._wake.wait(0.5)
                    self._wake.clear()
                    continue
                if client is not None and getattr(client, "is_input_blocked", False):
                    # The failed operation stays failed and is never replayed.
                    # Discard only the locked in-memory session so a later
                    # iteration can reconnect after the visible surface is
                    # clean. If a transient still exists, the fresh client
                    # will fail closed again on its next UI operation.
                    client.disconnect()
                    client = None
                    applied_action_delay = None
                    applied_mode = None
                    self._set_status(
                        wechat_connected=False,
                        active_chat=None,
                        last_error="resetting the UI session after a transient-window safety lock",
                    )
                    self._stop.wait(0.5)
                    continue
                if client is None:
                    try:
                        client = self._client_factory()
                        client.connect()
                        if not client.is_main_ui_ready:
                            raise RuntimeError("WeChat login or phone confirmation is still required")
                        self._set_status(wechat_connected=True, last_error=None)
                    except Exception as exc:
                        client = None
                        applied_action_delay = None
                        self._set_status(wechat_connected=False, last_error=str(exc))
                        self._stop.wait(2.0)
                        continue

                if self._paused.is_set():
                    continue
                if applied_action_delay != self.action_delay:
                    setter = getattr(client, "set_operation_delay", None)
                    if setter is not None:
                        setter(self.action_delay)
                    applied_action_delay = self.action_delay
                mode = self._mode
                if applied_mode is not mode:
                    next_poll = 0.0
                    next_discovery = 0.0
                    applied_mode = mode
                command = self._next_command()
                if command is not None:
                    self._execute_send(client, command)
                    next_poll = time.monotonic()
                    continue

                image_job = self._next_image_job()
                if image_job is not None:
                    self._execute_image_job(client, image_job)
                    next_poll = time.monotonic()
                    continue

                # Sender-card reads are background work. A newly queued send
                # is always selected before the next card, so a burst of group
                # messages cannot hold outgoing traffic behind the full burst.
                sender_job = self._next_sender_job()
                if sender_job is not None:
                    self._execute_sender_job(client, sender_job)
                    next_poll = time.monotonic()
                    continue

                now = time.monotonic()
                if not self._paused.is_set() and now >= next_discovery:
                    try:
                        processed_unread = self._drain_unread_chats(client)
                        if mode is SyncMode.UNREAD and not processed_unread:
                            self._poll_visible_chat(client)
                        self._set_status(last_error=None)
                    except Exception as exc:
                        self._set_status(last_error=f"discovery: {exc}")
                    interval = (
                        self.poll_gap
                        if mode is SyncMode.UNREAD
                        else self.discovery_interval
                    )
                    next_discovery = time.monotonic() + interval

                # A stable ID order prevents new-message reordering in the UI
                # from starving a quieter conversation.
                chats = sorted(
                    (
                        item
                        for item in self.store.list_chats(enabled_only=True)
                        if item.source != "simulation"
                    ),
                    key=lambda item: item.id,
                )
                if (
                    mode is SyncMode.POLLING
                    and not self._paused.is_set()
                    and chats
                    and now >= next_poll
                ):
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

    def _drain_unread_chats(self, client: WeChatClient, *, limit: int = 32) -> bool:
        """Read every currently visible unread row in one wake cycle."""

        names = client.discover_unread_chats(limit=limit)
        processed = bool(names)
        for name in dict.fromkeys(names):
            type_reader = getattr(client, "discovered_chat_type", None)
            chat_type = type_reader(name) if type_reader is not None else ChatType.UNKNOWN
            discovered = self.add_chat(name, chat_type, source="unread")
            self._poll_chat(client, discovered)
        return processed

    def _poll_visible_chat(self, client: WeChatClient) -> None:
        """Read the already-open monitored chat without switching windows."""

        reader = getattr(client, "visible_chat_name", None)
        if reader is None:
            return
        name = reader()
        if not name:
            return
        chat = next(
            (item for item in self.store.list_chats(enabled_only=True) if item.name == name),
            None,
        )
        if chat is not None:
            self._poll_chat(client, chat)

    def _queue_sender_job(self, chat_id: int, message_id: str) -> None:
        key = (int(chat_id), str(message_id))
        if key in self._sender_job_keys or len(self._sender_jobs) >= 512:
            return
        self._sender_jobs.append(_SenderJob(*key))
        self._sender_job_keys.add(key)

    def _queue_image_job(self, chat_id: int, message_id: str) -> None:
        key = (int(chat_id), str(message_id))
        if key in self._image_job_keys or len(self._image_jobs) >= 128:
            return
        self._image_jobs.append(_ImageJob(*key))
        self._image_job_keys.add(key)

    def _next_image_job(self) -> _ImageJob | None:
        if not self._image_jobs:
            return None
        job = self._image_jobs.popleft()
        self._image_job_keys.discard((job.chat_id, job.message_id))
        return job

    def _execute_image_job(self, client: WeChatClient, job: _ImageJob) -> None:
        try:
            chat = self.store.get_chat(job.chat_id)
            self._set_status(active_chat=chat.name)
            messages = client.get_visible_messages(
                chat.name, chat_type=chat.chat_type, enrich_senders=False
            )
            target = next((item for item in messages if item.id == job.message_id), None)
            if target is None:
                return
            upgraded = client.extract_visible_image(target, chat=chat.name)
            stored, _created = self.store.save_message(upgraded)
            self.events.publish({"type": "message", "message": stored.as_dict()})
            self.store.set_chat_error(chat.id, None)
            self._set_status(last_error=None)
        except Exception as exc:
            try:
                self.store.set_chat_error(job.chat_id, str(exc))
            except Exception:
                pass
            self._set_status(last_error=f"image extraction: {exc}")
        finally:
            self._set_status(active_chat=None)

    def _next_sender_job(self) -> _SenderJob | None:
        if not self._sender_jobs:
            return None
        job = self._sender_jobs.popleft()
        self._sender_job_keys.discard((job.chat_id, job.message_id))
        return job

    def _execute_sender_job(self, client: WeChatClient, job: _SenderJob) -> None:
        try:
            chat = self.store.get_chat(job.chat_id)
            self._set_status(active_chat=chat.name)
            messages = client.get_visible_messages(
                chat.name,
                chat_type=chat.chat_type,
                enrich_senders=False,
            )
            target = next((item for item in messages if item.id == job.message_id), None)
            if target is None:
                return
            enriched = client.enrich_visible_senders([target], chat=chat.name)
            if not enriched or enriched[0].sender is None:
                return
            stored, _created = self.store.save_message(enriched[0])
            self.events.publish({"type": "message", "message": stored.as_dict()})
            self._sender_event_seqs.add(stored.seq)
            if len(self._sender_event_seqs) > 4096:
                self._sender_event_seqs = set(sorted(self._sender_event_seqs)[-2048:])
            self.store.set_chat_error(chat.id, None)
            self._set_status(last_error=None)
        except Exception as exc:
            try:
                self.store.set_chat_error(job.chat_id, str(exc))
            except Exception:
                pass
            self._set_status(last_error=f"sender enrichment: {exc}")
        finally:
            self._set_status(active_chat=None)

    def _execute_send(self, client: WeChatClient, command: _SendCommand) -> None:
        worker_started = time.monotonic()
        try:
            chat = self.store.get_chat(command.chat_id)
            self._set_status(active_chat=chat.name)
            receipt = client.send_text(
                chat.name,
                command.text,
                chat_type=chat.chat_type,
                mentions=command.mentions,
            )
            timings = {
                "queue_wait_s": max(0.0, worker_started - command.queued_at),
                **{key: round(float(value), 3) for key, value in receipt.timings.items()},
                "worker_total_s": round(time.monotonic() - worker_started, 3),
                "end_to_end_s": round(time.monotonic() - command.queued_at, 3),
            }
            stored = self.store.complete_pending_send(command.pending_seq, receipt)
            command.result = stored
            self.events.publish({"type": "message", "message": stored.as_dict()})
            self.store.set_chat_error(chat.id, None)
            self._set_status(last_error=None, last_send_timings=timings)
        except BaseException as exc:
            command.error = exc
            try:
                failed = self.store.fail_pending_send(command.pending_seq, str(exc))
                self.events.publish({"type": "message", "message": failed.as_dict()})
            except Exception:
                pass
            try:
                self.store.set_chat_error(command.chat_id, str(exc))
            except Exception:
                pass
            self._set_status(last_error=str(exc))
        finally:
            self._set_status(active_chat=None)
            command.completed.set()
            self._commands.task_done()
            self.events.publish({"type": "status", "status": self.status()})

    def _poll_chat(self, client: WeChatClient, chat: StoredChat) -> None:
        self._set_status(active_chat=chat.name)
        try:
            uia_sender = bool(getattr(client, "uia_sender", False))
            messages = client.get_visible_messages(
                chat.name,
                chat_type=chat.chat_type,
                # Persist and publish the whole burst first. Profile cards are
                # then opened only for newly created rows, so old viewport
                # history cannot turn every poll into a long blocking scan.
                enrich_senders=not uia_sender,
            )
            new_sender_candidates = []
            image_candidates = []
            backlog_candidate = None
            for message in messages:
                if self.store.is_historical_quote_source(message):
                    # Qt may expose an older source row after a quoted reply
                    # is opened or the viewport is virtualized. The quote is
                    # explicit evidence that this text predates the reply, so
                    # it must not be appended as a new arrival.
                    continue
                stored, created = self.store.save_message(message)
                sender_update = bool(
                    (message.sender is not None or message.sender_organization is not None)
                    and stored.seq not in self._sender_event_seqs
                )
                if created or sender_update:
                    self.events.publish({"type": "message", "message": stored.as_dict()})
                if message.sender is not None or message.sender_organization is not None:
                    self._sender_event_seqs.add(stored.seq)
                    if len(self._sender_event_seqs) > 4096:
                        self._sender_event_seqs = set(sorted(self._sender_event_seqs)[-2048:])
                if (
                    message.type is MessageType.IMAGE
                    and stored.image_source != "viewer_clipboard"
                    and isinstance(message.raw.get("image_bounds"), dict)
                    and (
                        message.bounds is None
                        or int(message.raw["image_bounds"].get("width", 0))
                        < message.bounds.width * 0.85
                    )
                ):
                    image_candidates.append(message)
                if (
                    uia_sender
                    and message.chat_type is ChatType.GROUP
                    and message.direction is Direction.INCOMING
                    and not self.store.sender_profile_checked(stored.seq)
                ):
                    if created:
                        new_sender_candidates.append(message)
                    elif backlog_candidate is None:
                        backlog_candidate = message

            # Queue every new row. The worker processes these jobs one at a
            # time only when no send is waiting. When there is no new burst,
            # use the otherwise-idle cycle to repair one historical row.
            sender_candidates = new_sender_candidates
            if not sender_candidates and backlog_candidate is not None:
                sender_candidates = [backlog_candidate]
            for message in sender_candidates:
                self._queue_sender_job(chat.id, message.id)
            for message in image_candidates:
                self._queue_image_job(chat.id, message.id)
            self.store.set_chat_error(chat.id, None)
            self._set_status(last_error=None)
        except Exception as exc:
            self.store.set_chat_error(chat.id, str(exc))
            self._set_status(last_error=f"{chat.name}: {exc}")
        finally:
            self._set_status(active_chat=None)
