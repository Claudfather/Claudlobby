"""The mention rewriter: allowlist inversion + the fail-toward-rewriting rule.

Follow-up to the guard merged in #1021, which matched a denylist of composed bot
names. That is right as far as it goes and provably does not go far enough.

THE FIXTURES ARE REAL. Of the eleven accounts we actually notified, three appear
below because no bot-name list would ever have contained them:

    Botfather  — a real user. In our issues only because we documented
                 Telegram's BotFather.
    latest     — a real account. Reads like a version string.
    216        — a real account. Reads like a number.

Those three are the argument for the inversion in a way synthetic names cannot
be: each looks like ordinary prose, none is a teammate, and each emailed a
person. The harm class is "any @word that happens to be a real handle", which
is unbounded and grows without us acting — so it cannot be enumerated, and the
guard must default to rewriting.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "lib" / "mention-rewrite.py"
_spec = importlib.util.spec_from_file_location("mention_rewrite", _SRC)
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

AT = chr(64)  # never written literally: this file is itself gh-bound content
BOTS = {"vera", "ravi", "dara", "ari"}
ALLOW = {"chrisrogers37"}


def rw(text, bots=BOTS, allow=ALLOW, **kw):
    return mr.rewrite(text, bots, allow, **kw)


class TestTheAccountsWeActuallyNotified:
    """Each of these is a real person the merged denylist guard still misses."""

    @pytest.mark.parametrize("handle", ["Botfather", "latest", "216"])
    def test_non_bot_real_accounts_are_rewritten(self, handle):
        assert rw(f"see @{handle} for details") == f"see `{handle}` for details"

    def test_the_merged_guard_would_have_missed_all_three(self):
        """Pins the gap this PR closes, so it cannot silently reopen.

        With an EMPTY bot list — i.e. the denylist guard's behaviour for any
        handle that is not a fleet bot — the inversion still rewrites them.
        """
        text = "@Botfather @latest @216"
        assert rw(text, bots=set()) == "`Botfather` `latest` `216`"

    def test_bot_names_still_rewritten(self):
        """The merged guard's behaviour is preserved, not replaced."""
        assert rw("thanks @vera and @ari") == "thanks `vera` and `ari`"


class TestAllowlistInversion:
    def test_declared_handle_is_left_alone(self):
        assert rw("cc @chrisrogers37") == "cc @chrisrogers37"

    def test_undeclared_handle_is_rewritten(self):
        assert rw("cc @someone-unrelated") == "cc `someone-unrelated`"

    def test_empty_allowlist_means_nobody_is_notified(self):
        assert rw("cc @chrisrogers37", allow=set()) == "cc `chrisrogers37`"

    def test_bot_name_beats_the_allowlist(self):
        """The deny-override. Without it, someone eventually allowlists a bot's
        name meaning OUR bot and silently re-arms the original bug."""
        assert rw("cc @vera", allow={"vera"}) == "cc `vera`"

    def test_match_is_case_insensitive_because_github_is(self):
        assert rw("cc @VERA") == "cc `VERA`"
        assert rw("cc @ChrisRogers37") == "cc @ChrisRogers37"


class TestNotAMention:
    """GitHub only notifies on a handle-shaped token starting a word."""

    @pytest.mark.parametrize(
        "text",
        [
            "mail user@example.com now",
            "run gh api -F body=@- here",
            "a@b",
        ],
    )
    def test_left_alone(self, text):
        assert rw(text) == text

    def test_npm_scope_in_bare_prose_IS_rewritten(self):
        """Expectation corrected after the first run said otherwise.

        `@scope` written bare in prose is word-initial, so GitHub linkifies it
        and notifies if `scope` is a real account — the same class as `@latest`.
        The rewriter was right and the original assertion here was wrong. The
        correct home for a package name is a code span, which is respected
        below; `` `scope` ``/pkg is the harmless cost of getting this wrong.
        """
        assert rw("install @scope/pkg via npm") == "install `scope`/pkg via npm"

    def test_npm_scope_inside_a_code_span_is_left_alone(self):
        t = "install `@scope/pkg` via npm"
        assert rw(t) == t

    def test_url_is_not_rewritten(self):
        t = "see https://github.com/@latest/repo here"
        assert rw(t) == t

    def test_over_39_chars_is_not_a_handle(self):
        long = "a" * 40
        assert rw(f"@{long}") == f"@{long}"


