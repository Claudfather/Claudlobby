"""A refused report notice can be sent again under its own request (#2842).

A linked report (assignment progress, block, return, complete and fail) records its task
transition first, then sends its manager one notice. When the manager's pane refuses that
notice before typing (a box that already holds text, an open menu), the transition stands
and the notice is lost: the command exits 5, a plain rerun of the same request replays the
retained attempt and sends nothing, and the report verbs had no --retry-uncertain, which
message send and reply, assignment deliver and fleet reports submit have. Nor did the
request say why the send failed: the transport's reason never reached the receipt.

Each report verb now takes --retry-uncertain: the same command and request ID make one new
attempt under the same message ID, refused once a submission is recorded. A report's retry
types only into an idle pane: read_box, asked with busy, answers busy while a turn runs, and
the retry then sends nothing. The native send asks again on the capture it takes under the
manager's send lock, right before it types, so a turn that starts while the retry waits for
that lock gets nothing either. The first attempt's admission is unchanged, and no other caller
asks for the busy answer. The transport's reason reaches the command's data
(transport_reason) and request show.

The manager's pane is faked where the decision under test is the owner's (which read, which
send, which receipt); read_box's busy answer and a retry that waits for the send lock run
against real tmux.
"""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from claudlobby import assignment_delivery, message_operations, message_queries, message_transport
from claudlobby.__main__ import main
from claudlobby.message_queries import MessageIdentity, ReceiptObservation
from claudlobby.message_transport import TransportOutcome
from claudlobby.recording_alerts import ChannelOutcome, RecordingAlertOutcome
from tests.package_fixtures import source_package
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 (short private root)
from tests.test_releases import installed  # noqa: F401 (cold dependency)
from tests.test_task_read_cli import active  # noqa: F401 (active private Plane)
from tests.test_message_write_cli import _assigned, _generated
from tests.test_message_enter_repair import (BUSY, EMPTY, HELD, LIVE_BUSY, _HOLD_LOCK, _frame,
                                             _send_lock_file, _stub_pane)

_MENU = "recipient pane showed a menu; nothing was sent"  # message_transport.send's own refusal
_BUSY = "recipient pane was busy; nothing was sent"
_UNREAD = "recipient pane could not be read; nothing was sent"
_SUBMITTED = TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
_REFUSED = TransportOutcome("failed", reason=_MENU)
_QUIET = {"notify": lambda *a, **k: RecordingAlertOutcome(ChannelOutcome("submitted", True),
                                                          ChannelOutcome("unconfigured", True)),
          "clear": lambda *a, **k: None}
_VERBS = (("progress", "--summary"), ("block", "--reason"), ("return", "--reason"),
          ("complete", "--summary"), ("fail", "--reason"))


class _Pane:
    """The recipient's pane. ``box`` is what a busy read answers (busy, empty, held or
    unknown); a plain read answers as read_box always has, so a running turn reads empty.
    Each native send takes the next outcome, and a submitted one earns its receipt unless
    ``receives`` is off. Every read records whether it asked about a running turn, and every
    send whether it asked the native helper to refuse one under the send lock (idle_only)."""

    def __init__(self, monkeypatch, *outcomes, box="empty", receives=True):
        self.outcomes, self.box, self.receives = list(outcomes), box, receives
        self.reads, self.sends, self.idle_only, self.submitted = [], [], [], set()
        monkeypatch.setattr(message_transport, "read_box", self.read_box)
        monkeypatch.setattr(message_transport, "press_held_enter", self.press)
        monkeypatch.setattr(message_queries, "receipt", self.receipt)

    def read_box(self, package, destination, *, timeout=5, runner=None, busy=False):
        self.reads.append(busy)
        if self.box == "busy" and not busy:
            return "empty"  # a plain read ignores a running turn
        return self.box

    def transport(self, package, destination, *, message_id, body, **options):
        self.sends.append(message_id)
        self.idle_only.append(options.get("idle_only", False))
        outcome = self.outcomes.pop(0)
        if outcome.status == "submitted":
            self.submitted.add(message_id)
        return outcome

    def press(self, package, destination, *, message_id, expect=None, before=None, timeout=15,
              runner=None):
        return SimpleNamespace(verdict="not-held", pressed=False, match=None, reason=None)

    def receipt(self, ctx, message_id, *, destination, wait):
        peer = ctx.bots[destination.rsplit("/", 1)[1]]
        sender = MessageIdentity(ctx.caller.uid, ctx.caller.alias, ctx.caller_fleet_uid)
        recipient = MessageIdentity(peer.uid, peer.alias, ctx.fleet_uid)
        if self.receives and message_id in self.submitted:
            return ReceiptObservation(message_id, str(ctx.root), sender, recipient, "received",
                                      "delivered", 0, None, None)
        return ReceiptObservation(message_id, str(ctx.root), sender, recipient, "missing",
                                  "unconfirmed", 8, "timeout", None)

    def native(self, monkeypatch, owner, name, **extra):
        """Send ``owner.name``'s native leg into this pane."""
        real = getattr(owner, name)
        monkeypatch.setattr(owner, name, lambda *a, **k: real(*a, transport=self.transport, **extra, **k))


