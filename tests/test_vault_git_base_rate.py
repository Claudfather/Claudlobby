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
