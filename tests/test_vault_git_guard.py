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


class TestEveryFlagThatPointsTheInvocation:
    """`-C` was not the only way to aim a git command (review of #1725).

    The guard shipped reading `-C` alone, so the two flags a script reaches for
    when it walks several repositories WITHOUT `cd`-ing into each resolved
    their scope from `cwd` and never met the vault check. That is
    accident-shaped rather than evasion-shaped, which is the threat model here:
    the outage began with a bot running `rebase` in the vault because its
    instructions were ambiguous.

    Every deny below has an allow twin differing only in WHERE it points.
    """

    @pytest.fixture()
    def nested(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / ".git").mkdir(parents=True)
        outside = tmp_path / "elsewhere" / "repo"
        (outside / ".git").mkdir(parents=True)
        return os.path.realpath(vault), os.path.realpath(outside)

    @pytest.mark.parametrize("flag", ["--git-dir", "--work-tree"])
    @pytest.mark.parametrize("joiner", [" ", "="])
    def test_a_scope_flag_into_the_vault_is_denied_from_outside_it(
            self, nested, flag, joiner):
        vault, outside = nested
        target = f"{vault}/.git" if flag == "--git-dir" else vault
        assert D.decide(
            f"git {flag}{joiner}{target} checkout main", vault, outside
        )[0] == "deny"

    @pytest.mark.parametrize("joiner", [" ", "="])
    def test_work_tree_pointed_elsewhere_is_allowed_from_the_vault(
            self, nested, joiner):
        """The twin. `--work-tree` REPLACES the working tree, so this command
        cannot touch the vault however the shell got here. A hook that denied
        it would refuse legitimate work in a bot's own checkout, which is the
        failure this guard fears most."""
        vault, outside = nested
        assert D.decide(
            f"git --work-tree{joiner}{outside} checkout main", vault, vault
        )[0] == "allow"

    @pytest.mark.parametrize("joiner", [" ", "="])
    def test_git_dir_elsewhere_does_NOT_take_cwd_out_of_scope(
            self, nested, joiner):
        """`--git-dir` is not `--work-tree`, and the difference is measured.

        On git 2.39.5, a `--git-dir` naming another repository with no
        `--work-tree` makes git treat the CURRENT DIRECTORY as that
        repository's working tree: run inside a vault,
        `git --git-dir=<other>/.git reset --hard` wrote the other repo's
        tracked files into the vault's tree. So pointing the repo elsewhere
        must not subtract the place the command is standing.
        """
        vault, outside = nested
        assert D.decide(
            f"git --git-dir{joiner}{outside}/.git reset --hard", vault, vault
        )[0] == "deny"

    @pytest.mark.parametrize("joiner", [" ", "="])
    def test_the_twin_for_that_one_is_the_same_command_run_outside(
            self, nested, joiner):
        """Standing outside the vault, the identical command touches nothing
        of the vault's and stays allowed — so the rule above costs no
        legitimate work."""
        vault, outside = nested
        assert D.decide(
            f"git --git-dir{joiner}{outside}/.git reset --hard", vault, outside
        )[0] == "allow"

    def test_an_explicit_work_tree_does_replace_cwd_even_with_git_dir(self, nested):
        """Both flags given and both pointed away: nothing of the vault's is
        reachable, so it is allowed even from inside the vault."""
        vault, outside = nested
        assert D.decide(
            f"git --work-tree={outside} --git-dir={outside}/.git reset --hard",
            vault, vault)[0] == "allow"

    def test_the_verdict_is_ANY_target_not_the_first_one(self, nested):
        """A harmless-looking `-C` must not launder the flag beside it:
        `--git-dir` still moves the vault's refs whatever `-C` says."""
        vault, outside = nested
        assert D.decide(
            f"git -C {outside} --git-dir={vault}/.git reset --hard",
            vault, outside)[0] == "deny"

    def test_a_linked_worktrees_git_dir_resolves_to_the_vault(self, nested):
        """`--git-dir` for a linked worktree points inside the vault's own
        `.git`, so the walk-up lands on the vault and the command is guarded."""
        vault, outside = nested
        wt = f"{vault}/.git/worktrees/wt"
        assert D.decide(f"git --git-dir={wt} reset --hard", vault, outside)[0] == "deny"


class TestPullIsOnlyEverAFastForward:
    """`pull` reached neither table when the guard shipped (review of #1725).

    So `git pull --rebase` was ALLOWED inside the vault while `git rebase` was
    denied three lines away — the same operation under a more ordinary
    spelling. A bare `git pull` is the same hole without needing a flag at all:
    under `pull.rebase` it rebases the vault, which is the twelve-day outage's
    own mechanism.

    The vault's stated invariant is that it only ever fast-forwards, so the
    fast-forward spelling passes and nothing else does.
    """

    @pytest.mark.parametrize("cmd", [
        "git pull",
        "git pull origin main",
        "git pull --rebase",
        "git pull -r",
        "git pull --ff-only --rebase",
    ])
    def test_pull_without_a_fast_forward_guarantee_is_denied_in_the_vault(
            self, tree, cmd):
        vault, _ = tree
        assert D.decide(cmd, vault, vault)[0] == "deny"

    @pytest.mark.parametrize("cmd", [
        "git pull",
        "git pull origin main",
        "git pull --rebase",
        "git pull -r",
        "git pull --ff-only --rebase",
    ])
    def test_the_same_pull_in_a_projects_checkout_is_allowed(self, tree, cmd):
        """The twin, and the load-bearing half: `pull --rebase` is ordinary
        work in a bot's own repository and must stay untouched."""
        vault, proj = tree
        assert D.decide(cmd, vault, proj)[0] == "allow"

    def test_pull_ff_only_stays_allowed_in_the_vault(self, tree):
        """The positive control. The guardrail page names `pull --ff-only` as
        permitted; a fix that denied it would contradict the page it enforces."""
        vault, _ = tree
        assert D.decide("git pull --ff-only", vault, vault)[0] == "allow"
        assert D.decide("git pull --ff-only origin main", vault, vault)[0] == "allow"

    def test_the_refusal_names_the_missing_flag(self, tree):
        """The detail reaches the operator inside the hook's deny message and
        the plane's `vault_guard_denied` event, so it has to say which spelling
        would have passed."""
        vault, _ = tree
        assert D.decide("git pull", vault, vault)[1] == "pull without --ff-only"
        assert D.decide("git pull --rebase", vault, vault)[1] == "pull --rebase"
