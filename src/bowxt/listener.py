from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable, Iterable

from .models import ChatType, Direction, Message


class MessageListener:
    """Conservative polling listener over one or more visible chats."""

    def __init__(
        self,
        client,
        chats: Iterable[str],
        on_message: Callable[[Message], str | None],
        *,
        chat_type: ChatType = ChatType.GROUP,
        poll_interval: float = 3.0,
        auto_reply: bool = False,
        on_error: Callable[[Exception], None] | None = None,
    ):
        self.client = client
        self.chats = list(dict.fromkeys(chats))
        if not self.chats:
            raise ValueError("at least one chat is required")
        if poll_interval < 1.5:
            raise ValueError("poll_interval below 1.5s is intentionally rejected")
        self.on_message = on_message
        self.chat_type = chat_type
        self.poll_interval = poll_interval
        self.auto_reply = auto_reply
        self.on_error = on_error
        self._seen: dict[str, set[str]] = defaultdict(set)
        self._outgoing: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=32))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, *, block: bool = False) -> "MessageListener":
        self._baseline()
        self._stop.clear()
        if block:
            self._run()
        else:
            self._thread = threading.Thread(target=self._run, name="bowxt-listener", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(5.0, self.poll_interval + 1))

    def _baseline(self) -> None:
        for chat in self.chats:
            messages = self.client.get_visible_messages(chat, chat_type=self.chat_type)
            self._seen[chat].update(item.id for item in messages)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            for chat in self.chats:
                if self._stop.is_set():
                    break
                try:
                    self._poll_chat(chat)
                except Exception as exc:
                    if self.on_error:
                        self.on_error(exc)
                    else:
                        # Keep the service alive; callers can opt into structured errors.
                        pass
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.05, self.poll_interval - elapsed))

    def _poll_chat(self, chat: str) -> None:
        messages = self.client.get_visible_messages(chat, chat_type=self.chat_type)
        for message in messages:
            if message.id in self._seen[chat]:
                continue
            self._seen[chat].add(message.id)
            if self._is_own_echo(chat, message):
                continue
            response = self.on_message(message)
            if self.auto_reply and response:
                receipt = self.client.send_text(chat, response, chat_type=self.chat_type)
                self._outgoing[chat].append(receipt.content)

    def _is_own_echo(self, chat: str, message: Message) -> bool:
        if message.direction is Direction.OUTGOING:
            return True
        for content in list(self._outgoing[chat]):
            if content == message.content or (len(content) >= 12 and content in message.content):
                self._outgoing[chat].remove(content)
                return True
        return False
