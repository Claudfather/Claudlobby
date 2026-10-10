"""Exact version-2 wire metadata for the two fixed prepared task actions.

Only shape and scalar validation live here; each action owns its authority,
selection, semantic digest and evidence classification.
"""
from datetime import datetime
import re
from uuid import UUID

from .ids import ID_PATTERNS
from .owner_access import AccessDenied
from .owner_actions import _body, _exact, _text

_FIELDS = {"version", "request_id", "kind", "scope", "target", "submitted_at", "semantic_sha256"}
_TARGET = {"recipient", "task_id", "assignment_id", "release_id"}


def _id(value, kind):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERNS[kind], value):
        raise AccessDenied("invalid_action_body")


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AccessDenied("invalid_action_body")


def metadata(action, payload, *, kind):
    if kind not in ("nudge", "feedback") or action not in ("prepare", "send", "receipt"):
        raise AccessDenied("unsupported_owner_action")
    fields = _FIELDS - ({"semantic_sha256"} if action == "prepare" else set())
    _exact(payload, fields | ({"body"} if action in {"prepare", "send"} else set()))
    if type(payload["version"]) is not int or payload["version"] != 2 or payload["kind"] != kind:
        raise AccessDenied("unsupported_owner_action")
    _exact(payload["scope"], {"workspace", "host", "fleet", "viewer"})
    _exact(payload["target"], _TARGET)
    for value in payload["scope"].values():
        _text(value)
    _id(payload["scope"]["host"], "host")
    target = payload["target"]
    _id(target["recipient"], "actor")
    _id(target["task_id"], "work_item")
    if target["assignment_id"] is not None:
        _id(target["assignment_id"], "assignment")
    if not isinstance(target["release_id"], str) or not re.fullmatch(r"r-[0-9a-f]{64}", target["release_id"]):
        raise AccessDenied("invalid_action_body")
    try:
        if not isinstance(payload["request_id"], str) or str(UUID(payload["request_id"])) != payload["request_id"]:
            raise ValueError
        _text(payload["submitted_at"])
        stamp = datetime.fromisoformat(payload["submitted_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise AccessDenied("invalid_action_body") from exc
    if action != "prepare":
        _digest(payload["semantic_sha256"])
    if action in {"prepare", "send"}:
        _body(payload["body"])
    return fields
