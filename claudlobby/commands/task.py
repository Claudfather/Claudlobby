"""claudlobby task — the operator's acts on ONE task (chunk M-A, #1481).

``nudge`` is the first EVENT-DRIVEN reaction the estate has: everything that
reacts to a plane fact today is a timer (fleet-pulse, the briefings) or a
human reading a page. A nudge is two things in one door — the fact, and the
reaction:

  1. a NON-terminal ``nudged`` task event on the assignment, actor
     ``human:<who>``, the reason in its detail. It is a plane fact like any
     other, so the attention card shows it, the manager's next act clears it
     (the arm holds only while ``nudged`` is the assignment's newest task
     event), and an unanswered one past its grace re-enters the queue;
  2. ONE id-less re-check dispatched to the task's manager, carrying the four
     verbs. Id-less because a re-check is a COMMUNICATION, not a task: an
     id'd one would open a row of its own that nobody closes, which is the
     defect the whole chunk exists to remove.

The two halves are ordered and their failures are NOT symmetric. The record
is written FIRST and a failed send does not undo it: a nudge nobody delivered
is still a nudge the operator made, it is visible on the card, and the
re-check timer (M4) will carry it. A send failure is disclosed and exits 1 —
loud, but the fact stands. The reverse order would let a delivered nudge
leave no trace, which is the one shape the plane must never produce.

Resolution is ``task-act.sh``'s rule, for its reason: an id matching more than
one OPEN assignment is REFUSED with the candidates named, never resolved to
the newest. A wrong nudge sends a manager to chase the wrong worker.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..plane.db import open_ro
from ..plane.queries import NON_TERMINAL_CLAUSE
from ._helpers import _resolve_paths

# Every OPEN assignment carrying the legacy task id, newest first, with the
# facts the re-check names. `assigned_by` IS the task's manager — the plane's
# own fact, recorded by the door that dispatched it — so the nudge reaches
# whoever actually owns the row rather than whoever a manifest calls the
# fleet's manager.
OPEN_BY_TASK_SQL = (
    "SELECT a.work_item_id, a.assignment_id, a.occurred_at, w.title,"
    " i.alias AS assignee, m.alias AS assigned_by, f.alias AS fleet"
    " FROM assignments a"
    " LEFT JOIN work_items w ON w.work_item_id = a.work_item_id"
    " LEFT JOIN identity_registry i ON i.uid = a.assignee_uid"
    " LEFT JOIN identity_registry m ON m.uid = a.assigned_by_uid"
    " LEFT JOIN identity_registry f ON f.uid = a.fleet_uid"
    " WHERE a.source_ref = ? AND" + NON_TERMINAL_CLAUSE +
    " ORDER BY a.ingest_seq DESC"
)


def _short(alias: str | None) -> str:
    """`bot:<fleet>/<name>` / `human:<name>` -> the name."""
    if not alias:
        return ""
    return alias.rpartition("/")[2] if "/" in alias else alias.partition(":")[2]


def _age(occurred_at: str | None, now: datetime | None = None) -> str:
    """A coarse age, the page's own granularity. An unparseable instant reads
    `age unknown` — never `0s`, which would say the task was just dispatched."""
    if not occurred_at:
        return "age unknown"
    try:
        at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError:
        return "age unknown"
    now = now or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    s = (now - at).total_seconds()
    if s < 60:
        return f"{max(0, int(s))}s old"
    if s < 3600:
        return f"{int(s // 60)}m old"
    if s < 86400:
        return f"{int(s // 3600)}h old"
    return f"{int(s // 86400)}d old"


def nudge_request(row, *, task_id: str, who: str, why: str) -> dict:
    """The ONE event a nudge records. `by` rides the detail beside the actor
    so the attention card names the person without a registry join, and the
    reason rides it as CONTENT (capped, and stripped by a metadata-mode
    capture like every other authored text)."""
    return {
        "event_type": "task", "emitter": "task-nudge", "fleet": row["fleet"],
        "source_ref": f"dispatch-log:{task_id}",
        "payload": {
            "work_item_id": row["work_item_id"],
            "assignment_id": row["assignment_id"],
            "event": "nudged",
            "actor": f"human:{who}",
            "by": who,
            # omitted rather than "" when the operator gave no reason: an
            # empty string in the detail reads as a reason that was recorded
            # and was blank, which is a different fact from none given
            **({"reason": why} if why else {}),
            "summary": f"nudged by {who}" + (f": {why}" if why else ""),
        },
    }


def recheck_message(row, *, task_id: str, who: str, why: str,
                    now: datetime | None = None) -> str:
    """The id-less re-check the manager receives: what happened, and the four
    verbs it may answer with. The same menu M4's timer sends, scoped to one
    row — written here so the nudge and the timer cannot drift into offering
    a manager different options for the same situation."""
    title = (row["title"] or "").strip() or "untitled"
    return (
        f"NUDGE from {who}: task {task_id} ({title}, {_age(row['occurred_at'], now)},"
        f" assignee {_short(row['assignee']) or 'unknown'})"
        f" — {why or 'no reason given'}."
        " Act: chase (query the worker), supersede (dispatch-task.sh"
        f" --supersedes {task_id}), withdraw (task-act.sh withdraw {task_id}),"
        f' or escalate (task-act.sh escalate {task_id} "…");'
        " report what you did."
    )


def send_to_bot(paths, bot: str, message: str) -> tuple[int, str]:
    """Hand the message to a bot through `lib/dispatch.sh`, the ONE cross-
    socket send primitive (it resolves the bot's private tmux socket itself).
    The CLI runs on the HOST, not inside a bot session, so it cannot use
    `dispatch-task.sh` — that door records a dispatch under the *sending
    bot's* identity, and there is no sending bot here. A missing script is a
    failed send, said as one. The seam a test monkeypatches."""
    script = Path(paths.lib) / "dispatch.sh"
    if not script.is_file():
        return 127, f"no {script} — the install has no dispatch door"
    try:
        r = subprocess.run(["bash", str(script), bot, message],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return r.returncode, (r.stderr or "").strip()


def manager_of(row) -> str:
    """The task's manager: the plane's `assigned_by`, and nothing else.

    There is deliberately no `MANAGER_TMUX` fallback. `assigned_by` is
    REQUIRED on the Assignment contract, so no assignment can reach the plane
    without one — a fallback here would be unreachable code claiming to
    handle a case the wire model forbids, and it would answer with the
    FLEET's policy where the row's own history is the fact. The empty answer
    below survives only for an assignment whose manager alias the registry
    cannot name, which ingest also cannot produce; it discloses rather than
    guessing."""
    return _short(row["assigned_by"])


def cmd_task_nudge(args) -> int:
    paths = _resolve_paths(args)
    task_id = args.task_id
    who = args.as_who or os.environ.get("USER") or "operator"
    why = (args.why or "").strip()

    if os.environ.get("PLANE_EMIT_DISABLED") == "1":
        print("nudge: PLANE_EMIT_DISABLED=1 — the plane is silenced, so a nudge"
              " would leave no record; nothing was sent", file=sys.stderr)
        return 3

    conn, why_not = open_ro(paths.root)
    if conn is None:
        print(f"nudge: {why_not} — unreachable, not empty", file=sys.stderr)
        return 3
    try:
        rows = [dict(r) for r in
                conn.execute(OPEN_BY_TASK_SQL, (f"dispatch-log:{task_id}",))]
    finally:
        conn.close()

    if not rows:
        print(f"nudge: no OPEN assignment carries {task_id} (a closed one answers"
              " empty too — check `claudlobby brief`)", file=sys.stderr)
        return 2
    if len(rows) > 1:
        print(f"nudge: {task_id} matches {len(rows)} open assignments —"
              " refusing to pick one:", file=sys.stderr)
        for r in rows:
            print(f"  {r['assignment_id']}  assignee {r['assignee'] or '-'}"
                  f"  fleet {r['fleet'] or '-'}", file=sys.stderr)
        return 2
    row = rows[0]
    if not row["fleet"]:
        # ingest requires a fleet; a row with none could never be re-emitted
        # under one without fabricating it (the expiry sweep's rule).
        print(f"nudge: {task_id} has no fleet attribution on the plane —"
              " refusing to record an act under a fabricated fleet", file=sys.stderr)
        return 2

    from ..plane.emit_api import emit_batch
    try:
        out = emit_batch(paths.root, [nudge_request(row, task_id=task_id,
                                                    who=who, why=why)])
    except Exception as exc:  # noqa: BLE001 — every verdict is a failed nudge, by name
        print(f"nudge: the plane did NOT record this nudge"
              f" ({type(exc).__name__}: {exc}) — nothing was sent", file=sys.stderr)
        return 3
    status = out[0].status if out else ""
    if status not in ("committed", "duplicate", "spooled"):
        print(f"nudge: the plane did NOT record this nudge ({status or 'no outcome'})"
              " — nothing was sent", file=sys.stderr)
        return 3
    print(f"recorded: nudged {task_id} ({row['assignment_id']}) as {who}"
          + (f" — {why}" if why else ""))

    manager = manager_of(row)
    if not manager:
        print("nudge: recorded, but the plane cannot name this task's manager"
              " — nobody was asked to act", file=sys.stderr)
        return 1
    rc, err = send_to_bot(paths, manager,
                          recheck_message(row, task_id=task_id, who=who, why=why))
    if rc != 0:
        print(f"nudge: recorded, but the re-check did NOT reach {manager}"
              f" (rc={rc}{': ' + err if err else ''}) — the nudge stands on the"
              " plane and shows on the card", file=sys.stderr)
        return 1
    print(f"asked {manager} to act")
    return 0