class TestMentionAfterAQuote:
    """Regression: a mention immediately after a quote was NOT rewritten.

    Found while documenting the probe traps — a batched-probe repro printed
    `-b "@vera"` back unchanged, which looked like the guard under-firing and
    was. The `@` was only matched after whitespace or an opening bracket, so a
    body that OPENS by addressing someone slipped through:

        gh pr comment 1 --body "@vera thanks for the catch"

    That is a natural shape, and it is a false negative — the direction that
    emails a stranger.
    """

    @pytest.mark.parametrize("q", ['"', "'"])
    def test_mention_right_after_a_quote_is_rewritten(self, q):
        assert rw(f"{q}@vera{q}") == f"{q}`vera`{q}"

    def test_a_body_opening_with_a_mention(self):
        assert rw('--body "@latest thanks"') == '--body "`latest` thanks"'

    def test_email_and_shell_at_still_excluded(self):
        """The fix must not widen into these — an alphanumeric still blocks."""
        assert rw("user@example.com") == "user@example.com"
        assert rw("-F body=@- x") == "-F body=@- x"


class TestFailTowardRewriting:
    """THE INVARIANT: when unsure whether text is code, REWRITE.

    Skipping genuine code is legitimate — GitHub does not linkify mentions
    inside fences or inline spans, which is exactly why backticks are the fix.
    The danger is the parser WRONGLY believing something is code: then a live
    mention passes through and emails a stranger, and "we only ever rewrite"
    does nothing to bound that, because the harm is the thing we failed to do.

        wrongly rewrote code  -> a corrupted sample. Visible, fixable.
        wrongly skipped prose -> a stranger gets an email. Neither.
    """

    def test_closed_fence_is_respected(self):
        t = "before\n```\n@vera in code\n```\nafter"
        assert rw(t) == t

    def test_unclosed_fence_does_NOT_protect(self):
        """A stray triple-backtick would otherwise hide every mention after it."""
        assert "`vera`" in rw("before\n```\n@vera after an unclosed fence")

    def test_paired_inline_span_is_respected(self):
        t = "use `@dataclass` here"
        assert rw(t) == t

    def test_unmatched_backtick_does_NOT_protect(self):
        assert "`216`" in rw("stray ` tick then @216")

    def test_decorators_in_a_fence_survive(self):
        t = "```python\n@pytest.mark.skipif\n@dataclass\ndef f(): ...\n```"
        assert rw(t) == t

    def test_a_mention_after_a_closed_fence_is_still_rewritten(self):
        out = rw("```\ncode\n```\nthanks @latest")
        assert out.endswith("thanks `latest`")


class TestShellSurfaceNeverGetsBackticks:
    """A comment body sits in a double-quoted shell string, where a backtick is
    command substitution — the naive rewrite would make the shell EXECUTE the
    handle, turning a notification bug into arbitrary code execution."""

    def test_bare_style(self):
        assert rw("hi @vera", style="bare") == "hi vera"

    def test_no_backtick_is_ever_emitted_in_bare_style(self):
        assert "`" not in rw("@vera @Botfather @216", style="bare")


class TestReportMode:
    """`--report` backs the refusal message for by-reference content.

    A block saying "this file has a mention" is barely better than a silent
    rewrite. One that names line and handle lets the author fix it in the same
    turn without re-deriving anything.
    """

    def test_reports_line_and_handle(self):
        text = "intro\nthanks " + AT + "vera\nnothing\nand " + AT + "latest\n"
        assert mr.report(text, BOTS, ALLOW) == [(2, "vera"), (4, "latest")]

    def test_clean_text_reports_nothing(self):
        assert mr.report("an ordinary body\nno mentions\n", BOTS, ALLOW) == []

    def test_allowlisted_handle_is_not_reported(self):
        """Otherwise a declared mention would block a post it should permit."""
        assert mr.report("cc " + AT + "chrisrogers37", BOTS, ALLOW) == []

    def test_a_mention_inside_a_fence_is_not_reported(self):
        """Same fence rules as the rewriter — a block and a rewrite must not
        disagree about what counts, or a file gets refused for a mention that
        would never have notified."""
        assert mr.report("```\n" + AT + "vera\n```\n", BOTS, ALLOW) == []


