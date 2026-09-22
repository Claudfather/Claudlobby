"""The vault git-state guard (#1720) — scope before verb.

A live host's vault clone sat on a side branch for a month: 59 commits of
knowledge off the default branch, 53 on no remote, and the rebase that tried to
reconcile them was killed mid-pick and left the tree detached for twelve days
with a fleet's mission and manifest gone from disk. Prose saying "do not"
existed. This is the mechanism.

**The tests are weighted toward the ALLOW side on purpose.** The dangerous
failure is not missing a rebase in the vault — Claudron's own sync refuses a
side branch as the belt to this hook's braces. It is refusing legitimate git
work in every bot's `projects/` checkout at once, because a composed hook is
live the instant `generate` writes it and there is no canary window. So every
deny case here has an allow twin that differs only in WHERE the command points.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "lib" / "vault-git-guard.sh"


def _decider():
    spec = importlib.util.spec_from_file_location(
        "vault_git_decide", REPO / "lib" / "vault-git-decide.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


D = _decider()


@pytest.fixture()
def tree(tmp_path):
    vault = tmp_path / "vault"
    proj = tmp_path / "projects" / "repo"
    vault.mkdir(parents=True)
    proj.mkdir(parents=True)
    return os.path.realpath(vault), os.path.realpath(proj)


def _run(payload: dict, vault: str | None) -> tuple[int, str]:
    env = dict(os.environ)
    if vault is None:
        env.pop("CLAUDRON_VAULT_PATH", None)
    else:
        env["CLAUDRON_VAULT_PATH"] = vault
    p = subprocess.run(["bash", str(GUARD)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout


def _decision(out: str) -> str | None:
    if not out.strip():
        return None
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


# --- the acceptance rows, through the REAL hook -----------------------------

class TestTheHookEndToEnd:
    def test_checkout_inside_the_vault_is_denied(self, tree):
        vault, _ = tree
        rc, out = _run({"tool_name": "Bash", "cwd": vault,
                        "tool_input": {"command": "git checkout -b x"}}, vault)
        assert rc == 0
        assert _decision(out) == "deny"
        assert "claudron sync" in out and "projects/" in out

    def test_the_same_command_in_a_projects_checkout_is_allowed(self, tree):
        """The twin. A guard that denied this would break every bot's own work."""
        vault, proj = tree
        rc, out = _run({"tool_name": "Bash", "cwd": proj,
                        "tool_input": {"command": "git checkout -b x"}}, vault)
        assert rc == 0 and _decision(out) is None

    def test_dash_C_into_the_vault_is_denied_from_outside_it(self, tree):
        vault, proj = tree
        _, out = _run({"tool_name": "Bash", "cwd": proj,
                       "tool_input": {"command": f"git -C {vault} rebase origin/main"}}, vault)
        assert _decision(out) == "deny"

    def test_cd_into_the_vault_then_reset_is_denied(self, tree):
        vault, proj = tree
        _, out = _run({"tool_name": "Bash", "cwd": proj,
                       "tool_input": {"command": f"cd {vault} && git reset --hard"}}, vault)
        assert _decision(out) == "deny"

    def test_a_read_only_command_in_the_vault_is_allowed(self, tree):
        vault, proj = tree
        _, out = _run({"tool_name": "Bash", "cwd": proj,
                       "tool_input": {"command": f"git -C {vault} status"}}, vault)
        assert _decision(out) is None

    def test_claudron_sync_is_allowed(self, tree):
        vault, _ = tree
        _, out = _run({"tool_name": "Bash", "cwd": vault,
                       "tool_input": {"command": "claudron sync"}}, vault)
        assert _decision(out) is None

    def test_a_bot_with_no_vault_gets_no_decision(self, tree):
        """Nothing to guard, so no claim to make — even on the worst verb."""
        vault, _ = tree
        _, out = _run({"tool_name": "Bash", "cwd": vault,
                       "tool_input": {"command": "git reset --hard"}}, None)
        assert _decision(out) is None

    def test_a_non_Bash_tool_gets_no_decision(self, tree):
        vault, _ = tree
        _, out = _run({"tool_name": "Read", "cwd": vault,
                       "tool_input": {"command": "git reset --hard"}}, vault)
        assert _decision(out) is None

    def test_a_malformed_payload_FAILS_OPEN(self, tree):
        """Refusing every git command fleet-wide is worse than the hazard."""
        vault, _ = tree
        env = dict(os.environ, CLAUDRON_VAULT_PATH=vault)
        p = subprocess.run(["bash", str(GUARD)], input="Bash git {not json",
                           capture_output=True, text=True, env=env, timeout=60)
        assert p.returncode == 0 and _decision(p.stdout) is None

    def test_a_payload_that_is_not_git_at_all_is_untouched(self, tree):
        vault, _ = tree
        _, out = _run({"tool_name": "Bash", "cwd": vault,
                       "tool_input": {"command": "ls -la"}}, vault)
        assert _decision(out) is None


# --- the decision rule, unit level ------------------------------------------

