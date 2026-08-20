"""Tests for lib/exit-token-mixture.py — the #1236 discrimination's primary output.

Offline by construction: no boots, no tmux, no model calls. The end-to-end case
builds a trace directory in the shape `_pane_verify_trace` writes and drives the
real shipped renderer over it.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


etm = _load("etm", LIB / "exit-token-mixture.py")
summary = _load("bss", LIB / "boot-strand-summary.py")
cp = summary.cp_interval


class TestConfIsRequired:
    """#1236: a default silently substitutes for a decision. It is gone."""

    def test_cp_interval_refuses_a_bare_call(self):
        with pytest.raises(TypeError):
            cp(2, 4)

    def test_mover_difference_refuses_a_bare_call(self):
        with pytest.raises(TypeError):
            summary.mover_difference(7, 10, 2, 10)

    def test_explicit_095_is_still_reachable(self):
        lo, hi = cp(2, 4, 0.95)
        assert (round(lo, 4), round(hi, 4)) == (0.0676, 0.9324)


class TestRatifiedStatistic:
    def test_label_carries_both_forms(self):
        # The pre-registration says "90% one-sided"; the code says conf=0.80.
        # An artifact must prove its own conformance without redoing the algebra.
        assert "90% one-sided" in etm.STAT_LABEL
        assert "conf=0.80" in etm.STAT_LABEL

    @pytest.mark.parametrize(
        "k,want", [(10, "DOMINANT"), (9, "DOMINANT"), (8, "DOMINANT"),
                   (7, "INCONCLUSIVE"), (6, "INCONCLUSIVE")]
    )
    def test_ratified_decision_table(self, k, want):
        m = etm.mixture(["empty-box"] * k + ["below-floor"] * (10 - k), etm.RATIFIED_CONF, cp)
        row = next(r for r in m["rows"] if r["token"] == "empty-box")
        assert row["verdict"] == want

    def test_eight_of_ten_is_the_two_dissenter_case(self):
        lo, _ = cp(8, 10, etm.RATIFIED_CONF)
        assert round(lo * 100, 1) == 55.0
        assert lo > etm.DOMINANT_THRESHOLD


class TestEliminationIsUnavailable:
    def test_threshold_is_derived_not_folklore(self):
        assert cp(0, 21, etm.RATIFIED_CONF)[1] >= etm.ELIMINATED_THRESHOLD
        assert cp(0, 22, etm.RATIFIED_CONF)[1] < etm.ELIMINATED_THRESHOLD
        assert etm.ELIMINATION_MIN_N == 22

    def test_zero_count_at_planned_n_is_not_observed_never_eliminated(self):
        m = etm.mixture(["empty-box"] * 10, etm.RATIFIED_CONF, cp)
        row = next(r for r in m["rows"] if r["token"] == "not-substring")
        assert row["verdict"] == "NOT-OBSERVED"
        assert round(row["hi"] * 100, 1) == 20.6

    def test_elimination_only_once_n_clears_the_floor(self):
        m = etm.mixture(["empty-box"] * etm.ELIMINATION_MIN_N, etm.RATIFIED_CONF, cp)
        row = next(r for r in m["rows"] if r["token"] == "not-substring")
        assert row["verdict"] == "ELIMINATED"

    def test_report_states_unavailability_at_planned_n(self):
        m = etm.mixture(["empty-box"] * 10, etm.RATIFIED_CONF, cp)
        text = etm.format_report(m, [], etm.RATIFIED_CONF)
        assert "ELIMINATED is UNAVAILABLE at n=10" in text


class TestExitTickRule:
    def test_last_tick_wins(self):
        ticks = [{"tick": 0, "candidate": "held"}, {"tick": 1, "candidate": "empty-box"}]
        assert etm.exit_token(ticks)[0] == "empty-box"

    def test_out_of_order_ticks_still_pick_the_highest(self):
        ticks = [{"tick": 4, "candidate": "below-floor"}, {"tick": 1, "candidate": "held"}]
        assert etm.exit_token(ticks)[0] == "below-floor"

    def test_no_ticks_is_unscored_not_a_default_token(self):
        tok, why = etm.exit_token([])
        assert tok is None and why == "no-ticks"

    def test_unknown_candidate_is_unscored(self):
        assert etm.exit_token([{"tick": 1, "candidate": "banana"}])[0] is None

    def test_held_at_exit_is_separated_from_the_mixture(self):
        tok, why = etm.exit_token([{"tick": 2, "candidate": "held"}])
        assert tok == "held" and why == "held-at-exit"


