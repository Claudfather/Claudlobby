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
            out = _child_env_seen_by_start_bot(tmp_path, {knob: "7"})
            assert f"{knob}=7" in out, f"{knob} did not reach the child env"

    def test_unset_knobs_do_not_leak_into_the_child_env(self, tmp_path):
        # Forwarding unconditionally would send EMPTY strings, shadowing the
        # lib-common defaults and reaching arithmetic tests as non-integers.
        # Absent is not the same as empty: with nothing set, NO pane knob may
        # appear in the child env at all.
        out = _child_env_seen_by_start_bot(tmp_path, {})
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


# ── #843 arm identity: the row must evidence its own IV ───────────────────────
#
# The blocker these close: a settle=0.3 row and a settle=6.0 row were
# byte-identical in shape, so arm identity lived only in a filename. A
# mislabelled arm is undetectable from the artifact afterwards — the failure
# has no symptom, only a clean confident number nobody can audit. So the tests
# below assert on the RECORD the boot produces, never on a label a caller
# supplied, and the summarizer ones assert refusals BY NAME rather than on the
# absence of a rate (an empty report and a refused one read alike otherwise).


def _arm_record(**overrides) -> dict:
    return json.loads(
        call_script_fn(SAMPLER, "arm_knobs_json", env=_sampler_env(**overrides))
    )


def _lib_common() -> str:
    return (REPO_ROOT / "lib" / "lib-common.sh").read_text(encoding="utf-8")


def _lib_common_constants() -> dict[str, str]:
    """Every `_PANE_*=<number>` constant lib-common declares, by name."""
    return dict(
        re.findall(r"^(_PANE_[A-Z0-9_]+)=([0-9.]+)", _lib_common(), re.MULTILINE)
    )


def _forwarded_knobs() -> list[str]:
    return call_script_fn(
        SAMPLER, 'echo "$_FORWARDED_PANE_KNOBS"', env=_sampler_env()
    ).split()


def _child_env_seen_by_start_bot(tmp_path: Path, overrides: dict[str, str]) -> str:
    """The environment run_start_bot actually hands the child, via a start-bot
    stub that just prints it. Module-level because both the passthrough tests
    and the arm-record tests need it — the same env is the subject of one and
    the oracle of the other."""
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