def _run(capsys, root, *argv, expected):
    assert main(["--root", str(root), "--json", *argv]) == expected
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1 and out["ok"] is (expected == 0), out
    return out


def _attempts(capsys, root, request_id):
    """request show's native attempts: each one's transport status and reason."""
    view = _run(capsys, root, "request", "show", request_id, expected=0)["data"]["request"]
    return [(item["transport"]["status"], item["transport"]["reason"]) for item in view["transmissions"]]


def _worker_assignment(monkeypatch, root, host):
    _generated(monkeypatch, root, host.installation)
    _ctx, _task, assigned = _assigned(root)
    _generated(monkeypatch, root, host.installation, bot="worker")
    return assigned.assignment_id


def test_a_refused_report_notice_is_sent_again_under_its_request(active, monkeypatch, capsys):
    root, host = active
    assignment = _worker_assignment(monkeypatch, root, host)
    pane = _Pane(monkeypatch, _REFUSED, _SUBMITTED)
    pane.native(monkeypatch, message_operations, "send_committed_native_attempt")
    request_id = str(uuid4())
    command = ("assignment", "complete", assignment, "--summary", "Finished", "--request-id", request_id)
    first = _run(capsys, root, *command, expected=5)
    assert first["error"]["code"] == "notification_failed"
    assert (first["data"]["state"], first["data"]["recording"]) == ("completed", "committed")
    assert (first["data"]["transport"], first["data"]["transport_reason"]) == ("failed", _MENU)
    plain = _run(capsys, root, *command, expected=5)
    assert plain["data"]["replayed"] and plain["data"]["transport_reason"] == _MENU
    assert len(pane.sends) == 1, "a plain rerun sends nothing"
    retried = _run(capsys, root, *command, "--retry-uncertain", expected=0)
    assert retried["data"]["message_id"] == first["data"]["message_id"]
    assert retried["data"]["replayed"] and retried["data"]["notification"] == "received"
    assert retried["data"]["transport_reason"] is None
    assert pane.sends == [first["data"]["message_id"]] * 2
    assert pane.reads == [False, False, False, True], "only the retry asks whether a turn runs"
    assert pane.idle_only == [False, True], "only the retry's native send refuses a turn under the lock"
    assert _attempts(capsys, root, request_id) == [("failed", _MENU), ("submitted", None)]
    assert main(["--root", str(root), "request", "show", request_id]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("transmission ")]
    assert [("\ttransport=failed\t" in line, line.endswith(f"\treason={_MENU}")) for line in lines] == [
        (True, True), (False, False)], lines


def test_every_report_verb_takes_the_retry(active, monkeypatch, capsys):
    root, host = active
    pane = _Pane(monkeypatch, *([_REFUSED, _SUBMITTED] * len(_VERBS)))
    pane.native(monkeypatch, message_operations, "send_committed_native_attempt")
    for verb, field in _VERBS:
        assignment = _worker_assignment(monkeypatch, root, host)
        command = ("assignment", verb, assignment, field, "Reported", "--request-id", str(uuid4()))
        first = _run(capsys, root, *command, expected=5)
        retried = _run(capsys, root, *command, "--retry-uncertain", expected=0)
        assert retried["data"]["message_id"] == first["data"]["message_id"], verb
        assert retried["data"]["notification"] == "received", verb
    assert len(pane.sends) == 2 * len(_VERBS)


def test_every_report_verb_names_the_retry_in_its_help(capsys):
    for verb, _field in _VERBS:
        with pytest.raises(SystemExit) as exited:
            main(["assignment", verb, "--help"])
        assert exited.value.code == 0 and "--retry-uncertain" in capsys.readouterr().out, verb


