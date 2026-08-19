class BowxtError(RuntimeError):
    """Base exception."""


class WeChatNotFound(BowxtError):
    """The official WeChat desktop process/window was not found."""


class AccessibilityUnavailable(BowxtError):
    """WeChat did not publish an AT-SPI accessibility tree."""


class ControlNotFound(BowxtError):
    """A required visible control could not be located."""


class ChatNotFound(BowxtError):
    """A chat could not be found through the visible search UI."""


class MentionSelectionError(BowxtError):
    """A visible group member could not be selected as a rich mention."""


class SafetyLimitExceeded(BowxtError):
    """A configured anti-abuse safety limit rejected an operation."""
