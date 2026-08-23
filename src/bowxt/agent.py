"""Stable process-outside-the-UI API for building bowxt Agents."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import ChatType
from .store import AgentDelivery, AgentLog, StoredChat, StoredMessage


class AgentAPIError(RuntimeError):
    """The local bowxt HTTP service rejected or could not serve a request."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class AgentClient:
    """Durable Agent client for a running bowxt service.

    Each ``consumer`` owns an independent delivery state. Incoming messages
    are leased and redelivered after a crash until ``ack`` succeeds. All sends
    remain asynchronous and idempotent through ``client_id``.
    """

    def __init__(
        self,
        consumer: str,
        *,
        base_url: str = "http://127.0.0.1:8787",
        request_timeout: float = 10.0,
    ):
        self.consumer = str(consumer)
        self.base_url = base_url.rstrip("/")
        self.request_timeout = float(request_timeout)

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/status")

    def list_chats(self) -> list[StoredChat]:
        value = self._request("GET", "/api/chats")
        return [_stored_chat(item) for item in value.get("chats", [])]

    def get_history(
        self,
        chat: StoredChat | int | str,
        *,
        duration_seconds: float,
        until: datetime | None = None,
    ) -> list[StoredMessage]:
        """Read every persisted message in one chat during a bounded time range."""

        duration = float(duration_seconds)
        if duration <= 0 or duration > 31 * 86400:
            raise ValueError("duration_seconds must be between 0 and 31 days")
        target = self._history_chat(chat)
        end = until or datetime.now(timezone.utc)
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("until must include a timezone")
        end = end.astimezone(timezone.utc)
        start = end - timedelta(seconds=duration)
        messages: list[StoredMessage] = []
        after = 0
        while True:
            query = urllib.parse.urlencode(
                {
                    "since": start.isoformat(),
                    "until": end.isoformat(),
                    "after": after,
                    "limit": 1000,
                }
            )
            value = self._request("GET", f"/api/chats/{target.id}/history?{query}")
            page = [_stored_message(item) for item in value.get("messages", [])]
            messages.extend(page)
            next_after = value.get("next_after")
            if next_after is None:
                return messages
            next_value = int(next_after)
            if next_value <= after:
                raise AgentAPIError("bowxt history pagination did not advance")
            after = next_value

    def ensure_chat(
        self,
        name: str,
        chat_type: ChatType | str = ChatType.UNKNOWN,
    ) -> StoredChat:
        value = self._request(
            "POST",
            "/api/chats",
            {"name": name, "chat_type": ChatType(chat_type).value},
        )
        return _stored_chat(value["chat"])

    def claim(
        self,
        *,
        chat_ids: Iterable[int] = (),
        limit: int = 8,
        lease_seconds: float = 60.0,
        timeout: float = 20.0,
        require_sender: bool = False,
        require_at_me: bool = False,
        replay_existing: bool = False,
    ) -> list[AgentDelivery]:
        consumer = urllib.parse.quote(self.consumer, safe="")
        value = self._request(
            "POST",
            f"/api/agents/{consumer}/claim",
            {
                "chat_ids": list(chat_ids),
                "limit": int(limit),
                "lease_seconds": float(lease_seconds),
                "timeout": float(timeout),
                "require_sender": bool(require_sender),
                "require_at_me": bool(require_at_me),
                "replay_existing": bool(replay_existing),
            },
            timeout=max(self.request_timeout, float(timeout) + 5.0),
        )
        return [
            AgentDelivery(
                consumer=str(item["consumer"]),
                message=_stored_message(item["message"]),
                lease_token=str(item["lease_token"]),
                lease_until=str(item["lease_until"]),
                attempt=int(item["attempt"]),
            )
            for item in value.get("deliveries", [])
        ]

    def ack(self, delivery: AgentDelivery) -> None:
        self._finish_delivery(delivery, "ack", {})

    def nack(
        self,
        delivery: AgentDelivery,
        error: str | BaseException = "",
        *,
        retry_delay: float = 5.0,
    ) -> None:
        self._finish_delivery(
            delivery,
            "nack",
            {"error": str(error), "retry_delay": float(retry_delay)},
        )

    def send_text(
        self,
        chat: StoredChat | int | str,
        text: str,
        *,
        mentions: Iterable[str] = (),
        client_id: str | None = None,
        chat_type: ChatType | str = ChatType.UNKNOWN,
    ) -> StoredMessage:
        target_id = int(chat) if isinstance(chat, int) else self._resolve_chat(
            chat, chat_type=chat_type
        ).id
        value = self._request(
            "POST",
            f"/api/chats/{target_id}/messages",
            {
                "text": text,
                "mentions": list(mentions),
                "client_id": client_id or f"agent-{uuid.uuid4().hex}",
            },
        )
        return _stored_message(value["message"])

    def get_message(self, seq: int) -> StoredMessage:
        value = self._request("GET", f"/api/messages/{int(seq)}")
        return _stored_message(value["message"])

    def wait_delivery(self, message: StoredMessage, *, timeout: float = 30.0) -> StoredMessage:
        """Wait for a queued send to leave ``pending`` without touching WeChat UI."""

        deadline = time.monotonic() + max(float(timeout), 0.0)
        current = message
        while current.delivery_status == "pending":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"message {message.seq} is still pending")
            time.sleep(min(0.25, remaining))
            current = self.get_message(message.seq)
        return current

    def download_image(self, message: StoredMessage) -> bytes:
        """Download a persisted image captured through the visible viewer."""

        if not message.image_url:
            raise ValueError("message has no captured image")
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base_url + "/", message.image_url.lstrip("/")),
            headers={"Accept": "image/png", "X-Bowxt-Agent": self.consumer},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                if response.headers.get_content_type() != "image/png":
                    raise AgentAPIError("bowxt returned an unexpected image type")
                data = response.read(25 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentAPIError(f"cannot download image: {exc}") from exc
        if len(data) > 25 * 1024 * 1024:
            raise AgentAPIError("captured image exceeds 25 MiB")
        return data

    def reply_text(
        self,
        delivery: AgentDelivery,
        text: str,
        *,
        mentions: Iterable[str] = (),
        key: str = "reply",
    ) -> StoredMessage:
        """Queue one idempotent reply tied to the source message."""

        client_id = self._delivery_client_id(delivery, key or "reply")
        return self.send_text(
            delivery.message.chat_id,
            text,
            mentions=mentions,
            client_id=client_id,
        )

    def forward_text(
        self,
        delivery: AgentDelivery,
        target: StoredChat | int | str,
        text: str,
        *,
        mentions: Iterable[str] = (),
        key: str = "forward",
        chat_type: ChatType | str = ChatType.UNKNOWN,
    ) -> StoredMessage:
        """Queue an idempotent text result to a different configured chat."""

        client_id = self._delivery_client_id(delivery, key or "forward")
        return self.send_text(
            target,
            text,
            mentions=mentions,
            client_id=client_id,
            chat_type=chat_type,
        )

    def log(
        self,
        level: str,
        message: str,
        *,
        event: str = "log",
        context: dict[str, Any] | None = None,
    ) -> AgentLog:
        value = self._request(
            "POST",
            "/api/agent/logs",
            {
                "agent": self.consumer,
                "level": level,
                "event": event,
                "message": message,
                "context": context or {},
            },
        )
        return _agent_log(value["log"])

    def publish_panel(
        self,
        panel_id: str,
        title: str,
        nodes: list[dict[str, Any]],
        *,
        empty_text: str = "暂无数据",
    ) -> dict[str, Any]:
        """Publish a declarative tree panel in this Agent's WebIM card."""

        value = self._request(
            "PUT",
            f"/api/agent/panels/{urllib.parse.quote(str(panel_id), safe='')}",
            {
                "title": title,
                "document": {
                    "version": 1,
                    "type": "tree",
                    "nodes": nodes,
                    "empty_text": empty_text,
                },
            },
        )
        return dict(value["panel"])

    def delete_panel(self, panel_id: str) -> None:
        self._request(
            "DELETE",
            f"/api/agent/panels/{urllib.parse.quote(str(panel_id), safe='')}",
        )

    def run_forever(
        self,
        handler: Callable[[AgentDelivery], Any],
        *,
        chat_ids: Iterable[int] = (),
        stop_event: threading.Event | None = None,
        lease_seconds: float = 60.0,
        require_sender: bool = False,
        require_at_me: bool = False,
        replay_existing: bool = False,
    ) -> None:
        """Run a sequential at-least-once handler with automatic ack/nack."""

        stopping = stop_event or threading.Event()
        selected = tuple(chat_ids)
        while not stopping.is_set():
            try:
                deliveries = self.claim(
                    chat_ids=selected,
                    lease_seconds=lease_seconds,
                    timeout=20.0,
                    require_sender=require_sender,
                    require_at_me=require_at_me,
                    replay_existing=replay_existing,
                )
                for delivery in deliveries:
                    if stopping.is_set():
                        self.nack(delivery, "consumer is stopping")
                        break
                    try:
                        handler(delivery)
                    except Exception as exc:
                        self.nack(delivery, exc)
                        self.log(
                            "error",
                            str(exc),
                            event="handler_failed",
                            context={"message_seq": delivery.message.seq, "attempt": delivery.attempt},
                        )
                    else:
                        self.ack(delivery)
            except AgentAPIError:
                if stopping.wait(1.0):
                    return

    def _finish_delivery(
        self,
        delivery: AgentDelivery,
        action: str,
        body: dict[str, Any],
    ) -> None:
        if delivery.consumer != self.consumer:
            raise ValueError("delivery belongs to a different consumer")
        consumer = urllib.parse.quote(self.consumer, safe="")
        payload = {"lease_token": delivery.lease_token, **body}
        self._request(
            "POST",
            f"/api/agents/{consumer}/deliveries/{delivery.message.seq}/{action}",
            payload,
        )

    def _delivery_client_id(self, delivery: AgentDelivery, key: str) -> str:
        normalized_key = "".join(
            character for character in str(key) if character.isalnum() or character in "_-"
        )[:20] or "action"
        consumer_hash = hashlib.sha256(self.consumer.encode("utf-8")).hexdigest()[:12]
        return f"agent-{consumer_hash}-{delivery.message.seq}-{normalized_key}"[:80]

    def _resolve_chat(
        self,
        chat: StoredChat | int | str,
        *,
        chat_type: ChatType | str,
    ) -> StoredChat:
        if isinstance(chat, StoredChat):
            return chat
        if isinstance(chat, int):
            match = next((item for item in self.list_chats() if item.id == chat), None)
            if match is None:
                raise KeyError(f"unknown chat id {chat}")
            return match
        match = next((item for item in self.list_chats() if item.name == str(chat)), None)
        return match or self.ensure_chat(str(chat), chat_type)

    def _history_chat(self, chat: StoredChat | int | str) -> StoredChat:
        if isinstance(chat, StoredChat):
            return chat
        values = self.list_chats()
        if isinstance(chat, int):
            match = next((item for item in values if item.id == chat), None)
        else:
            match = next((item for item in values if item.name == str(chat)), None)
        if match is None:
            raise KeyError(f"unknown chat {chat}")
        return match

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "X-Bowxt-Agent": self.consumer}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.request_timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                detail = None
            finally:
                exc.close()
            raise AgentAPIError(detail or f"bowxt returned HTTP {exc.code}", status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentAPIError(f"cannot reach bowxt: {exc}") from exc
        if not isinstance(value, dict):
            raise AgentAPIError("bowxt returned a non-object response")
        return value


def _stored_chat(value: dict[str, Any]) -> StoredChat:
    return StoredChat(
        id=int(value["id"]),
        name=str(value["name"]),
        chat_type=ChatType(value["chat_type"]),
        source=str(value["source"]),
        enabled=bool(value["enabled"]),
        created_at=str(value["created_at"]),
        updated_at=str(value["updated_at"]),
        last_message_at=value.get("last_message_at"),
        last_error=value.get("last_error"),
    )


def _stored_message(value: dict[str, Any]) -> StoredMessage:
    return StoredMessage(
        seq=int(value["seq"]),
        message_id=str(value["message_id"]),
        chat_id=int(value["chat_id"]),
        chat=str(value["chat"]),
        chat_type=ChatType(value["chat_type"]),
        sender=value.get("sender"),
        sender_organization=value.get("sender_organization"),
        content=str(value["content"]),
        message_type=str(value["message_type"]),
        direction=str(value["direction"]),
        timestamp=value.get("timestamp"),
        observed_at=str(value["observed_at"]),
        is_at_me=bool(value["is_at_me"]),
        verified=value.get("verified"),
        delivery_status=str(value["delivery_status"]),
        delivery_error=value.get("delivery_error"),
        client_id=value.get("client_id"),
        image_url=value.get("image_url"),
        image_mime_type=value.get("image_mime_type"),
        image_width=value.get("image_width"),
        image_height=value.get("image_height"),
        image_sha256=value.get("image_sha256"),
        image_source=value.get("image_source"),
    )


def _agent_log(value: dict[str, Any]) -> AgentLog:
    return AgentLog(
        seq=int(value["seq"]),
        agent=str(value["agent"]),
        level=str(value["level"]),
        event=str(value["event"]),
        message=str(value["message"]),
        context=dict(value.get("context") or {}),
        created_at=str(value["created_at"]),
    )