def test_a_retry_types_nothing_into_a_busy_or_unread_pane(active, monkeypatch, capsys):
    root, host = active
    assignment = _worker_assignment(monkeypatch, root, host)
    pane = _Pane(monkeypatch, _REFUSED, _SUBMITTED, box="busy")
    pane.native(monkeypatch, message_operations, "send_committed_native_attempt")
    request_id = str(uuid4())
    command = ("assignment", "progress", assignment, "--summary", "Started", "--request-id", request_id)
    first = _run(capsys, root, *command, expected=5)
    assert len(pane.sends) == 1, "the first attempt's admission is unchanged: a running turn does not stop it"
    for box, reason in (("busy", _BUSY), ("unknown", _UNREAD)):
        pane.box = box
        refused = _run(capsys, root, *command, "--retry-uncertain", expected=5)
        assert (refused["data"]["transport"], refused["data"]["transport_reason"]) == ("failed", reason)
        assert refused["data"]["message_id"] == first["data"]["message_id"]
        assert len(pane.sends) == 1, f"a retry typed into a {box} pane"
    pane.box = "empty"
    sent = _run(capsys, root, *command, "--retry-uncertain", expected=0)
    assert sent["data"]["notification"] == "received" and len(pane.sends) == 2
    assert _attempts(capsys, root, request_id) == [
        ("failed", _MENU), ("failed", _BUSY), ("failed", _UNREAD), ("submitted", None)]


def test_a_retry_is_refused_once_a_submission_is_recorded(active, monkeypatch, capsys):
    root, host = active
    assignment = _worker_assignment(monkeypatch, root, host)
    pane = _Pane(monkeypatch, _SUBMITTED, receives=False)
    pane.native(monkeypatch, message_operations, "send_committed_native_attempt")
    request_id = str(uuid4())
    command = ("assignment", "complete", assignment, "--summary", "Finished", "--request-id", request_id)
    first = _run(capsys, root, *command, expected=5)
    assert first["data"]["transport"] == first["data"]["notification"] == "submitted"
    refused = _run(capsys, root, *command, "--retry-uncertain", expected=4)
    assert refused["error"]["code"] == "conflict"
    assert len(pane.sends) == 1 and True not in pane.reads, "refused before any look or send"
    assert _attempts(capsys, root, request_id) == [("submitted", None)]


def test_an_unlinked_report_retry_types_nothing_into_a_busy_pane(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.installation, bot="worker")
    pane = _Pane(monkeypatch, _REFUSED, _SUBMITTED, box="busy")
    pane.native(monkeypatch, message_operations, "send_unlinked_report", **_QUIET)
    request_id = str(uuid4())
    command = ("fleet", "reports", "submit", "--status", "completed", "--summary", "Finished",
               "--request-id", request_id)
    first = _run(capsys, root, *command, expected=5)
    assert (first["data"]["transport"], first["data"]["transport_reason"]) == ("failed", _MENU)
    busy = _run(capsys, root, *command, "--retry-uncertain", expected=5)
    assert (busy["data"]["transport"], busy["data"]["transport_reason"]) == ("failed", _BUSY)
    assert len(pane.sends) == 1
    pane.box = "empty"
    sent = _run(capsys, root, *command, "--retry-uncertain", expected=0)
    assert sent["data"]["message_id"] == first["data"]["message_id"] and len(pane.sends) == 2
    assert _attempts(capsys, root, request_id) == [("failed", _MENU), ("failed", _BUSY), ("submitted", None)]
    assert pane.idle_only == [False, True], "only the retry's native send refuses a turn under the lock"


def test_no_other_send_asks_whether_a_turn_runs(active, monkeypatch, capsys, tmp_path):  # noqa: F811
    """The busy answer is a report retry's alone: message send's and assignment deliver's own
    retries still send into a busy pane, as every first attempt does."""
    root, host = active
    _generated(monkeypatch, root, host.installation)
    pane = _Pane(monkeypatch, _REFUSED, _SUBMITTED, _REFUSED, _SUBMITTED, box="busy")
    pane.native(monkeypatch, message_operations, "send_message", **_QUIET)
    send = ("message", "send", "--to", "worker", "--text", "Private body", "--request-id", str(uuid4()))
    assert _run(capsys, root, *send, expected=5)["data"]["transport_reason"] == _MENU
    assert _run(capsys, root, *send, "--retry-uncertain", expected=0)["data"]["delivery"] == "received"
    _ctx, _task, assigned = _assigned(root)
    body = tmp_path / "assignment.txt"
    body.write_text("Private assignment delivery", encoding="utf-8")
    pane.native(monkeypatch, assignment_delivery, "deliver")
    deliver = ("assignment", "deliver", assigned.assignment_id, "--file", str(body),
               "--request-id", str(uuid4()))
    _run(capsys, root, *deliver, expected=5)
    assert _run(capsys, root, *deliver, "--retry-uncertain", expected=0)["data"]["delivery"] == "received"
    assert len(pane.sends) == 4 and True not in pane.reads and True not in pane.idle_only