class TestPlausibleButWrongOutputs:
    """Signatures pre-registered BEFORE the first real run, per the #1236 gate.

    The first real run exercises the instrument and the phenomenon at once, so a
    strange result leaves the analyzer a suspect alongside the data. These are the
    two ways it could produce a CONFIDENT WRONG answer, both measured on synthetic
    input while there was no result to be attached to.
    """

    def test_two_sends_in_one_dir_are_refused_not_scored(self):
        # THE DANGEROUS ONE. pane_send_verified restarts tick at 0 and overwrites
        # payload per call while ticks.tsv is appended, so a boot that sends twice
        # collides ids. Measured before guarding: six such traces reported
        # 100% empty-box DOMINANT at 68.1% -- unanimous support for the leading
        # hypothesis, entirely artifact.
        ticks = [{"tick": 0, "candidate": "held"}, {"tick": 1, "candidate": "empty-box"},
                 {"tick": 0, "candidate": "held"}, {"tick": 1, "candidate": "empty-box"}]
        tok, why = etm.exit_token(ticks)
        assert tok is None
        assert "duplicate-tick-ids" in why

    def test_unarmed_trace_is_named_not_scored(self):
        # no-payload is a SIXTH token, absent from the pre-registration's table of
        # five. It means the trace was never armed -- with an empty payload every
        # frame classifies as not-substring, so scoring it manufactures a
        # unanimous verdict out of an instrument failure.
        tok, why = etm.exit_token([{"tick": 0, "candidate": "no-payload"}])
        assert tok is None
        assert why.startswith("no-payload")
        assert "NOT a candidate observation" in why

    def test_no_payload_is_not_in_the_scoreable_set(self):
        assert "no-payload" in etm.INSTRUMENT_FAILURE_TOKENS
        assert "no-payload" not in etm.ALL_TOKENS

    def test_every_token_the_predicate_emits_is_accounted_for(self):
        # Derived from lib-common.sh rather than from the pre-registration table,
        # which lists five and misses no-payload.
        src = (REPO / "lib" / "lib-common.sh").read_text()
        body = src.split("_pane_trace_candidate()", 1)[1].split("\n}", 1)[0]
        emitted = set(re.findall(r"printf '([a-z-]+)'", body))
        assert emitted, "failed to parse the predicate's emissions"
        assert emitted <= set(etm.ALL_TOKENS) | set(etm.INSTRUMENT_FAILURE_TOKENS), (
            f"predicate emits tokens this module does not handle: "
            f"{emitted - set(etm.ALL_TOKENS) - set(etm.INSTRUMENT_FAILURE_TOKENS)}"
        )


class TestCoverageHonesty:
    def test_empty_input_is_undefined_not_a_clean_sweep(self):
        m = etm.mixture([], etm.RATIFIED_CONF, cp)
        assert {r["verdict"] for r in m["rows"]} == {"NO-DATA"}
        assert "NO SCORED STRANDS" in etm.format_report(m, [], etm.RATIFIED_CONF)

    def test_unscored_traces_are_disclosed_by_name(self):
        m = etm.mixture(["empty-box"] * 3, etm.RATIFIED_CONF, cp)
        text = etm.format_report(m, [("trace-boot-7", "no-ticks")], etm.RATIFIED_CONF)
        assert "UNSCORED: 1 trace(s)" in text and "trace-boot-7" in text

    def test_every_token_appears_even_at_zero(self):
        m = etm.mixture(["empty-box"] * 5, etm.RATIFIED_CONF, cp)
        assert {r["token"] for r in m["rows"]} == set(etm.ALL_TOKENS)


class TestEndToEndOverARealTrace:
    """Drives the SHIPPED pane_trace_render over a trace dir built in its own format."""

    def _trace(self, tmp_path: Path, payload: str, frames: list[tuple[int, str, str]]) -> Path:
        d = tmp_path / "trace-boot-1"
        d.mkdir()
        (d / "payload").write_text(payload)
        rows = []
        for tick, box, pane in frames:
            (d / f"tick-{tick}.pane").write_text(pane)
            rows.append(f"{tick}\t{box}")
        (d / "ticks.tsv").write_text("\n".join(rows) + "\n")
        return d

    def test_empty_box_at_exit_scores_as_render_lag(self, tmp_path):
        payload = "set +H; Welcome back. Read your CLAUDE.md. Idle and await Telegram."
        d = self._trace(tmp_path, payload, [
            (0, "drawn", f"> {payload}"),   # held
            (1, "drawn", "> "),             # glyph, nothing after it -> empty-box
        ])
        tok, why = etm.exit_token(etm.render_trace(d, LIB / "lib-common.sh"))
        assert why == "ok"
        assert tok == "empty-box"

    def test_a_still_held_payload_is_not_a_false_clean(self, tmp_path):
        payload = "set +H; Welcome back. Read your CLAUDE.md. Idle and await Telegram."
        d = self._trace(tmp_path, payload, [(0, "drawn", f"> {payload}")])
        tok, why = etm.exit_token(etm.render_trace(d, LIB / "lib-common.sh"))
        assert tok == "held" and why == "held-at-exit"

    def test_missing_trace_dir_is_unscored_not_a_crash(self, tmp_path):
        assert etm.exit_token(etm.render_trace(tmp_path / "nope", LIB / "lib-common.sh"))[0] is None


