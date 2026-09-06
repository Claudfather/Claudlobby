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

THE ASK IS RECORDED TOO (the fold's F3). It used to reach the manager's pane
through ``lib/dispatch.sh`` and land nowhere: the plane held a nudge with no
trace of anyone being asked to act on it, and a manager that never answered
was indistinguishable from one that was never asked. So the communication is
emitted BEFORE the send (intent before transport, ``report-back.sh``'s rule)
and the transmission after it, honestly — ``pane_submitted`` when the send
returned 0, ``failed`` when it did not. A manager-down nudge is now a
recorded failure rather than a silent one.

The two halves are ordered and their failures are NOT symmetric. The record
is written FIRST and a failed send does not undo it: a nudge nobody delivered
is still a nudge the operator made, it is visible on the card, and the
re-check timer (M4) will carry it. A send failure is disclosed and exits 1 —
loud, but the fact stands. The reverse order would let a delivered nudge
leave no trace, which is the one shape the plane must never produce.

Resolution is ``task-act.sh``'s rule, for its reason: an id matching more than
one OPEN assignment is REFUSED with the candidates named, never resolved to
the newest. A wrong nudge sends a manager to chase the wrong worker. The
refusal names ``--assignment <asg_id>``, a door that exists — it NARROWS the
task id's own open set rather than querying by assignment, so the row acted
on provably carries the id the caller named.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..plane.db import open_ro
from ..plane.ids import mint_msg_id
from ..plane.inventory import short_alias as _short
from ..plane.queries import OPEN_BY_TASK_REF_SQL
from ._helpers import _resolve_paths

# Every OPEN assignment carrying the legacy task id, newest first, with the
# facts the re-check names. `assigned_by` IS the task's manager — the plane's
# own fact, recorded by the door that dispatched it — so the nudge reaches
# whoever actually owns the row rather than whoever a manifest calls the
# fleet's manager. ONE definition of the question, shared with the bash door
# through `lib/plane-readers.TASK_OPEN_SQL` (byte-identical, pinned).
OPEN_BY_TASK_SQL = OPEN_BY_TASK_REF_SQL

# An actor alias is minted from this, so it is not free text: `human:<who>`
# becomes a first-class identity the registry keeps forever, and an arbitrary
# string (a newline, a quote, someone's whole sentence) mints an identity
# nobody can name again. Letters, digits, dot, dash, underscore — the same
# characters `plane-telegram-in.sh` clamps a channel user down to.
_WHO_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def _one_line(text: str) -> str:
    """Operator text collapsed to ONE line before it goes anywhere near a
    pane (the fold's F9). `lib/dispatch.sh` sends through tmux `send-keys`,
    where a newline in the payload is a RETURN: a `why` containing one
    submitted everything before it and left `/exit` sitting at the manager's
    prompt (reproduced). `plane-lookup --escalated` already strips for the
    same reason. Collapsing also normalises tabs, which the escalated read
    uses as its field separator."""
    return " ".join((text or "").split())


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


def recheck_requests(row, *, msg_id: str, who: str, manager: str,
                     message: str) -> list[dict]:
    """The ASK, as plane facts (the fold's F3): one communication from the
    person to the task's manager, and nothing else — the transmission is
    emitted after the send, because only the send knows what happened.

    `message_class` is `task_request` and `command_type` `query`: it asks the
    manager to go and look, and it deliberately carries NO task id of its own
    (an id'd re-check would open a row nobody closes — the defect the chunk
    exists to remove). It DOES carry the `work_item_id`, so the ask threads
    under the task it is about instead of floating loose in the channel.
    `human:<who>` as the sender is `plane-telegram-in.sh`'s precedent: a human
    is a first-class actor, not a bot speaking on their behalf."""
    return [{
        "event_type": "communication", "emitter": "task-nudge",
        "fleet": row["fleet"], "source_ref": f"task-nudge:{row['assignment_id']}",
        "payload": {
            "msg_id": msg_id,
            "sender": f"human:{who}",
            "recipient": manager,
            "message_class": "task_request",
            "command_type": "query",
            "work_item_id": row["work_item_id"],
            "body": message,
        },
    }]


def transmission_request(row, *, msg_id: str, destination: str, ok: bool,
                         detail: str = "") -> dict:
    """What the carrier did with the ask. `pane_submitted` is the strongest
    fact tmux can yield (§6b) and is only claimed when the send returned 0;
    anything else is `failed` and says so. Fabricating the accepted state on
    a send that failed is the one thing this must never do — the whole point
    of recording the ask is that a manager who was never reached looks
    different from one who ignored it."""
    return {
        "event_type": "transmission", "emitter": "task-nudge",
        "fleet": row["fleet"],
        "payload": {
            "msg_id": msg_id, "attempt_no": 1, "carrier": "tmux",
            "destination": destination,
            "state": "pane_submitted" if ok else "failed",
            **({"error": _one_line(detail)[:512]} if (not ok and detail) else {}),
        },
    }


def recheck_message(row, *, task_id: str, who: str, why: str,
                    now: datetime | None = None) -> str:
    """The id-less re-check the manager receives: what happened, and the four
    verbs it may answer with. The same menu M4's timer sends, scoped to one
    row — written here so the nudge and the timer cannot drift into offering
    a manager different options for the same situation."""
    title = _one_line(row["title"] or "") or "untitled"
    return (
        f"NUDGE from {who}: task {task_id} ({title}, {_age(row['occurred_at'], now)},"
        f" assignee {_short(row['assignee']) or 'unknown'})"
        f" — {why or 'no reason given'}."
        " Act: chase (query the worker), supersede (dispatch-task.sh"
        f" --supersedes {task_id}), withdraw (task-act.sh withdraw {task_id}),"
        f' or escalate (task-act.sh escalate {task_id} "…");'
        " report what you did."
    )


def send_to_bot(paths, bot: str, message: str, fleet: str | None = None) -> tuple[int, str]:
    """Hand the message to a bot through `lib/dispatch.sh`, the ONE cross-
    socket send primitive (it resolves the bot's private tmux socket itself).
    The CLI runs on the HOST, not inside a bot session, so it cannot use
    `dispatch-task.sh` — that door records a dispatch under the *sending
    bot's* identity, and there is no sending bot here. A missing script is a
    failed send, said as one. The seam a test monkeypatches.

    The ROW's fleet rides the environment (the fold's F4). Without it the
    resolver falls through to `_resolve_cross_fleet_bot_dir`, which picks the
    first LIVE manager of that short name across every fleet on the host —
    #526's class, and a nudge delivered to the wrong fleet's manager is worse
    than one not delivered at all. `BOT_DIR` is dropped for the same reason:
    inherited from a bot session it anchors the search on THAT bot's fleet.

    The carrier is `CLAUDLOBBY_FLEET`, the TIMER-unit anchor, deliberately not
    `FLEET_NAME`, the SESSION one: `FLEET_NAME` additionally switches
    `tmux_socket_for_bot` / `bot_tmux` into per-bot-socket strict mode, which
    is right for a bot talking from inside its own fleet and wrong for a
    host-side tool that serves every fleet on the box. Both anchor
    `resolve_bots_dir` identically, which is the part the fix needs."""
    script = Path(paths.lib) / "dispatch.sh"
    if not script.is_file():
        return 127, f"no {script} — the install has no dispatch door"
    env = {k: v for k, v in os.environ.items() if k != "BOT_DIR"}
    env["CLAUDLOBBY_ROOT"] = str(paths.root)
    if fleet:
        env["CLAUDLOBBY_FLEET"] = fleet
    try:
        r = subprocess.run(["bash", str(script), bot, message],
                           capture_output=True, text=True, timeout=60, env=env)
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
    return _short(row["assigned_by"]) or ""


def _emit(root, requests) -> tuple[bool, str]:
    """emit_batch, with every verdict collapsed to (recorded?, why not).
    Imported lazily so `claudlobby task --help` does not pay pydantic."""
    from ..plane.emit_api import emit_batch
    try:
        out = emit_batch(root, requests)
    except Exception as exc:  # noqa: BLE001 — every verdict is a failure, by name
        return False, f"{type(exc).__name__}: {exc}"
    bad = [o.status for o in out if o.status not in ("committed", "duplicate", "spooled")]
    return (not bad), (bad[0] if bad else "")


def cmd_task_nudge(args) -> int:
    paths = _resolve_paths(args)
    task_id = args.task_id
    # collapse BEFORE the fallback: `--as "  "` is a value the operator did
    # not really give, and refusing it would be pedantry where $USER is right
    who = (_one_line(args.as_who or "") or _one_line(os.environ.get("USER") or "")
           or "operator")
    why = _one_line(args.why or "")

    if not _WHO_RE.match(who):
        print(f"nudge: --as {who!r} is not a usable name — a nudge mints"
              " `human:<who>` as a plane identity, so it must be letters,"
              " digits, '.', '-' or '_' (max 64); nothing was recorded",
              file=sys.stderr)
        return 2
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

    named = task_id
    asg = getattr(args, "assignment", None)
    if asg:
        # NARROWS the task id's own open set — never a lookup by assignment,
        # or the act would stamp this task's `source_ref` on another task's row
        named = f"{task_id} / {asg}"
        rows = [r for r in rows if r["assignment_id"] == asg]

    if not rows:
        print(f"nudge: no OPEN assignment carries {named} (a closed one answers"
              " empty too — check `claudlobby brief`)", file=sys.stderr)
        return 2
    if len(rows) > 1:
        print(f"nudge: {named} matches {len(rows)} open assignments —"
              " refusing to pick one:", file=sys.stderr)
        for r in rows:
            print(f"  {r['assignment_id']}  assignee {r['assignee'] or '-'}"
                  f"  fleet {r['fleet'] or '-'}", file=sys.stderr)
        print("nudge: re-run with --assignment <asg_id> naming the row you mean",
              file=sys.stderr)
        return 2
    row = rows[0]
    if not row["fleet"]:
        # ingest requires a fleet; a row with none could never be re-emitted
        # under one without fabricating it (the expiry sweep's rule).
        print(f"nudge: {task_id} has no fleet attribution on the plane —"
              " refusing to record an act under a fabricated fleet", file=sys.stderr)
        return 2

    ok, verdict = _emit(paths.root, [nudge_request(row, task_id=task_id,
                                                   who=who, why=why)])
    if not ok:
        print(f"nudge: the plane did NOT record this nudge"
              f" ({verdict or 'no outcome'}) — nothing was sent", file=sys.stderr)
        return 3
    print(f"recorded: nudged {task_id} ({row['assignment_id']}) as {who}"
          + (f" — {why}" if why else ""))

    manager = manager_of(row)
    if not manager:
        print("nudge: recorded, but the plane cannot name this task's manager"
              " — nobody was asked to act", file=sys.stderr)
        return 1

    message = recheck_message(row, task_id=task_id, who=who, why=why)
    msg_id = mint_msg_id()
    # INTENT BEFORE TRANSPORT: the ask is recorded before it is attempted, so
    # a send that dies mid-flight still leaves the question on the plane.
    asked, verdict = _emit(paths.root, recheck_requests(
        row, msg_id=msg_id, who=who,
        manager=f"bot:{row['fleet']}/{manager}", message=message))
    if not asked:
        print(f"nudge: the re-check was NOT recorded ({verdict or 'no outcome'})"
              " — sending it anyway; the nudge itself stands", file=sys.stderr)

    rc, err = send_to_bot(paths, manager, message, fleet=row["fleet"])
    if asked:
        _emit(paths.root, [transmission_request(
            row, msg_id=msg_id, destination=manager, ok=(rc == 0), detail=err)])
    if rc != 0:
        print(f"nudge: recorded, but the re-check did NOT reach {manager}"
              f" (rc={rc}{': ' + err if err else ''}) — the nudge stands on the"
              " plane and shows on the card", file=sys.stderr)
        return 1
    print(f"asked {manager} to act")
    return 0