def test_a_receipt_kept_before_reasons_were_still_loads_and_retries(active, monkeypatch, capsys):
    root, host = active
    assignment = _worker_assignment(monkeypatch, root, host)
    pane = _Pane(monkeypatch, _REFUSED, _SUBMITTED)
    pane.native(monkeypatch, message_operations, "send_committed_native_attempt")
    request_id = str(uuid4())
    command = ("assignment", "progress", assignment, "--summary", "Started", "--request-id", request_id)
    first = _run(capsys, root, *command, expected=5)
    path = next((root / "state/requests").glob(f"*/{request_id}.json"))
    raw = json.loads(path.read_text())
    for attempt in raw["message_attempts"]:
        del attempt["observation"]["reason"]
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n")
    assert _attempts(capsys, root, request_id) == [("failed", None)]
    retried = _run(capsys, root, *command, "--retry-uncertain", expected=0)
    assert retried["data"]["message_id"] == first["data"]["message_id"]
    assert _attempts(capsys, root, request_id) == [("failed", None), ("submitted", None)]


_BUSY_EMPTY = _frame("", above=("✻ Cogitating… (12s · esc to interrupt)",))


@pytest.mark.parametrize(("frame", "plain", "asked"), [
    (BUSY, "held", "busy"), (LIVE_BUSY, "held", "busy"), (_BUSY_EMPTY, "empty", "busy"),
    (HELD, "held", "held"), (EMPTY, "empty", "empty"),
], ids=["busy-box-holding-text", "busy-live-frame", "busy-empty-box", "idle-box-holding-text",
        "idle-empty-box"])
def test_read_box_answers_busy_only_when_asked(frame, plain, asked):
    with _stub_pane(frame) as (destination, log, package):
        assert message_transport.read_box(package, destination(), timeout=10) == plain
        assert message_transport.read_box(package, destination(), timeout=10, busy=True) == asked
        assert message_transport.read_box(package, destination("absent"), timeout=10, busy=True) == "unknown"
        assert log.read_bytes() == b""


def test_a_busy_answer_counts_only_when_asked(tmp_path):  # noqa: F811
    package = replace(source_package(), native=Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts")
    destination = message_transport.TransportDestination(tmp_path, "fleet", "private-none", "worker", tmp_path)
    asked = []

    def runner(command, **kwargs):
        asked.append(command[-1])
        return SimpleNamespace(stdout=b"box-v1\tbusy\n")

    assert message_transport.read_box(package, destination, runner=runner) == "unknown"
    assert message_transport.read_box(package, destination, runner=runner, busy=True) == "busy"
    assert asked == ["worker", "busy"], "a plain read's command is unchanged"


_MSG = "msg_" + "e" * 32


@pytest.mark.parametrize("turn", [True, False], ids=["a-turn-starts-while-it-waits", "no-turn-starts"])
def test_a_retry_refuses_a_turn_that_starts_while_it_waits_for_the_send_lock(turn, monkeypatch):
    """Another sender holds the manager's send lock. The retry looks and sees an idle pane;
    then the other sender's message starts a turn while the retry waits for the lock. The retry
    refuses on the capture it takes under the lock and types nothing. With no turn it types once
    the lock frees. A real tmux pane, the shipped native helper and message_transport.send,
    wrapped as a report's retry is."""
    with _stub_pane(EMPTY, stub="redraw-box-stub.py") as (destination, log, package):
        dest = destination()
        lock = _send_lock_file(package, dest)
        held = Path(lock).parent / "held"
        holder = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK, lock, str(held), "1.5"])
        try:
            deadline = time.monotonic() + 10
            while not held.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert held.exists(), "the other sender never took the lock"
            real, looks = message_transport.read_box, []

            def look(*args, **kwargs):
                looks.append(real(*args, **kwargs))
                if turn:  # the other sender's message is submitted, and a turn starts
                    running = dest.root / "turn.txt"
                    running.write_text(_BUSY_EMPTY, encoding="utf-8")
                    os.replace(running, dest.root / "frame.txt")
                return looks[-1]
            monkeypatch.setattr(message_transport, "read_box", look)
            outcome = message_operations._idle_only(message_transport.send)(
                package, dest, message_id=_MSG, body="retried notice")
            freed = holder.poll() is not None  # the holder frees the lock as it exits
            time.sleep(0.5)  # a keystroke still in flight reaches the stub's log
        finally:
            holder.kill()
            holder.wait(timeout=5)
        typed = log.read_bytes()
        assert looks == ["empty"], "the retry's own look saw an idle pane"
        assert freed, "the send went on before the other sender freed the lock"
        if turn:
            assert (outcome.status, outcome.reason) == ("failed", _BUSY), outcome
            assert typed == b"", "the retry typed into a running turn"
        else:
            assert outcome.status == "submitted" and b"retried notice" in typed, (outcome, typed)