class TestStrandSelectionIsDerivedNotTyped:
    """#1236 mode C, found by the two-boot smoke run — the worst-PLACED of the three.

    A and B need an unusual condition. This one needs only that someone points the
    tool at the artifacts directory, which is the obvious thing to do. A trace is
    written for EVERY boot, and a clean boot exits on `empty-box` because the
    payload was genuinely submitted -- so scoring clean boots measures successful
    sends and reports them as render lag. Measured on two real clean boots:
    `empty-box 2/2 = 100%`, well-formed and confident.
    """

    def _run(self, *args):
        return subprocess.run([sys.executable, str(LIB / "exit-token-mixture.py"), *args],
                              capture_output=True, text=True, timeout=180)

    def test_bare_glob_refuses(self, tmp_path):
        d = tmp_path / "trace-boot-0"; d.mkdir()
        r = self._run(str(d))
        assert r.returncode == 3
        assert "strand selection cannot be derived" in r.stderr

    def test_missing_rows_file_refuses_rather_than_crashing(self, tmp_path):
        d = tmp_path / "trace-boot-0"; d.mkdir()
        r = self._run("--rows", str(tmp_path / "nope.jsonl"), str(d))
        assert r.returncode == 3
        assert "not a readable file" in r.stderr

    def test_clean_boots_are_excluded_by_name_with_their_outcome(self, tmp_path):
        rows = tmp_path / "rows.jsonl"
        rows.write_text('{"i": 0, "outcome": "clean"}\n{"i": 1, "outcome": "strand"}\n')
        for i in (0, 1):
            d = tmp_path / f"trace-boot-{i}"; d.mkdir()
            (d / "payload").write_text("set +H; probe payload long enough to match")
            (d / "tick-1.pane").write_text("> ")
            (d / "ticks.tsv").write_text("1\tdrawn\n")
        r = self._run("--rows", str(rows), str(tmp_path / "trace-boot-0"), str(tmp_path / "trace-boot-1"))
        assert r.returncode == 0
        assert "scored): 1" in r.stdout          # only the strand counted
        assert "outcome='clean'" in r.stdout      # and the clean one named

    def test_boot_index_parsing(self):
        assert etm.boot_index(Path("/x/trace-boot-12")) == 12
        assert etm.boot_index(Path("/x/not-a-trace")) is None
        assert etm.boot_index(Path("/x/trace-boot-abc")) is None

    def test_a_dir_with_no_matching_row_is_disclosed(self, tmp_path):
        rows = tmp_path / "rows.jsonl"; rows.write_text('{"i": 0, "outcome": "strand"}\n')
        d = tmp_path / "trace-boot-9"; d.mkdir()
        r = self._run("--rows", str(rows), str(d))
        assert r.returncode == 0
        assert "no row for boot 9" in r.stdout


class TestManufacturedVerdictScrutiny:
    """empty-box DOMINANT is what BOTH known defects emit, by unrelated mechanisms.

    So its prior moves DOWN, not up: it is the result to trust least, because it is
    what a broken instrument produces. Registered as a named scrutiny condition
    rather than left to whoever reads the output.
    """

    def _mix(self, k, n_):
        return etm.mixture(["empty-box"] * k + ["below-floor"] * (n_ - k), etm.RATIFIED_CONF, cp)

    def test_fires_on_empty_box_dominant(self):
        text = etm.format_report(self._mix(10, 10), [], etm.RATIFIED_CONF, derived=True)
        assert "SCRUTINY CONDITION" in text
        assert "MANUFACTURED verdict" in text

    def test_silent_when_empty_box_is_not_dominant(self):
        text = etm.format_report(self._mix(6, 10), [], etm.RATIFIED_CONF, derived=True)
        assert "SCRUTINY CONDITION" not in text

    def test_silent_when_a_DIFFERENT_token_dominates(self):
        mix = etm.mixture(["below-floor"] * 10, etm.RATIFIED_CONF, cp)
        text = etm.format_report(mix, [], etm.RATIFIED_CONF, derived=True)
        assert "DOMINANT" in text
        assert "SCRUTINY CONDITION" not in text, "only the manufactured token gets scrutiny"

    def test_undervied_selection_is_reported_as_FAIL(self):
        text = etm.format_report(self._mix(10, 10), [], etm.RATIFIED_CONF, derived=False)
        assert "[FAIL] strand selection DERIVED" in text
        assert "artifact, not a finding" in text

    def test_derived_selection_passes_that_check(self):
        text = etm.format_report(self._mix(10, 10), [], etm.RATIFIED_CONF, derived=True)
        assert "[OK ] strand selection DERIVED" in text

    def test_counts_the_artifacts_it_actually_refused(self):
        unscored = [("a", "duplicate-tick-ids:[0, 1] — trace dir holds more than one send"),
                    ("b", "no-payload — trace not armed (empty payload); NOT a candidate observation"),
                    ("c", "outcome='clean' — only 'strand' is scoreable")]
        text = etm.format_report(self._mix(10, 10), unscored, etm.RATIFIED_CONF, derived=True)
        assert "duplicate tick ids: 1" in text
        assert "unarmed (no-payload): 1" in text
        assert "excluded by classification: 1" in text


