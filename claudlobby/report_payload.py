"""Typed report content and two-fact encoding, with no recording or transport.

Versioned JSON lives only in the communication's CONTENT-classified body.
Full capture preserves repeated evidence exactly; metadata capture withholds
it. The companion task/marker keeps status and PR attribution, never a hidden
copy of authored prose. The caller owns identity/link checks and passes these
raw facts through emit_api.validate_item/emit_batch and their capture policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from .plane import PLANE_SCHEMA_VERSION
from .plane.ids import ID_PATTERNS
from .plane.registries import PR_ROLES, cap_for
from .task_state import TASK_EMITTER

ReportStatus = Literal["progress", "blocked", "completed", "failed"]
ReportTransition = Literal["progress", "blocked_waiting", "returned_blocked", "completed", "failed"]
_TRANSITIONS = {"progress": ("progress",), "blocked": ("blocked_waiting", "returned_blocked"),
                "completed": ("completed",), "failed": ("failed",)}
_FORMAT = "claudlobby.report"


class ReportPayloadError(ValueError):
    """Refused input or unreadable versioned content, never an empty report."""


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ReportPayloadError(f"{field} must be nonempty text")


def _cap(value, family, field):
    if len(value.encode("utf-8")) > cap_for(family, field):
        raise ReportPayloadError(f"{family}.{field} exceeds {cap_for(family, field)} bytes")


def _url(value):
    _text(value, "URL")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ReportPayloadError("invalid URL") from exc
    if not parsed.scheme or not (parsed.netloc or parsed.path) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ReportPayloadError("evidence must use absolute URLs without whitespace")


@dataclass(frozen=True)
class ReportPayload:
    status: ReportStatus
    summary: str | None = None
    reason: str | None = None
    percent: int | None = None
    pr_url: str | None = None
    pr_role: Literal["authored", "reviewed"] | None = None
    artifacts: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    skill: str | None = None

    def __post_init__(self):
        if self.status not in _TRANSITIONS:
            raise ReportPayloadError("unsupported report status")
        if (self.summary is None) == (self.reason is None):
            raise ReportPayloadError("supply exactly one summary or reason")
        for field in ("summary", "reason"):
            value = getattr(self, field)
            if value is not None:
                _text(value, field)
                _cap(value, "task", field)
        if self.reason is not None and self.status not in ("blocked", "failed"):
            raise ReportPayloadError("reason is only valid for blocked or failed reports")
        if self.percent is not None and (self.status != "progress" or type(self.percent) is not int
                                         or not 0 <= self.percent <= 100):
            raise ReportPayloadError("percent must be an integer 0..100 on progress only")
        if (self.pr_url is None) != (self.pr_role is None):
            raise ReportPayloadError("PR URL and role must occur together")
        if self.pr_role is not None and self.pr_role not in PR_ROLES:
            raise ReportPayloadError("unsupported PR role")
        if self.pr_url is not None:
            _url(self.pr_url)
        for field in ("artifacts", "issues"):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list)):
                raise ReportPayloadError(f"{field} must be a sequence of URLs")
            object.__setattr__(self, field, tuple(values))
            for value in values:
                _url(value)
        if self.skill is not None:
            _text(self.skill, "skill")
        self.to_body()  # Reject before an emit door could truncate structured JSON.

    def to_body(self) -> str:
        body = json.dumps({"format": _FORMAT, "version": 1, **asdict(self)},
                          ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        _cap(body, "communication", "body")
        return body


@dataclass(frozen=True)
class ReportLink:
    task_id: str
    assignment_id: str
    transition: ReportTransition


@dataclass(frozen=True)
class DecodedReport:
    state: Literal["captured", "withheld", "truncated", "legacy", "invalid", "unsupported"]
    payload: ReportPayload | None = None
    reason: str | None = None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReportPayloadError("duplicate JSON field")
        result[key] = value
    return result


def decode_report_body(body: str | None, *, privacy: str = "full", truncated: bool = False) -> DecodedReport:
    """Decode new content; callers explicitly dispatch legacy bodies elsewhere.

    No absent/partial/unknown payload becomes empty evidence. Keep the stored
    companion's status/PR metadata separately when content is unavailable.
    """
    if privacy not in ("full", "metadata", "preview"):
        return DecodedReport("invalid", reason="unknown capture mode")
    if privacy != "full":
        return DecodedReport("withheld", reason="report content is unavailable under capture policy")
    if body is None:
        return DecodedReport("invalid", reason="full-capture report body is missing")
    if truncated:
        return DecodedReport("truncated", reason="partial report content cannot prove evidence")
    if not isinstance(body, str):
        return DecodedReport("invalid", reason="report body is not text")
    text = body.lstrip()
    if text.startswith("[BOTREPORT]") or not text.startswith(("{", "[")):
        return DecodedReport("legacy", reason="historical body requires the historical decoder")
    try:
        _cap(body, "communication", "body")
        data = json.loads(body, object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or data.get("format") != _FORMAT:
            return DecodedReport("invalid", reason="unrecognized structured report format")
        if type(data.get("version")) is not int or data["version"] != 1:
            return DecodedReport("unsupported", reason="unsupported report content version")
        if set(data) != {"format", "version", *ReportPayload.__dataclass_fields__}:
            raise ReportPayloadError("report content fields differ from version 1")
        return DecodedReport("captured", ReportPayload(**{k: v for k, v in data.items()
                                                         if k not in ("format", "version")}))
    except (ValueError, TypeError, RecursionError) as exc:
        return DecodedReport("invalid", reason=str(exc))


def _id(value, kind):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERNS[kind], value):
        raise ReportPayloadError(f"canonical {kind} ID required")


def encode_report_facts(report: ReportPayload, *, fleet: str, sender: str, recipient: str,
                        msg_id: str, event_ids: tuple[str, str], occurred_at: str,
                        link: ReportLink | None = None) -> tuple[dict, dict]:
    """Return communication + exact linked transition OR unlinked status marker.

    All IDs/timestamps/parties are supplied and frozen by the operation owner.
    This encoder never chooses an assignment, closes idless work or mints IDs.
    """
    for value, field in ((fleet, "fleet"), (sender, "sender"), (recipient, "recipient")):
        _text(value, field)
    _id(msg_id, "msg")
    if len(event_ids) != 2 or event_ids[0] == event_ids[1]:
        raise ReportPayloadError("two distinct frozen event IDs required")
    for value in event_ids:
        _id(value, "event")
    try:
        instant = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("offset required")
    except (AttributeError, ValueError) as exc:
        raise ReportPayloadError("report timestamp requires an offset") from exc
    comm = {"msg_id": msg_id, "sender": sender, "recipient": recipient,
            "message_class": "report", "body": report.to_body()}
    provenance = {"pr_url": report.pr_url, "pr_role": report.pr_role} if report.pr_url else {}
    if link is not None:
        _id(link.task_id, "work_item")
        _id(link.assignment_id, "assignment")
        if link.transition not in _TRANSITIONS[report.status]:
            raise ReportPayloadError("report status and explicit task transition disagree")
        comm.update(work_item_id=link.task_id, assignment_id=link.assignment_id)
        detail = {"work_item_id": link.task_id, "assignment_id": link.assignment_id,
                  "event": link.transition, "actor": sender, "link_source": "named", **provenance}
        for field in ("summary", "reason"):
            if getattr(report, field) is not None:
                detail[field] = getattr(report, field)
        if report.percent is not None:
            detail["progress"] = report.percent
        family = "task"
    else:
        data = {"status": report.status, "msg_id": msg_id, **provenance}
        if report.percent is not None:
            data["progress"] = report.percent
        _cap(json.dumps(data, ensure_ascii=False), "system", "data")
        detail = {"event": "report_status", "subject_kind": "actor", "subject": sender, "data": data}
        family = "system"
    envelope = {"fleet": fleet, "emitter": TASK_EMITTER, "source_ref": f"report:{msg_id}",
                "occurred_at": occurred_at, "schema_version": PLANE_SCHEMA_VERSION}
    return ({**envelope, "event_id": event_ids[0], "event_type": "communication", "payload": comm},
            {**envelope, "event_id": event_ids[1], "event_type": family, "payload": detail})
