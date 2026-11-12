"""Fleet inbox projection over existing task, report, and attention readers.

The cursor only continues two read lanes. It is not a report ACK capability or
a durable queue position; alerts and recorded escalations are current context.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass
import json

from .task_queries import list_task_escalations, list_tasks


class InboxCursorError(ValueError):
    """A page cannot continue under these selected identities or read position."""


@dataclass(frozen=True)
class InboxPage:
    work: tuple
    issues: tuple
    reports: tuple[dict, ...]
    recorded_escalations: tuple[dict, ...]
    prior_seq: int | None
    prior_ack_seq: int | None
    next_cursor: str | None


def _decode(token: str | None, scope: dict, prior_seq: int | None,
            prior_ack_seq: int | None) -> tuple[bool, str | None, bool, tuple[int, str] | None]:
    if token is None:
        return False, None, False, None
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {
                "v", "scope", "prior_seq", "prior_ack_seq", "work_done", "work_cursor",
                "reports_done", "report_after"}
                or type(value["v"]) is not int or value["v"] != 1
                or value["scope"] != scope or value["prior_seq"] != prior_seq
                or value["prior_ack_seq"] != prior_ack_seq
                or type(value["work_done"]) is not bool
                or type(value["reports_done"]) is not bool):
            raise ValueError
        work_done, work_cursor = value["work_done"], value["work_cursor"]
        reports_done, report_after = value["reports_done"], value["report_after"]
        if ((work_done and work_cursor is not None)
                or (not work_done and (not isinstance(work_cursor, str) or not work_cursor))
                or (reports_done and report_after is not None)
                or (not reports_done and (not isinstance(report_after, list)
                    or len(report_after) != 2 or type(report_after[0]) is not int
                    or report_after[0] < 1 or not isinstance(report_after[1], str)
                    or not report_after[1]))):
            raise ValueError
        return work_done, work_cursor, reports_done, (tuple(report_after) if report_after else None)
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise InboxCursorError("invalid inbox cursor or changed selection, viewer or read position") from exc


def _encode(scope: dict, prior_seq: int | None, prior_ack_seq: int | None, *,
            work_done: bool, work_cursor: str | None, reports_done: bool,
            report_after: tuple[int, str] | None) -> str:
    value = {"v": 1, "scope": scope, "prior_seq": prior_seq,
             "prior_ack_seq": prior_ack_seq, "work_done": work_done,
             "work_cursor": work_cursor, "reports_done": reports_done,
             "report_after": report_after}
    token = base64.urlsafe_b64encode(json.dumps(value, sort_keys=True,
                                                separators=(",", ":")).encode()).decode().rstrip("=")
    if len(token) > 4096:
        raise InboxCursorError("inbox cursor exceeds its size bound")
    return token


def project_inbox(conn, readers, *, fleet: str, fleet_uid: str,
                  viewer_uids: list[str], scope: dict, limit: int,
                  cursor: str | None) -> InboxPage:
    """Page open fleet work and one viewer's unacked reports under one snapshot.

    Limit applies independently to each lane. A finished lane stays finished
    for this continuation; a new inbox read begins a fresh projection.
    """
    ack = readers.newest_ack(conn, viewer_uids)
    prior_seq = ack["seq"] if ack else None
    prior_ack_seq = ack["landed_seq"] if ack else None
    work_done, work_cursor, reports_done, report_after = _decode(
        cursor, scope, prior_seq, prior_ack_seq)

    # Even after its lane is exhausted, ask the canonical reducer for issues:
    # a report-only continuation must not silently certify clean work state.
    work_page = list_tasks(conn, fleet_uid=fleet_uid, state="open", limit=limit,
                           cursor=None if work_done else work_cursor)
    work = () if work_done else work_page.items
    if not work_done:
        work_cursor = work_page.next_cursor
        work_done = work_cursor is None

    reports: tuple[dict, ...] = ()
    if not reports_done:
        rows = readers.report_rows(conn, fleet, since_seq=prior_seq)
        rows = readers.unacked_rows(rows, prior_seq)
        rows.sort(key=lambda row: (row["_seq"], row["plane_msg_id"]))
        if report_after is not None:
            rows = [row for row in rows if (row["_seq"], row["plane_msg_id"]) > report_after]
        reports = tuple(rows[:limit])
        reports_done = len(rows) <= limit
        report_after = None if reports_done else (reports[-1]["_seq"], reports[-1]["plane_msg_id"])

    recorded = tuple(asdict(row) for row in list_task_escalations(
        conn, fleet_uid=fleet_uid).items)
    next_cursor = None
    if not work_done or not reports_done:
        next_cursor = _encode(scope, prior_seq, prior_ack_seq, work_done=work_done,
                              work_cursor=work_cursor, reports_done=reports_done,
                              report_after=report_after)
    return InboxPage(work, work_page.issues, reports, recorded,
                     prior_seq, prior_ack_seq, next_cursor)