class TestPreMatrixGate:
    """The gate is run-blocking because an instruction to run it is not a gate.

    It must also refuse a FIXTURE-ONLY pass: mode C passed every fixture test in
    #1293 and was found by two real boots, so a gate that can go green without
    real traces reproduces exactly the state that merged.
    """

    def _boots(self, tmp_path, outcomes, frame="> "):
        rows = tmp_path / "rows.jsonl"
        rows.write_text("".join(
            '{"i": %d, "outcome": "%s"}\n' % (i, o) for i, o in enumerate(outcomes)))
        dirs = []
        for i, _ in enumerate(outcomes):
            d = tmp_path / f"trace-boot-{i}"; d.mkdir()
            (d / "payload").write_text("set +H; a probe payload long enough to clear the floor")
            (d / "tick-1.pane").write_text(frame)
            (d / "ticks.tsv").write_text("1\tdrawn\n")
            dirs.append(d)
        return rows, dirs

    def _gate(self, *args):
        return subprocess.run(
            [sys.executable, str(LIB / "exit-token-mixture.py"), "--gate", *args],
            capture_output=True, text=True, timeout=300)

    def test_refuses_a_fixture_only_pass(self):
        # No --rows at all: the real half never ran, and that must not read green.
        r = self._gate()
        assert r.returncode == 4
        assert "no REAL clean boots supplied" in r.stdout

    def test_passes_on_real_shaped_clean_boots(self, tmp_path):
        rows, dirs = self._boots(tmp_path, ["clean", "clean"])
        r = self._gate("--rows", str(rows), *[str(d) for d in dirs])
        assert r.returncode == 0, r.stdout
        assert "GATE PASSED" in r.stdout

    def test_blocks_when_a_clean_boot_would_be_scored(self, tmp_path):
        # Mislabel the clean boots as strands: the production path would score
        # them, which is precisely mode C, and the gate must block.
        rows, dirs = self._boots(tmp_path, ["clean", "clean"])
        rows.write_text('{"i": 0, "outcome": "clean"}\n{"i": 1, "outcome": "clean"}\n')
        r = self._gate("--rows", str(rows), *[str(d) for d in dirs])
        assert r.returncode == 0  # correctly labelled -> excluded
        # Now break the labelling the way a real desync would.
        rows.write_text('{"i": 0, "outcome": "strand"}\n{"i": 1, "outcome": "strand"}\n')
        r2 = subprocess.run(
            [sys.executable, str(LIB / "exit-token-mixture.py"), "--rows", str(rows),
             *[str(d) for d in dirs]], capture_output=True, text=True, timeout=300)
        assert "scored): 2" in r2.stdout, "control: mislabelled boots DO get scored"

    def test_dead_positive_control_is_a_failure_not_a_pass(self, tmp_path):
        # A frame that does NOT look like a cleared box: the defect this gate
        # exercises is not live, so a pass would prove nothing and it must fail.
        rows, dirs = self._boots(tmp_path, ["clean"], frame="> set +H; a probe payload long enough to clear the floor")
        r = self._gate("--rows", str(rows), *[str(d) for d in dirs])
        assert r.returncode == 4
        assert "POSITIVE CONTROL DEAD on real boots" in r.stdout


class TestSelfTestEntryPoint:
    def test_self_test_passes(self):
        r = subprocess.run([sys.executable, str(LIB / "exit-token-mixture.py"), "--self-test"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "self-test: PASS" in r.stdout

    def test_no_dirs_refuses_rather_than_printing_an_empty_result(self):
        r = subprocess.run([sys.executable, str(LIB / "exit-token-mixture.py")],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 2
