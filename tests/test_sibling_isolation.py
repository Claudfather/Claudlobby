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


def _run_seed(cwd: str, cfg: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}/lib-common.sh"; set +e; seed_workspace_trust "{cwd}" "{cfg}"'],
        capture_output=True, text=True,
    )


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
        _run_seed("/bots/alpha", str(cfg))
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
        _run_seed("/bots/alpha", str(cfg))
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
        _run_seed("/bots/alpha", str(cfg))
        first = (cfg / ".claude.json").read_bytes()
        _run_seed("/bots/alpha", str(cfg))
        assert (cfg / ".claude.json").read_bytes() == first

    def test_an_unparseable_config_is_left_alone(self, tmp_path):
        """Not ours to repair — and overwriting it would be the clobber this avoids."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text("{ this is not json")
        _run_seed("/bots/alpha", str(cfg))
        assert (cfg / ".claude.json").read_text() == "{ this is not json"

    def test_it_creates_a_config_when_none_exists(self, tmp_path):
        cfg = tmp_path / "cfg"
        _run_seed("/bots/alpha", str(cfg))
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["projects"]["/bots/alpha"]["hasTrustDialogAccepted"] is True
