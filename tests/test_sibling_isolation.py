"""Layer-0 sibling isolation — the three defects that made it decorative (#1312, #873, #970).

The control had never once worked. Three independent defects, each sufficient on
its own to neuter it, fixed together because the control has no value until all
three hold — NOT because any one masks another. That distinction matters: an
earlier framing said untrusted workspaces made the other two unobservable, and
reproduction refuted it (deny fires untrusted), so the bundling argument is
belt-and-braces on a control with no track record, not a dependency chain.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from claudlobby import path_audit
from claudlobby.validator import _inert_path_errors

LIB = Path(__file__).resolve().parent.parent / "lib"


class TestInertPathRefusal:
    """#1312: a bare-absolute path rule can never match, so it is an ERROR.

    A single leading slash anchors at the SETTINGS SOURCE, not the filesystem
    root. Measured in a scratch project with a no-rule control, and the mechanism
    shown directly: `Read(/target/**)` BLOCKS `<project>/target/inside.txt`.
    """

    @pytest.mark.parametrize(
        "rule", ["Read(/home/x/**)", "Edit(/a/b)", "Write(/a/**)",
                 "MultiEdit(/a/**)", "Glob(/a/**)", "Grep(/a/**)"]
    )
    def test_bare_absolute_path_rules_are_refused(self, rule):
        errs = _inert_path_errors("b", "expertise", "x", [rule])
        assert errs and "can never match" in errs[0]
        assert "//" in errs[0], "the message must show the corrected form"

    @pytest.mark.parametrize(
        "rule", ["Read(//home/x/**)", "Edit(//a/b)", "Bash(git *)", "Read(**)",
                 "Read(~/x/**)", "mcp__github__*", "Read"]
    )
    def test_correct_and_non_path_rules_pass(self, rule):
        assert _inert_path_errors("b", "expertise", "x", [rule]) == []

    def test_a_non_string_grant_does_not_crash_the_validator(self):
        assert _inert_path_errors("b", "expertise", "x", [None, 123, ["a"]]) == []


class TestRulePathNormalisation:
    """The `//` prefix is permission-rule SYNTAX, not a filesystem path.

    `os.path.normpath` preserves exactly two leading slashes (POSIX says the form
    is implementation-defined) and collapses three or more — measured. So the
    path auditor compared `//<fleet root>/x` against `/<fleet root>` and flagged
    the composer's own correct output as a foreign-rooted leak.
    """

    def test_normpath_really_does_preserve_a_double_slash(self):
        """Pin the platform behaviour this helper exists to correct."""
        assert os.path.normpath("//tmp/x") == "//tmp/x"
        assert os.path.normpath("///tmp/x") == "/tmp/x"

    @pytest.mark.parametrize(
        "raw,want",
        [("//tmp/x/y", "/tmp/x/y"), ("/tmp/x/y", "/tmp/x/y"),
         ("///tmp/x/y", "/tmp/x/y"), ("//a/../b", "/b")],
    )
    def test_rule_paths_collapse_to_their_filesystem_meaning(self, raw, want):
        assert path_audit._normalize_rule_path(raw) == want

    def test_collapsing_does_not_bless_a_foreign_root(self):
        """The safe direction, asserted rather than argued.

        Collapsing must only stop a correct path being falsely accused; it must
        not let a foreign path through. `//other/x` and `/other/x` normalise
        identically, so whatever the auditor did with one it does with the other.
        """
        assert path_audit._normalize_rule_path("//other/fleet/x") == \
            path_audit._normalize_rule_path("/other/fleet/x")


def _seed_script(cwd: str, config_json: str | None = None) -> str:
    """The exact bash the harness runs. Factored out so a test can pin its SHAPE.

    Without this the "production shape" claim is unverifiable: a test that builds
    its own one-arg script proves a one-arg call works, not that the harness makes
    one. The first version of this file did exactly that.
    """
    arg = f' "{config_json}"' if config_json else ""
    return f'. "{LIB}/lib-common.sh"; set +e; seed_workspace_trust "{cwd}"{arg}'


def _run_seed(cwd: str, *, config_json: str | None = None,
              home: str | None = None, config_dir: str | None = None):
    """Invoke seed_workspace_trust.

    ``config_json`` passes the optional second argument (what tests do).
    ``home`` / ``config_dir`` leave it OFF and drive the resolution the way
    ``start-bot.sh`` does — which is the shape that mattered.
    """
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    if home:
        env["HOME"] = home
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return subprocess.run(
        ["bash", "-c", _seed_script(cwd, config_json)],
        capture_output=True, text=True, env=env,
    )


class TestProductionCallShape:
    """The call shape `start-bot.sh` actually makes: ONE argument (#1325 review).

    THE DEFECT THIS EXISTS TO PREVENT, and it shipped once: every test passed an
    explicit second argument, so every test exercised a path production never
    takes. The fixture was strictly more complete than reality — the direction
    that hides the bug — and the suite stayed green while the one-arg call
    resolved to `$HOME/.claude/.claude.json`, a file that does not exist and that
    nothing reads.

    That is the third instance of one shape in 24 hours (a payload fixture
    defaulting a field the documented command omits; an alert harness with no
    sender in it). **The test was easier than production.** So this class calls
    it the production way and asserts on the RESOLVED PATH, not just the effect.
    """

    def test_one_arg_with_no_config_dir_writes_the_live_config(self, tmp_path):
        """Default population: every shared-account bot.

        `$HOME/.claude/.config.json` is where Claude Code keeps `projects[]` when
        `CLAUDE_CONFIG_DIR` is unset — measured: it carries trust entries for
        scratch dirs from other bots' live sessions today, while
        `$HOME/.claude.json` holds 29 trust-flagged projects frozen at the
        installed binary's own date and has not been written in 16 days.
        """
        home = tmp_path / "home"
        home.mkdir()
        _run_seed("/bots/alpha", home=str(home))
        live = home / ".claude" / ".config.json"
        assert live.is_file(), f"nothing written; tree was {list(home.rglob('*'))}"
        data = json.loads(live.read_text())
        assert data["projects"]["/bots/alpha"]["hasTrustDialogAccepted"] is True
        # The two paths that LOOK right and are read by nothing.
        assert not (home / ".claude" / ".claude.json").exists()
        assert not (home / ".claude.json").exists()

    def test_one_arg_with_config_dir_set_writes_that_dir(self, tmp_path):
        """The other branch, also measured: an isolated config dir gets
        `.claude.json` — claude creates exactly that filename there, unseeded."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        _run_seed("/bots/beta", config_dir=str(cfg))
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["projects"]["/bots/beta"]["hasTrustDialogAccepted"] is True
        assert not (cfg / ".config.json").exists()

    def test_start_bot_calls_it_with_exactly_one_argument(self):
        """Pin the call site itself.

        Without this the resolution can be correct and the caller still wrong —
        which is precisely what happened. Reading the shipped script rather than
        trusting that it matches the tests.
        """
        text = (LIB / "start-bot.sh").read_text()
        calls = [l.strip() for l in text.splitlines() if "seed_workspace_trust" in l
                 and not l.strip().startswith("#")]
        assert calls, "start-bot.sh no longer seeds workspace trust"
        for call in calls:
            body = call.split("seed_workspace_trust", 1)[1].split("||")[0].strip()
            assert body.count('"') == 2, f"expected one quoted arg, got: {call}"


class TestWorkspaceTrustSeed:
    """#970: the composed settings.local.json is ignored until the workspace is trusted.

    Measured on this host: 21 of 21 production bot dirs had NO project entry,
    while the throwaway bots the validation harnesses create DID — because those
    harnesses seed trust and the production boot path never did. **The test
    environment differed from production in exactly the variable under test**,
    which is why this survived.
    """

    def _cfg(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True,
            "oauthAccount": {"emailAddress": "someone@example.com"},
            "projects": {
                "/existing/one": {"hasTrustDialogAccepted": True, "history": ["a"]},
                "/existing/two": {"hasTrustDialogAccepted": False},
            },
            "otherKey": [1, 2, 3],
        }))
        return cfg

    def test_it_trusts_the_named_workspace(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _run_seed("/bots/alpha", config_json=str(cfg / ".claude.json"))
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["projects"]["/bots/alpha"]["hasTrustDialogAccepted"] is True

    def test_it_does_not_clobber_the_operator_config(self, tmp_path):
        """The property that makes this safe to run on every boot of every bot.

        `seed_claude_auth_and_trust` WRITES THE WHOLE FILE and is correct only for
        a throwaway harness config. Pointed at the operator's real `~/.claude.json`
        it would destroy every other project entry, their history and their
        settings — so this asserts survival field by field rather than trusting
        that a merge is a merge.
        """
        cfg = self._cfg(tmp_path)
        _run_seed("/bots/alpha", config_json=str(cfg / ".claude.json"))
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["projects"]["/existing/one"] == {
            "hasTrustDialogAccepted": True, "history": ["a"]
        }
        assert data["projects"]["/existing/two"]["hasTrustDialogAccepted"] is False
        assert data["oauthAccount"]["emailAddress"] == "someone@example.com"
        assert data["otherKey"] == [1, 2, 3]
        assert data["hasCompletedOnboarding"] is True

    def test_it_is_idempotent_and_byte_stable(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _run_seed("/bots/alpha", config_json=str(cfg / ".claude.json"))
        first = (cfg / ".claude.json").read_bytes()
        _run_seed("/bots/alpha", config_json=str(cfg / ".claude.json"))
        assert (cfg / ".claude.json").read_bytes() == first

    def test_an_unparseable_config_is_left_alone(self, tmp_path):
        """Not ours to repair — and overwriting it would be the clobber this avoids."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text("{ this is not json")
        _run_seed("/bots/alpha", config_json=str(cfg / ".claude.json"))
        assert (cfg / ".claude.json").read_text() == "{ this is not json"

    def test_it_creates_a_config_when_none_exists(self, tmp_path):
        cfg = tmp_path / "cfg"
        _run_seed("/bots/alpha", config_json=str(cfg / ".claude.json"))
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["projects"]["/bots/alpha"]["hasTrustDialogAccepted"] is True


class TestTheHarnessItselfTakesProductionShape:
    """A test of the test — because "it passes the right path explicitly" is the
    same defect wearing a correct value (#1325 review, dara).

    The original bug survived because every test supplied an explicit second
    argument. Fixing the resolution and then asserting it with an explicit
    argument would leave that hole exactly where it was, just harder to see. So
    the harness's own call shape is OBSERVED at runtime here, not read.
    """

    def test_the_production_helper_really_passes_one_argument(self, tmp_path):
        """Shadow the function and capture what it actually receives.

        Asserts on `$#` as bash sees it, so a helper that quietly grew a second
        argument fails here rather than passing with a correct-looking value.
        """
        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env["HOME"] = str(home)
        # Take the harness's OWN script and shadow the function inside it, so
        # what is observed is what _run_seed actually executes — not a
        # look-alike built here, which would prove nothing about the harness.
        base = _seed_script("/bots/alpha")
        assert base.endswith('seed_workspace_trust "/bots/alpha"'), base
        script = base.replace(
            'seed_workspace_trust "/bots/alpha"',
            'seed_workspace_trust() { echo "ARGC=$#"; echo "ARG2=${2:-UNSET}"; '
            'echo "CCD=${CLAUDE_CONFIG_DIR:-UNSET}"; echo "RESOLVED=$(claude_config_json)"; }; '
            'seed_workspace_trust "/bots/alpha"',
        )
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
        assert "ARGC=1" in out, out
        assert "ARG2=UNSET" in out, out
        assert "CCD=UNSET" in out, out
        assert f"RESOLVED={home}/.claude/.config.json" in out, out

    def test_the_harness_script_carries_exactly_one_argument(self):
        """Pin the constructed command, not its effect.

        `_run_seed(cwd)` — the production-shape call — must emit a script whose
        invocation has one quoted argument. A helper that regained a default
        second argument fails HERE, loudly, rather than passing with a
        correct-looking path.
        """
        script = _seed_script("/bots/alpha")
        invocation = script.split("seed_workspace_trust", 1)[1]
        assert invocation.strip() == '"/bots/alpha"', invocation
        assert _seed_script("/bots/alpha", "/x/y.json").strip().endswith(
            'seed_workspace_trust "/bots/alpha" "/x/y.json"'
        )

    def test_the_call_site_and_the_harness_agree(self):
        """Both must be one-arg. Either alone can be right while the pair is not."""
        call = [l for l in (LIB / "start-bot.sh").read_text().splitlines()
                if "seed_workspace_trust" in l and not l.strip().startswith("#")]
        assert len(call) == 1, call
        body = call[0].split("seed_workspace_trust", 1)[1].split("||")[0].strip()
        assert body.count('"') == 2 and body == '"$BOT_DIR"', body


class TestLivePathWasDeterminedByLiveness:
    """HOW the live config path was determined, pinned — because the dead file
    wins every other heuristic.

    Three candidates existed; two look right:

      $HOME/.claude.json          102KB, 29 projects, 13 bot dirs — mtime 2026-08-05
      $HOME/.claude/.config.json   70KB, 14 projects,  4 bot dirs — mtime 2026-08-21
      $HOME/.claude/.claude.json  ABSENT

    The STALE one is bigger, has more projects, and has more bot-dir entries. A
    reader reasoning "the authoritative config is the fuller one" lands on the
    dead file — which is what happened twice before this landed. **Only recency
    discriminates, and only if you look at it.**
    """

    def test_the_resolver_returns_neither_decoy(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env["HOME"] = str(home)
        out = subprocess.run(
            ["bash", "-c", f'. "{LIB}/lib-common.sh"; claude_config_json'],
            capture_output=True, text=True, env=env,
        ).stdout.strip()
        assert out == f"{home}/.claude/.config.json"
        # The two that look right and are read by nothing.
        assert out != f"{home}/.claude.json"
        assert out != f"{home}/.claude/.claude.json"