class TestArmRecording:
    def test_every_forwarded_knob_has_a_declared_fate(self):
        # Currency pin, same shape as the knob-list pin above: a knob joining
        # _FORWARDED_PANE_KNOBS without a case in pane_knob_fate would be
        # recorded with no in-force value at all, and every row in that arm
        # would evidence one knob less than it forwards. Fails HERE by name.
        rec = _arm_record()
        knobs = _forwarded_knobs()
        assert set(rec) == set(knobs)
        unresolved = [k for k, v in rec.items() if v["src"] == "UNRESOLVED"]
        assert not unresolved, f"no fate declared for {unresolved} in pane_knob_fate"

    def test_arm_record_env_field_matches_the_child_env(self, tmp_path):
        # THE invariant that keeps the recorded arm honest. run_start_bot
        # decides what reaches the child; pane_knob_fate decides what the row
        # says reached it. They are two reads of the same variable, so they can
        # drift apart in a one-line edit — and a row claiming a knob it did not
        # forward is the mislabel, arriving through the door built to prevent
        # it. Generic over the list so a new knob is covered on arrival.
        for knob in _forwarded_knobs():
            child = _child_env_seen_by_start_bot(tmp_path, {knob: "7"})
            rec = _arm_record(**{knob: "7"})
            in_child = f"{knob}=7" in child
            assert (rec[knob]["env"] == "7") == in_child, (
                f"{knob}: arm record says env={rec[knob]['env']!r} but the child "
                f"env {'carries' if in_child else 'does not carry'} it"
            )

    def test_unset_knob_records_the_lib_common_default_not_a_local_literal(self):
        # The in-force value must track lib-common, or a default that moves
        # there leaves every historical row silently re-describing a condition
        # that changed. Read from lib-common source rather than hardcoded here,
        # so this pin cannot drift with the thing it pins.
        declared = _lib_common_constants()
        rec = _arm_record()
        assert rec["PANE_SEND_SETTLE_S"] == {
            "env": None,
            "v": float(declared["_PANE_SEND_SETTLE_DEFAULT"]),
            "src": "default",
        }
        assert rec["PANE_SEND_VERIFY_TICKS"]["v"] == int(
            declared["_PANE_SEND_VERIFY_TICKS_DEFAULT"]
        )

    def test_default_fate_tracks_the_constant_lib_common_ACTUALLY_READS(self):
        # The sibling test above pins the recorded value against the
        # constant's DEFINITION. That is not enough on its own, and the gap is
        # not theoretical: repoint lib-common's `${PANE_SEND_SETTLE_S:-...}`
        # read sites at a new constant while leaving the old one defined but
        # dead, and every boot runs at the new value while every row records
        # the old one — a mislabelled arm manufactured by the recorder, which
        # is the exact class this change exists to close. Verified by mutation:
        # the suite stayed fully green under that edit before this pin existed.
        #
        # So walk the read site, not the definition: for every knob whose fate
        # is `default`, find what lib-common falls back to and confirm the
        # recorded in-force value is THAT number. Generic over the fate table,
        # the same shape as the two currency pins above.
        text, consts = _lib_common(), _lib_common_constants()
        rec = _arm_record()
        checked = []
        for knob, got in rec.items():
            if got["src"] != "default":
                continue
            sites = set(re.findall(r"\$\{" + knob + r":-\$(\w+)\}", text))
            assert sites, (
                f"{knob} is recorded as `default` but lib-common has no "
                f"${{{knob}:-$CONST}} read site — the recorded value has no source"
            )
            assert len(sites) == 1, (
                f"{knob} falls back to {sorted(sites)} at different read sites; "
                "the arm record can only be true of one of them"
            )
            const = sites.pop()
            assert const in consts, f"{knob} falls back to undeclared ${const}"
            assert float(got["v"]) == float(consts[const]), (
                f"{knob}: rows record {got['v']}, but lib-common reads "
                f"${const}={consts[const]} — every boot ran at a value the "
                "artifact does not name"
            )
            checked.append(knob)
        assert checked, "no knob resolved to `default` — this pin checked nothing"

    def test_forwarded_settle_is_recorded_as_the_arm(self):
        assert _arm_record(PANE_SEND_SETTLE_S="1.5")["PANE_SEND_SETTLE_S"] == {
            "env": "1.5",
            "v": 1.5,
            "src": "forwarded",
        }

    def test_boot_armed_knob_records_the_value_in_force_not_the_override(self):
        # Why "what the caller set" is the wrong thing to record: start-bot
        # arms PANE_READY_TICKS itself at both injection sites, so a run
        # launched with 200 ran every boot at the boot-armed value. Recording
        # caller intent would label it a 200 arm — a mislabel manufactured by
        # the recorder, which is the exact class this record exists to close.
        armed = int(_lib_common_constants()["_PANE_READY_TICKS_BOOT"])
        rec = _arm_record(PANE_READY_TICKS="200")["PANE_READY_TICKS"]
        assert rec == {"env": "200", "v": armed, "src": "boot-armed"}
        assert rec["v"] != 200

    def test_settle_s_is_hoisted_from_the_arm_record_not_supplied_beside_it(self):
        # Two independently-supplied copies of one fact is how a row comes to
        # disagree with itself. The emitter derives the hoisted IV from $arm in
        # the same jq expression, so disagreement is unreachable at write time;
        # boot-strand-summary.py catches it at read time for rows edited after
        # the fact. This pins the write-time half.
        src = SAMPLER.read_text(encoding="utf-8")
        assert (
            'settle_s: ($arm | if . == null then null else .["PANE_SEND_SETTLE_S"].v end)'
            in src
        )
        assert "--arg settle" not in src, "settle_s must not have a second source"

    def test_disclosure_and_arm_record_agree_on_every_knob(self):
        # One resolution, two renderings. knob_disclosure is what a human reads
        # in the run log and arm_knobs is what the analysis reads; a run whose
        # log says one arm while its rows say another is unauditable in the
        # worst way, because both look authoritative.
        env = {"PANE_SEND_SETTLE_S": "6.0", "PANE_READY_TICKS": "200"}
        line = call_script_fn(SAMPLER, "knob_disclosure", env=_sampler_env(**env))
        rec = _arm_record(**env)
        assert "PANE_SEND_SETTLE_S=6.0(forwarded)" in line
        assert rec["PANE_SEND_SETTLE_S"]["v"] == 6.0
        assert f"start-bot-arms-{rec['PANE_READY_TICKS']['v']}" in line


# ── #843 arm grouping: the two-arm statistic, and the refusals guarding it ────


def _knobs(settle: float, ticks: int = 5) -> dict:
    return {
        "PANE_SEND_VERIFY_TICKS": {"env": None, "v": ticks, "src": "default"},
        "PANE_SEND_SETTLE_S": {"env": str(settle), "v": settle, "src": "forwarded"},
        "PANE_READY_TICKS": {"env": None, "v": 90, "src": "boot-armed"},
    }


def _armed_rows(
    settle: float, outcomes: str, ticks: int = 5, burners: int = 0, start: int = 1
) -> list[dict]:
    """One arm's sample rows. `outcomes` is a compact string: c=clean, s=strand."""
    return [
        {
            "i": start + n,
            "kind": "sample",
            "outcome": {"c": "clean", "s": "strand"}[ch],
            "retry_fired": 0,
            "t_submit_s": 9 if ch == "c" else None,
            "load_burners": burners,
            "arm_knobs": _knobs(settle, ticks),
            "settle_s": settle,
        }
        for n, ch in enumerate(outcomes)
    ]


