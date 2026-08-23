from __future__ import annotations

import json
from typing import Any


PANEL_MAX_BYTES = 262_144
PANEL_MAX_NODES = 1_000
PANEL_MAX_DEPTH = 8


def validate_panel_document(value: Any) -> dict[str, Any]:
    """Validate the declarative, code-free Agent panel protocol (version 1)."""

    if not isinstance(value, dict):
        raise ValueError("panel document must be an object")
    unknown = set(value) - {"version", "type", "nodes", "empty_text"}
    if unknown:
        raise ValueError("panel document accepts only version, type, nodes and empty_text")
    if value.get("version") != 1 or value.get("type") != "tree":
        raise ValueError("panel document must use version 1 and type 'tree'")
    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("panel document nodes must be an array")
    counter = [0]
    normalized = {
        "version": 1,
        "type": "tree",
        "nodes": [_validate_node(item, 1, counter) for item in nodes],
    }
    if "empty_text" in value:
        normalized["empty_text"] = _text(
            value["empty_text"], "panel empty_text", maximum=500, allow_empty=True
        )
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PANEL_MAX_BYTES:
        raise ValueError(f"panel document must be at most {PANEL_MAX_BYTES} bytes")
    return normalized


def _validate_node(value: Any, depth: int, counter: list[int]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("each panel node must be an object")
    if depth > PANEL_MAX_DEPTH:
        raise ValueError(f"panel tree depth must be at most {PANEL_MAX_DEPTH}")
    counter[0] += 1
    if counter[0] > PANEL_MAX_NODES:
        raise ValueError(f"panel tree must contain at most {PANEL_MAX_NODES} nodes")
    unknown = set(value) - {
        "id", "label", "value", "meta", "tone", "expanded", "children"
    }
    if unknown:
        raise ValueError(
            "panel nodes accept only id, label, value, meta, tone, expanded and children"
        )
    node: dict[str, Any] = {
        "label": _text(value.get("label"), "panel node label", maximum=200),
    }
    if "id" in value:
        node["id"] = _text(value["id"], "panel node id", maximum=160)
    if "value" in value:
        node["value"] = _text(
            value["value"], "panel node value", maximum=4_000, allow_empty=True
        )
    if "meta" in value:
        node["meta"] = _text(
            value["meta"], "panel node meta", maximum=500, allow_empty=True
        )
    if "tone" in value:
        tone = str(value["tone"])
        if tone not in {"neutral", "info", "success", "warning", "danger"}:
            raise ValueError("panel node tone is invalid")
        node["tone"] = tone
    if "expanded" in value:
        if not isinstance(value["expanded"], bool):
            raise ValueError("panel node expanded must be a boolean")
        node["expanded"] = value["expanded"]
    if "children" in value:
        children = value["children"]
        if not isinstance(children, list):
            raise ValueError("panel node children must be an array")
        node["children"] = [
            _validate_node(child, depth + 1, counter) for child in children
        ]
    return node


def _text(value: Any, field: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if "\x00" in value or len(value) > maximum or (not allow_empty and not value.strip()):
        qualifier = f"0-{maximum}" if allow_empty else f"1-{maximum}"
        raise ValueError(f"{field} must contain {qualifier} characters")
    return value
