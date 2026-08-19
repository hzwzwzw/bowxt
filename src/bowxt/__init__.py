"""Public bowxt API."""

from .client import WeChatClient
from .errors import (
    AccessibilityUnavailable,
    ChatNotFound,
    ControlNotFound,
    MentionSelectionError,
    SafetyLimitExceeded,
    WeChatNotFound,
    BowxtError,
)
from .listener import MessageListener
from .input import X11Clipboard
from .models import ChatType, Direction, Message, MessageType, SendReceipt
from .safety import SafetyPolicy
from .service import BowxtService
from .store import SQLiteStore, StoredChat, StoredMessage

BowxtClient = WeChatClient

__all__ = [
    "AccessibilityUnavailable",
    "BowxtClient",
    "BowxtError",
    "BowxtService",
    "ChatNotFound",
    "ChatType",
    "ControlNotFound",
    "Direction",
    "MentionSelectionError",
    "Message",
    "MessageListener",
    "MessageType",
    "SafetyLimitExceeded",
    "SafetyPolicy",
    "SendReceipt",
    "SQLiteStore",
    "StoredChat",
    "StoredMessage",
    "WeChatClient",
    "WeChatNotFound",
    "X11Clipboard",
]

__version__ = "0.3.0"