def _paired_rows(
    spec: dict[float, str], ticks: int = 5, burners: int = 0, la: float = 12.0
) -> list[dict]:
    """Interleaved blocks: one boot per arm per block, arm order rotating.

    The counterpart to _armed_rows, which lays each arm out end to end — the
    arm-sequential shape v2 §4 BASELINE forbids by name. Any test asserting a
    BETWEEN-arm figure has to build its sample this way now, because the
    summarizer withholds that figure from a sample it cannot see alternating.
    `spec` maps arm -> outcome string; one character per block, equal lengths.
    """
    arms = list(spec)
    lengths = {len(v) for v in spec.values()}
    assert len(lengths) == 1, f"one outcome per arm per block, got {lengths}"
    rows: list[dict] = []
    i = 1
    for b in range(lengths.pop()):
        order = arms[b % len(arms) :] + arms[: b % len(arms)]
        for pos, arm in enumerate(order):
            ch = spec[arm][b]
            rows.append(
                {
                    "i": i,
                    "kind": "sample",
                    "outcome": {"c": "clean", "s": "strand"}[ch],
                    "retry_fired": 0,
                    "t_submit_s": 9 if ch == "c" else None,
                    "load_burners": burners,
                    "loadavg_1m": la,
                    "arm_knobs": _knobs(arm, ticks),
                    "settle_s": arm,
                    "block": b,
                    "pos": pos,
                    "arm_order": list(order),
                    "arm_seed": 7,
                }
            )
            i += 1
    return rows


class TestArmGrouping:
    """The other half of the blocker: with no arm grouping, summarize() was
    single-arm only, so no two-arm figure was reachable and a real run could
    only ever return INCONCLUSIVE. Pooling the arms instead would have been
    worse than refusing — it averages the independent variable away and reports
    the result as a measurement.
    """

    def test_arms_are_reported_separately_never_pooled(self):
        text, rc = summary.summarize(
            _armed_rows(0.3, "sssscccccc") + _armed_rows(1.5, "sccccccccc", start=11)
        )
        assert rc == 0
        assert "── arm settle_s=0.3" in text
        assert "── arm settle_s=1.5" in text
        assert "strand rate: 4/10" in text and "strand rate: 1/10" in text
        # The pooled rate (5/20) must appear nowhere: it is the answer this
        # grouping exists to stop being given.
        assert "5/20" not in text

    def test_machine_line_per_arm_carries_its_arm(self):
        text, _ = summary.summarize(
            _armed_rows(0.3, "sscccc") + _armed_rows(6.0, "cccccc", start=11)
        )
        lines = [ln for ln in text.splitlines() if ln.startswith("SAMPLER_RESULT ")]
        assert len(lines) == 2
        assert "strands=2 n=6" in lines[0] and "settle_s=0.3" in lines[0]
        assert "strands=0 n=6" in lines[1] and "settle_s=6" in lines[1]

    def test_difference_interval_names_its_method_and_says_it_is_unratified(self):
        # Paired, because a between-arm figure is only reachable from a paired
        # sample now — the arm-sequential build this test used to pass has its
        # difference withheld (TestPairing).
        text, _ = summary.summarize(
            _paired_rows({0.3: "s" * 30 + "c" * 10, 1.5: "s" * 4 + "c" * 36})
        )
        assert "── between-arm (control - treatment)" in text
        assert "MOVER" in text and "Clopper-Pearson" in text
        assert "UNRATIFIED" in text
        assert "0.3 - 1.5:" in text

    def test_no_verdict_is_emitted(self):
        # The pre-registration's bar is not ratified for the difference, and a
        # summarizer that declared SUPPORTED off an unratified statistic would
        # pre-empt the ratification (ab-recoverability-scorer.py precedent).
        text, _ = summary.summarize(_paired_rows({0.3: "s" * 10, 1.5: "c" * 10}))
        assert "NO VERDICT EMITTED" in text
        assert not [ln for ln in text.splitlines() if ln.startswith("VERDICT")]

    def test_stratifier_reports_both_strata_never_pooled(self):
        rows = _armed_rows(0.3, "sscc")
        rows[0]["retry_fired"] = 1
        text, _ = summary.summarize(rows)
        assert "strand rate | settle_s=0.3, retry_fired > 0: 1/1" in text
        assert "strand rate | settle_s=0.3, retry_fired == 0: 1/3" in text

    def test_control_arm_is_overridable_and_stated(self):
        rows = _paired_rows({0.3: "sscc", 1.5: "sccc"})
        default_text, _ = summary.summarize(rows)
        assert "control arm: settle_s=0.3 (smallest arm" in default_text
        chosen, _ = summary.summarize(rows, control=1.5)
        assert "control arm: settle_s=1.5" in chosen
        assert "1.5 - 0.3:" in chosen

    def test_absent_control_is_stated_not_silently_substituted(self):
        # Paired, so the absent control is the ONLY reason the between-arm
        # block is missing. Built arm-sequential, this test would pass for the
        # wrong reason — the pairing guard would have withheld it anyway.
        text, rc = summary.summarize(
            _paired_rows({0.3: "sscc", 1.5: "sccc"}), control=9.9
        )
        assert rc == 0
        assert "control 9.9 is not present in this sample" in text
        assert "── between-arm" not in text

    def test_load_burner_drift_between_arms_is_disclosed(self):
        text, _ = summary.summarize(
            _armed_rows(0.3, "sscc", burners=0)
            + _armed_rows(1.5, "sccc", burners=8, start=11)
        )
        assert "BETWEEN-ARM CONFOUND: load_burners is not constant across arms" in text

    def test_unlabelled_rows_report_one_arm_and_say_they_cannot_evidence_it(self):
        text, rc = summary.summarize(
            [
                {"i": 1, "kind": "sample", "outcome": "strand", "retry_fired": 0},
                {"i": 2, "kind": "sample", "outcome": "clean", "retry_fired": 0},
            ]
        )
        assert rc == 0
        assert "ARM: UNLABELLED" in text
        assert "cannot evidence the settle window it ran under" in text
        assert "── between-arm" not in text


