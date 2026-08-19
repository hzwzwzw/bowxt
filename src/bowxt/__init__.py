"""Public bowxt API."""

from .client import WeChatClient
from .agent import AgentAPIError, AgentClient
from .errors import (
    AccessibilityUnavailable,
    ChatNotFound,
    ControlNotFound,
    MentionSelectionError,
    SafetyLimitExceeded,
    ServicePaused,
    WeChatNotFound,
    BowxtError,
)
from .listener import MessageListener
from .input import X11Clipboard
from .models import ChatType, Direction, Message, MessageImage, MessageType, SendReceipt
from .safety import SafetyPolicy
from .service import BowxtService, SyncMode
from .store import AgentDelivery, AgentLog, SQLiteStore, StoredChat, StoredMessage

BowxtClient = WeChatClient

__all__ = [
    "AccessibilityUnavailable",
    "AgentAPIError",
    "AgentClient",
    "AgentDelivery",
    "AgentLog",
    "BowxtClient",
    "BowxtError",
    "BowxtService",
    "ChatNotFound",
    "ChatType",
    "ControlNotFound",
    "Direction",
    "MentionSelectionError",
    "Message",
    "MessageImage",
    "MessageListener",
    "MessageType",
    "SafetyLimitExceeded",
    "ServicePaused",
    "SafetyPolicy",
    "SendReceipt",
    "SQLiteStore",
    "StoredChat",
    "StoredMessage",
    "SyncMode",
    "WeChatClient",
    "WeChatNotFound",
    "X11Clipboard",
]

__version__ = "0.4.0"
