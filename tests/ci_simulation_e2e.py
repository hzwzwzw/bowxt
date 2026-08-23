"""Black-box smoke test for a built bowxt desktop image.

This intentionally lives outside unittest discovery. CI runs it against the
HTTP port of a fresh container, without a logged-in WeChat account.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.error
import urllib.request


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def request(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    value = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(value, timeout=10) as response:
        return response.status, json.loads(response.read())


def expect_http(base: str, method: str, path: str, body: dict, status: int) -> None:
    try:
        request(base, method, path, body)
    except urllib.error.HTTPError as exc:
        try:
            assert exc.code == status, (exc.code, exc.read())
        finally:
            exc.close()
    else:
        raise AssertionError(f"{method} {path} unexpectedly succeeded")


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
    _, status = request(base, "GET", "/api/status")
    assert status["wechat_connected"] is False

    _, created = request(
        base,
        "POST",
        "/api/simulated-chats",
        {"name": "CI 模拟群", "chat_type": "group"},
    )
    chat = created["chat"]
    assert chat["source"] == "simulation"

    consumer = "bowxt-container-ci"
    claim_path = f"/api/agents/{consumer}/claim"
    claim_body = {
        "chat_ids": [chat["id"]],
        "limit": 8,
        "lease_seconds": 30,
        "timeout": 0,
        "require_sender": True,
        "replay_existing": False,
    }
    assert request(base, "POST", claim_path, claim_body)[1]["deliveries"] == []

    result: dict = {}

    def claim_waiter() -> None:
        waiting = dict(claim_body, timeout=5)
        result.update(request(base, "POST", claim_path, waiting)[1])

    waiter = threading.Thread(target=claim_waiter)
    waiter.start()
    _, injected = request(
        base,
        "POST",
        f"/api/chats/{chat['id']}/simulate",
        {
            "text": "@kirotta CI 链路检查",
            "sender": "黄泽文",
            "sender_organization": "柯基服务队",
            "is_at_me": True,
        },
    )
    waiter.join(8)
    deliveries = result["deliveries"]
    assert len(deliveries) == 1
    message = deliveries[0]["message"]
    assert message["seq"] == injected["message"]["seq"]
    assert message["sender"] == "黄泽文"
    assert message["sender_organization"] == "柯基服务队"
    assert message["is_at_me"] is True
    request(
        base,
        "POST",
        f"/api/agents/{consumer}/deliveries/{message['seq']}/ack",
        {"lease_token": deliveries[0]["lease_token"]},
    )

    _, image_result = request(
        base,
        "POST",
        f"/api/chats/{chat['id']}/simulate",
        {
            "sender": "黄泽文",
            "sender_organization": "柯基服务队",
            "image": {
                "data": base64.b64encode(PNG_1X1).decode(),
                "mime_type": "image/png",
                "name": "ci.png",
            },
        },
    )
    image = image_result["message"]
    assert image["message_type"] == "image"
    assert image["image_mime_type"] == "image/png"
    with urllib.request.urlopen(base + image["image_url"], timeout=10) as response:
        assert response.read(8) == b"\x89PNG\r\n\x1a\n"

    _, sent_result = request(
        base,
        "POST",
        f"/api/chats/{chat['id']}/messages",
        {"text": "CI_REPLY_OK", "client_id": "bowxt-container-ci-reply"},
    )
    sent = sent_result["message"]
    assert sent["delivery_status"] == "sent"
    assert sent["verified"] is True

    request(base, "PATCH", "/api/control", {"mode": "paused"})
    expect_http(
        base,
        "POST",
        f"/api/chats/{chat['id']}/simulate",
        {"text": "paused", "sender": "CI"},
        409,
    )
    expect_http(
        base,
        "POST",
        f"/api/chats/{chat['id']}/messages",
        {"text": "paused"},
        409,
    )
    _, resumed = request(base, "PATCH", "/api/control", {"mode": "unread"})
    assert resumed["mode"] == "unread" and resumed["paused"] is False
    print("bowxt black-box simulation chain passed")


if __name__ == "__main__":
    main()
