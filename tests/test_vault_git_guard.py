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
    def test_work_tree_elsewhere_does_NOT_take_cwd_out_of_scope(
            self, nested, joiner):
        """**This test previously asserted the opposite, and it was wrong.**

        It read `--work-tree` as replacing the whole invocation's target, so it
        expected `allow`. `--work-tree` relocates the working TREE and not the
        git directory, which is still discovered from `cwd` — so a command
        carrying only `--work-tree`, run inside the vault, acts on the VAULT's
        refs.

        Measured on git 2.39.5 against a real repository: with the vault's HEAD
        on `main`, `git --work-tree=<elsewhere> checkout other` run from inside
        the vault printed `Switched to branch 'other'` and left the vault's own
        HEAD on `other`. That is the side-branch state that caused the outage
        this guard exists for, produced by a command the guard was allowing.
        """
        vault, outside = nested
        assert D.decide(
            f"git --work-tree{joiner}{outside} checkout main", vault, vault
        )[0] == "deny"

    @pytest.mark.parametrize("joiner", [" ", "="])
    def test_the_twin_for_work_tree_is_the_same_command_run_outside(
            self, nested, joiner):
        """Standing outside, nothing of the vault's is reachable — so the rule
        above costs no legitimate work in a bot's own checkout."""
        vault, outside = nested
        assert D.decide(
            f"git --work-tree{joiner}{outside} checkout main", vault, outside
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

    def test_only_BOTH_flags_together_take_cwd_out_of_scope(self, nested):
        """The two axes are independent, so it takes both to leave the vault.

        Measured: with both pointed away and run from inside the vault, the
        vault's HEAD stayed on `main` and the other repository's moved. This is
        the one shape where a scope flag genuinely removes `cwd`."""
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


class TestAnOptionTheGuardDoesNotRecognise:
    """The predicate reached SIX holes by enumerating what is dangerous.

    Each was a different way to point a command somewhere the guard did not
    look, and each was found by a different method — reading the code, mutating
    it, and running real git. The pattern, not any one hole, is the finding:
    git's scope flags do not compose the way the obvious reading suggests, so
    "the ones I thought of" is not a closed set and never becomes one.

    So the model is inverted. The guard enumerates the pre-verb options it
    UNDERSTANDS, with the arity of each, and anything else stops the read. An
    unmodelled flag becomes a refusal instead of a bypass — but only for a
    command that is vault-bound, so a bot's own checkout is untouched however
    exotic its flags.
    """

    @pytest.fixture()
    def tree2(self, tmp_path):
        vault = tmp_path / "vault"
        proj = tmp_path / "projects" / "repo"
        vault.mkdir(parents=True)
        proj.mkdir(parents=True)
        return os.path.realpath(vault), os.path.realpath(proj)

    def test_an_unknown_option_in_the_vault_is_refused(self, tree2):
        vault, _ = tree2
        verdict, detail = D.decide("git --some-future-flag checkout main",
                                   vault, vault)
        assert verdict == "deny"
        assert "does not recognise" in detail

    def test_this_refusal_is_UNCONDITIONAL_and_has_no_allow_twin(self, tree2):
        """**This test previously asserted the opposite, and it was wrong.**

        It pinned "refuse only once scope says vault-bound", which sounds like
        the careful version and is the bug: scope computed before the parser
        stopped is a statement about a PARTIAL read. Review demonstrated the
        consequence — the same command allowed or denied depending on which
        flag came first, because a real `--git-dir` sitting behind an unmodelled
        flag was never reached (pinned in `test_the_verdict_does_not_depend_on
        _flag_ORDER`).

        So the refusal is unconditional, and it is the one deny in this file
        with no allow twin. That is deliberate: a twin would assert that some
        command this guard could not finish reading is safe, which is the claim
        this branch exists to stop making. The cost is bounded and measured
        rather than assumed — every pre-verb flag in the fleet's own scripted
        git usage is already in `GLOBAL_FLAGS`.
        """
        vault, proj = tree2
        assert D.decide("git --some-future-flag checkout main",
                        vault, proj)[0] == "deny"
        assert D.decide("git --some-future-flag status", vault, proj)[0] == "deny"

    def test_the_verdict_does_not_depend_on_flag_ORDER(self, tree2):
        """The finding that made the refusal unconditional. Same command, two
        orderings, and before the fix they disagreed."""
        vault, proj = tree2
        first = D.decide(
            f"git --super-prefix foo/ --git-dir={vault}/.git reset --hard",
            vault, proj)
        second = D.decide(
            f"git --git-dir={vault}/.git --super-prefix foo/ reset --hard",
            vault, proj)
        assert first[0] == second[0] == "deny", (first, second)

    def test_a_modelled_flag_outside_the_vault_is_still_the_twin(self, tree2):
        """The allow twin that DOES hold, and the one that matters for the
        fleet: a flag the guard understands, pointed at a bot's own checkout,
        is untouched."""
        vault, proj = tree2
        assert D.decide("git --no-pager checkout main", vault, proj)[0] == "allow"
        assert D.decide(f"git -C {proj} reset --hard", vault, vault)[0] == "allow"

    def test_an_unknown_option_hides_the_verb_which_is_why_it_refuses(self, tree2):
        """Not merely unrecognised — UNREADABLE past that point. An unknown
        flag may take a separated value, so the token after it may be that
        value rather than the subcommand; the guard cannot tell. Measured on
        git 2.39.5, `git --super-prefix x/ checkout main` really does run
        `checkout`, while a parser assuming zero arity reads `x/` as the verb,
        finds nothing dangerous, and allows it."""
        vault, proj = tree2
        assert D.decide("git --super-prefix x/ checkout main",
                        vault, vault)[0] == "deny"
        # the second half of this test asserted `allow` here until the refusal
        # became unconditional; see the test above for why that was the bug
        assert D.decide("git --super-prefix x/ checkout main",
                        vault, proj)[0] == "deny"

    @pytest.mark.parametrize("cmd", [
        "git --no-pager log",
        "git -c core.pager=cat log",
        "git --literal-pathspecs status",
        "git --no-optional-locks status",
        "git -P diff",
    ])
    def test_known_options_with_safe_verbs_are_still_untouched(self, tree2, cmd):
        """The control on the inversion: modelled flags must not start
        refusing ordinary reads inside the vault."""
        vault, _ = tree2
        assert D.decide(cmd, vault, vault)[0] == "allow"

    def test_a_value_taking_option_does_not_hide_the_verb(self, tree2):
        """`-c` takes a separated value, so a parser that skipped one token
        would read `user.name=x` as the verb and allow the checkout."""
        vault, _ = tree2
        assert D.decide("git -c user.name=x checkout main", vault, vault)[0] == "deny"
        assert D.decide("git -c user.name=x status", vault, vault)[0] == "allow"


class TestScopeSetThroughTheEnvironment:
    """Finding nine: a second CHANNEL, not another flag (review of #1725).

    Everything the flag allowlist does is correct and simply does not apply to
    a caller that exports scope instead of passing it. Measured on git 2.39.5:
    `GIT_DIR=<vault>/.git git symbolic-ref --short HEAD`, run from outside the
    vault, answers the VAULT's branch.

    The bound is stated in the module docstring rather than implied: a variable
    exported by an EARLIER tool call cannot be seen here at all, because the
    hook is handed one command and no environment.
    """

    @pytest.fixture()
    def tree3(self, tmp_path):
        vault = tmp_path / "vault"
        proj = tmp_path / "projects" / "repo"
        vault.mkdir(parents=True)
        proj.mkdir(parents=True)
        return os.path.realpath(vault), os.path.realpath(proj)

    # BOTH HALVES OF THE PAIR, EVERY TIME. Before this, `GIT_DIR` had four
    # tests here and `GIT_WORK_TREE` had one -- the same asymmetry that made
    # `--git-dir` and `--work-tree` holes five and six as flags. Parametrising
    # over the pair makes the symmetry structural instead of remembered:
    # a future variable added to ENV_SCOPE gets every case by adding one row.
    #
    # `vault` is what points the variable AT the vault on that axis, `other`
    # what points it away, and `flag` is the command-line twin that must
    # override it.
    PAIR = [
        ("GIT_DIR", "--git-dir", "{vault}/.git", "{proj}/.git"),
        ("GIT_WORK_TREE", "--work-tree", "{vault}", "{proj}"),
    ]

    @pytest.mark.parametrize("var,flag,vault_t,other_t", PAIR)
    @pytest.mark.parametrize("spelling", [
        "{var}={t} git", "export {var}={t}; git", "env {var}={t} git",
    ])
    def test_every_spelling_of_either_variable_is_seen(
            self, tree3, var, flag, vault_t, other_t, spelling):
        """All three spellings arrive as one `NAME=value` token, which is why
        one rule covers the lot -- for both variables."""
        vault, proj = tree3
        t = vault_t.format(vault=vault, proj=proj)
        cmd = spelling.format(var=var, t=t) + " checkout main"
        assert D.decide(cmd, vault, proj)[0] == "deny"

    @pytest.mark.parametrize("var,flag,vault_t,other_t", PAIR)
    def test_the_twin_is_the_variable_pointed_elsewhere(
            self, tree3, var, flag, vault_t, other_t):
        vault, proj = tree3
        t = other_t.format(vault=vault, proj=proj)
        assert D.decide(f"{var}={t} git reset --hard", vault, proj)[0] == "allow"

    @pytest.mark.parametrize("var,flag,vault_t,other_t", PAIR)
    def test_a_flag_beats_the_variable_on_each_axis(
            self, tree3, var, flag, vault_t, other_t):
        """git's own precedence, measured on both axes:
        `GIT_DIR=<a> git --git-dir=<b> rev-parse --absolute-git-dir` answers
        `<b>`, and `--work-tree` likewise overrides `GIT_WORK_TREE`. Getting
        it backwards would deny work on a bot's own repo whenever a stale
        variable happened to name the vault."""
        vault, proj = tree3
        env_t = vault_t.format(vault=vault, proj=proj)
        flag_t = other_t.format(vault=vault, proj=proj)
        assert D.decide(f"{var}={env_t} git {flag}={flag_t} reset --hard",
                        vault, proj)[0] == "allow"

    @pytest.mark.parametrize("var,flag,vault_t,other_t", PAIR)
    def test_a_safe_verb_under_a_vault_bound_variable_is_still_allowed(
            self, tree3, var, flag, vault_t, other_t):
        """Scope before verb holds on this channel too -- the variable makes it
        the vault's business, and reading the vault is nobody's problem."""
        vault, proj = tree3
        t = vault_t.format(vault=vault, proj=proj)
        assert D.decide(f"{var}={t} git status", vault, proj)[0] == "allow"

    def test_the_work_tree_variable_moves_the_vaults_own_HEAD(self, tree3):
        """Named on its own because it is the harm, not just a mechanism.
        Measured on git 2.39.5: `GIT_WORK_TREE=<away> git checkout <branch>`
        run from INSIDE the vault switched the vault's own HEAD onto the other
        branch -- identical to the `--work-tree` flag that was hole six."""
        vault, proj = tree3
        assert D.decide(f"GIT_WORK_TREE={proj} git checkout other",
                        vault, vault)[0] == "deny"

    def test_an_unrelated_assignment_is_not_scope(self, tree3):
        """Only the two variables git actually honours for scope are read; a
        command that happens to set something else is ordinary work."""
        vault, proj = tree3
        assert D.decide(f"FOO={vault}/.git git reset --hard",
                        vault, proj)[0] == "allow"

    def test_the_pair_is_covered_symmetrically(self):
        """The meta-check, because this pair has been handled asymmetrically
        three times: as flags (holes five and six) and as variables (one
        modelled and named, its twin not). Every member of ENV_SCOPE must
        appear in PAIR, so adding a variable without its cases fails here."""
        assert {p[0] for p in self.PAIR} == set(D.ENV_SCOPE)


class TestAShellSeparatorAttachedToAPath:
    """Hole ten, and it was in the oldest, plainest part of the guard.

    `cd /vault; git reset --hard` tokenises as `['cd', '/vault;', ...]` — a
    semicolon needs no space before it, so it rides inside the path. The guard
    resolved `/vault;`, which is not the vault, and ALLOWED about the most
    ordinary command shape there is.

    It surfaced from making the `GIT_DIR`/`GIT_WORK_TREE` tests symmetric, and
    the asymmetry is exactly why it had been invisible: `GIT_DIR=<vault>/.git;`
    walks UP to the vault, so the enclosing-repo walk hid the defect on that
    axis while the same bug went straight through on its twin. One half of a
    pair passing by accident is what a symmetric test is for.
    """

    @pytest.fixture()
    def tree4(self, tmp_path):
        vault = tmp_path / "vault"
        proj = tmp_path / "projects" / "repo"
        vault.mkdir(parents=True)
        proj.mkdir(parents=True)
        return os.path.realpath(vault), os.path.realpath(proj)

    @pytest.mark.parametrize("sep", [";", " &&", " ;", "&"])
    def test_cd_into_the_vault_is_seen_through_any_separator(self, tree4, sep):
        vault, proj = tree4
        assert D.decide(f"cd {vault}{sep} git reset --hard",
                        vault, proj)[0] == "deny"

    @pytest.mark.parametrize("sep", [";", " &&"])
    def test_the_twin_is_cd_AWAY_from_the_vault(self, tree4, sep):
        """Standing in the vault and leaving it must stay allowed, or the fix
        would refuse a bot walking out to its own checkout."""
        vault, proj = tree4
        assert D.decide(f"cd {proj}{sep} git reset --hard",
                        vault, vault)[0] == "allow"

    def test_a_safe_verb_after_the_cd_is_still_allowed(self, tree4):
        vault, proj = tree4
        assert D.decide(f"cd {vault}; git status", vault, proj)[0] == "allow"

    @pytest.mark.parametrize("var,target", [
        ("GIT_DIR", "{vault}/.git"), ("GIT_WORK_TREE", "{vault}"),
    ])
    def test_an_exported_variable_is_seen_through_the_separator(
            self, tree4, var, target):
        """BOTH halves, because one of them passed by accident before the fix
        and would have gone on passing."""
        vault, proj = tree4
        t = target.format(vault=vault)
        assert D.decide(f"export {var}={t}; git checkout main",
                        vault, proj)[0] == "deny"


class TestShellCompositionSpellings:
    """Six live bypasses, all one class: the tokeniser vs the shell.

    `shlex.split` is a WORD SPLITTER, not a shell parser, so a metacharacter
    abutting a word changed the token and defeated the equality tests
    `decide()` is built on. `cd <vault>;git reset --hard` produced no `git`
    token at all — nothing to judge, so it was allowed.

    **These close the SPELLINGS, not the class.** The class is stated at the
    top of `vault-git-decide.py`: this catches a direct git invocation naming
    the vault, and cannot catch git reached through composition. `eval`,
    `sh -c`, a wrapper and a variable holding the path all remain open by
    construction, and no test here should be read as evidence otherwise.

    The one worth naming on its own is `(cd <vault> && git rebase --abort)`:
    that is the operation the guard's own refusal message forbids, and the one
    that started the twelve days.
    """

    @pytest.fixture()
    def tree5(self, tmp_path):
        vault = tmp_path / "vault"
        proj = tmp_path / "projects" / "repo"
        vault.mkdir(parents=True)
        proj.mkdir(parents=True)
        return os.path.realpath(vault), os.path.realpath(proj)

    @pytest.mark.parametrize("tmpl", [
        "cd {v};git reset --hard",
        "cd {v}&&git reset --hard",
        "(cd {v}; git reset --hard)",
        "(cd {v} && git rebase --abort)",
        "cd {v}; (git reset --hard)",
        "pushd {v}; git reset --hard",
        "cd {v} && git reset --hard",          # the spelling that always denied
        "cd {v}; git reset --hard",            # hole ten
    ])
    def test_composition_reaching_the_vault_is_denied(self, tree5, tmpl):
        vault, proj = tree5
        assert D.decide(tmpl.format(v=vault), vault, proj)[0] == "deny"

    @pytest.mark.parametrize("tmpl", [
        "cd {p};git reset --hard",
        "(cd {p} && git reset --hard)",
        "pushd {p}; git reset --hard",
    ])
    def test_the_twin_is_the_same_composition_pointed_elsewhere(self, tree5, tmpl):
        """Standing in the vault and composing a way OUT must stay allowed, or
        the fix refuses a bot walking to its own checkout."""
        vault, proj = tree5
        assert D.decide(tmpl.format(p=proj), vault, vault)[0] == "allow"

    def test_a_safe_verb_through_composition_is_still_allowed(self, tree5):
        vault, proj = tree5
        assert D.decide(f"(cd {vault}; git status)", vault, proj)[0] == "allow"

    def test_quoting_survives_the_operator_split(self, tree5):
        """`punctuation_chars` is quote-aware and a regex pre-split is not: a
        commit message holding shell operators must stay one token, or the
        guard starts seeing verbs inside prose."""
        vault, _ = tree5
        assert D.decide('git commit -m "a; b && c"', vault, vault)[0] == "allow"
        assert D.decide('git log --grep="reset; rebase"', vault, vault)[0] == "allow"

    def test_the_abort_the_refusal_message_forbids_is_itself_refused(self, tree5):
        """The guard's deny text says "do NOT abort or reset it". Until this
        round, the idiomatic way to run that abort walked straight past."""
        vault, proj = tree5
        for cmd in (f"(cd {vault} && git rebase --abort)",
                    f"cd {vault};git rebase --abort",
                    f"pushd {vault}; git rebase --abort"):
            assert D.decide(cmd, vault, proj)[0] == "deny", cmd


class TestTheComposedFormsAreATripwireBeforeTheContinuationFix:
    """#1759 review: pinned BEFORE the continuation fix touches anything.

    Measured tonight (lib/vault-git-base-rate.py): command substitution,
    xargs and a pipeline all leave `git` as a bare token, so the guard SEES
    them -- it is stronger than #1725 credited. A continuation fix that
    changes tokenisation could silently lose these. This class is the
    tripwire, not a claim about the fix: it must be green before the fix
    below is written and stay green after."""

    def test_command_substitution_reaching_the_vault_is_still_denied(self, tree):
        vault, proj = tree
        assert (
            D.decide(
                f"X=$(cd {vault} && git status); cd {vault} && git checkout main",
                vault,
                proj,
            )[0]
            == "deny"
        )

    def test_xargs_reaching_the_vault_is_still_denied(self, tree):
        vault, proj = tree
        assert (
            D.decide(
                f"cd {vault} && echo x | xargs -I{{}} git reset --hard", vault, proj
            )[0]
            == "deny"
        )

    def test_a_pipeline_reaching_the_vault_is_still_denied(self, tree):
        vault, proj = tree
        assert (
            D.decide(f"cd {vault} && true && git rebase --abort", vault, proj)[0]
            == "deny"
        )


class TestTheBackslashContinuationBypass:
    """#1759. `... && \\` then a newline: the escaped newline is not removed,
    it is GLUED onto the next word, so the token is `"\ngit"` and `_unseparate`
    -- which strips `;&` off the END of a token -- leaves it unequal to `git`.
    Nothing is composed here. No subshell, no eval, no variable, no wrapper.
    It is the guard's own tokeniser failing on a DIRECT invocation, the exact
    class this guard's docstring claims to catch, in the most ordinary
    multi-line shape this estate writes.

    Measured live against the transcript corpus, applying this fix on top of
    #1760's own residual-mechanism classifier (unmerged; the breakdown
    instrument does not exist on main): 81 of a 166-command residual were
    this mechanism before the fix, 0 of 85 after -- every other mechanism
    bucket unchanged, count for count. Counts, not a percentage: the harness
    reads its own sessions' transcripts, so a share drifts with each run.
    """

    def test_the_UNCONTINUED_form_is_denied(self, tree):
        """THE POSITIVE CONTROL. Without this half, a probe that denies
        nothing would look identical to a fixed guard -- this is what makes
        the bypass below a bypass rather than a broken probe. Standing IN the
        vault (cwd) with no redirecting flag: the ordinary case."""
        vault, proj = tree
        assert D.decide("echo x && git reset --hard", vault, vault)[0] == "deny"

    def test_the_CONTINUED_form_is_ALSO_denied(self, tree):
        """THE BYPASS, fixed. Before the fix this allowed -- confirmed live
        against the shipped decider before writing the fix below."""
        vault, proj = tree
        assert D.decide("echo x && \\\ngit reset --hard", vault, vault)[0] == "deny"

    def test_an_INDENTED_continuation_was_never_the_bug(self, tree):
        """The bound, measured: indentation ends the token at the shlex
        level, so this denied before the fix too. Not a regression test for
        the fix -- a boundary marker so the fix is not credited for
        something it did not do."""
        vault, proj = tree
        assert D.decide("echo x && \\\n    git reset --hard", vault, vault)[0] == "deny"

    def test_a_plain_newline_with_no_backslash_was_never_the_bug(self, tree):
        vault, proj = tree
        assert D.decide("echo x\ngit reset --hard", vault, vault)[0] == "deny"

    def test_an_explicit_scope_flag_does_not_save_the_continued_form(self, tree):
        """The glued token hides the VERB, not the scope flag -- an explicit
        -C does not help if `decide()` never finds a `git` token to read the
        verb from in the first place. Run from OUTSIDE the vault (proj) on
        purpose: the -C flag is the ONLY thing that could rescue scope here,
        and it cannot, because the glue hides the verb before the flag is
        ever read."""
        vault, proj = tree
        assert (
            D.decide(f"echo x && \\\ngit -C {vault} reset --hard", vault, proj)[0]
            == "deny"
        )

    def test_a_double_continuation_is_also_denied(self, tree):
        """Two stacked continuations glue two newlines onto the token, not
        one -- the fix must strip all of them, not exactly one."""
        vault, proj = tree
        assert D.decide("echo x && \\\n\\\ngit reset --hard", vault, vault)[0] == "deny"

    def test_a_safe_verb_through_a_continuation_is_still_allowed(self, tree):
        """The fix must not turn every continuation into a refusal -- only a
        continuation that glues onto a genuinely dangerous verb."""
        vault, proj = tree
        assert D.decide("echo x && \\\ngit status", vault, vault)[0] == "allow"