class TestArmRefusals:
    """Every refusal is named individually and asserted BY NAME. A refusal that
    only manifested as a missing rate would be indistinguishable from an empty
    sample — and 'no rate printed' is exactly what a broken summarizer also
    looks like.
    """

    @staticmethod
    def _refusal(rows: list[dict]) -> str:
        text, rc = summary.summarize(rows)
        assert rc == summary.REFUSE_RC, f"expected refusal, got rc={rc}:\n{text}"
        # A refusal must not ALSO print a rate: half a report reads as a
        # measurement with a warning attached, which is how a caveat gets
        # skimmed past.
        assert "strand rate:" not in text
        assert "SAMPLER_RESULT" not in text
        return text

    def test_hoisted_iv_disagreeing_with_the_arm_record_is_refused(self):
        # The planted mislabel: the row says one arm, its own record says
        # another. Unreachable at write time (one jq expression), so its only
        # cause is an edit after the fact — which is precisely the tampering an
        # arm recorded in a filename could never have exposed.
        rows = _armed_rows(0.3, "scc")
        rows[1]["settle_s"] = 6.0
        assert "MISLABELLED-ARM/arm-disagrees-with-record" in self._refusal(rows)

    def test_covarying_second_knob_within_one_arm_is_refused(self):
        # Two runs at the same settle but different verify budgets concatenate
        # into a group that reads as one arm and is not one. The IV agreeing is
        # not the condition agreeing.
        rows = _armed_rows(0.3, "scc") + _armed_rows(0.3, "scc", ticks=250, start=11)
        text = self._refusal(rows)
        assert "MISLABELLED-ARM/covarying-knob" in text
        assert "PANE_SEND_VERIFY_TICKS" in text

    def test_mixed_labelled_and_unlabelled_is_refused(self):
        rows = _armed_rows(0.3, "scc")
        rows.append({"i": 99, "kind": "sample", "outcome": "strand", "retry_fired": 0})
        assert "MISLABELLED-ARM/mixed-labelled-and-unlabelled" in self._refusal(rows)

    def test_arm_record_missing_while_the_iv_is_present_is_refused(self):
        rows = _armed_rows(0.3, "scc")
        del rows[1]["arm_knobs"]
        assert "MISLABELLED-ARM/missing-arm-record" in self._refusal(rows)

    def test_iv_missing_from_the_arm_record_is_refused(self):
        rows = _armed_rows(0.3, "scc")
        del rows[1]["arm_knobs"]["PANE_SEND_SETTLE_S"]
        assert "MISLABELLED-ARM/missing-arm-record" in self._refusal(rows)

    def test_non_numeric_arm_is_refused(self):
        rows = _armed_rows(0.3, "scc")
        rows[1]["settle_s"] = "0.3"
        rows[1]["arm_knobs"]["PANE_SEND_SETTLE_S"]["v"] = "0.3"
        assert "MISLABELLED-ARM/non-numeric-arm" in self._refusal(rows)

    def test_boolean_arm_is_not_accepted_as_a_number(self):
        # bool is an int subclass in Python; a JSON true would otherwise group
        # as arm 1.0 and read as a legitimate ladder rung.
        rows = _armed_rows(0.3, "scc")
        rows[1]["settle_s"] = True
        rows[1]["arm_knobs"]["PANE_SEND_SETTLE_S"]["v"] = True
        assert "MISLABELLED-ARM/non-numeric-arm" in self._refusal(rows)

    def test_refusal_names_the_offending_boot(self):
        rows = _armed_rows(0.3, "sccccc")
        rows[3]["settle_s"] = 6.0
        assert "boot 4" in self._refusal(rows)


