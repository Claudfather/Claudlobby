"""#1493 send-size probe — pytest wrapper for lib/send-size-probe.sh.

Two tiers, the tests/test_boot_strand_sampler.py shape:

1. Hermetic (always on): the probe's pure helpers — the arrival classifier, the
   payload builder, the median, the size parser, the table renderer, the
   scratch-path guard and the canonicalizer — driven directly, zero boots and
   zero model calls. These are what turn a transcript into a verdict, so a
   defect in one silently rewrites every row of a real run.

2. Real run (gated on SEND_PROBE_REAL=1): one interleaved pass at two sizes,
   asserting the property the #1493 fix CLAIMS — that a chunked send arrives
   whole. It deliberately does NOT assert that the unchunked control fails:
   the pty cliff is 1024 bytes on macOS and 4096 on Linux with the opposite
   overflow policy, so a control-must-fail assertion would be a
   platform-specific test wearing a behavioural name, red on the Pi for a
   reason that is not a regression.

   Its gate is its own, not conftest's realboot_skip_reason: that helper
   requires host credentials and the claudron CLI, and this probe requires
   NEITHER by design — it boots unauthenticated against a dead API base URL so
   it cannot spend. Reusing the shared gate would skip the probe on exactly the
   hosts it is cheapest to run on.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import call_script_fn, constructed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "lib" / "send-size-probe.sh"


def _fn(name: str, *args: str) -> str:
    return call_script_fn(PROBE, name, *args)


# --------------------------------------------------------------------------
# probe_payload — the thing every verdict is measured against
# --------------------------------------------------------------------------


class TestProbePayload:
    @pytest.mark.parametrize("size", [100, 500, 900, 1024, 2100, 4200])
    def test_payload_is_exactly_the_requested_byte_count(self, size):
        # The whole instrument is a comparison against a byte cliff. A payload
        # that is "about" N bytes measures the wrong side of it.
        out = _fn("probe_payload", str(size), "TOK123")
        assert len(out.encode()) == size

    def test_markers_sit_at_both_ends(self):
        out = _fn("probe_payload", "500", "TOK123")
        assert out.startswith("TOK123H")
        assert out.endswith("TTOK123")

    def test_a_head_loss_still_leaves_the_tail_marker(self):
        # The reason there are two markers: the record has to be findable when
        # the head is gone, which is the #1493 symptom.
        out = _fn("probe_payload", "2100", "TOK123")
        assert "TTOK123" in out[1024:]

    def test_the_varied_filler_repeats_no_long_identical_run(self):
        # The default filler moved for a measured reason: a one-character filler
        # gives consecutive chunks that are BYTE-IDENTICAL, and Claude Code's TUI
        # drops one of those (macOS, 2.1.263 — see probe_payload's header). That
        # is a recipient-side defect, not the pty race under test, and an
        # instrument whose own payload trips a different bug measures that bug.
        out = _fn("probe_payload", "2100", "TOK123")
        assert "z" * 200 not in out
        # every offset names itself, so a loss is localisable rather than just
        # counted
        assert "TOK1230000_" in out and "TOK1230010_" in out

    def test_the_repeat_filler_is_still_reachable(self):
        # Kept so the recipient-side finding can be re-measured against a future
        # binary instead of becoming folklore.
        out = _fn("probe_payload", "2100", "TOK123", "repeat")
        assert "z" * 1500 in out
        assert len(out.encode()) == 2100

    @pytest.mark.parametrize("size", [2100, 3100, 4200])
    def test_the_ident2_filler_makes_adjacent_chunks_IDENTICAL(self, size):
        """The realistic form of the recipient-side trigger (chunk O fold, F2).

        The earlier reading of that finding — "it needs ~1800 consecutive
        identical bytes, which no dispatch has" — was wrong, and a
        one-character filler is what made it look right. The trigger is
        `chunk[i] == chunk[i-1]` and nothing else, so this filler reaches it
        with ORDINARY numbered-line text.

        The block has to START at byte 0, head marker included, or the repeat
        is offset by the marker's length and no two 900-byte windows align —
        which would make the filler certify a shape it never produced.
        """
        out = _fn("probe_payload", str(size), "TOK123", "ident2")
        assert len(out.encode()) == size
        assert out[:900] == out[900:1800]
        assert out.startswith("TOK123H") and out.endswith("TTOK123")

    def test_the_ident2_filler_carries_NO_newline(self):
        """send-keys -l types a newline as a newline, so the TUI submits there
        and the payload arrives as several turns. Measured on the first real
        run of this filler: the positive control came back `tail-lost` on a
        100-byte payload, a shape no pty queue can produce."""
        for size in (100, 900, 2100, 4200):
            assert "\n" not in _fn("probe_payload", str(size), "TOK123", "ident2")

    @pytest.mark.parametrize("size", [100, 500, 900, 1024, 2100, 4200])
    def test_the_repeat_filler_is_also_exact(self, size):
        assert len(_fn("probe_payload", str(size), "TOK123", "repeat").encode()) == size

    def test_a_payload_too_small_for_its_markers_is_refused(self):
        # Not silently shortened: a short payload would be compared against a
        # cliff it never reached.
        r = subprocess.run(
            ["bash", "-c", f'. "{PROBE}"; probe_payload 5 TOK123'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 2


# --------------------------------------------------------------------------
# classify_arrival — the four-way verdict
# --------------------------------------------------------------------------


class TestClassifyArrival:
    def test_identical_is_whole(self):
        assert _fn("classify_arrival", "abcdef", "abcdef") == "whole"

    def test_a_strict_suffix_is_head_lost(self):
        # THE #1493 symptom: the recipient kept what followed the flushed queue.
        assert _fn("classify_arrival", "abcdef", "def") == "head-lost"

    def test_a_strict_prefix_is_tail_lost(self):
        assert _fn("classify_arrival", "abcdef", "abc") == "tail-lost"

    def test_neither_prefix_nor_suffix_is_other(self):
        # Never rounded into the nearest bucket. A shape nobody predicted is the
        # interesting row, and this probe found one: a payload of thousands of
        # IDENTICAL filler bytes loses a whole chunk out of its middle inside
        # the TUI, which reads as `other` and would have been invisible if a
        # mid-drop had been filed as head-lost.
        assert _fn("classify_arrival", "abcdef", "abef") == "other"

    def test_whole_wins_over_the_suffix_test(self):
        # Every string is a suffix of itself; the equality branch has to come
        # first or a clean send is reported as a loss.
        assert _fn("classify_arrival", "aaa", "aaa") == "whole"

    def test_a_repeated_payload_still_classifies_by_position(self):
        # Degenerate filler is exactly where prefix/suffix tests get delicate.
        assert _fn("classify_arrival", "zzzzz", "zzz") == "head-lost"


# --------------------------------------------------------------------------
# median / parse_sizes
# --------------------------------------------------------------------------


class TestMedian:
    def test_odd_count(self):
        assert _fn("median", "30", "10", "20") == "20"

    def test_even_count_takes_the_lower_middle(self):
        # A reported figure that is one of the observations, not an average that
        # is none of them — every loss here is quantised.
        assert _fn("median", "10", "20", "30", "40") == "20"

    def test_no_observations_is_empty(self):
        assert _fn("median") == ""

    def test_sorts_numerically_not_lexically(self):
        # "1006" sorts before "478" as text; as numbers it does not.
        assert _fn("median", "1006", "478", "2036") == "1006"


class TestParseSizes:
    def test_space_separated(self):
        assert _fn("parse_sizes", "500 900 1100").split() == ["500", "900", "1100"]

    def test_comma_separated(self):
        assert _fn("parse_sizes", "500,900,1100").split() == ["500", "900", "1100"]

    def test_non_numeric_entries_are_dropped(self):
        assert _fn("parse_sizes", "500 big 900").split() == ["500", "900"]


# --------------------------------------------------------------------------
# render_table
# --------------------------------------------------------------------------


ROWS = "\n".join(
    [
        # arm        size  rep verdict     arrived
        "chunked\t1100\t1\twhole\t1100",
        "chunked\t1100\t2\twhole\t1100",
        "chunked\t500\t1\twhole\t500",
        "unchunked\t1100\t1\thead-lost\t78",
        "unchunked\t1100\t2\thead-lost\t478",
        "unchunked\t1100\t3\thead-lost\t56",
        "unchunked\t500\t1\twhole\t500",
        "unchunked\t2100\t1\tabsent\t0",
        "unchunked\t2100\t2\ttail-lost\t900",
        "unchunked\t2100\t3\tother\t1200",
    ]
)


class TestRenderTable:
    @pytest.fixture
    def rows(self, tmp_path):
        p = tmp_path / "rows.tsv"
        p.write_text(ROWS + "\n")
        return p

    def _row(self, out: str, size: str) -> list[str]:
        for line in out.splitlines():
            f = line.split()
            if f and f[0] == size:
                return f
        raise AssertionError(f"no row for size {size} in:\n{out}")

    def test_only_the_named_arm_is_counted(self, rows):
        out = _fn("render_table", "chunked", str(rows))
        assert self._row(out, "1100")[1:] == ["2", "2", "0", "0", "0", "0", "-"]
        assert " 2100 " not in out  # unchunked-only size

    def test_each_verdict_lands_in_its_own_column(self, rows):
        out = _fn("render_table", "unchunked", str(rows))
        # size n whole head-lost tail-lost absent other median
        assert self._row(out, "2100")[1:] == ["3", "0", "0", "1", "1", "1", "-"]

    def test_median_arrived_covers_head_lost_only(self, rows):
        out = _fn("render_table", "unchunked", str(rows))
        assert self._row(out, "1100")[-1] == "78"  # median of 56, 78, 478

    def test_sizes_are_ordered_numerically(self, rows):
        out = _fn("render_table", "unchunked", str(rows))
        seen = [f.split()[0] for f in out.splitlines()[1:] if f.split()]
        assert seen == ["500", "1100", "2100"]


# --------------------------------------------------------------------------
# the reaper's guard and the trust-key canonicalizer
# --------------------------------------------------------------------------


def _rc(fn: str, *args: str) -> int:
    return subprocess.run(
        ["bash", "-c", f'. "{PROBE}"; {fn} "$@"', "_", *args],
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode


class TestScratchGuard:
    """The probe rm -rf's a tree on every exit path, so the one thing it must
    never be able to do is remove something that is not its own."""

    def test_its_own_scratch_tree_is_removable(self, tmp_path):
        d = tmp_path / "sendprobe.999.abc"
        d.mkdir()
        assert _rc("probe_path_is_scratch", str(d), "sendprobe.999") == 0

    def test_a_path_without_this_runs_marker_is_refused(self, tmp_path):
        d = tmp_path / "somebody-elses-dir"
        d.mkdir()
        assert _rc("probe_path_is_scratch", str(d), "sendprobe.999") != 0

    def test_a_bot_dir_is_refused_even_carrying_the_marker(self, tmp_path):
        # Belt and braces: a marker collision must not be enough to reach a
        # fleet-owned path (path_audit's shape convention).
        d = tmp_path / "runtime" / "bots" / "sendprobe.999"
        d.mkdir(parents=True)
        assert _rc("probe_path_is_scratch", str(d), "sendprobe.999") != 0

    def test_a_missing_directory_is_refused(self, tmp_path):
        assert _rc("probe_path_is_scratch", str(tmp_path / "sendprobe.999"), "sendprobe.999") != 0


class TestCanonicalDir:
    def test_a_symlinked_path_resolves(self, tmp_path):
        # macOS $TMPDIR is /var/folders/... which is a symlink to
        # /private/var/folders/... . Claude Code keys workspace trust on the
        # RESOLVED path, so seeding the unresolved one leaves the workspace
        # untrusted and the trust wizard eats the probe's first send — measured,
        # and it cost a run before this was pinned.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert _fn("probe_canonical_dir", str(link)).strip() == str(real.resolve())

    def test_an_unresolvable_path_comes_back_unchanged(self, tmp_path):
        missing = str(tmp_path / "nope")
        assert _fn("probe_canonical_dir", missing).strip() == missing


class TestWizardHint:
    def test_the_trust_wizard_is_named(self):
        hint = _fn("probe_wizard_hint", "Quick safety check: Is this a project you trust?")
        assert "trust" in hint

    def test_an_ordinary_pane_gets_no_hint(self):
        assert _fn("probe_wizard_hint", "> \n---\n auto mode on") == ""


# --------------------------------------------------------------------------
# the constructed child environment (chunk O fold, F4)
# --------------------------------------------------------------------------


class TestChildEnvironment:
    """The probe's two safety claims — it cannot spend, it cannot touch a
    fleet — rest entirely on what crosses into the child. The first cut set two
    variables on the command line and let `tmux new-session` pass the rest of
    the caller's environment through, so both claims held only in the
    environment their author happened to run in.

    Pinned on the LADDER, with no boot: the command line is what decides, and a
    test that needed a real `claude` to check it would be skipped on every host
    that most needs the check.
    """

    def _cmd(self) -> str:
        return _fn("probe_child_command", "/usr/bin/claude", "/scratch/cwd",
                   "/scratch/cfg", "/scratch/home", "/scratch/env.txt")

    def test_the_child_env_is_BUILT_not_inherited(self):
        cmd = self._cmd()
        assert "env -i" in cmd, cmd
        # everything the child is allowed, and it is allowed nothing else
        for var in ("PATH=", "HOME='/scratch/home'", "TERM=", "LANG=",
                    "LC_ALL=", "CLAUDE_CONFIG_DIR='/scratch/cfg'",
                    "ANTHROPIC_BASE_URL='http://127.0.0.1:9'"):
            assert var in cmd, f"{var} missing from: {cmd}"

    def test_the_dead_base_url_is_not_routed_around(self):
        """CLAUDE_CODE_USE_BEDROCK / _USE_VERTEX bypass ANTHROPIC_BASE_URL
        entirely, so with either inherited the "cannot spend" claim was simply
        false. They are not on the command line, and `env -i` drops them."""
        cmd = self._cmd()
        assert "CLAUDE_CODE_USE" not in cmd
        assert "ANTHROPIC_API_KEY" not in cmd

    def test_the_pane_dumps_its_own_environment_before_exec(self):
        """A constructed ladder is a CLAIM until something reads back what the
        child actually got. `ps eww` was measured and rejected — on macOS it
        prints the command and no environment at all, so a check built on it
        reads clean by construction."""
        cmd = self._cmd()
        assert "/scratch/env.txt" in cmd
        assert cmd.rstrip().endswith("""exec "/usr/bin/claude"'"""), cmd


