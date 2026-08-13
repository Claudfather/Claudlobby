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

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import _scrubbed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "lib"
TASK_ID_RE = re.compile(r"^t-[0-9]+-[0-9a-f]{4}$")


def _fake_lib(tmp_path: Path) -> tuple[Path, dict]:
    """Real dispatch-task.sh, stubbed transport — same shape as
    tests/test_task_id_dispatch.py::_fake_lib, which owns the rationale."""
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for name in ("dispatch-task.sh", "lib-common.sh"):
        (libdir / name).symlink_to(LIB_DIR / name)
    stub = libdir / "dispatch.sh"
    stub.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "$2" > "{tmp_path}/sent.txt"\n')
    stub.chmod(0o755)
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/bash\nexit 0\n")
    tmux.chmod(0o755)
    return libdir, {
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "TMUX_BIN": str(tmux),
        "OBSERVABILITY_DISPATCH_DEADLINE": "600",
        "BOT_ID": "lead",
    }


def _run(tmp_path: Path, args: str):
    libdir, env = _fake_lib(tmp_path)
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
        assert "| query |" in sent, sent

    # Parametrized rather than looped: each case gets its own tmp_path and its
    # own name in the failure summary, which is what the regression-diff recipe
    # in CLAUDE.md actually reads.
    @pytest.mark.parametrize("t", ["cancel", "compact", "restart", "query"])
    def test_the_type_reaches_the_envelope(self, tmp_path, t):
        # Before #1187 the type was hardcoded `task` at the emit site, so every
        # message a manager sent claimed to be a task whatever it was.
        _r, _row, sent = _run(tmp_path, f'--type {t} w1 "x"')
        assert f"| {t} |" in sent, (t, sent)


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
        r, _row, _sent = _run(tmp_path, '--type w1 "x"')
        # `--type w1` consumes w1 as the value, leaving an unknown type; either
        # way it must fail rather than send something unintended.
        assert r.returncode != 0, r.stdout + r.stderr


class TestVocabularyMatchesTheProtocols:
    """PROSE IS THE SOURCE — the bots read the protocol, not the script.

    The type vocabulary lives in two library docs and had no code-side
    definition before this change. Adding one creates a second source, so it is
    reconciled here rather than left to drift: the estate has already been bitten
    by a shared vocabulary that nothing checks.
    """

    def _script_types(self) -> set[str]:
        line = next(
            ln
            for ln in (LIB_DIR / "dispatch-task.sh").read_text().splitlines()
            if ln.startswith("DISPATCH_TYPES=")
        )
        return set(line.split("=", 1)[1].strip().strip('"').split())

    def test_matches_worker_lifecycle(self):
        txt = (REPO_ROOT / "library/protocols/worker-lifecycle.md").read_text()
        line = next(ln for ln in txt.splitlines() if ln.startswith("**Types:**"))
        doc = set(re.findall(r"`([a-z]+)`", line))
        assert doc, "parser found no types — the doc's shape changed, fix the parser"
        assert self._script_types() == doc, (self._script_types(), doc)

    def test_matches_dispatch_protocol_table(self):
        txt = (REPO_ROOT / "library/protocols/dispatch.md").read_text()
        doc = set(re.findall(r"^\| `([a-z]+)` \| ", txt, re.M))
        assert doc, "parser found no table rows — the doc's shape changed"
        assert self._script_types() == doc, (self._script_types(), doc)
