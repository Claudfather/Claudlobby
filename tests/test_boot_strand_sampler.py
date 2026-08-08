"""#843 boot-strand sampler — pytest wrapper for lib/boot-strand-sampler.sh.

Two tiers, mirroring tests/test_freshbox_boot_harness.py:

1. Hermetic (always on): the sampler's own classifier mapping against the
   committed #837 pane fixtures — the fixture GEOMETRY is lib-common's
   contract, pinned by tests/test_pane_send_verified.sh; here only the
   sampler's verdict mapping over that predicate is under test, including the
   #843 transcript-echo trap — plus the stdlib summary module: exact
   Clopper-Pearson values, counts, the mechanism slice, and the
   machine-readable SAMPLER_RESULT contract.

2. Real-boot smoke (gated): BOOT_SAMPLER_REALBOOT=1 plus the heavy deps runs a
   1-boot sample against the real claude binary and asserts the
   SAMPLER_RESULT contract. The n=20 measurement run for #843 is an operator
   action, not a test.

3. #933 red-first reproducer (separately gated, EXPECTED RED on main):
   BOOT_SAMPLER_LOAD_REPRO=1 boots under synthetic CPU load and asserts that a
   stranded boot is never reported as a clean send. It has its own gate because
   it is deliberately failing and it loads a shared host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from tests.conftest import (
    call_script_fn,
    constructed_env,
    load_lib_module,
    realboot_skip_reason,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLER = REPO_ROOT / "lib" / "boot-strand-sampler.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pane-states"

summary = load_lib_module("boot-strand-summary")

# A stranded STARTUP_PROMPT payload, passed in FULL exactly as pane_send_verified
# now passes it (#1082). It used to be truncated to 60 chars here, mirroring the
# retired `_PANE_PROBE_MAX_CHARS` cap; under reversed containment a truncated
# probe is actively wrong, because a rendered line longer than the prefix is not
# a substring of it and a genuinely-stranded boot reclassifies as no-evidence.
WRAPPED_PROBE = (
    "set +H; PROBE763WRAP You just started up. Read your CLAUDE.md, then post a "
    "brief ready message and wait for task assignments."
)


def _verdict(fixture: str, probe: str) -> str:
    pane = (FIXTURES / fixture).read_text(encoding="utf-8")
    return call_script_fn(SAMPLER, "final_verdict", pane, probe).strip()


def test_unknown_arg_exits_nonzero_with_usage():
    result = subprocess.run(
        ["bash", str(SAMPLER), "--no-such-flag"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "unknown arg" in result.stderr


class TestFinalVerdict:
    """The sampler's mapping over pane_holds_unsubmitted (predicate geometry
    is pinned in tests/test_pane_send_verified.sh, not re-tested here)."""

    def test_stuck_startup_prompt_is_strand(self):
        assert _verdict("input-stuck-wrapped.txt", WRAPPED_PROBE) == "strand"

    def test_transcript_echo_is_not_strand(self):
        # #843's trap: the submitted probe is still visible in the TRANSCRIPT
        # (with its own prompt glyph); last-glyph anchoring must not match it.
        assert (
            _verdict("input-clean-submit.txt", "PROBE763TRANSCRIPT reply ok")
            == "other:no_evidence"
        )


class TestClopperPearson:
    def test_zero_of_twenty_matches_closed_form(self):
        # k=0 upper bound has the closed form 1 - (alpha/2)^(1/n) = 0.1684.
        lo, hi = summary.cp_interval(0, 20)
        assert lo == 0.0
        assert abs(hi - (1 - 0.025 ** (1 / 20))) < 1e-6

    def test_baseline_two_of_four_spans_the_unit_line(self):
        # The #843 point: the n=4 baseline is known only to (0.068, 0.932).
        lo, hi = summary.cp_interval(2, 4)
        assert abs(lo - 0.0676) < 1e-3
        assert abs(hi - 0.9324) < 1e-3

    def test_degenerate_bounds(self):
        assert summary.cp_interval(20, 20)[1] == 1.0
        with pytest.raises(ValueError):
            summary.cp_interval(5, 4)


class TestSummarize:
    @staticmethod
    def _rows(*outcomes: str, warmup: str = "clean") -> list[dict]:
        rows = [{"i": 0, "kind": "warmup", "outcome": warmup, "retry_fired": 0}]
        rows += [
            {
                "i": n + 1,
                "kind": "sample",
                "outcome": o,
                "retry_fired": 0,
                "t_submit_s": 9,
            }
            for n, o in enumerate(outcomes)
        ]
        return rows

    def test_counts_and_machine_line(self):
        text, rc = summary.summarize(
            self._rows("clean", "clean", "strand", "other:session_died")
        )
        assert rc == 0
        assert "SAMPLER_RESULT strands=1 n=3" in text
        assert "other=1" in text
        assert "other:session_died" in text  # no silent truncation of others

    def test_warmup_excluded_from_sample(self):
        text, _ = summary.summarize(self._rows("clean", warmup="strand"))
        assert "SAMPLER_RESULT strands=0 n=1" in text

    def test_retry_save_reported(self):
        rows = self._rows("clean")
        rows[1]["retry_fired"] = 1
        text, _ = summary.summarize(rows)
        assert "retry_saves=1" in text

    def test_mechanism_slice_conditions_on_glyph(self):
        rows = self._rows("clean", "strand", "strand")
        rows[1]["glyph_at_inject"] = 1
        rows[2]["glyph_at_inject"] = 0
        rows[3]["glyph_at_inject"] = 0
        rows[3]["t_glyph_s"] = 14
        text, _ = summary.summarize(rows)
        assert "strand rate | box drawn at inject: 0/1" in text
        assert "strand rate | box NOT drawn at inject: 2/2" in text
        assert "input-box draw time (t_glyph): min 14s" in text

    def test_parity_histogram_surfaced(self):
        rows = self._rows("strand", "strand")
        rows[1]["parity_procs"] = "bun:1 node:2 "
        rows[2]["parity_procs"] = "bun:1 node:2 "
        text, _ = summary.summarize(rows)
        assert "per-boot process tree x2: bun:1 node:2" in text

    def test_zero_valid_boots_is_failure(self):
        _, rc = summary.summarize(self._rows("other:startbot_rc_1"))
        assert rc == 1

    def test_truncated_row_costs_one_row_not_the_sample(self, tmp_path):
        rows_file = tmp_path / "rows.jsonl"
        good = json.dumps(
            {"i": 1, "kind": "sample", "outcome": "strand", "retry_fired": 0}
        )
        rows_file.write_text(good + '\n{"i": 2, "kind": "sam', encoding="utf-8")
        loaded = summary.load_rows(str(rows_file))
        assert loaded == [json.loads(good)]


# ── gated real-boot smoke ─────────────────────────────────────────────────────

_skip = realboot_skip_reason("BOOT_SAMPLER_REALBOOT", extra_bins=("tmux",))


@pytest.mark.skipif(bool(_skip), reason=_skip)
def test_real_boot_smoke_one_boot():
    env = {**os.environ, "CLAUDLOBBY_SRC": str(REPO_ROOT)}
    result = subprocess.run(
        ["bash", str(SAMPLER), "-n", "1", "--deadline", "90"],
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"sampler smoke failed:\n{out}"
    line = [ln for ln in out.splitlines() if ln.startswith("SAMPLER_RESULT ")]
    assert line, f"no SAMPLER_RESULT contract line:\n{out}"
    fields = dict(kv.split("=", 1) for kv in line[0].split()[1:])
    assert fields["n"] == "1"
    assert json.loads(fields["strands"]) in (0, 1)


# ── #933 red-first reproducer ─────────────────────────────────────────────────
#
# EXPECTED TO FAIL on current main. That is the point: it states the contract
# pane_send_verified is supposed to hold, under the condition that actually
# occurs in production, and current code does not hold it.
#
# The contract: a boot classified `strand` means the payload was never
# submitted and is still sitting in a drawn input box. pane_send_verified saw
# that box and returned CLEAN anyway, emitting nothing. So for every stranded
# boot there must be some ledger evidence that the send path did not consider
# itself finished — today `retry_fired` is 0 for exactly those boots.
#
# Load is not a tuning parameter here, it is the reproduction condition:
# measured on the 4-core reference host, the idle arm submits and the loaded
# arm strands. Running this without --load samples the wrong condition and
# passes for the wrong reason, which is how the path shipped green.
#
# Separate opt-in from the smoke test above: it is deliberately red, it burns
# CPU on a shared host, and a run takes minutes per boot.
_load_skip = _skip or (
    ""
    if os.environ.get("BOOT_SAMPLER_LOAD_REPRO") == "1"
    else "set BOOT_SAMPLER_LOAD_REPRO=1 (red-first #933 reproducer; loads the host)"
)


@pytest.mark.skipif(bool(_load_skip), reason=_load_skip)
def test_strand_under_load_is_never_reported_as_a_clean_send():
    env = {**os.environ, "CLAUDLOBBY_SRC": str(REPO_ROOT)}
    burners = os.environ.get("BOOT_SAMPLER_LOAD_BURNERS", "20")
    result = subprocess.run(
        [
            "bash",
            str(SAMPLER),
            "-n",
            "6",
            "--deadline",
            "90",
            "--load",
            burners,
            "--keep",
        ],
        capture_output=True,
        text=True,
        timeout=3600,
        env=env,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"sampler failed:\n{out}"

    kept = [
        ln.split(": ", 1)[1]
        for ln in out.splitlines()
        if ln.startswith("kept artifacts")
    ]
    assert kept, f"no kept-artifacts path to read rows from:\n{out}"
    rows = [
        json.loads(ln)
        for ln in (Path(kept[0]) / "artifacts" / "rows.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    sample = [r for r in rows if r["kind"] == "sample"]
    strands = [r for r in sample if r["outcome"] == "strand"]

    # A run with no strands has not exercised the contract. Report that as a
    # skip rather than a pass: a green light off zero observations is the same
    # false all-clear #933 is about.
    if not strands:
        pytest.skip(
            "no strands at loadavg "
            f"{[r.get('loadavg_1m') for r in sample]} — condition not reproduced, "
            "nothing asserted (raise BOOT_SAMPLER_LOAD_BURNERS)"
        )

    silent = [r for r in strands if not r["retry_fired"]]
    assert not silent, (
        f"{len(silent)} of {len(strands)} stranded boots were reported as CLEAN sends "
        f"(retry_fired=0). loadavg at those boots: "
        f"{[r.get('loadavg_1m') for r in silent]}. "
        "pane_send_verified decided the box was empty and returned success on a "
        "payload that is still sitting in it."
    )


def _sampler_env(**overrides) -> dict[str, str]:
    """constructed_env (#846) plus the one variable the sampler needs at
    SOURCE time: HOST_CREDS interpolates ``${HOME}`` under ``set -u``. PANE_*
    absence is by construction, so an inherited dev-shell knob can never leak
    into an assertion."""
    return constructed_env(HOME="/nonexistent-sampler-home", **overrides)


class TestKnobPassthrough:
    """#843/#1084/#1109: the sampler must be able to VARY its measurement knobs.

    ``run_start_bot`` builds its child env with ``env -i`` and an explicit
    allowlist (#846) — the right shape, but a knob this harness exists to
    sweep has to be forwarded or every run is secretly the default, and a
    ladder prints a refutation the harness manufactured (#1084 the verify
    budget; #1109 the settle window that bounds repaint). These assert on the
    child ENVIRONMENT rather than on whether a boot looked different — the
    boot path is what a knob changes, so it cannot also be the oracle.
    """

    def _child_env_seen_by_start_bot(
        self, tmp_path: Path, overrides: dict[str, str]
    ) -> str:
        (tmp_path / "lib").mkdir(exist_ok=True)
        stub = tmp_path / "lib" / "start-bot.sh"
        stub.write_text("#!/usr/bin/env bash\nenv\n")
        stub.chmod(0o755)
        return call_script_fn(
            SAMPLER,
            "run_start_bot",
            "10",
            str(tmp_path),
            str(tmp_path / "botdir"),
            env=_sampler_env(**overrides),
        )

    def test_every_forwarded_knob_reaches_start_bot(self, tmp_path):
        # Generic over the list, so forwarding stays true for every knob the
        # list ever holds: a knob added to _FORWARDED_PANE_KNOBS without
        # reaching the child env fails here BY NAME — the lying-disclosure
        # drift (forwarded per the list, scrubbed per the env) cannot land.
        knobs = call_script_fn(
            SAMPLER, 'echo "$_FORWARDED_PANE_KNOBS"', env=_sampler_env()
        ).split()
        assert knobs, "forwarded-knob list is empty — sampler contract broken"
        for knob in knobs:
            out = self._child_env_seen_by_start_bot(tmp_path, {knob: "7"})
            assert f"{knob}=7" in out, f"{knob} did not reach the child env"

    def test_unset_knobs_do_not_leak_into_the_child_env(self, tmp_path):
        # Forwarding unconditionally would send EMPTY strings, shadowing the
        # lib-common defaults and reaching arithmetic tests as non-integers.
        # Absent is not the same as empty: with nothing set, NO pane knob may
        # appear in the child env at all.
        out = self._child_env_seen_by_start_bot(tmp_path, {})
        assert "PANE_" not in out


class TestKnobDisclosure:
    """#1109's load-bearing half: the output must disclose each pane knob's
    effective state, so a scrubbed or inert override is visible in the run
    artifact instead of silently measuring defaults — the #1084
    manufactured-refutation class, made output-visible.
    """

    def _disclosure(self, overrides: dict[str, str]) -> str:
        return call_script_fn(
            SAMPLER, "knob_disclosure", env=_sampler_env(**overrides)
        ).strip()

    def test_defaults_disclose_every_forwarded_knob(self):
        out = self._disclosure({})
        for knob in (
            "PANE_SEND_VERIFY_TICKS",
            "PANE_SEND_SETTLE_S",
            "PANE_READY_TICKS",
        ):
            assert knob in out
        assert "SCRUBBED" not in out

    def test_set_settle_discloses_value_as_forwarded(self):
        out = self._disclosure({"PANE_SEND_SETTLE_S": "0.9"})
        assert "PANE_SEND_SETTLE_S=0.9" in out
        assert "forwarded" in out

    def test_set_ready_discloses_inert(self):
        # start-bot.sh arms PANE_READY_TICKS itself at both injection call
        # sites, so a forwarded override cannot reach the measured path; the
        # disclosure must say so rather than let a ladder read as if it varied
        # something.
        out = self._disclosure({"PANE_READY_TICKS": "200"})
        assert "PANE_READY_TICKS=200" in out
        assert "INERT" in out

    def test_set_unforwarded_poll_knob_discloses_scrubbed(self):
        out = self._disclosure({"PANE_READY_POLL_S": "0.1"})
        assert "PANE_READY_POLL_S=0.1" in out
        assert "SCRUBBED" in out

    def test_set_unforwarded_recover_knob_discloses_scrubbed(self):
        out = self._disclosure({"PANE_RECOVER_TICKS": "99"})
        assert "PANE_RECOVER_TICKS=99" in out
        assert "SCRUBBED" in out

    def test_disclosure_prints_before_the_dependency_gates(self, tmp_path):
        # The line must land in the artifact even when the run aborts — an
        # aborted sample that silently omits its knob state is the same
        # silent-null one layer up. CLAUDE_BIN at a nonexistent path forces
        # the first precondition gate to SKIP (exit 2) hermetically; the
        # disclosure must already be on stdout by then.
        env = _sampler_env(
            PANE_SEND_SETTLE_S="0.9", CLAUDE_BIN=tmp_path / "no-such-claude"
        )
        r = subprocess.run(
            ["bash", str(SAMPLER), "-n", "1"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert "PANE_SEND_SETTLE_S=0.9" in r.stdout
        assert "forwarded" in r.stdout

    def test_knob_lists_match_lib_common_env_reads(self):
        # Currency pin: the disclosure lists are constants, so lib-common
        # growing a sixth pane knob must fail HERE rather than let the new
        # knob be scrubbed silently. The regex reads `${PANE_X...}` expansion
        # reads — a bare `$PANE_X` read would escape it, which is accepted as
        # the floor (lib-common uses braced reads for every current knob).
        lib_common = (REPO_ROOT / "lib" / "lib-common.sh").read_text(encoding="utf-8")
        actual = set(re.findall(r"\$\{(PANE_[A-Z0-9_]+)", lib_common))
        declared = set(
            call_script_fn(
                SAMPLER,
                'echo "$_FORWARDED_PANE_KNOBS $_UNFORWARDED_PANE_KNOBS"',
                env=_sampler_env(),
            ).split()
        )
        assert declared == actual

    def test_inert_label_matches_start_bot_arming(self):
        # The disclosure calls a forwarded PANE_READY_TICKS INERT because
        # start-bot.sh prefix-arms its own value at both pane_send_verified
        # call sites. If this pin fails, start-bot now honors an inherited
        # value — update knob_disclosure's INERT branch (and this pin), or the
        # disclosure starts lying in the safe-but-wrong direction.
        start_bot = (REPO_ROOT / "lib" / "start-bot.sh").read_text(encoding="utf-8")
        arming = re.findall(
            r'PANE_READY_TICKS="\$_PANE_READY_TICKS_BOOT"\s*\\\s*\n\s*pane_send_verified',
            start_bot,
        )
        assert len(arming) == 2, (
            "start-bot.sh no longer arms PANE_READY_TICKS unconditionally at "
            "exactly its two injection sites; re-derive the INERT disclosure."
        )
