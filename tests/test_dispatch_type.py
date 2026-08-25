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
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import _scrubbed_env
from tests.test_task_id_dispatch import TASK_ID_RE, _fake_lib

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
        timeout=10,
    )
    ledger = tmp_path / "state" / "dispatch-log.jsonl"
    row = (
        json.loads(ledger.read_text().splitlines()[-1])
        if ledger.exists() and ledger.read_text().strip()
        else None
    )
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
        assert row is not None, "a non-task send must still leave a ledger row"
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
        # field while the harm is an alert. A null deadline is skipped by
        # `_classify_all`; this pins that the two agree.
        import subprocess as sp
        import time

        _r, _row, _sent = _run(tmp_path, '--type query w1 "peer note"')
        ledger = tmp_path / "state" / "dispatch-log.jsonl"
        rows = [json.loads(x) for x in ledger.read_text().splitlines()]
        for d in rows:
            # Backdate BOTH. Backdating only `dispatched_at` leaves the deadline
            # in the future, so the row is not overdue and the assertion passes
            # whatever the gate does — the test would not operate. Caught by
            # mutation: removing the gate left this green.
            d["dispatched_at"] = int(time.time()) - 200
            if isinstance(d["expected_by"], int):
                d["expected_by"] = int(time.time()) - 100
        ledger.write_text("".join(json.dumps(d) + "\n" for d in rows))
        empty = tmp_path / "reports.jsonl"
        empty.write_text("")
        out = sp.run(
            [
                "python3",
                str(LIB_DIR / "dispatch-overdue.py"),
                "--all",
                str(ledger),
                str(empty),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", (
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
        assert row is None, "a refused dispatch must not write a ledger row"
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

    report-back.sh and dispatch-overdue.py are the two the resolver needs;
    without them report-back's lookup fails open and every arm reads clean —
    a harness that cannot see the defect it was written for.
    """
    libdir, env = _fake_lib(
        tmp_path, f'#!/bin/bash\nprintf \'%s\\n\' "$2" > "{tmp_path}/sent.txt"\n'
    )
    for name in ("report-back.sh", "dispatch-overdue.py"):
        (libdir / name).symlink_to(LIB_DIR / name)
    env["MANAGER_TMUX"] = "lead"
    return libdir, env


# RELATIVE TO NOW, never an absolute instant. An epoch pinned in a fixture is a
# time bomb by construction, and this one went off: the row was stamped
# 2026-08-13T10:00:00Z and silently aged past rotate_jsonl_by_ts's 7-day
# retention at 2026-08-20T10:00:00Z, reaping itself out of the ledger it seeds.
# The five tests below then asserted membership in an EMPTY set and went red on
# every branch in the repo at once, including main (#918).
#
# Bumping the number only sets a new fuse. The age is COMPUTED, so the row is
# an hour old on every run forever. It is also stamped ONCE here rather than
# per-call, so ts / dispatched_at / expected_by cannot disagree the way the
# hardcoded set did (its ts said 10:00:00Z while its dispatched_at said
# 05:46:40Z, four hours apart in the same row).
_SEED_AGE_S = 3600
_SEED_DISPATCHED_AT = int(time.time()) - _SEED_AGE_S
_SEED_TS = dt.datetime.fromtimestamp(_SEED_DISPATCHED_AT, dt.timezone.utc).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)
REAL_ID = f"t-{_SEED_DISPATCHED_AT}-real"


def _seed_open_task(tmp_path: Path, bot: str = "w1") -> Path:
    """One real, in-progress, id'd dispatch this bot is already holding."""
    ledger = tmp_path / "state" / "dispatch-log.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # COMPACT separators, deliberately. rotate_jsonl_by_ts splits on the literal
    # `"ts":"` and drops every line that does not yield a field (`NF>1`), so a
    # seed written with json.dumps' default `"ts": "..."` is silently REAPED by
    # the next shell write to the same ledger. The fixture then tests an empty
    # log and passes for the wrong reason.
    ledger.write_text(
        json.dumps(
            {
                "ts": _SEED_TS,
                "manager": "dara",
                "bot": bot,
                "task_id": REAL_ID,
                "workstream": "",
                "task": "implement the thing",
                "dispatched_at": _SEED_DISPATCHED_AT,
                "expected_by": _SEED_DISPATCHED_AT + 1800,
                "claudron_hits": "",
                "supersedes": "",
                "open_at_dispatch": 0,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return ledger


def _still_open(libdir: Path, tmp_path: Path, env: dict, bot: str = "w1") -> set[str]:
    out = subprocess.run(
        [
            "python3",
            str(libdir / "dispatch-overdue.py"),
            "--open",
            bot,
            str(tmp_path / "state" / "dispatch-log.jsonl"),
            str(tmp_path / "runtime" / "fleet" / "report-back.jsonl"),
        ],
        capture_output=True,
        text=True,
        env=_scrubbed_env(**env),
        timeout=10,
    ).stdout
    return set(re.findall(r"t-\d+-[0-9a-z]+", out))


class TestANonTaskReportClosesNothingElse:
    @pytest.mark.parametrize("t", ["query", "cancel", "compact", "restart"])
    def test_terminal_report_for_a_non_task_leaves_the_real_row_open(self, tmp_path, t):
        # THE regression, all four types. `restart` and `cancel` route to a
        # terminal report the same way `query` does; the review measured query
        # and reasoned the rest, so they are measured here.
        libdir, env = _roundtrip_lib(tmp_path)
        _seed_open_task(tmp_path)
        subprocess.run(
            ["bash", "-c", f'"{libdir}/dispatch-task.sh" --type {t} w1 "a peer note"'],
            capture_output=True,
            text=True,
            env=_scrubbed_env(**env),
            timeout=10,
        )
        # A COMPLIANT worker: it echoes an id when it was given one, and it was
        # given none. This is the arm main was safe on, which is what makes it a
        # regression rather than a pre-existing class.
        rb = subprocess.run(
            ["bash", "-c", f'"{libdir}/report-back.sh" w1 completed "answered inline"'],
            capture_output=True,
            text=True,
            env=_scrubbed_env(**env),
            timeout=10,
        )
        assert rb.returncode == 0, rb.stderr
        assert REAL_ID in _still_open(libdir, tmp_path, env), (
            f"a `{t}` peer note closed unrelated in-progress work as completed"
        )

    def test_a_raw_text_dispatch_does_not_close_it_either(self, tmp_path):
        # Not introduced by --type: the same silent close is reachable on main
        # through a bare raw-text send, which carries no envelope at all. That
        # is why the guard lives in the resolver and not in the envelope — a
        # transmit-side marker has nowhere to go on this path.
        libdir, env = _roundtrip_lib(tmp_path)
        _seed_open_task(tmp_path)
        subprocess.run(
            ["bash", "-c", f'"{libdir}/dispatch-task.sh" w1 "a peer note"'],
            capture_output=True,
            text=True,
            env=_scrubbed_env(**env),
            timeout=10,
        )
        subprocess.run(
            ["bash", "-c", f'"{libdir}/report-back.sh" w1 completed "answered inline"'],
            capture_output=True,
            text=True,
            env=_scrubbed_env(**env),
            timeout=10,
        )
        assert REAL_ID in _still_open(libdir, tmp_path, env)

    def test_an_id_dispatch_still_resolves_without_an_echo(self, tmp_path):
        # THE POSITIVE CONTROL, and it is what stops the fix being "never
        # resolve". #835 exists because workers routinely omit the id; a guard
        # that suppressed unconditionally would pass every test above while
        # silently reverting it. Here the latest dispatch IS id'd, so the
        # resolver must still fire and close that row.
        libdir, env = _roundtrip_lib(tmp_path)
        _seed_open_task(tmp_path)
        subprocess.run(
            ["bash", "-c", f'"{libdir}/dispatch-task.sh" --type task w1 "real work"'],
            capture_output=True,
            text=True,
            env=_scrubbed_env(**env),
            timeout=10,
        )
        subprocess.run(
            ["bash", "-c", f'"{libdir}/report-back.sh" w1 completed "did the work"'],
            capture_output=True,
            text=True,
            env=_scrubbed_env(**env),
            timeout=10,
        )
        # FIFO closes the OLDEST open row — documented #835 behaviour, unchanged.
        assert REAL_ID not in _still_open(libdir, tmp_path, env), (
            "the resolver stopped firing for id'd dispatches — #835 reverted"
        )

    def test_a_later_report_resolves_again_once_the_note_is_discharged(self, tmp_path):
        # The suppression is scoped to an UNANSWERED note, not to the bot. A
        # single peer note must not strand every later report for the rest of
        # the session — that would trade one silent-close bug for a permanent
        # #835 outage, which is the same defect wearing the other coat.
        libdir, env = _roundtrip_lib(tmp_path)
        _seed_open_task(tmp_path)
        subprocess.run(
            [
                "bash",
                "-c",
                f'"{libdir}/dispatch-task.sh" --type query w1 "a peer note"',
            ],
            capture_output=True,
            text=True,
            env=_scrubbed_env(**env),
            timeout=10,
        )
        for _ in range(2):
            subprocess.run(
                ["bash", "-c", f'"{libdir}/report-back.sh" w1 completed "r"'],
                capture_output=True,
                text=True,
                env=_scrubbed_env(**env),
                timeout=10,
            )
        assert REAL_ID not in _still_open(libdir, tmp_path, env), (
            "the note was already answered by the first report; the second "
            "should resolve normally"
        )