class TestPairing:
    """#843 blocker 6: the rows must EVIDENCE that arms alternated.

    `block` and `pos` are written by the same sampler loop that does the
    interleaving, so a test asserting only on them would check the claim
    against itself — the cannot-fail check this ladder exists to measure. Every
    mutation below therefore leaves the LABELS intact and changes the
    CONDITION, so nothing here can pass by reading a field that describes
    itself. The precedent is `_covarying_knobs`, which guards the condition
    rather than the label.
    """

    def test_paired_sample_earns_the_between_arm_figure(self):
        # Positive control FIRST, so every refusal below means something: the
        # instrument can still return a difference when the pairing is real.
        text, rc = summary.summarize(_paired_rows({0.3: "sscc", 1.5: "cccc"}))
        assert rc == 0
        assert "PAIRED: 4 complete block(s) x 2 arms" in text
        assert "── between-arm (control - treatment)" in text

    def test_sequential_arms_under_interleaved_labels_are_refused(self):
        # THE bound. Labels stay exactly as an interleaved run wrote them; only
        # the in-force arms are rewritten into arm-sequential order. A reader
        # trusting block/pos sees a textbook interleave. The refusal can only
        # come from the settle_s sequence.
        rows = _paired_rows({0.3: "cccc", 1.5: "cccc"})
        rows.sort(key=lambda r: r["i"])
        for n, r in enumerate(rows):
            arm = 0.3 if n < len(rows) // 2 else 1.5
            r["settle_s"] = arm
            r["arm_knobs"] = _knobs(arm)
        text, rc = summary.summarize(rows)
        assert rc == 3
        assert "UNPAIRED-SAMPLE/block-holds-duplicate-arm" in text
        # The labels really are still intact — otherwise this passes for the
        # wrong reason and proves nothing about deriving from the condition.
        assert all(r["block"] is not None and r["pos"] is not None for r in rows)
        assert [r["pos"] for r in rows] == [0, 1, 0, 1, 0, 1, 0, 1]

    def test_one_arm_per_block_is_caught_even_though_every_block_validates(self):
        # The shape a per-block DRIVER produces: it stamps block ids but does
        # not own the boot loop, so it can only put one arm in each block. Every
        # structural check passes — no duplicate arm, pos is a valid one-element
        # permutation, blocks are contiguous — and the run is still arm-sequential.
        # Only the in-force sequence sees it, which is why the block record
        # cannot be the evidence.
        rows = []
        for n, arm in enumerate([0.3, 0.3, 1.5, 1.5]):
            rows.append(
                {
                    "i": n + 1,
                    "kind": "sample",
                    "outcome": "clean",
                    "retry_fired": 0,
                    "loadavg_1m": 12.0,
                    "load_burners": 0,
                    "arm_knobs": _knobs(arm),
                    "settle_s": arm,
                    "block": n,
                    "pos": 0,
                }
            )
        text, rc = summary.summarize(rows)
        assert rc == 0
        assert "blocks are declared, but the in-force arms are contiguous by" in text
        assert "BETWEEN-ARM DIFFERENCE SUPPRESSED" in text

    def test_concatenated_single_arm_runs_get_no_between_arm_figure(self):
        # The ladder procedure the sampler header used to document: invoke once
        # per settle value, concatenate the rows files. §4 BASELINE forbids it
        # by name, and until now the summarizer reported a difference from it.
        text, rc = summary.summarize(
            _armed_rows(0.3, "sscc") + _armed_rows(1.5, "cccc", start=11)
        )
        assert rc == 0
        assert "── between-arm" not in text
        assert "BETWEEN-ARM DIFFERENCE SUPPRESSED" in text
        assert "arm-sequential" in text
        assert "no block record" in text

    def test_suppression_is_targeted_and_per_arm_rates_survive(self):
        # The confound is in the comparison, not the attribution: an
        # arm-sequential boot is perfectly attributable, so refusing the sample
        # would discard sound per-arm rates to punish an analysis nobody can
        # run from it anyway.
        text, rc = summary.summarize(
            _armed_rows(0.3, "sscc") + _armed_rows(1.5, "cccc", start=11)
        )
        assert rc == 0
        assert "strand rate: 2/4" in text and "strand rate: 0/4" in text
        assert "INCONCLUSIVE" in text

    def test_arm_run_count_is_reported_so_a_human_can_check_it_too(self):
        # The DISCRIMINATION is what must hold, not a magic number: with two
        # arms a rotating order puts the same arm either side of a block
        # boundary, so 4 interleaved blocks give 5 runs rather than 8. What
        # cannot vary is that interleaved sits strictly above the arm count
        # while arm-sequential sits exactly on it.
        paired = summary.summarize(_paired_rows({0.3: "cccc", 1.5: "cccc"}))[0]
        flat = summary.summarize(
            _armed_rows(0.3, "cccc") + _armed_rows(1.5, "cccc", start=11)
        )[0]

        def runs(text: str) -> int:
            line = next(ln for ln in text.splitlines() if "arm-runs observed" in ln)
            return int(line.split("attributable boots: ")[1].split()[0])

        assert runs(flat) == 2, "arm-sequential must sit exactly on the arm count"
        assert runs(paired) > 2, "interleaved must sit strictly above it"
        assert "arm-sequential would be 2" in paired and "would be 2" in flat

    def test_pos_not_a_permutation_is_refused(self):
        rows = _paired_rows({0.3: "cc", 1.5: "cc"})
        rows[1]["pos"] = 0  # block 0 now declares positions 0 and 0
        text, rc = summary.summarize(rows)
        assert rc == 3
        assert "UNPAIRED-SAMPLE/pos-not-a-permutation" in text

    def test_block_interleaved_with_another_block_is_refused(self):
        # A block whose boots are not contiguous in i did not run as one
        # pairing unit, so its within-block delta spans other blocks' time.
        rows = _paired_rows({0.3: "cc", 1.5: "cc"})
        rows[0]["i"], rows[3]["i"] = rows[3]["i"], rows[0]["i"]
        text, rc = summary.summarize(rows)
        assert rc == 3
        assert "UNPAIRED-SAMPLE/block-not-contiguous" in text

    def test_partial_block_is_excluded_and_disclosed_never_silent(self):
        rows = _paired_rows({0.3: "ccc", 1.5: "ccc"})
        del rows[-1]  # a run cut mid-block
        text, rc = summary.summarize(rows)
        assert rc == 0
        assert "1 block(s) hold fewer than one boot per arm" in text
        assert "PAIRED: 2 complete block(s)" in text

    def test_drift_band_discards_a_block_that_straddles_an_excursion(self):
        rows = _paired_rows({0.3: "cccc", 1.5: "cccc"})
        for r in rows:
            if r["block"] == 1:
                r["loadavg_1m"] = 10.0 if r["pos"] == 0 else 20.0  # ratio 2.0
        text, rc = summary.summarize(rows)
        assert rc == 0
        assert "1 of 4 block(s) discarded" in text
        assert "discarded blocks: [1]" in text

    def test_block_without_loadavg_is_excluded_not_assumed_to_pass(self):
        # A missing covariate cannot be SHOWN to sit inside the band. Keeping
        # it silently would let an unmeasured block pass as a measured one.
        rows = _paired_rows({0.3: "cccc", 1.5: "cccc"})
        for r in rows:
            if r["block"] == 2:
                r["loadavg_1m"] = None
        text, rc = summary.summarize(rows)
        assert rc == 0
        assert "1 block(s) carry no usable loadavg_1m" in text
        assert "not assumed to pass: [2]" in text

    def test_heavy_exclusion_is_named_an_inconclusive_condition(self):
        rows = _paired_rows({0.3: "cccc", 1.5: "cccc"})
        for r in rows:
            if r["block"] in (0, 1):
                r["loadavg_1m"] = 10.0 if r["pos"] == 0 else 20.0
        text, _ = summary.summarize(rows)
        assert "OVER 25% OF BLOCKS EXCLUDED" in text
        assert "INCONCLUSIVE" in text

    def test_estimator_gap_between_sections_4_and_5_is_flagged_not_resolved(self):
        # §4 BASELINE says aggregate within-block deltas; §5 pins MOVER over the
        # two marginal intervals. Picking one silently would be the author
        # choosing the bar, so the summary names the gap and computes the
        # pinned statistic.
        text, _ = summary.summarize(_paired_rows({0.3: "sscc", 1.5: "cccc"}))
        assert "ESTIMATOR GAP" in text
        assert "does not yet enter the interval" in text

    def test_no_verdict_note_stops_calling_the_band_uncomputed_once_it_runs(self):
        # Blocks made the drift band computable, so the note listing it as
        # uncomputed became false the moment a paired sample existed — a
        # disclosure that outlives its defect is its own untruth. The note is
        # reachable ONLY from a paired sample (comparison_block returns early
        # below two arms, and an unpaired sample never gets that far), which is
        # why it can state this outright instead of carrying a flag.
        paired = summary.summarize(_paired_rows({0.3: "sscc", 1.5: "cccc"}))[0]
        assert "The drift-exclusion band IS applied" in paired
        assert "neither of which is" not in paired
        for unpaired in (
            summary.summarize(_armed_rows(0.3, "sscc"))[0],
            summary.summarize(
                _armed_rows(0.3, "sscc") + _armed_rows(1.5, "cccc", start=11)
            )[0],
        ):
            assert "NO VERDICT EMITTED" not in unpaired

    def test_single_arm_sample_is_untouched_by_any_of_this(self):
        # Regression guard: pairing is meaningless with one arm, and the
        # pre-interleave behaviour has to survive byte-for-byte in that mode.
        text, rc = summary.summarize(_armed_rows(0.3, "sscc"))
        assert rc == 0
        assert "pairing" not in text.lower()
        assert "SUPPRESSED" not in text
        assert "strand rate: 2/4" in text


