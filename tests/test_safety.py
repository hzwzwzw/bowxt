import unittest

from bowxt.errors import SafetyLimitExceeded
from bowxt.safety import SafetyPolicy, SendRateLimiter


class SafetyTests(unittest.TestCase):
    def test_text_limit_is_enforced(self):
        limiter = SendRateLimiter(SafetyPolicy(max_text_length=3))
        with self.assertRaises(SafetyLimitExceeded):
            limiter.validate_text("four")

    def test_per_chat_minute_limit_is_enforced(self):
        now = [0.0]
        limiter = SendRateLimiter(
            SafetyPolicy(
                min_send_interval=0,
                send_jitter=0,
                max_messages_per_minute=5,
                max_messages_per_chat_per_minute=1,
            ),
            clock=lambda: now[0],
            sleeper=lambda seconds: None,
        )
        limiter.acquire("a")
        with self.assertRaises(SafetyLimitExceeded):
            limiter.acquire("a")