# --- #1329: the lookbehind was an allowlist, so markdown escaped it ----------
# A verdict comment reading `**@<bot>s line about this PR is exact**` was
# linkified by GitHub and notified a REAL user, who replied on the PR to say we
# had the wrong person. Second real person hit by this class after #1019.
#
# The guard RAN and did not match. Its lookbehind admitted only whitespace,
# bracket, paren or quote — so it covered the rare positions and missed the
# universal ones. House style opens every verdict with `**`, every bullet with
# `-`, every table cell with `|`.
#
# TABLE, NOT A REPRESENTATIVE CASE. The defect is a SET of characters, and a
# single-case test goes green while the other members still escape — which is
# exactly how this survived 39 existing tests in this file.

_ESCAPED_PREFIXES = [
    ("bold", "**@vera** ships it"),
    ("italic-star", "*@vera*"),
    ("italic-underscore", "_@vera_"),
    ("bold-underscore", "__@vera__"),
    ("strikethrough", "~~@vera~~"),
    ("blockquote", "> @vera take a look"),
    ("bullet-spaced", "- @vera please review"),
    ("bullet-tight", "-@vera"),
    ("heading", "# @vera"),
    ("table-cell", "|@vera|"),
    ("slash", "cc/@vera"),
    ("colon", "cc:@vera"),
    ("underscore-suffix", "snake_@vera"),
]


@pytest.mark.parametrize("label,text", _ESCAPED_PREFIXES, ids=[p[0] for p in _ESCAPED_PREFIXES])
def test_every_markdown_position_is_rewritten(label, text):
    """Each prefix is its own case so one member cannot hide behind another."""
    out = mr.rewrite(text, {"vera"}, set(), style="backtick")
    assert out != text, f"{label}: mention escaped the guard in {text!r}"
    assert "@vera" not in out, f"{label}: an @-mention survived in {out!r}"


_MUST_STAY_QUIET = [
    ("email", "email@vera"),
    ("real-email", "someone@vera.com"),
    ("alnum-before", "a1@vera"),
    ("hyphen-email", "my-email@vera"),
    ("curl-stdin", "-F body=@-"),
]


@pytest.mark.parametrize("label,text", _MUST_STAY_QUIET, ids=[p[0] for p in _MUST_STAY_QUIET])
def test_mid_token_at_signs_are_left_alone(label, text):
    """The inversion must not turn into "rewrite every @".

    The real intent is *do not match mid-token*, which is what keeps
    `user@example.com` and curl's `body=@-` intact.
    """
    assert mr.rewrite(text, {"vera"}, set(), style="backtick") == text


def test_underscore_and_hyphen_are_absent_from_the_denylist_on_purpose():
    """The obvious denylist `[A-Za-z0-9_-]` still misses three enumerated cases.

    Measured before choosing it: with `_` and `-` included, `_@vera_`, `__@vera__`
    and `-@vera` all still escape. They buy nothing, because `my-email@vera` and
    `snake_case@vera` are already blocked by the ALPHANUMERIC immediately before
    the `@`. This pins the narrower denylist against a well-intentioned widening.
    """
    for text in ("_@vera_", "__@vera__", "-@vera"):
        assert mr.rewrite(text, {"vera"}, set(), style="backtick") != text
    assert mr.rewrite("my-email@vera", {"vera"}, set(), style="backtick") == "my-email@vera"


def test_the_trailing_anchor_is_not_a_word_boundary():
    """A second, independent defect: `\\b` fails when a handle is followed by `_`.

    There is no word boundary between `a` and `_`, so `_@vera_` stayed unmatched
    even with the lookbehind fixed. Fixing only the lookbehind — the obvious
    reading of the bug — leaves markdown italic and bold still escaping.
    """
    assert "\\b" not in mr._MENTION.pattern
    assert mr.rewrite("_@vera_", {"vera"}, set(), style="backtick") != "_@vera_"


def test_backtick_is_decided_by_the_span_parser_not_the_lookbehind():
    """Backtick was on the escape list only because the lookbehind excluded it.

    It belongs to the fence/span parser, which can see whether the span CLOSES —
    a character test cannot. Both directions asserted, because moving the
    decision is only correct if the closed case still holds.
    """
    unclosed = "see `@vera for detail"
    assert mr.rewrite(unclosed, {"vera"}, set(), style="backtick") != unclosed

    closed = "`@vera` is code"
    assert mr.rewrite(closed, {"vera"}, set(), style="backtick") == closed