class TestBootPlan:
    """The sampler half: --arms builds interleaved blocks in ONE invocation.

    A per-block driver would pay a warm-up boot per invocation (6 boots for 3
    samples, turning the 123-boot matrix into ~240) and still could not
    randomize arm order WITHIN a block, because it does not own the boot loop —
    the same arm-sequential shape it was meant to fix, one level up.
    """

    @staticmethod
    def _plan(*args: str) -> list[list[str]]:
        # Trailing `true` is load-bearing: call_script_fn appends ` "$@"` to
        # the snippet, which would otherwise land on the printf and emit the
        # leftover argv as extra plan lines.
        out = call_script_fn(
            SAMPLER,
            '_LCG_STATE="$1"; shift; build_boot_plan "$@"; '
            'printf "%s\\n" "${_BOOT_PLAN[@]}"; true',
            *args,
            env=_sampler_env(),
        )
        return [ln.split("\x1f") for ln in out.splitlines()]

    def test_one_boot_per_arm_per_block_plus_one_warmup_for_the_run(self):
        plan = self._plan("7", "4", "0.3", "1.5", "6")
        assert len(plan) == 1 + 4 * 3
        assert plan[0][0] == "warmup"
        # ONE warm-up, not one per arm — that difference is the cost argument
        # for interleaving in the sampler rather than driving it per block.
        assert len([e for e in plan if e[0] == "warmup"]) == 1
        for block in range(4):
            rows = [e for e in plan[1:] if e[1] == str(block)]
            assert sorted(e[3] for e in rows) == ["0.3", "1.5", "6"]
            assert sorted(e[2] for e in rows) == ["0", "1", "2"]

    def test_same_seed_reproduces_the_arm_order(self):
        # An arm order nobody can restate is a covariate the artifact cannot
        # evidence, so the seed is recorded and has to actually replay.
        first = [e[3] for e in self._plan("11", "5", "0.3", "1.5", "6")]
        again = [e[3] for e in self._plan("11", "5", "0.3", "1.5", "6")]
        assert first == again
        other = [e[3] for e in self._plan("12", "5", "0.3", "1.5", "6")]
        assert other != first

    def test_blocks_do_not_all_share_one_permutation(self):
        # The subshell trap: `j=$(_lcg_next)` forks, so the advance would be
        # discarded and every block would get ONE fixed order while block/pos
        # still labelled the rows interleaved. Randomizing per block is what
        # §4 BASELINE asks for, and this is the assertion that would catch it
        # silently reverting to a single permutation.
        orders = {
            tuple(e[3] for e in self._plan("3", "12", "0.3", "1.5", "6")[1:][i : i + 3])
            for i in range(0, 36, 3)
        }
        assert len(orders) > 1, f"every block ran the same arm order: {orders}"

    def test_single_arm_plan_carries_no_arm_and_no_block(self):
        # Without --arms the caller owns the knob and the loop must not touch
        # it: an empty value would shadow the lib-common default and reach an
        # arithmetic test as a non-integer.
        plan = self._plan("0", "3")
        assert len(plan) == 4
        assert [e[0] for e in plan] == ["warmup", "sample", "sample", "sample"]
        assert all(e[1] == "" and e[2] == "" and e[3] == "" for e in plan)


