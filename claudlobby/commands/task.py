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
    `age unknown` — never `0s`, which would say the task was just dispatched.
    The formatter itself is `_span` (chunk M-B), shared with the deadline and
    the last-progress phrases so one message cannot describe two of its own
    clocks in two grammars."""
    at = _instant(occurred_at)
    if at is None:
        return "age unknown"
    now = now or datetime.now(timezone.utc)
    return f"{_span((now - at).total_seconds())} old"


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
            # chunk P fold F3: the delivery JOIN admits a `received` proof only
            # when its destination is this recipient — the receiver records the
            # short bot name, so the short name must be on the communication too
            # (recheck_ask_request already carries it; the nudge did not).
            "recipient_raw": _short(manager) or manager,
            "message_class": "task_request",
            "command_type": "query",
            "work_item_id": row["work_item_id"],
            "body": message,
        },
    }]


def transmission_request(row, *, msg_id: str, destination: str, ok: bool,
                         detail: str = "",
                         wire: tuple[str, int] | None = None) -> dict:
    """What the carrier did with the ask. `pane_submitted` is the strongest
    fact tmux can yield (§6b) and is only claimed when the send returned 0;
    anything else is `failed` and says so. Fabricating the accepted state on
    a send that failed is the one thing this must never do — the whole point
    of recording the ask is that a manager who was never reached looks
    different from one who ignored it.

    On a submission (ok), it rides the SENDER's wire proof (chunk P fold F1/F5)
    so the delivery JOIN can read DELIVERED for the ask; `wire` is None when the
    send did not yield a proof (a sha-less host), and the JOIN then stays at
    unconfirmed rather than fabricating a verdict."""
    payload = {
        "msg_id": msg_id, "attempt_no": 1, "carrier": "tmux",
        "destination": destination,
        "state": "pane_submitted" if ok else "failed",
        **({"error": _one_line(detail)[:512]} if (not ok and detail) else {}),
    }
    if ok and wire:
        payload["wire_sha256"], payload["wire_bytes"] = wire[0], wire[1]
    return {
        "event_type": "transmission", "emitter": "task-nudge",
        "fleet": row["fleet"],
        "payload": payload,
    }


# THE MENU, ONCE (chunk M-B). The nudge speaks it, the re-check timer speaks
# it, and `claudlobby brief` prints it — three surfaces that must never offer a
# manager different options for the same situation, which is why M-A wrote the
# nudge's copy here and said so. Each verb carries its EXACT command, because
# "supersede it" is advice and `dispatch-task.sh --supersedes <id> …` is a
# thing a manager can run without going to look anything up.
TASK_VERBS = ("chase", "supersede", "withdraw", "escalate")


def verb_commands(task_id: str, assignee: str = "<bot>") -> str:
    """The four verbs and their commands for ONE row, on one line.

    `assignee` names the worker the chase would go to — known from the row, so
    the command is copy-pasteable rather than a template. An id-less row has no
    task id to name: the caller passes the assignment id, which `--assignment`
    takes on both acts.

    Prefixed `$CLAUDLOBBY_ROOT/lib/` (the fold's F7): `start-bot.sh`'s
    exported PATH does not include the fleet's `lib/`, so a bare
    `task-act.sh`/`dispatch-task.sh` a manager pastes verbatim from this line
    resolves to nothing — every OTHER example in the dispatch protocol already
    prefixes it, and a command a manager cannot run is not really the menu."""
    bot = assignee or "<bot>"
    return (
        f'chase ($CLAUDLOBBY_ROOT/lib/dispatch-task.sh --type query {bot} "…"),'
        f' supersede ($CLAUDLOBBY_ROOT/lib/dispatch-task.sh --supersedes {task_id} {bot} "…"),'
        f' withdraw ($CLAUDLOBBY_ROOT/lib/task-act.sh withdraw {task_id} --reason "…")'
        f' or escalate ($CLAUDLOBBY_ROOT/lib/task-act.sh escalate {task_id} "…")'
    )


def recheck_message(row, *, task_id: str, who: str, why: str,
                    now: datetime | None = None) -> str:
    """The id-less re-check the manager receives: what happened, and the four
    verbs it may answer with. The same menu M4's timer sends, scoped to one
    row — written here so the nudge and the timer cannot drift into offering
    a manager different options for the same situation."""
    title = _one_line(row["title"] or "") or "untitled"
    assignee = _short(row["assignee"]) or "unknown"
    return (
        f"NUDGE from {who}: task {task_id} ({title}, {_age(row['occurred_at'], now)},"
        f" assignee {assignee})"
        f" — {why or 'no reason given'}."
        f" Act: {verb_commands(task_id, assignee)};"
        " report what you did."
    )


def send_to_bot(paths, bot: str, message: str, fleet: str | None = None,
                msg_id: str | None = None,
                wire_out: str | None = None) -> tuple[int, str]:
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
    # chunk P fold F5: carry the plane routing trailer so the RECEIVER records a
    # `received` proof. Without it the nudge/recheck class was UNCONFIRMED
    # forever — a timer-driven send whose delivery could never be shown. bash
    # bot_tmux_send grammar-guards this value before it touches the payload.
    # PLANE_WIRE_OUT (fold F1) lets that same door hand back the wire proof so
    # the delivery JOIN reads DELIVERED, not merely records a receipt.
    if msg_id:
        env["PLANE_MSG_ID"] = msg_id
    if wire_out:
        env["PLANE_WIRE_OUT"] = str(wire_out)
    try:
        r = subprocess.run(["bash", str(script), bot, message],
                           capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return r.returncode, (r.stderr or "").strip()


def _read_wire_out(path: str) -> tuple[str, int] | None:
    """Read the `sha=`/`bytes=` pair lib/lib-common.sh:bot_tmux_send wrote as the
    delivery-JOIN wire proof (chunk P fold F1). Returns (wire_sha256, wire_bytes)
    or None (a sha-less host, or a faked send that never ran the door)."""
    try:
        sha: str | None = None
        nbytes: int | None = None
        for line in Path(path).read_text().splitlines():
            key, _, val = line.partition("=")
            if key == "sha":
                sha = val
            elif key == "bytes":
                try:
                    nbytes = int(val)
                except ValueError:
                    nbytes = None
        if sha and nbytes is not None:
            return sha, nbytes
    except OSError:
        pass
    return None


def _send_to_bot_with_wire(paths, bot: str, message: str, *,
                           fleet: str | None = None,
                           msg_id: str | None = None
                           ) -> tuple[int, str, tuple[str, int] | None]:
    """`send_to_bot` plus the chunk-P wire proof (fold F1/F5): run the send with a
    scratch file bot_tmux_send writes its wire proof to, and return
    (rc, err, wire) where wire is (wire_sha256, wire_bytes) or None. The
    nudge/recheck transmission rides that proof so the delivery JOIN reads
    DELIVERED rather than staying UNCONFIRMED. `send_to_bot` stays the
    monkeypatched seam — a fake that does not run dispatch.sh leaves the file
    empty and wire is None, which the delivery JOIN treats as unconfirmed."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="plane-wire-")
    os.close(fd)
    try:
        rc, err = send_to_bot(paths, bot, message, fleet=fleet,
                              msg_id=msg_id, wire_out=path)
        wire = _read_wire_out(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return rc, err, wire


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

    rc, err, wire = _send_to_bot_with_wire(paths, manager, message,
                                           fleet=row["fleet"], msg_id=msg_id)
    if asked:
        _emit(paths.root, [transmission_request(
            row, msg_id=msg_id, destination=manager, ok=(rc == 0), detail=err,
            wire=wire)])
    if rc != 0:
        print(f"nudge: recorded, but the re-check did NOT reach {manager}"
              f" (rc={rc}{': ' + err if err else ''}) — the nudge stands on the"
              " plane and shows on the card", file=sys.stderr)
        return 1
    print(f"asked {manager} to act")
    return 0


# --- M4: the re-check, ONE message per manager (chunk M-B, #1481) -------------
# The first REACTION the estate has that nobody asked for by hand. A nudge is
# event-driven and needs an operator; this is the clock's own turn: every N
# hours, each manager is handed the rows of theirs that stopped moving and the
# four verbs, and is asked to say what it did with each.
#
# THE MESSAGE IS ID-LESS, deliberately and for the chunk's own reason: an id'd
# re-check would open a row nobody closes, which is the defect the task loop
# exists to remove. It is a COMMUNICATION, and the plane records one per ROW
# rather than one per message — a re-check is N asks delivered in one payload
# (a manager should read one message, not five), and only a per-row record can
# answer "have I already asked about this row?" without re-parsing prose. That
# question IS the debounce: `source_ref = task-recheck:<assignment_id>` is the
# ONE stamp, and `plane-readers.rechecked_at` is the only reader of it, so the
# repeat window is a plane read with no timer state file to lose or to lie.
#
# THE SENDER IS `system:task-recheck` — `briefing-trigger.sh`'s precedent for a
# machine-originated communication. Not a human (nobody asked), not the manager
# (it is the recipient), not the fleet (an actor alias is minted from this, and
# a fleet is not an actor).

# The stdlib twin of `plane-readers.RECHECK_REF_PREFIX` (pinned equal by test):
# the reader is bash-facing and cannot import this package, so the write side
# spells the stamp here and the pin keeps the two from forking. A fork does not
# crash — it silently disables the debounce and re-asks a manager every run.
RECHECK_REF_PREFIX = "task-recheck:"
RECHECK_SENDER = "system:task-recheck"
DEFAULT_MAX_AGE_H = 48.0
DEFAULT_REPEAT_H = 24.0
# How many rows ONE message names. A manager holding 22 stale rows would
# otherwise get a 3000-character pane send, which is a wall nobody reads and a
# payload the send path has never been measured on. The rest are NOT stamped —
# only a row the manager was actually told about counts as re-checked — so they
# lead the next run instead of being silently skipped for a day.
#
# THIS CAP BOUNDS ROWS, NOT BYTES (the fold's F6): a title is authored prose
# with no length contract, and eight rows of whole dispatch titles measured
# 3272 characters in one message before `_clip` — a row count says nothing
# about the pane send's actual size when the row's own content is unbounded.
# `_clip`'s ~80-char title cap (the id already carries the rest of a row's
# identity) brings a realistic worst case — long title, one nudge fact, no
# escalation (F5 excludes those before they ever reach here) — down to
# roughly 2.4KB for 8 rows plus the overflow and waiting footers (measured).
# The cap is still ROWS, deliberately: a byte cap would silently drop
# whichever row happened to push the message over, which row that is
# depending on send order rather than staleness.
RECHECK_MAX_ROWS = 8


def _instant(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _span(seconds: float) -> str:
    """A coarse span, the page's granularity — `_age`'s formatter, shared so a
    deadline and an age can never be phrased on two different clocks."""
    s = max(0.0, seconds)
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


def _deadline_phrase(expected_by: str | None, now: datetime) -> str:
    at = _instant(expected_by)
    if at is None:
        return "no deadline" if not expected_by else "deadline unreadable"
    if at <= now:
        return f"deadline passed {_span((now - at).total_seconds())} ago"
    return f"deadline in {_span((at - now).total_seconds())}"


def row_would_be_due(row, *, now: datetime, max_age_s: float) -> bool:
    """The clock-and-deadline half of `row_is_due`, WITHOUT the escalation
    exemption (F5) — its deadline has passed, or it has been open longer than
    the max age. The two are OR'd because they catch different failures — a
    deadline that passed is a promise broken, and an ageing row with no
    deadline (every id-less dispatch, and every row dispatched before M-A)
    has no promise to break and would otherwise never be re-checked at all. A
    row whose dispatch instant is unreadable is NOT due: the plane cannot
    date it, so no clock claim about it would be true.

    Named and kept separate so `cmd_task_recheck` can still ask "would this
    row otherwise be due" for an escalated row, to report it in the
    'waiting on the human' footer rather than making it silently vanish."""
    at = _instant(row.get("occurred_at"))
    if at is None:
        return False
    exp = _instant(row.get("expected_by"))
    if exp is not None and exp <= now:
        return True
    return (now - at).total_seconds() > max_age_s


def row_is_due(row, *, now: datetime, max_age_s: float) -> bool:
    """A row the re-check names. An ESCALATED row is exempt (the fold's F5):
    its newest word is `escalated` (`fleet_open_rows`'s own `menu_facts` join
    — the same fact the digest's line renders, never re-derived here), which
    means a manager already raised it and the row is the HUMAN's to answer,
    not the manager's to chase again. Left in, a manager who did the right
    thing got the automated four-verb nag on the SAME PASS fleet-pulse pages
    the operator about the very same row (reproduced). See
    `row_would_be_due` for the clock-and-deadline test alone."""
    if row.get("escalated"):
        return False
    return row_would_be_due(row, now=now, max_age_s=max_age_s)


def _clip(text: str, limit: int = 80) -> str:
    """A free-text field capped so ONE row cannot dominate the pane send (the
    fold's F6): a dispatch TITLE is authored prose with no length contract of
    its own, and eight rows of whole titles measured 3272 characters in one
    message — most of it a title nobody needed to read in full, since the
    task id already carries the row's own identity for every act. `limit`
    bounds the visible text; the id is always still there to look the rest
    up by."""
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def recheck_row_line(row, *, index: int, now: datetime) -> str:
    """What the manager is told about ONE row — and, byte for byte, what the
    plane records as the ask about it."""
    tid = row.get("task_id") or row.get("assignment_id") or "?"
    title = _clip(_one_line(row.get("title") or "") or "untitled")
    assignee = _short(row.get("assignee") or "") or "unknown"
    bits = [f"assignee {assignee}", _age(row.get("occurred_at"), now),
            _deadline_phrase(row.get("expected_by"), now)]
    prog = _instant(row.get("last_progress_at"))
    bits.append(f"last progress {_span((now - prog).total_seconds())} ago"
                if prog else "no progress on this row")
    esc = row.get("escalated") or None
    if esc:
        q = _one_line(esc.get("question") or "") or "question not recorded"
        bits.append(f"ESCALATED by {_short(esc.get('by') or '') or 'unknown'}"
                    f" ({q}) — the human owes an answer")
    nud = row.get("nudged") or None
    if nud:
        bits.append(f"nudged by {_short(nud.get('by') or '') or 'someone'}"
                    f" {_age(nud.get('at'), now)}")
    return f"[{index}] task {tid} ({title}) — " + ", ".join(bits)


def recheck_digest(lines: list[str], *, fleet: str, max_age_h: float,
                   extra: int = 0, manager: str = "", waiting: int = 0) -> str:
    """The whole message, ONE line (tmux `send-keys` reads a newline as a
    RETURN — `_one_line`'s lesson, applied to the assembly rather than after
    it). The verbs are stated once, as a template the row lines fill: naming
    the four commands per row is the same sentence eight times over, and the
    row lines already carry the id and the assignee it needs.

    `waiting` (the fold's F5) is a COUNT, never row lines: an escalated row
    is the human's to answer, not the manager's to be nagged about, so it
    never reaches `lines` — this only says how many were left out for that
    reason, without the four verbs, so the manager does not read silence as
    "nothing else is stale"."""
    head = (f"TASK RE-CHECK ({fleet}): {len(lines)} open row(s) of yours are past"
            f" deadline or older than {max_age_h:g}h.")
    tail = (f" For EACH: {verb_commands('<task-id>', '<assignee>')}."
            " Then report what you did per row — a row you leave untouched comes"
            " back next sweep.")
    more = (f" (+{extra} more of yours are stale; `claudlobby brief --bot"
            f" {manager}` lists them all.)") if extra > 0 else ""
    waiting_line = (f" (waiting on the human: {waiting} row(s) already escalated"
                    " — no verbs needed there.)") if waiting > 0 else ""
    return _one_line(head + " " + " ".join(lines) + more + waiting_line + tail)


def recheck_ask_request(row, *, msg_id: str, manager: str, body: str) -> dict:
    """The ask about ONE row, as the plane's own record: a communication from
    the machinery to the manager, carrying the row's ids so it threads under
    the task, and stamped with the debounce key. It mints no assignment — a
    re-check must never become a task."""
    return {
        "event_type": "communication", "emitter": "task-recheck",
        "fleet": row["fleet"],
        "source_ref": RECHECK_REF_PREFIX + row["assignment_id"],
        "payload": {
            "msg_id": msg_id,
            "sender": RECHECK_SENDER,
            "recipient": manager,
            "recipient_raw": _short(manager) or manager,
            "message_class": "task_request",
            "command_type": "query",
            "work_item_id": row["work_item_id"],
            "assignment_id": row["assignment_id"],
            "body": body,
        },
    }


def _group_by_manager(rows: list[dict]) -> tuple[dict, list[dict]]:
    """(manager → its rows, oldest first; the rows no manager can be named
    for). The second list is DISCLOSED rather than dropped or reassigned to a
    fleet default: `assigned_by` is the plane's own fact about who owns a row,
    and inventing an owner sends a re-check to someone who never dispatched
    it."""
    by: dict[str, list[dict]] = {}
    orphans: list[dict] = []
    for row in rows:
        mgr = manager_of(row)
        if not mgr:
            orphans.append(row)
            continue
        by.setdefault(mgr, []).append(row)
    return by, orphans


def _collect_due(plane, *, now: datetime, max_age_s: float, repeat_s: float
                 ) -> tuple[list[dict], list[dict], list[dict]]:
    """(rows to re-check, rows a re-check already named inside the repeat
    window, rows ESCALATED and waiting on the human) — ONE plane session, the
    stdlib readers' own answers. The third list (the fold's F5) is never
    named to a manager as a row to act on — it exists so the digest can say
    how many of a manager's stale rows are the human's to answer, rather than
    the manager reading silence as "nothing else is stale"."""
    rows = plane.pr.fleet_open_rows(plane.conn, plane.fleet)
    due = [r for r in rows if row_is_due(r, now=now, max_age_s=max_age_s)]
    waiting = [r for r in rows if r.get("escalated")
              and row_would_be_due(r, now=now, max_age_s=max_age_s)]
    stamps = plane.pr.rechecked_at(plane.conn, [r["assignment_id"] for r in due])
    fresh, held = [], []
    for row in due:
        at = _instant(stamps.get(row["assignment_id"]))
        if at is not None and (now - at).total_seconds() < repeat_s:
            held.append(row)
        else:
            fresh.append(row)
    return fresh, held, waiting


def cmd_task_recheck(args) -> int:
    """`claudlobby task recheck --fleet <F>` — the dormant `task-recheck`
    timer's door, and a hand-runnable one.

    Exit ladder, the acts' own: 0 = asked (or nothing was due), 1 = something
    was NOT asked or NOT delivered (a manager down, a row nobody owns), 2 = the
    call cannot be answered (no fleet named), 3 = the plane cannot serve —
    unreachable is not empty, so it refuses rather than reporting a quiet
    fleet."""
    # `--fleet` here names the PLANE's fleet — an alias in a per-ROOT db — and
    # deliberately does NOT anchor the overlay the way the global `--fleet`
    # does. The two are different questions and conflating them made the door
    # refuse to answer for a fleet whose rows the plane holds but whose
    # `local/<fleet>/` this root does not (root mode, a moved overlay, a
    # timer run from an install that composes elsewhere). The matcher's own
    # contract, `--fleet F --root R`, is exactly this shape.
    fleet_arg = _one_line(getattr(args, "recheck_fleet", "") or "")
    paths = _resolve_paths(args)
    max_age_h = getattr(args, "max_age_h", DEFAULT_MAX_AGE_H)
    repeat_h = getattr(args, "repeat_h", DEFAULT_REPEAT_H)
    # A NEGATIVE window is refused, loudly (the fold's F7 — `dispatch-task.sh`
    # `_reject_non_integer`'s precedent, M-A's `--deadline-min`): argparse's
    # `type=float` already refuses a non-number before this door ever runs,
    # but it has no opinion on sign, and a negative age/repeat window is not
    # a real request. ZERO is not refused: `--max-age-h 0` is the honest way
    # to ask for every open row with a readable dispatch instant, since any
    # already-dispatched row is then "older than" a zero-length window.
    for flag, value in (("--max-age-h", max_age_h), ("--repeat-h", repeat_h)):
        if value < 0:
            print(f"recheck: {flag} must be zero or positive, got {value:g}"
                  " (0 = every open row with a readable dispatch instant"
                  " qualifies on age alone)", file=sys.stderr)
            return 2
    max_age_s = float(max_age_h) * 3600
    repeat_s = float(repeat_h) * 3600
    dry = bool(getattr(args, "dry_run", False))

    if os.environ.get("PLANE_EMIT_DISABLED") == "1":
        print("recheck: PLANE_EMIT_DISABLED=1 — the plane is silenced, so no ask"
              " could be recorded and every run would re-ask the same rows"
              " forever; nothing was sent", file=sys.stderr)
        return 3

    from ..brief import plane_session, resolve_fleet_name

    fleet = fleet_arg or resolve_fleet_name(paths)
    if not fleet:
        print("recheck: no fleet is named (--fleet <name>, or a fleet.yaml naming"
              " one) — the plane's rows are per fleet", file=sys.stderr)
        return 2
    plane, note = plane_session(paths, fleet)
    if plane is None:
        print(f"recheck: {note} — unreachable, not empty; nothing was sent",
              file=sys.stderr)
        return 3
    now = datetime.now(timezone.utc)
    try:
        if not hasattr(plane.pr, "fleet_open_rows"):
            print(f"recheck: the readers installed at {paths.lib /'plane-readers.py'}"
                  " predate the task-loop menu — pull the install and re-run",
                  file=sys.stderr)
            return 3
        fresh, held, waiting = _collect_due(plane, now=now, max_age_s=max_age_s,
                                           repeat_s=repeat_s)
    finally:
        plane.close()

    by_manager, orphans = _group_by_manager(fresh)
    # F5: escalated-and-otherwise-due rows, per manager — a COUNT for the
    # digest's footer, never a list of rows to act on (their orphans are
    # discarded here: an escalated row with no nameable manager still names
    # nobody to footer it for, and the row itself is already excluded from
    # `fresh` regardless of who owns it).
    waiting_by_manager, _waiting_orphans = _group_by_manager(waiting)
    for row in orphans:
        print(f"recheck: {row.get('task_id') or row['assignment_id']} has no"
              " manager on the plane (no assigned_by the registry can name) —"
              " nobody was asked", file=sys.stderr)
    if not by_manager:
        if orphans:
            return 1
        print(f"recheck: nothing in {fleet} is past deadline or older than"
              f" {max_age_s / 3600:g}h"
              + (f" ({len(held)} row(s) re-checked inside the last"
                 f" {repeat_s / 3600:g}h)" if held else "")
              + (f" ({len(waiting)} row(s) waiting on the human, already"
                 " escalated)" if waiting else "")
              + " — nothing sent")
        return 0

    rc = 1 if orphans else 0
    for manager, rows in sorted(by_manager.items()):
        named = rows[:RECHECK_MAX_ROWS]
        lines = [recheck_row_line(r, index=i, now=now)
                 for i, r in enumerate(named, 1)]
        message = recheck_digest(lines, fleet=fleet, max_age_h=max_age_s / 3600,
                                 extra=len(rows) - len(named), manager=manager,
                                 waiting=len(waiting_by_manager.get(manager, [])))
        if dry:
            print(f"[dry-run] {manager}: {len(named)} row(s)")
            print(f"[dry-run] {message}")
            continue
        alias = f"bot:{fleet}/{manager}"
        ids = [mint_msg_id() for _ in named]
        # INTENT BEFORE TRANSPORT (report-back.sh's rule): the asks are recorded
        # before the send, so a send that dies mid-flight still leaves the
        # question on the plane. An UNRECORDED ask is said loudly and sent
        # anyway — the mission is the ask, and the only cost of the missing
        # stamp is that the next sweep asks again, which is the safe direction.
        asked, verdict = _emit(paths.root, [
            recheck_ask_request(r, msg_id=m, manager=alias, body=line)
            for r, m, line in zip(named, ids, lines)])
        if not asked:
            print(f"recheck: the plane did NOT record the ask to {manager}"
                  f" ({verdict or 'no outcome'}) — sending anyway; these rows"
                  " will be re-checked again next sweep", file=sys.stderr)
        # fold F5: one digest is one physical send, so it carries ONE trailer —
        # ids[0], whose row can then read DELIVERED (the receiver records a
        # `received` for it); the other rows stay UNCONFIRMED (no receipt of
        # their own). The one wire proof rides every row's transmission; it is
        # inert on a row that has no matching `received`.
        send_rc, err, wire = _send_to_bot_with_wire(paths, manager, message,
                                                    fleet=fleet, msg_id=ids[0])
        if asked:
            _emit(paths.root, [
                transmission_request(r, msg_id=m, destination=manager,
                                     ok=(send_rc == 0), detail=err, wire=wire)
                for r, m in zip(named, ids)])
        if send_rc != 0:
            print(f"recheck: the re-check did NOT reach {manager}"
                  f" (rc={send_rc}{': ' + err if err else ''}) —"
                  f" {len(named)} row(s) stay on the plane unanswered",
                  file=sys.stderr)
            rc = 1
            continue
        print(f"asked {manager} about {len(named)} row(s)"
              + (f" (+{len(rows) - len(named)} held over)"
                 if len(rows) > len(named) else ""))
    return rc
