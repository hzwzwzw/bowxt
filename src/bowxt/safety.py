from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from .errors import SafetyLimitExceeded


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    """Conservative defaults intended for human-scale UI automation."""

    min_send_interval: float = 1.8
    send_jitter: float = 0.35
    max_messages_per_minute: int = 18
    max_messages_per_chat_per_minute: int = 8
    max_text_length: int = 2000
    action_delay: float = 0.12
    paste_settle_delay: float = 0.25
    allow_newlines: bool = True


class SendRateLimiter:
    def __init__(self, policy: SafetyPolicy, *, clock=time.monotonic, sleeper=time.sleep):
        self.policy = policy
        self._clock = clock
        self._sleep = sleeper
        self._global: deque[float] = deque()
        self._by_chat: dict[str, deque[float]] = defaultdict(deque)
        self._last_send = float("-inf")
        self._lock = threading.Lock()

    def validate_text(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise ValueError("message text must be a non-empty string")
        if len(text) > self.policy.max_text_length:
            raise SafetyLimitExceeded(
                f"text length {len(text)} exceeds max_text_length={self.policy.max_text_length}"
            )
        if not self.policy.allow_newlines and "\n" in text:
            raise SafetyLimitExceeded("multi-line messages are disabled by the safety policy")

    def acquire(self, chat: str) -> None:
        with self._lock:
            now = self._clock()
            self._prune(self._global, now)
            per_chat = self._by_chat[chat]
            self._prune(per_chat, now)
            if len(self._global) >= self.policy.max_messages_per_minute:
                raise SafetyLimitExceeded("global per-minute send limit reached")
            if len(per_chat) >= self.policy.max_messages_per_chat_per_minute:
                raise SafetyLimitExceeded(f"per-minute send limit reached for chat {chat!r}")

            wait = self.policy.min_send_interval - (now - self._last_send)
            if self.policy.send_jitter:
                wait += random.uniform(0, self.policy.send_jitter)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()

            self._last_send = now
            self._global.append(now)
            per_chat.append(now)

    @staticmethod
    def _prune(entries: deque[float], now: float) -> None:
        while entries and now - entries[0] >= 60:
            entries.popleft()