class TestArmsRefusals:
    """--arms validation. Each refusal names the condition it caught, because a
    run that silently reinterprets its ladder is the artifact nobody can audit.
    """

    @staticmethod
    def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SAMPLER), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_arms_with_the_knob_also_set_is_refused_as_two_sources(self):
        r = self._run(["--arms", "0.3 1.5"], env=_sampler_env(PANE_SEND_SETTLE_S="0.9"))
        assert r.returncode == 1
        assert "One fact, two sources" in r.stdout + r.stderr

    def test_repeated_arm_is_refused(self):
        r = self._run(["--arms", "0.3 0.3 1.5"], env=_sampler_env())
        assert r.returncode == 1
        assert "repeats 0.3" in r.stdout + r.stderr

    def test_numerically_equal_arms_are_caught_as_a_repeat(self):
        # "6 6.0" is two arms as strings and one arm to the summarizer, which
        # would land as a block holding the same arm twice.
        r = self._run(["--arms", "6 6.0"], env=_sampler_env())
        assert r.returncode == 1
        assert "repeats 6" in r.stdout + r.stderr

    def test_single_arm_ladder_is_refused(self):
        r = self._run(["--arms", "1.5"], env=_sampler_env())
        assert r.returncode == 1
        assert "nothing to interleave" in r.stdout + r.stderr

    def test_non_numeric_arm_is_refused(self):
        r = self._run(["--arms", "0.3 abc"], env=_sampler_env())
        assert r.returncode == 1
        assert "bad --arms value: abc" in r.stdout + r.stderr


