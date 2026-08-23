from __future__ import annotations

import base64
import binascii
import io
import json
import mimetypes
import os
import queue
import re
import warnings
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

from .agent_plugins import AgentManager
from .errors import ServicePaused
from .models import ChatType, MessageImage
from .service import BowxtService


def _required_aware_datetime(query: dict[str, list[str]], name: str) -> datetime:
    values = query.get(name)
    if not values or not values[0].strip():
        raise ValueError(f"{name} is required")
    return _aware_datetime(values[0], name)


def _optional_aware_datetime(query: dict[str, list[str]], name: str) -> datetime | None:
    values = query.get(name)
    if not values or not values[0].strip():
        return None
    return _aware_datetime(values[0], name)


def _aware_datetime(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _simulation_image(value: object) -> MessageImage | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("image must be an object")
    unknown = set(value) - {"data", "mime_type", "name"}
    if unknown:
        raise ValueError("image accepts only data, mime_type and name")
    encoded = str(value.get("data") or "").strip()
    if encoded.startswith("data:"):
        if "," not in encoded:
            raise ValueError("invalid image data URL")
        encoded = encoded.split(",", 1)[1]
    try:
        source = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image data must be valid base64") from exc
    if not source or len(source) > 10 * 1024 * 1024:
        raise ValueError("image must be between 1 byte and 10 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as opened:
                opened.load()
                normalized = ImageOps.exif_transpose(opened)
                width, height = normalized.size
                if width <= 0 or height <= 0 or width > 8192 or height > 8192:
                    raise ValueError("image dimensions must be between 1 and 8192 pixels")
                if width * height > 40_000_000:
                    raise ValueError("image contains too many pixels")
                converted = normalized.convert(
                    "RGBA" if "A" in normalized.getbands() else "RGB"
                )
                output = io.BytesIO()
                converted.save(output, format="PNG", optimize=True)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("image is too large to decode safely") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("unsupported or corrupt image") from exc
    png = output.getvalue()
    if len(png) > 25 * 1024 * 1024:
        raise ValueError("normalized PNG exceeds 25 MiB")
    return MessageImage(
        png,
        mime_type="image/png",
        width=width,
        height=height,
        source="simulation_upload",
    )


class BowxtHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: BowxtService,
        *,
        agent_manager: AgentManager | None = None,
    ):
        super().__init__(address, BowxtRequestHandler)
        self.service = service
        self.static_root = Path(__file__).with_name("webui")
        self.agent_manager = agent_manager or AgentManager(
            service.store,
            service.events,
            base_url=f"http://127.0.0.1:{self.server_port}",
        )

    def server_close(self) -> None:
        self.agent_manager.close()
        super().server_close()


class BowxtRequestHandler(BaseHTTPRequestHandler):
    server: BowxtHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("BOWXT_HTTP_LOG") == "1":
            super().log_message(format, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._json(HTTPStatus.OK, self.server.service.status())
            return
        if parsed.path == "/api/chats":
            chats = self.server.service.store.list_chats()
            consumer = self._agent_consumer()
            if consumer:
                allowed = self.server.agent_manager.allowed_chats(consumer, "read")
                if allowed is not None:
                    allowed_ids = {chat.id for chat in allowed}
                    chats = [chat for chat in chats if chat.id in allowed_ids]
            self._json(
                HTTPStatus.OK,
                {"chats": [chat.as_dict() for chat in chats]},
            )
            return
        if parsed.path == "/api/messages":
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["200"])[0])
            if query.get("recent", ["0"])[0] == "1":
                messages = self.server.service.store.latest_messages(limit=limit)
            else:
                messages = self.server.service.store.messages_after(after, limit=limit)
            consumer = self._agent_consumer()
            if consumer:
                allowed = self.server.agent_manager.allowed_chats(consumer, "read")
                if allowed is not None:
                    allowed_ids = {chat.id for chat in allowed}
                    messages = [item for item in messages if item.chat_id in allowed_ids]
            self._json(HTTPStatus.OK, {"messages": [item.as_dict() for item in messages]})
            return
        message_match = re.fullmatch(r"/api/messages/(\d+)", parsed.path)
        if message_match:
            try:
                message = self.server.service.store.get_message(int(message_match.group(1)))
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            if not self._check_chat_permission(message.chat_id, "read"):
                return
            self._json(HTTPStatus.OK, {"message": message.as_dict()})
            return
        if parsed.path == "/api/agent/logs":
            query = parse_qs(parsed.query)
            agent = query.get("agent", [None])[0]
            after = int(query.get("after", ["0"])[0])
            before_value = query.get("before", [None])[0]
            before = int(before_value) if before_value is not None else None
            limit = int(query.get("limit", ["200"])[0])
            recent = query.get("recent", ["0"])[0] == "1"
            logs = self.server.service.store.get_agent_logs(
                agent=agent,
                after_seq=after,
                before_seq=before,
                limit=limit,
                recent=recent,
            )
            self._json(HTTPStatus.OK, {"logs": [item.as_dict() for item in logs]})
            return
        if parsed.path == "/api/agent/plugins":
            self._json(HTTPStatus.OK, {"plugins": self.server.agent_manager.plugins()})
            return
        if parsed.path == "/api/agent/instances":
            self._json(
                HTTPStatus.OK,
                {"instances": self.server.agent_manager.instances()},
            )
            return
        panel_match = re.fullmatch(
            r"/api/agent/instances/([^/]+)/panels/([^/]+)", parsed.path
        )
        if panel_match:
            try:
                panel = self.server.service.store.get_agent_panel(
                    unquote(panel_match.group(1)), unquote(panel_match.group(2))
                )
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"panel": panel.as_dict()})
            return
        panels_match = re.fullmatch(
            r"/api/agent/instances/([^/]+)/panels", parsed.path
        )
        if panels_match:
            try:
                instance_id = unquote(panels_match.group(1))
                self.server.service.store.get_agent_instance(instance_id)
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            panels = self.server.service.store.list_agent_panels(instance_id)
            self._json(
                HTTPStatus.OK,
                {"panels": [item.as_dict(include_document=False) for item in panels]},
            )
            return
        instance_match = re.fullmatch(r"/api/agent/instances/([^/]+)", parsed.path)
        if instance_match:
            try:
                instance = self.server.agent_manager.describe(
                    unquote(instance_match.group(1))
                )
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"instance": instance})
            return
        image_match = re.fullmatch(r"/api/messages/(\d+)/image", parsed.path)
        if image_match:
            try:
                message = self.server.service.store.get_message(int(image_match.group(1)))
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            if not self._check_chat_permission(message.chat_id, "read"):
                return
            self._image(int(image_match.group(1)))
            return
        history_match = re.fullmatch(r"/api/chats/(\d+)/history", parsed.path)
        if history_match:
            query = parse_qs(parsed.query)
            try:
                chat_id = int(history_match.group(1))
                self.server.service.store.get_chat(chat_id)
                if not self._check_chat_permission(chat_id, "read"):
                    return
                since = _required_aware_datetime(query, "since")
                until = _optional_aware_datetime(query, "until") or datetime.now(timezone.utc)
                if since > until:
                    raise ValueError("since must not be after until")
                if until - since > timedelta(days=31):
                    raise ValueError("history range must not exceed 31 days")
                after = int(query.get("after", ["0"])[0])
                limit = min(max(int(query.get("limit", ["1000"])[0]), 1), 1000)
                messages = self.server.service.store.message_history(
                    chat_id,
                    since=since.isoformat(),
                    until=until.isoformat(),
                    after_seq=after,
                    limit=limit,
                )
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            except (OverflowError, ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            next_after = messages[-1].seq if len(messages) == limit else None
            self._json(
                HTTPStatus.OK,
                {
                    "messages": [item.as_dict() for item in messages],
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "next_after": next_after,
                },
            )
            return
        match = re.fullmatch(r"/api/chats/(\d+)/messages", parsed.path)
        if match:
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["200"])[0])
            try:
                self.server.service.store.get_chat(int(match.group(1)))
                if not self._check_chat_permission(int(match.group(1)), "read"):
                    return
                before_value = query.get("before", [None])[0]
                if before_value is not None:
                    messages = self.server.service.store.messages_before(
                        int(match.group(1)), int(before_value), limit=limit
                    )
                elif query.get("recent", ["0"])[0] == "1":
                    messages = self.server.service.store.recent_messages(
                        int(match.group(1)), limit=limit
                    )
                else:
                    messages = self.server.service.store.get_messages(
                        int(match.group(1)), after_seq=after, limit=limit
                    )
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"messages": [item.as_dict() for item in messages]})
            return
        if parsed.path == "/api/events":
            self._events()
            return
        self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/simulated-chats":
                body = self._read_json()
                if self._agent_consumer():
                    raise PermissionError("Agents cannot create simulated chats")
                unknown = set(body) - {"name", "chat_type"}
                if unknown:
                    raise ValueError("simulated chat accepts only name and chat_type")
                chat = self.server.service.add_simulated_chat(
                    str(body.get("name", "")),
                    ChatType(body.get("chat_type", "contact")),
                )
                self._json(HTTPStatus.CREATED, {"chat": chat.as_dict()})
                return
            simulate_match = re.fullmatch(
                r"/api/chats/(\d+)/simulate", parsed.path
            )
            if simulate_match:
                body = self._read_json(max_size=15 * 1024 * 1024)
                if self._agent_consumer():
                    raise PermissionError("Agents cannot fabricate incoming messages")
                unknown = set(body) - {
                    "text",
                    "sender",
                    "sender_organization",
                    "timestamp",
                    "is_at_me",
                    "image",
                }
                if unknown:
                    raise ValueError(
                        "simulation accepts only text, sender, sender_organization, "
                        "timestamp, is_at_me and image"
                    )
                if "is_at_me" in body and not isinstance(body["is_at_me"], bool):
                    raise ValueError("is_at_me must be a boolean")
                timestamp_value = body.get("timestamp")
                timestamp = (
                    _aware_datetime(str(timestamp_value), "timestamp")
                    if timestamp_value is not None and timestamp_value != ""
                    else None
                )
                message = self.server.service.inject_simulated_message(
                    int(simulate_match.group(1)),
                    text=str(body.get("text", "")),
                    sender=body.get("sender"),
                    sender_organization=body.get("sender_organization"),
                    timestamp=timestamp,
                    is_at_me=bool(body.get("is_at_me", False)),
                    image=_simulation_image(body.get("image")),
                )
                self._json(HTTPStatus.CREATED, {"message": message.as_dict()})
                return
            body = self._read_json()
            if parsed.path == "/api/chats":
                chat = self.server.service.add_chat(
                    str(body.get("name", "")),
                    ChatType(body.get("chat_type", "unknown")),
                )
                self._json(HTTPStatus.CREATED, {"chat": chat.as_dict()})
                return
            match = re.fullmatch(r"/api/chats/(\d+)/messages", parsed.path)
            if match:
                self._require_chat_permission(int(match.group(1)), "write")
                message = self.server.service.enqueue_text(
                    int(match.group(1)),
                    str(body.get("text", "")),
                    mentions=tuple(body.get("mentions") or ()),
                    client_id=body.get("client_id"),
                )
                self._json(HTTPStatus.ACCEPTED, {"message": message.as_dict()})
                return
            claim_match = re.fullmatch(r"/api/agents/([^/]+)/claim", parsed.path)
            if claim_match:
                chat_ids = body.get("chat_ids", [])
                if not isinstance(chat_ids, list):
                    raise ValueError("chat_ids must be an array")
                for flag in ("require_sender", "require_at_me", "replay_existing"):
                    if flag in body and not isinstance(body[flag], bool):
                        raise ValueError(f"{flag} must be a boolean")
                consumer = unquote(claim_match.group(1))
                header_consumer = self._agent_consumer()
                if header_consumer and header_consumer != consumer:
                    raise PermissionError("Agent consumer header does not match claim path")
                requested_chat_ids = tuple(dict.fromkeys(int(item) for item in chat_ids))
                allowed_chat_ids = self.server.agent_manager.filter_read_chat_ids(
                    consumer, requested_chat_ids
                )
                managed = allowed_chat_ids is not None
                deliveries = self.server.service.claim_agent_messages(
                    consumer,
                    chat_ids=allowed_chat_ids if managed else requested_chat_ids,
                    limit=int(body.get("limit", 8)),
                    lease_seconds=float(body.get("lease_seconds", 60.0)),
                    timeout=float(body.get("timeout", 0.0)),
                    require_sender=bool(body.get("require_sender", False)),
                    require_at_me=bool(body.get("require_at_me", False)),
                    replay_existing=bool(body.get("replay_existing", False)),
                    deny_all_chats=managed and not allowed_chat_ids,
                )
                if managed:
                    self.server.agent_manager.publish_activity(consumer)
                self._json(
                    HTTPStatus.OK,
                    {"deliveries": [item.as_dict() for item in deliveries]},
                )
                return
            delivery_match = re.fullmatch(
                r"/api/agents/([^/]+)/deliveries/(\d+)/(ack|nack)", parsed.path
            )
            if delivery_match:
                consumer = unquote(delivery_match.group(1))
                message_seq = int(delivery_match.group(2))
                lease_token = str(body.get("lease_token", ""))
                if delivery_match.group(3) == "ack":
                    self.server.service.ack_agent_message(
                        consumer, message_seq, lease_token
                    )
                else:
                    self.server.service.nack_agent_message(
                        consumer,
                        message_seq,
                        lease_token,
                        error=str(body.get("error", "")),
                        retry_delay=float(body.get("retry_delay", 5.0)),
                    )
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if parsed.path == "/api/agent/logs":
                log = self.server.service.log_agent(
                    str(body.get("agent", "")),
                    str(body.get("level", "info")),
                    str(body.get("message", "")),
                    event=str(body.get("event", "log")),
                    context=body.get("context") or {},
                )
                self._json(HTTPStatus.CREATED, {"log": log.as_dict()})
                return
            if parsed.path == "/api/agent/instances":
                config = body.get("config")
                secrets = body.get("secrets")
                permissions = body.get("permissions")
                if config is not None and not isinstance(config, dict):
                    raise ValueError("config must be an object")
                if secrets is not None and not isinstance(secrets, dict):
                    raise ValueError("secrets must be an object")
                if permissions is not None and not isinstance(permissions, dict):
                    raise ValueError("permissions must be an object")
                instance = self.server.agent_manager.create(
                    str(body.get("plugin_id", "")),
                    str(body.get("id", "")),
                    str(body.get("name", "")),
                    config=config,
                    secrets=secrets,
                    permissions=permissions,
                    autostart=bool(body.get("autostart", False)),
                )
                self._json(HTTPStatus.CREATED, {"instance": instance})
                return
            action_match = re.fullmatch(
                r"/api/agent/instances/([^/]+)/(start|stop|restart)", parsed.path
            )
            if action_match:
                instance_id = unquote(action_match.group(1))
                action = action_match.group(2)
                status = getattr(self.server.agent_manager, action)(instance_id)
                self._json(HTTPStatus.OK, {"status": status})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except TimeoutError as exc:
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": str(exc)})
        except ServicePaused as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/agent/panels/([^/]+)", parsed.path)
        if not match:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        try:
            consumer = self._agent_consumer()
            if not consumer:
                raise PermissionError("X-Bowxt-Agent header is required")
            body = self._read_json(max_size=262_144)
            unknown = set(body) - {"title", "document"}
            if unknown or "title" not in body or "document" not in body:
                raise ValueError("panel accepts only required title and document fields")
            panel = self.server.agent_manager.publish_panel(
                consumer,
                unquote(match.group(1)),
                str(body["title"]),
                body["document"],
            )
            self._json(HTTPStatus.OK, {"panel": panel})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        instance_match = re.fullmatch(r"/api/agent/instances/([^/]+)", parsed.path)
        if instance_match:
            try:
                body = self._read_json()
                unknown = set(body) - {
                    "name", "config", "secrets", "permissions", "autostart", "restart"
                }
                if unknown or not body:
                    raise ValueError(
                        "agent configuration accepts only name, config, secrets, permissions, "
                        "autostart and restart"
                    )
                if "restart" in body and not isinstance(body["restart"], bool):
                    raise ValueError("restart must be a boolean")
                instance = self.server.agent_manager.update(
                    unquote(instance_match.group(1)),
                    name=body.get("name") if "name" in body else None,
                    config=body.get("config") if "config" in body else None,
                    secrets=body.get("secrets") if "secrets" in body else None,
                    permissions=body.get("permissions") if "permissions" in body else None,
                    autostart=body.get("autostart") if "autostart" in body else None,
                    restart=bool(body.get("restart", False)),
                )
                self._json(HTTPStatus.OK, {"instance": instance})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if parsed.path == "/api/control":
            try:
                body = self._read_json()
                unknown = set(body) - {"mode", "paused", "poll_gap", "action_delay"}
                if unknown or not body:
                    raise ValueError(
                        "control accepts only mode, paused, poll_gap and action_delay"
                    )
                status = self.server.service.configure(
                    paused=body.get("paused") if "paused" in body else None,
                    mode=body.get("mode") if "mode" in body else None,
                    poll_gap=body.get("poll_gap") if "poll_gap" in body else None,
                    action_delay=body.get("action_delay") if "action_delay" in body else None,
                )
                self._json(HTTPStatus.OK, status)
            except (ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        match = re.fullmatch(r"/api/chats/(\d+)", parsed.path)
        if not match:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        try:
            body = self._read_json()
            chat = self.server.service.update_chat_type(
                int(match.group(1)), ChatType(body.get("chat_type", "unknown"))
            )
            self._json(HTTPStatus.OK, {"chat": chat.as_dict()})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        panel_match = re.fullmatch(r"/api/agent/panels/([^/]+)", parsed.path)
        if panel_match:
            try:
                consumer = self._agent_consumer()
                if not consumer:
                    raise PermissionError("X-Bowxt-Agent header is required")
                self.server.agent_manager.delete_panel(
                    consumer, unquote(panel_match.group(1))
                )
                self._json(HTTPStatus.OK, {"ok": True})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except PermissionError as exc:
                self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        match = re.fullmatch(r"/api/agent/instances/([^/]+)", parsed.path)
        if not match:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        try:
            self.server.agent_manager.delete(unquote(match.group(1)))
            self._json(HTTPStatus.OK, {"ok": True})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _read_json(self, *, max_size: int = 65536) -> dict[str, Any]:
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > max_size:
            raise ValueError(f"request body must be between 1 and {max_size} bytes")
        value = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _agent_consumer(self) -> str | None:
        value = self.headers.get("X-Bowxt-Agent", "").strip()
        return value or None

    def _check_chat_permission(self, chat_id: int, capability: str) -> bool:
        try:
            self._require_chat_permission(chat_id, capability)
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            return False
        return True

    def _require_chat_permission(self, chat_id: int, capability: str) -> None:
        consumer = self._agent_consumer()
        if not consumer:
            return
        permitted = self.server.agent_manager.permits_chat(consumer, capability, chat_id)
        if permitted is False:
            label = "读取" if capability == "read" else "发送到"
            raise PermissionError(f"Agent {consumer} is not allowed to {label} chat {chat_id}")

    def _events(self) -> None:
        subscriber = self.server.service.events.subscribe()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not self.server.service._stop.is_set():
                try:
                    event = subscriber.get(timeout=15.0)
                    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.service.events.unsubscribe(subscriber)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        if relative not in {"index.html", "app.js", "styles.css"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        path = self.server.static_root / relative
        try:
            data = path.read_bytes()
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _image(self, seq: int) -> None:
        try:
            path, mime_type, digest = self.server.service.store.image_asset(seq)
            data = path.read_bytes()
        except (KeyError, FileNotFoundError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "image not found"})
            return
        except (OSError, ValueError) as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("ETag", f'"{digest}"')
        self.send_header("Content-Disposition", f'inline; filename="{digest}.png"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: HTTPStatus, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def serve(service: BowxtService, host: str = "127.0.0.1", port: int = 8787) -> None:
    service.start()
    server = BowxtHTTPServer((host, int(port)), service)
    server.agent_manager.start_autostart()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.stop()
