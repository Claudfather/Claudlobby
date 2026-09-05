"""#1187 upstream — the envelope format and the tracking are separate decisions.

`--botcommand` alone used to mint. So a manager who wanted the fleet message
format for a peer note — a finding, a relay, a retraction — got a tracked row as
a side effect, and because nothing had been asked of the recipient, nothing could
ever close it. Measured before the fix: 68 open rows on this host, 57 of them
addressed to managers.

`--type` splits the two. Only `task` mints.

THE INVARIANT EVERY TEST HERE SERVES: a misclassification must degrade to
UNTRACKED, never to UNCLOSABLE. Calling a real task a `query` costs an id-less
row, which is exactly what every raw-text send already is. The reverse costs a
row nobody can close, which is the defect.

Sibling: tests/test_task_id_dispatch.py owns the mint itself and the id'd round
trip; this file owns only what the type gates.

F18 closure (R1): the door writes NO ledger — the plane is the only record — so
every "row" below is the dispatch as the plane holds it (the sibling's
`plane_dispatch_row`, in the retired row's field names), and the round trip
seeds its open task through the REAL door rather than a hand-written ledger
line.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import _scrubbed_env
from tests.test_task_id_dispatch import FLEET, TASK_ID_RE, _fake_lib, plane_dispatch_row

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "lib"


def _run(tmp_path: Path, args: str):
    # The sibling's harness, not a third copy of it. It already takes the one
    # thing that differs — the transport stub — as a parameter, and the copies
    # have a real cost: none of them symlinks `dispatch-supersede-hint.py`, and
    # that call site is `2>/dev/null || true`, so a harness missing it is a
    # silent no-op rather than an error. One more copy is one more place that
    # stays green while testing less than it appears to.
    libdir, env = _fake_lib(
        tmp_path, f'#!/bin/bash\nprintf \'%s\\n\' "$2" > "{tmp_path}/sent.txt"\n'
    )
    r = subprocess.run(
        ["bash", "-c", f'"{libdir}/dispatch-task.sh" {args}'],
        capture_output=True,
        text=True,
        env=_scrubbed_env(**env),
        timeout=60,
    )
    # The dispatch as the PLANE holds it (None when nothing was recorded).
    row = plane_dispatch_row(tmp_path)
    sent_f = tmp_path / "sent.txt"
    return r, row, (sent_f.read_text() if sent_f.exists() else "")


class TestOnlyTaskMints:
    def test_task_type_still_mints(self, tmp_path):
        # The half that must NOT change. Every existing id'd call site routes
        # through here; if this breaks, tracked dispatch is gone.
        r, row, sent = _run(tmp_path, '--type task w1 "fix the widget"')
        assert r.returncode == 0, r.stderr
        assert TASK_ID_RE.match(row["task_id"]), row
        assert f"task:{row['task_id']}" in sent

    def test_query_does_not_mint_but_still_sends_and_still_ledgers(self, tmp_path):
        # Three assertions because three things could regress independently:
        # the id must be absent, the message must STILL be delivered, and the
        # row must STILL be written. A fix that stopped minting by dropping the
        # send or the row would pass a weaker test and lose the audit trail.
        r, row, sent = _run(tmp_path, '--type query w1 "did the sweep run?"')
        assert r.returncode == 0, r.stderr
        assert row is not None, "a non-task send must still be recorded on the plane"
        assert row["task_id"] == "", row
        assert "did the sweep run?" in sent, "the message must still be delivered"

    def test_a_non_task_envelope_carries_no_empty_task_field(self, tmp_path):
        # An unguarded append emits a bare `| task:` — a field with no value,
        # which reads as a truncated message rather than a deliberate absence,
        # and which a worker would echo back as nothing.
        _r, _row, sent = _run(tmp_path, '--type query w1 "ping"')
        assert "task:" not in sent, f"empty task field transmitted: {sent!r}"

    # Parametrized rather than looped: each case gets its own tmp_path and its
    # own name in the failure summary, which is what the regression-diff recipe
    # in CLAUDE.md actually reads.
    @pytest.mark.parametrize("t", ["cancel", "compact", "restart", "query"])
    def test_the_type_reaches_the_envelope(self, tmp_path, t):
        # Before #1187 the type was hardcoded `task` at the emit site, so every
        # message a manager sent claimed to be a task whatever it was.
        _r, _row, sent = _run(tmp_path, f'--type {t} w1 "x"')
        assert f"| {t} |" in sent, (t, sent)


class TestTheDeadlineIsGatedToo:
    """Withholding the id alone was half a fix, and the first version shipped it.

    `dispatch-overdue.py` matches on `expected_by`, NOT on the id. So an id-less
    row with a deadline still goes overdue and still pushes a `[FLEET-PULSE]`
    alert — and because `overdue_ids` drops the empty id, that alert says a task
    is late and names nothing. Measured before the second half landed:
    `vera <at> <by> 100 -`. Harder to diagnose than the row this change set out
    to stop minting.
    """

    def test_a_non_task_row_records_no_deadline(self, tmp_path):
        _r, row, _sent = _run(tmp_path, '--type query w1 "peer note"')
        assert row["expected_by"] is None, row

    def test_a_task_row_still_records_one(self, tmp_path):
        _r, row, _sent = _run(tmp_path, '--type task w1 "real work"')
        assert isinstance(row["expected_by"], int), row

    def test_a_raw_text_send_keeps_its_deadline(self, tmp_path):
        # The gate is the TYPE, never the emptiness of the id. Raw sends are
        # id-less too but are matched by bot+time on purpose — documented
        # behaviour and a live call pattern, so gating on id-lessness would have
        # silently untracked them.
        _r, row, _sent = _run(tmp_path, 'w1 "just a note"')
        assert row["task_id"] == "", row
        assert isinstance(row["expected_by"], int), row

    def test_the_watchdog_does_not_page_a_non_task_row(self, tmp_path):
        # End to end through the REAL matcher, because the unit above asserts a
        # field while the harm is an alert. The plane's overdue reader skips an
        # assignment with no deadline; this pins that the two agree — with a
        # POSITIVE CONTROL beside it (a task dispatched in the same run pages
        # at the same far-future instant), so a matcher that paged nothing at
        # all could not pass.
        import subprocess as sp
        import time

        _r, note, _sent = _run(tmp_path, '--type query w1 "peer note"')
        _r, task, _sent = _run(tmp_path, '--type task w1 "real work"')
        assert note["expected_by"] is None and isinstance(task["expected_by"], int)
        out = sp.run(
            [
                "python3", str(LIB_DIR / "dispatch-overdue.py"), "--all",
                # an hour later: the task's 600s deadline has passed, and the
                # dispatch is still inside the matcher's expiry cap
                # (DISPATCH_OVERDUE_MAX_AGE_S, 24h) — a day later it would be
                # EXPIRED rather than overdue, and page nothing for that reason
                str(int(time.time()) + 3600),
                "--fleet", FLEET, "--root", str(tmp_path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1 and task["task_id"] in lines[0], (
            f"expected exactly the task to page; got {out.stdout!r}"
        )
        assert not any(ln.rstrip().endswith(" -") for ln in lines), (
            f"a peer note paged the manager: {out.stdout!r}"
        )


class TestNoSilentChangeToExistingCallSites:
    """The constraint dara set: existing id-minting callers must be untouched."""

    def test_botcommand_alone_still_mints(self, tmp_path):
        r, row, sent = _run(tmp_path, '--botcommand w1 "fix the widget"')
        assert r.returncode == 0, r.stderr
        assert TASK_ID_RE.match(row["task_id"]), (
            "--botcommand with no --type must behave exactly as before"
        )
        assert f"task:{row['task_id']}" in sent

    @pytest.mark.parametrize(
        "flag", ["--repo r", "--priority high", "--ref http://x", "--workstream w"]
    )
    def test_envelope_field_flags_still_mint_without_botcommand(self, tmp_path, flag):
        _r, row, sent = _run(tmp_path, f'{flag} w1 "x"')
        assert TASK_ID_RE.match(row["task_id"]), (flag, row)
        assert f"task:{row['task_id']}" in sent, flag

    def test_a_raw_text_send_is_still_id_less_and_unenveloped(self, tmp_path):
        _r, row, sent = _run(tmp_path, 'w1 "just a note"')
        assert row["task_id"] == "", row
        assert "[BOTCOMMAND]" not in sent, sent


class TestUnknownTypeFailsLoud:
    def test_a_typo_is_refused_rather_than_defaulted(self, tmp_path):
        # THE FAILURE THIS FLAG EXISTS TO PREVENT, one layer up. Falling back to
        # `task` would mint a tracked row for a message that asks nothing and
        # give the caller no signal — the exact defect, re-created inside its fix.
        r, row, sent = _run(tmp_path, '--type quiery w1 "x"')
        assert r.returncode != 0, "unknown type must not be accepted"
        assert "unknown --type" in r.stderr, r.stderr
        assert row is None, "a refused dispatch must not be recorded"
        assert sent == "", "a refused dispatch must not be transmitted"

    def test_missing_type_value_is_a_loud_error(self, tmp_path):
        # `--type` with NOTHING after it — the `_flag_val` guard. Deliberately
        # not `--type w1 "x"`: that consumes w1 as the value and lands on the
        # unknown-type branch above, so it would restate that test with weaker
        # assertions while leaving this guard unexercised.
        r, _row, _sent = _run(tmp_path, "--type")
        assert r.returncode != 0, r.stdout + r.stderr
        assert "needs a value" in r.stderr, r.stderr


class TestVocabularyMatchesTheProtocols:
    """PROSE IS THE SOURCE — the bots read the protocol, not the script.

    The type vocabulary lives in two library docs and had no code-side
    definition before this change. Adding one creates a second source, so it is
    reconciled here rather than left to drift: the estate has already been bitten
    by a shared vocabulary that nothing checks.
    """

    def _script_types(self) -> set[str]:
        line = next(
            (
                ln
                for ln in (LIB_DIR / "dispatch-task.sh").read_text().splitlines()
                if ln.startswith("DISPATCH_TYPES=")
            ),
            None,
        )
        assert line, (
            "no DISPATCH_TYPES= line in dispatch-task.sh — it moved or was renamed"
        )
        return set(line.split("=", 1)[1].strip().strip('"').split())

    def test_matches_worker_lifecycle(self):
        txt = (REPO_ROOT / "library/protocols/worker-lifecycle.md").read_text()
        line = next(
            (ln for ln in txt.splitlines() if ln.startswith("**Types:**")), None
        )
        assert line, "no '**Types:**' line in worker-lifecycle.md — the anchor moved"
        doc = set(re.findall(r"`([a-z]+)`", line))
        assert doc, "parser found no types — the doc's shape changed, fix the parser"
        assert self._script_types() == doc, (self._script_types(), doc)

    def test_matches_dispatch_protocol_table(self):
        txt = (REPO_ROOT / "library/protocols/dispatch.md").read_text()
        doc = set(re.findall(r"^\|\s*`([a-z]+)`\s*\|", txt, re.M))
        assert doc, "parser found no table rows — the doc's shape changed"
        assert self._script_types() == doc, (self._script_types(), doc)


# --- the round trip ---------------------------------------------------------
#
# Everything above stops at the dispatch side: it proves the envelope withholds
# the id, and nothing more. The property the design actually rests on is what
# happens NEXT — and it was false. `worker-lifecycle` routes a `query` to Step 8
# (line 66 -> line 51), so a compliant worker DOES file a terminal report; with
# no id to echo it falls into report-back.sh's #835 resolver, which stamps the
# bot's OLDEST open id'd dispatch. A peer note silently closed unrelated
# in-progress work as `completed`.
#
# These tests fail against the tree that shipped ask 1, which is the point.


def _roundtrip_lib(tmp_path: Path):
    """`_fake_lib` plus the receive side. Same harness, one more leg.

    report-back.sh and dispatch-overdue.py are what the resolver needs (both
    in the sibling's DOOR_FILES); without them report-back's lookup fails open
    and every arm reads clean — a harness that cannot see the defect it was
    written for. The resolver answers from the PLANE, the only source (F18
    R2a): no ledger, no flag, no declaration — the fleet rides the FLEET_NAME
    the harness env already carries.
    """
    libdir, env = _fake_lib(
        tmp_path, f'#!/bin/bash\nprintf \'%s\\n\' "$2" > "{tmp_path}/sent.txt"\n'
    )
    env["MANAGER_TMUX"] = "lead"
    (tmp_path / "state").mkdir(exist_ok=True)
    return libdir, env


def _seed_open_task(libdir: Path, tmp_path: Path, env: dict, bot: str = "w1") -> str:
    """One real, in-progress, id'd dispatch this bot is already holding —
    through the REAL door, so it is the plane's oldest open id'd row. Returns
    the minted id."""
    r = subprocess.run(
        ["bash", "-c", f'"{libdir}/dispatch-task.sh" --type task {bot} "implement the thing"'],
        capture_output=True, text=True, env=_scrubbed_env(**env), timeout=60,
    )
    assert r.returncode == 0, r.stderr
    row = plane_dispatch_row(tmp_path)
    assert row and TASK_ID_RE.match(row["task_id"]), row
    return row["task_id"]


def _still_open(libdir: Path, tmp_path: Path, env: dict, bot: str = "w1") -> set[str]:
    out = subprocess.run(
        [
            "python3",
            str(libdir / "dispatch-overdue.py"),
            "--open",
            bot,
            "--fleet", FLEET, "--root", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=_scrubbed_env(**env),
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return set(re.findall(r"t-\d+-[0-9a-z]+", out.stdout))


def _door(libdir: Path, script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'"{libdir}/{script}'], capture_output=True, text=True,
                          env=_scrubbed_env(**env), timeout=60)


class TestANonTaskReportClosesNothingElse:
    @pytest.mark.parametrize("t", ["query", "cancel", "compact", "restart"])
    def test_terminal_report_for_a_non_task_leaves_the_real_row_open(self, tmp_path, t):
        # THE regression, all four types. `restart` and `cancel` route to a
        # terminal report the same way `query` does; the review measured query
        # and reasoned the rest, so they are measured here.
        libdir, env = _roundtrip_lib(tmp_path)
        real_id = _seed_open_task(libdir, tmp_path, env)
        _door(libdir, f'dispatch-task.sh" --type {t} w1 "a peer note"', env)
        # A COMPLIANT worker: it echoes an id when it was given one, and it was
        # given none. This is the arm main was safe on, which is what makes it a
        # regression rather than a pre-existing class.
        rb = _door(libdir, 'report-back.sh" w1 completed "answered inline"', env)
        assert rb.returncode == 0, rb.stderr
        assert real_id in _still_open(libdir, tmp_path, env), (
            f"a `{t}` peer note closed unrelated in-progress work as completed"
        )

    def test_a_raw_text_dispatch_does_not_close_it_either(self, tmp_path):
        # Not introduced by --type: the same silent close is reachable on main
        # through a bare raw-text send, which carries no envelope at all. That
        # is why the guard lives in the resolver and not in the envelope — a
        # transmit-side marker has nowhere to go on this path.
        libdir, env = _roundtrip_lib(tmp_path)
        real_id = _seed_open_task(libdir, tmp_path, env)
        _door(libdir, 'dispatch-task.sh" w1 "a peer note"', env)
        _door(libdir, 'report-back.sh" w1 completed "answered inline"', env)
        assert real_id in _still_open(libdir, tmp_path, env)

    def test_an_id_dispatch_still_resolves_without_an_echo(self, tmp_path):
        # THE POSITIVE CONTROL, and it is what stops the fix being "never
        # resolve". #835 exists because workers routinely omit the id; a guard
        # that suppressed unconditionally would pass every test above while
        # silently reverting it. Here the latest dispatch IS id'd, so the
        # resolver must still fire and close that row.
        libdir, env = _roundtrip_lib(tmp_path)
        real_id = _seed_open_task(libdir, tmp_path, env)
        _door(libdir, 'dispatch-task.sh" --type task w1 "real work"', env)
        _door(libdir, 'report-back.sh" w1 completed "did the work"', env)
        # FIFO closes the OLDEST open row — documented #835 behaviour, unchanged.
        assert real_id not in _still_open(libdir, tmp_path, env), (
            "the resolver stopped firing for id'd dispatches — #835 reverted"
        )

    def test_a_later_report_resolves_again_once_the_note_is_discharged(self, tmp_path):
        # The suppression is scoped to an UNANSWERED note, not to the bot. A
        # single peer note must not strand every later report for the rest of
        # the session — that would trade one silent-close bug for a permanent
        # #835 outage, which is the same defect wearing the other coat.
        libdir, env = _roundtrip_lib(tmp_path)
        real_id = _seed_open_task(libdir, tmp_path, env)
        _door(libdir, 'dispatch-task.sh" --type query w1 "a peer note"', env)
        for _ in range(2):
            _door(libdir, 'report-back.sh" w1 completed "r"', env)
        assert real_id not in _still_open(libdir, tmp_path, env), (
            "the note was already answered by the first report; the second "
            "should resolve normally"
        )