class TestMoverDifference:
    def test_matches_the_hand_computed_mover_combination(self):
        d, lo, hi = summary.mover_difference(7, 10, 2, 10)
        lo1, hi1 = summary.cp_interval(7, 10)
        lo2, hi2 = summary.cp_interval(2, 10)
        assert abs(d - 0.5) < 1e-12
        assert abs(lo - (0.5 - ((0.7 - lo1) ** 2 + (hi2 - 0.2) ** 2) ** 0.5)) < 1e-12
        assert abs(hi - (0.5 + ((hi1 - 0.7) ** 2 + (0.2 - lo2) ** 2) ** 0.5)) < 1e-12

    def test_defined_at_the_degenerate_ends(self):
        # k=0 and k=n are the arms a ceiling is meant to produce. A Wald
        # interval collapses to zero width there and reports a clean separation
        # that the data does not support; the exact inputs keep width.
        d, lo, hi = summary.mover_difference(20, 20, 0, 20)
        assert d == 1.0 and lo < 1.0 and hi > 1.0 - 1e-9

    def test_identical_arms_straddle_zero(self):
        _, lo, hi = summary.mover_difference(5, 20, 5, 20)
        assert lo < 0 < hi


class TestSummaryExitPropagation:
    """The bash tail must PROPAGATE the summarizer's exit status (#1141).

    `boot-strand-summary.py` refuses a sample it cannot attribute to arms with
    exit 3 rather than pooling it, and that refusal is the safety property of
    the whole arm-identity mechanism (#1139). It protects nothing unless a
    caller notices it: a swallowed exit 3 turns "not attributable, no rate
    printed" into a silent success — the precise failure the refusal exists to
    prevent, reintroduced one layer down.

    Reaching the tail through `main` requires every dependency gate plus a real
    boot loop, so before `emit_summary` was extracted this propagation was
    unreachable by any test and a mutation of it left the whole suite green.
    """

    @staticmethod
    def _run(tmp_path: Path, exit_code: int) -> subprocess.CompletedProcess:
        """Drive emit_summary against a stub summarizer that exits `exit_code`.

        LIB_DIR is overridden AFTER sourcing so the real python3 still runs the
        stub — the exit path under test is the shell's, not python's.
        """
        (tmp_path / "boot-strand-summary.py").write_text(
            f"import sys\nprint('stub summary')\nsys.exit({exit_code})\n",
            encoding="utf-8",
        )
        rows = tmp_path / "rows.jsonl"
        rows.write_text("", encoding="utf-8")
        return subprocess.run(
            [
                "bash",
                "-c",
                f'. "{SAMPLER}"; LIB_DIR="{tmp_path}"; emit_summary "$1"',
                "_",
                str(rows),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_refusal_exit_3_reaches_the_caller(self, tmp_path):
        """The load-bearing case: an unattributable sample must not read clean."""
        r = self._run(tmp_path, 3)
        assert r.returncode == 3, (
            f"summarizer refused with 3 but the tail exited {r.returncode} — "
            "a swallowed refusal is a silent all-clear"
        )

    def test_success_exit_0_reaches_the_caller(self, tmp_path):
        # Positive control: without this, a tail hardcoded to any non-zero
        # would pass the refusal test while failing every real run.
        assert self._run(tmp_path, 0).returncode == 0

    def test_measured_nothing_exit_1_is_not_collapsed_into_the_refusal(self, tmp_path):
        # 1 ("measured nothing") and 3 ("refuses to pool") are different facts:
        # one means the artifact is empty, the other that it is wrong. A tail
        # that flattened either to a single non-zero would pass the 3 test.
        assert self._run(tmp_path, 1).returncode == 1

    def test_summary_stdout_still_reaches_the_caller(self, tmp_path):
        # Propagating the status while swallowing the summary would leave the
        # run statusful but productless — the summary IS the product.
        assert "stub summary" in self._run(tmp_path, 0).stdout