class TestScopeIsDecidedBeforeTheVerb:
    @pytest.mark.parametrize("verb", [
        "checkout", "switch", "rebase", "reset", "merge",
        "stash", "clean", "worktree", "cherry-pick", "revert", "am",
    ])
    def test_every_state_verb_is_denied_in_the_vault_and_allowed_outside(self, tree, verb):
        vault, proj = tree
        assert D.decide(f"git {verb} x", vault, vault)[0] == "deny"
        assert D.decide(f"git {verb} x", vault, proj)[0] == "allow", (
            f"{verb} must stay usable in a bot's own checkout"
        )

    @pytest.mark.parametrize("cmd", [
        "git status", "git log --oneline", "git diff", "git add -A",
        "git commit -m x", "git fetch", "git pull --ff-only", "git push",
        "git branch", "git remote -v", "git show HEAD",
    ])
    def test_safe_verbs_are_allowed_even_in_the_vault(self, tree, cmd):
        vault, _ = tree
        assert D.decide(cmd, vault, vault)[0] == "allow"

    @pytest.mark.parametrize("cmd", [
        "git commit --amend", "git push --force", "git push -f",
        "git push --force-with-lease", "git branch -D old", "git branch -m a b",
    ])
    def test_conditional_verbs_deny_only_with_their_flag(self, tree, cmd):
        vault, _ = tree
        assert D.decide(cmd, vault, vault)[0] == "deny"

    def test_a_verb_NAMED_in_a_flag_is_not_a_verb(self, tree):
        """`git log --grep=reset` is a read. Matching the whole command line
        instead of the subcommand token would refuse it."""
        vault, _ = tree
        assert D.decide("git log --grep=reset", vault, vault)[0] == "allow"
        assert D.decide("git log --grep=rebase", vault, vault)[0] == "allow"

    def test_an_unreadable_target_falls_back_to_cwd_not_to_allow(self, tree):
        """The fallback direction is the whole point. A variable or glob is
        what someone reaches for when doing something wide, so treating
        'cannot read' as 'must be outside' would put the blind spot exactly
        where the risk is."""
        vault, proj = tree
        assert D.decide('git -C "$SOMEVAR" rebase', vault, vault)[0] == "deny"
        assert D.decide('git -C "$SOMEVAR" rebase', vault, proj)[0] == "allow"

    def test_no_cwd_and_an_unreadable_target_is_unresolved_not_denied(self, tree):
        vault, _ = tree
        verdict, _ = D.decide('git -C "$SOMEVAR" rebase', vault, None)
        assert verdict == "unresolved"

    def test_a_relative_path_resolves_against_cwd(self, tree):
        vault, proj = tree
        parent = os.path.dirname(vault)
        assert D.decide("git -C vault rebase", vault, parent)[0] == "deny"
        assert D.decide("git -C repo rebase", vault, os.path.dirname(proj))[0] == "allow"

    def test_a_path_merely_PREFIXED_by_the_vault_is_outside_it(self, tree):
        """`/x/vault-backup` is not inside `/x/vault`. A bare startswith would
        say otherwise and refuse work in an unrelated directory."""
        vault, _ = tree
        assert D.decide(f"git -C {vault}-backup rebase", vault, None)[0] == "allow"

    def test_unbalanced_quotes_do_not_crash_the_decider(self, tree):
        vault, _ = tree
        verdict, _ = D.decide('git -C "unterminated rebase', vault, vault)
        assert verdict in {"allow", "deny", "unresolved"}


class TestTheRealHostShape:
    """The layout the synthetic fixtures got wrong, pinned.

    Every test above builds vault and projects as SIBLINGS. On a live host they
    are not: a bot's `projects/<repo>` checkout nests INSIDE the vault
    directory — measured during this change's canary at seven segments below
    it. Under a prefix-based scope rule that refuses `checkout`, `rebase` and
    `reset` in every bot's own repository, fleet-wide, the instant `generate`
    runs. It reached a live canary because no fixture had the real shape.

    So the scope rule asks which REPOSITORY a path belongs to, not which
    directory it sits under, and these tests build the nesting explicitly.
    """

    @pytest.fixture()
    def nested(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / ".git").mkdir(parents=True)
        # the real shape: bots live under the vault, each checkout its own repo
        checkout = vault / "home" / "f" / "runtime" / "bots" / "b" / "projects" / "repo"
        (checkout / ".git").mkdir(parents=True)
        loose = vault / "knowledge" / "notes"      # vault's own tree, no repo
        loose.mkdir(parents=True)
        return (os.path.realpath(vault), os.path.realpath(checkout),
                os.path.realpath(loose))

    def test_a_nested_checkout_under_the_vault_is_NOT_the_vault(self, nested):
        vault, checkout, _ = nested
        assert D.decide("git switch -c feature", vault, checkout)[0] == "allow"
        assert D.decide("git rebase origin/main", vault, checkout)[0] == "allow"
        assert D.decide("git reset --hard", vault, checkout)[0] == "allow"

    def test_the_vault_itself_is_still_guarded(self, nested):
        """The control: the fix must not have disarmed the guard entirely."""
        vault, _, _ = nested
        assert D.decide("git switch -c probe", vault, vault)[0] == "deny"
        assert D.decide("git reset --hard", vault, vault)[0] == "deny"

    def test_a_path_in_the_vaults_own_tree_is_guarded(self, nested):
        """Under the vault and in no nested repo — the vault's own files."""
        vault, _, loose = nested
        assert D.decide("git checkout -- .", vault, loose)[0] == "deny"

    def test_dash_C_into_a_nested_checkout_is_allowed(self, nested):
        vault, checkout, _ = nested
        assert D.decide(f"git -C {checkout} rebase", vault, vault)[0] == "allow"

    def test_dash_C_into_the_vault_from_a_nested_checkout_is_denied(self, nested):
        vault, checkout, _ = nested
        assert D.decide(f"git -C {vault} rebase", vault, checkout)[0] == "deny"
