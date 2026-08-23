from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class ChatType(str, Enum):
    CONTACT = "contact"
    GROUP = "group"
    UNKNOWN = "unknown"


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    VOICE = "voice"
    VIDEO = "video"
    STICKER = "sticker"
    LINK = "link"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class Direction(str, Enum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    SYSTEM = "system"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass(frozen=True, slots=True)
class MessageImage:
    """Pixels read from a visible WeChat image bubble without OCR."""

    data: bytes = field(repr=False, compare=False, hash=False)
    mime_type: str = "image/png"
    width: int | None = None
    height: int | None = None
    source: str = "window_pixels"


@dataclass(frozen=True, slots=True)
class Message:
    """A best-effort structured view of one visible WeChat message."""

    id: str
    chat: str
    content: str
    type: MessageType
    direction: Direction
    sender: str | None = None
    sender_organization: str | None = None
    timestamp: datetime | None = None
    chat_type: ChatType = ChatType.UNKNOWN
    is_at_me: bool = False
    bounds: Rect | None = None
    image: MessageImage | None = field(default=None, compare=False, hash=False)
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True, slots=True)
class SendReceipt:
    chat: str
    content: str
    sent_at: datetime
    verified: bool
    matched_message_id: str | None = None
    mentions: tuple[str, ...] = ()
    timings: Mapping[str, float] = field(default_factory=dict, compare=False)