class TestEnvLeaks:
    def test_a_clean_environment_reports_nothing(self):
        assert _fn("probe_env_leaks",
                   "PATH=/bin HOME=/tmp/h TERM=xterm LANG=C.UTF-8 "
                   "CLAUDE_CONFIG_DIR=/tmp/c ANTHROPIC_BASE_URL=http://127.0.0.1:9"
                   ) == ""

    @pytest.mark.parametrize("leak", [
        "ANTHROPIC_API_KEY=sk-ant-xxxx",     # spend
        "CLAUDE_CODE_USE_BEDROCK=1",         # routes around the dead base URL
        "CLAUDE_CODE_USE_VERTEX=1",
        "BOT_DIR=/opt/fleet/runtime/bots/kev",   # the #846 vector
        "CLAUDLOBBY_ROOT=/opt/fleet",
        "FLEET_NAME=production",
    ])
    def test_each_forbidden_family_is_caught(self, leak):
        out = _fn("probe_env_leaks", f"PATH=/bin {leak} TERM=xterm")
        assert out.strip() == leak.split("=")[0]

    def test_the_families_are_PREFIXES_not_a_fixed_list(self):
        """CLAUDE_CODE_USE_* and CLAUDLOBBY_* are open families — whatever
        routes around the base URL next is in one of them. A list of exact
        names would go stale in the direction that reads clean, which is the
        only direction that matters here."""
        out = _fn("probe_env_leaks",
                  "CLAUDE_CODE_USE_SOMETHING_NEW=1 CLAUDLOBBY_SRC=/x")
        assert set(out.split()) == {"CLAUDE_CODE_USE_SOMETHING_NEW",
                                    "CLAUDLOBBY_SRC"}

    def test_a_value_containing_a_glob_does_not_expand(self, tmp_path):
        # `for tok in $text` word-splits; without `set -f` a value carrying `*`
        # would expand into the working directory's filenames.
        assert _fn("probe_env_leaks", "PATH=/bin:* TERM=xterm") == ""


