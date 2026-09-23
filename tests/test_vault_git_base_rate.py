"""#1725's base-rate harness — pinned against the way it has already been wrong.

Three readings came out of building this: 68.1%, 82.8%, 81.9%. Only the last
answers the sentence the number is used to justify. The first two were the
harness measuring a different population than the claim — the same defect as the
independent reproduction that first read 42.6%, which is why each is pinned here
rather than merely fixed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "vault_git_base_rate", _ROOT / "lib" / "vault-git-base-rate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bx():
    return _load()


class TestTheDenominatorIsIndependentOfTheGuard:
    """If the guard's parser decided BOTH halves the answer would be 100% by
    construction — the harness certifying the claim by assuming it."""

    def test_a_composed_invocation_still_counts(self, bx):
        for cmd in ('X=$(git checkout main)',
                    'echo a | xargs git reset --hard',
                    'sh -c "git rebase main"',
                    'true && git merge topic'):
            assert bx._is_state_changing(bx._strip_heredocs(cmd)), cmd

    def test_which_composed_forms_the_guard_ACTUALLY_misses(self, bx):
        """MEASURED, not assumed — and it corrected this harness's first guess.

        A substitution, `xargs` and a pipeline stage all leave `git` as a bare
        token, so the guard SEES them; it is stronger than it looks. The real
        blind spot is a quoted body, which tokenizes as one string. Pinned in
        both directions because an over-stated blind spot argues for changes the
        guard does not need, and an under-stated one hides the ceiling."""
        guard = bx._load_guard(_ROOT / "lib")
        for seen in ('git checkout main',
                     'X=$(git checkout main)',
                     'echo a | xargs git reset --hard'):
            assert bx._guard_sees_git_state(guard, seen), seen
        for blind in ('sh -c "git rebase main"',
                      "bash -c 'git reset --hard'"):
            assert not bx._guard_sees_git_state(guard, blind), blind


class TestTheDefectsThatProducedTheWrongReadings:
    def test_git_quoted_in_a_HEREDOC_is_not_an_invocation(self, bx):
        """Reading 1, 68.1%: report prose and memory notes quote git commands.
        Counting those inflated the denominator and deflated the rate."""
        cmd = "cat > /tmp/note.md <<'EOF'\nI ran git checkout main earlier.\nEOF"
        assert not bx._is_state_changing(bx._strip_heredocs(cmd)), (
            "a git command quoted in authored text was counted as an invocation")

    def test_a_grep_FOR_a_git_string_is_not_an_invocation(self, bx):
        assert not bx._is_state_changing(bx._strip_heredocs(
            "grep -rn 'git push' lib/"))

    def test_a_READ_ONLY_conditional_verb_is_not_state_changing(self, bx):
        """Reading 2, 82.8%: `branch`/`commit`/`push`/`pull` are dangerous only
        with a flag, so counting them bare filled the gap with harmless reads."""
        assert not bx._is_state_changing('B=$(git branch --show-current)')
        assert not bx._is_state_changing('git commit -m "ordinary"')
        assert not bx._is_state_changing('git push origin HEAD')

    def test_the_same_verbs_DO_count_with_the_dangerous_flag(self, bx):
        """The other half of the same rule — without this the previous test is
        satisfied by a matcher that simply never fires."""
        assert bx._is_state_changing('git commit --amend --no-edit')
        assert bx._is_state_changing('git push --force origin main')
        assert bx._is_state_changing('git branch -D old-thing')
        assert bx._is_state_changing('git pull --rebase')


    def test_a_HYPHENATED_plumbing_name_is_not_the_verb(self, bx):
        """Reading 5 (review): `\b` sits between `merge` and the `-` of
        `merge-base`, so a bare word boundary matched inside every hyphenated
        plumbing name. None of these change the tree the way the verb alone
        does, so counting them inflates the denominator exactly as the heredocs
        and the read-only conditionals did.

        `checkout-index` was not in the reported pair — it fell out of fixing
        this as a RULE instead of denylisting the two that were named."""
        for benign in ('git merge-base main HEAD',
                       'git merge-tree a b c',
                       'git checkout-index -a',
                       'git commit-tree $TREE'):
            assert not bx._is_state_changing(benign), benign

    def test_the_hyphen_rule_does_not_eat_cherry_pick(self, bx):
        """The other half: a verb that legitimately CONTAINS a hyphen must still
        match. Without this, the previous test is satisfied by a rule that
        simply stopped matching hyphens at all."""
        assert bx._is_state_changing('git cherry-pick abc123')
        assert bx._is_state_changing('git merge topic')
        assert bx._is_state_changing('git commit --amend')

    def test_the_numerator_is_not_flag_blind_on_conditional_verbs(self, bx):
        """Reading 6 (#1755): the numerator checked CONDITIONAL verb
        membership alone, so `git branch -v` counted as guard-visible the
        same as `git branch -D` -- every safe bare use of a conditional verb
        inflated the numerator, which inflates the rate. The denominator
        already got this right (reading 3); the numerator never did."""
        guard = bx._load_guard(_ROOT / "lib")
        for safe in ('git branch -v',
                     'git branch --show-current',
                     'git commit -m "ordinary"',
                     'git push origin HEAD',
                     'git pull'):
            assert not bx._guard_sees_git_state(guard, safe), safe

    def test_the_same_conditional_verbs_STILL_count_with_their_flag(self, bx):
        """The other half -- without this the previous test is satisfied by a
        rule that stopped seeing CONDITIONAL verbs at all."""
        guard = bx._load_guard(_ROOT / "lib")
        for dangerous in ('git branch -D old-thing',
                           'git commit --amend --no-edit',
                           'git push --force origin main',
                           'git pull --rebase'):
            assert bx._guard_sees_git_state(guard, dangerous), dangerous


class TestItStatesItsBounds:
    """#1742: a base rate with no denominator description is a number people
    quote."""

    def test_the_bound_names_corpus_window_and_what_was_skipped(self, bx):
        text = " ".join(bx._bound_lines({
            "files": 1, "commands": 2, "transcripts_root": "/x",
            "tokenizer_failures": 0}))
        for needed in ("CORPUS", "WINDOW", "SKIPPED", "COUNTING", "HARNESS"):
            assert needed in text, needed
        assert "UPPER bound" in text, (
            "the output must say which direction it errs in")

    def test_tokenizer_failures_are_reported_not_assumed_zero(self, bx):
        text = " ".join(bx._bound_lines({
            "files": 1, "commands": 2, "transcripts_root": "/x",
            "tokenizer_failures": 7}))
        assert "7 command(s)" in text, (
            "a tokenizer failure deflates the numerator and is indistinguishable "
            "from composition — it must be counted, not silently folded in")


class TestUnreachableIsNotEmpty:
    def test_a_missing_corpus_REFUSES_rather_than_reporting_a_rate(self, bx, tmp_path):
        rc = bx.main(["--transcripts", str(tmp_path / "nope")])
        assert rc == 3, "a missing corpus must refuse, never report 0%"

    def test_a_corpus_with_no_git_commands_REFUSES_too(self, bx, tmp_path):
        (tmp_path / "t.jsonl").write_text("")
        rc = bx.main(["--transcripts", str(tmp_path)])
        assert rc == 3, "no observations is not a 0% base rate"

class TestTheVerbNeedsABoundaryOnBOTHSides:
    """The seventh defect (#1753), and the mirror image of reading 5.

    Fixing the RIGHT side of the verb left the LEFT side open, so `\b` still
    matched the `merge` inside `.git/rebase-merge` — which is how everyone
    probes for a stopped rebase, and was 37 occurrences of one string.
    Measured: 54 commands, ~30% of the whole residual, and NONE of them
    visible to the guard, which is the shape of a pure denominator artifact.
    """

    def test_a_hyphen_BEFORE_the_verb_is_not_the_verb(self, bx):
        for cmd in ('printf \'%s\' "$(git rev-parse HEAD)" "$([ -d .git/rebase-merge ])"',
                    'git rev-parse HEAD && ls .git/rebase-apply',
                    'git config --get pull.rebase',
                    'git log --oneline; cat .git/rebase-merge/head-name'):
            assert not bx._is_state_changing(bx._strip_heredocs(cmd)), cmd

    def test_a_FLAG_spelled_like_a_verb_is_not_the_verb(self, bx):
        """`git commit -am "x"` matched the state verb `am` through the hyphen,
        and `git pull --rebase` matched the unconditional `rebase`. The second
        is still counted — by the CONDITIONAL rule, which is where it belongs."""
        assert not bx._is_state_changing('git commit -am "wip"')
        assert bx._is_state_changing('git pull --rebase')

    def test_the_left_boundary_does_not_eat_a_real_invocation(self, bx):
        """The control: the rule must remove artifacts and nothing else."""
        for cmd in ('git checkout main', 'git cherry-pick abc123',
                    'git merge topic', 'git worktree add /tmp/x main',
                    'cd /v && git reset --hard', 'git commit --amend'):
            assert bx._is_state_changing(bx._strip_heredocs(cmd)), cmd


class TestTheResidualBreakdown:
    """#1753 asked for the residual by mechanism, because the remedy on the
    table (parse one level into `sh -c`) was sized by intuition.

    Measured on the estate corpus: the `sh -c` form is **0 of 163**, and
    **50.9%** is a backslash line continuation gluing the newline onto the
    word so the token is `"\ngit"` — a DIRECT invocation, no composition at
    all, which the guard's own claim says it catches.
    """

    def _classify(self, bx, cmd, glued_tok=False):
        shell = bx._strip_heredocs(cmd)
        hit = bx._GIT_STATE_RE.search(shell)
        verb_at = shell.index(hit.group(1), hit.start()) if hit else None
        if hit is None:
            for rx in bx._GIT_COND_RES:
                hit = rx.search(shell)
                if hit:
                    break
        assert hit is not None, f"not in the denominator at all: {cmd!r}"
        return bx._classify_residual(shell, hit.start(), verb_at, glued_tok)[0]

    def test_the_sh_c_DETECTOR_WORKS_so_a_zero_means_absence(self, bx):
        """THE LOAD-BEARING TEST. The headline of this measurement is that the
        wrapper form is 0% of real traffic — and a detector that has never
        been fed a positive is indistinguishable from a broken one. Feed it
        one, in every spelling the class claims."""
        for cmd in ('sh -c "git rebase main"',
                    'bash -c "git reset --hard"',
                    "eval 'git checkout main'",
                    'ssh host "git merge topic"',
                    'timeout 5 sh -c "git clean -fd"'):
            assert self._classify(bx, cmd) == "wrapper_quoted_body", cmd

    def test_a_backslash_continuation_is_its_own_class(self, bx):
        assert self._classify(bx, 'echo a && \\\ngit reset --hard') == (
            "continuation_glued_token")

    def test_an_INDENTED_continuation_is_not_that_class(self, bx):
        """The bound, measured live against the guard: an indented continuation
        DENIES correctly, because the whitespace ends the token. Calling it
        glued would overstate the finding."""
        assert self._classify(bx, 'echo a && \\\n    git reset --hard') != (
            "continuation_glued_token")

    def test_a_continuation_ELSEWHERE_does_not_claim_the_match(self, bx):
        """The rule is IMMEDIATELY-before, and this is what pins it.

        The first version searched the whole prefix for a continuation, so any
        command carrying one anywhere scored as glued — inflating this class in
        the direction that flatters the finding the file had just made. The
        indented case does not discriminate (its continuation is followed by
        whitespace, which both rules reject), so it took this shape: a real
        continuation early, and the matched git reached some other way.

        Mutation-checked: reverting to the loose rule turns THIS test red and
        nothing else in the file."""
        cmd = 'echo a && \\\necho "stashes: $(git stash list)"'
        assert self._classify(bx, cmd) != "continuation_glued_token"
        assert self._classify(bx, cmd) == "string_data_or_prose"

    def test_the_GUARDS_TOKENIZER_CANNOT_FLIP_THE_CLASSIFICATION(self, bx):
        """THE COLLAPSE TRAP, ONE LEVEL DOWN — and review had to find it.

        The first version read `if glued or glued_by_tokenizer`, so the guard's
        own parser could put a command in this bucket. That contradicts the
        stated "the independent rule decides", and the bias was
        ONE-DIRECTIONAL: an `or` can only ever ADD, and it added to the flagship
        bucket. A small one-sided error on the headline number is worth more
        than a large symmetric one on a footnote.

        Forced here the way the reviewer forced it: hand the classifier a
        tokenizer verdict that DISAGREES and assert the classification ignores
        it. The cross-check may count; it may not decide."""
        shell = 'echo a; git stash list'
        hit = bx._GIT_STATE_RE.search(shell)
        verb_at = shell.index(hit.group(1), hit.start())
        with_tok = bx._classify_residual(shell, hit.start(), verb_at, True)[0]
        without = bx._classify_residual(shell, hit.start(), verb_at, False)[0]
        assert with_tok == without, (
            "the guard's tokenizer changed the classification — the residual "
            "breakdown must not consult the parser it is describing")
        assert with_tok != "continuation_glued_token"

    def test_a_continuation_INSIDE_A_QUOTED_STRING_is_not_a_bypass(self, bx):
        """The second correction from the same review, and it is the one that
        moved the number: a backslash-newline inside a quoted string is TEXT,
        not a shell continuation. Both cross-check disagreements on the corpus
        were this shape — a continuation inside one of this estate's own probe
        strings — and the earlier ordering counted them in the flagship bucket.
        Quoted-first is the conservative reading of a published number."""
        cmd = "probe 'echo a && \\\ngit reset --hard'"
        assert self._classify(bx, cmd) != "continuation_glued_token"

    def test_our_own_guard_PROBES_are_counted_apart(self, bx):
        """Folding the estate's own test fixtures into the wrapper class would
        inflate the blind spot with the tests that measure it."""
        assert self._classify(bx, 'for c in "cd $V && git reset --hard"; do :; done') == (
            "harness_command_string")

    def test_every_class_is_named_in_the_partition(self, bx):
        assert set(bx.RESIDUAL_MECHANISMS) >= {
            "verb_belongs_to_another_command", "continuation_glued_token",
            "wrapper_quoted_body", "harness_command_string",
            "string_data_or_prose", "quoted_introducer_unrecognised",
            "unclassified"}

    def test_the_breakdown_states_its_own_bound(self, bx):
        r = {"files": 1, "commands": 1, "git_state_by_text": 1,
             "git_state_guard_visible": 0, "directness_pct": 0.0,
             "cd_any": 1, "cd_bare": 1, "bare_cd_pct": 100.0,
             "tokenizer_failures": 0, "transcripts_root": "/x",
             "mechanism_overlaps": 3, "glued_crosscheck_disagreements": 1}
        text = "\n".join(bx._breakdown_bound_lines(r))
        assert "INDEPENDENT text rules" in text
        assert "one bucket" in text          # why the guard's parser is not used
        assert "3 match(es)" in text         # the overlap count, not a boast
        assert "1 disagreement" in text      # the cross-check, counted
        assert "never folded" in text        # unclassified is not absorbed

