"""#843 boot-strand sampler — pytest wrapper for lib/boot-strand-sampler.sh.

Two tiers, mirroring tests/test_freshbox_boot_harness.py:

1. Hermetic (always on): the script parses, its pure classifier sources cleanly
   and gives the right verdict on the committed #837 pane fixtures — including
   the two traps #843 documents (a transcript echo must not read as a strand;
   submission evidence must beat any pane state) — and the stdlib summary
   module's exact intervals match known Clopper-Pearson values.

2. Real-boot smoke (gated): BOOT_SAMPLER_REALBOOT=1 plus the heavy deps runs a
   1-boot sample against the real claude binary and asserts the machine-
   readable SAMPLER_RESULT contract. The n=20 measurement run for #843 is an
   operator action, not a test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.conftest import load_lib_module

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLER = REPO_ROOT / "lib" / "boot-strand-sampler.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pane-states"

summary = load_lib_module("boot-strand-summary")


def _verdict(submitted: str, pane: str, probe: str) -> str:
    """Run final_verdict from the sourced (not executed) sampler script."""
    script = f'. "{SAMPLER}"\nfinal_verdict "$1" "$(cat "$2")" "$3"\n'
    result = subprocess.run(
        ["bash", "-c", script, "verdict", submitted, pane, probe],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"final_verdict failed: {result.stderr}"
    return result.stdout.strip()


def _fixture_path(name: str) -> str:
    return str(FIXTURES / name)


def test_sampler_parses():
    result = subprocess.run(
        ["bash", "-n", str(SAMPLER)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


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
    """The classifier against the committed #837 pane geometries."""

    def test_stuck_startup_prompt_is_strand(self):
        # input-stuck-wrapped is literally a stranded STARTUP_PROMPT pane; the
        # probe is capped at 60 chars exactly as pane_send_verified caps it.
        probe = (
            "set +H; PROBE763WRAP You just started up. Read your CLAUDE.md, then post"[
                :60
            ]
        )
        assert (
            _verdict("0", _fixture_path("input-stuck-wrapped.txt"), probe) == "strand"
        )

    def test_stuck_literal_is_strand(self):
        assert (
            _verdict(
                "0",
                _fixture_path("input-stuck-literal.txt"),
                "/claudna:session resume --auto",
            )
            == "strand"
        )

    def test_collapsed_paste_is_strand(self):
        # The placeholder branch: the literal payload is nowhere in the pane.
        assert (
            _verdict(
                "0", _fixture_path("input-stuck-collapsed-paste.txt"), "NOT_IN_PANE"
            )
            == "strand"
        )

    def test_transcript_echo_is_not_strand(self):
        # #843's trap: the submitted probe is still visible in the TRANSCRIPT
        # (with its own prompt glyph); last-glyph anchoring must not match it.
        assert (
            _verdict(
                "0",
                _fixture_path("input-clean-submit.txt"),
                "PROBE763TRANSCRIPT reply ok",
            )
            == "other:no_evidence"
        )

    def test_submission_evidence_beats_any_pane(self):
        # Even a stuck-looking pane cannot override transcript ground truth.
        probe = (
            "set +H; PROBE763WRAP You just started up. Read your CLAUDE.md, then post"[
                :60
            ]
        )
        assert _verdict("1", _fixture_path("input-stuck-wrapped.txt"), probe) == "clean"


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

    def test_fisher_zero_twenty_vs_two_four(self):
        # C(4,2)*C(20,0)/C(24,2) = 6/276.
        assert abs(summary.fisher_one_sided(0, 20, 2, 4) - 6 / 276) < 1e-9


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
        assert "warm-up boot: strand (excluded from the sample)" in text

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

    def test_zero_valid_boots_is_failure(self):
        _, rc = summary.summarize(self._rows("other:startbot_rc_1"))
        assert rc == 1

    def test_baseline_caveat_always_present(self):
        text, _ = summary.summarize(self._rows("clean"))
        assert "no crisp sample" in text
        assert "n=4" in text


# ── gated real-boot smoke ─────────────────────────────────────────────────────

_OPT_IN = os.environ.get("BOOT_SAMPLER_REALBOOT") == "1"
_missing = [
    name
    for name, present in (
        ("claude", shutil.which("claude") is not None),
        ("jq", shutil.which("jq") is not None),
        ("tmux", shutil.which("tmux") is not None),
        ("claudron", shutil.which("claudron") is not None),
        (
            "auth ~/.claude/.credentials.json",
            (Path.home() / ".claude" / ".credentials.json").is_file(),
        ),
    )
    if not present
]
if not _OPT_IN:
    _skip = "gated — set BOOT_SAMPLER_REALBOOT=1 to run the real-boot smoke"
elif _missing:
    _skip = f"real-boot smoke needs: {', '.join(_missing)}"
else:
    _skip = ""


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