# --------------------------------------------------------------------------
# the reaper (chunk O fold, F7)
# --------------------------------------------------------------------------


class TestReap:
    def test_it_removes_only_sendprobe_sockets(self, tmp_path):
        """The EXIT/INT/TERM/HUP trap covers the ordinary paths; this is the
        door for a run that still got away — a closed terminal used to leave a
        tmux server holding a live `claude` and a socket an operator then has
        to tell apart from a bot's. Scoped by the harness's own prefix."""
        (tmp_path / "sendprobe1234").write_text("")
        (tmp_path / "sendprobe5678").write_text("")
        keep = tmp_path / "ari"          # a real bot's socket, same directory
        keep.write_text("")
        out = _fn("probe_reap", str(tmp_path))
        assert "reaped 2 leftover probe server(s)" in out
        assert keep.exists(), "the reaper took a socket that was not its own"
        assert not (tmp_path / "sendprobe1234").exists()

    def test_an_empty_directory_reaps_nothing_and_says_so(self, tmp_path):
        assert "reaped 0 leftover probe server(s)" in _fn("probe_reap",
                                                          str(tmp_path))


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------


class TestCli:
    def _run(self, *args, **env):
        return subprocess.run(
            ["bash", str(PROBE), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=constructed_env(**env),
        )

    def test_unknown_argument_is_rc_2_with_usage(self):
        r = self._run("--no-such-flag")
        assert r.returncode == 2
        assert "unknown argument" in r.stderr
        assert "Usage:" in r.stderr

    def test_a_bad_arm_is_refused(self):
        r = self._run("--arm", "sideways")
        assert r.returncode == 2
        assert "--arm must be" in r.stderr

    def test_a_bad_filler_is_refused(self):
        r = self._run("--filler", "sideways")
        assert r.returncode == 2
        assert "--filler must be" in r.stderr

    def test_reap_needs_no_gate(self):
        """It exists for the run that did NOT finish, so it must not sit behind
        SEND_PROBE_REAL or a dependency check that run may have been waiting
        on. It destroys only this harness's own litter."""
        r = self._run("--reap")
        assert r.returncode == 0, r.stderr
        assert "reaped" in r.stdout

    def test_it_refuses_to_boot_without_the_real_gate(self):
        # A real `claude` must never start from a plain test sweep.
        r = self._run("--n", "1")
        assert r.returncode == 2
        assert "SEND_PROBE_REAL=1" in r.stderr

    def test_help_needs_no_gate(self):
        r = self._run("--help")
        assert r.returncode == 0
        assert "--arm" in r.stdout


# --------------------------------------------------------------------------
# the real run (gated)
# --------------------------------------------------------------------------


def _real_skip_reason() -> str:
    import os

    if os.environ.get("SEND_PROBE_REAL") != "1":
        return "gated — set SEND_PROBE_REAL=1 to boot a real claude"
    missing = [b for b in ("claude", "jq", "tmux") if shutil.which(b) is None]
    return f"send-size probe needs: {', '.join(missing)}" if missing else ""


@pytest.mark.skipif(bool(_real_skip_reason()), reason=_real_skip_reason())
def test_real_run_chunked_arm_arrives_whole():
    r = subprocess.run(
        ["bash", str(PROBE), "--arm", "both", "--n", "1", "--sizes", "500 2100"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "positive control: 100-byte payload recorded, verdict=whole" in r.stdout
    chunked = [ln for ln in r.stdout.splitlines() if " chunked " in ln and "bytes ->" in ln]
    assert chunked, f"no chunked rows:\n{r.stdout}"
    # The claim the fix makes, and the only one asserted here; see the module
    # docstring for why the control's outcome is not.
    assert all("-> whole" in ln for ln in chunked), "\n".join(chunked)
