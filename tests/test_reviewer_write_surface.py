"""#1406 — what a reviewer may write, and what must never be denied to it.

`library/expertise/code-review.md` carried `deny: [Write, Edit, NotebookEdit]`
from 2026-05-09, meaning to make reviewers read-only. A BARE TOOL NAME blocks
only the native tool and does not bind Bash, so hundreds of memory files were
written by four bots while it was armed — the count is stamped and dated in
`library/expertise/code-review.md` and derived in #1409, deliberately not
duplicated here, because it is still rising. It is removed.

The path-scoped replacement over `projects/` was proposed and WITHDRAWN on
evidence: a reviewer's documented workflow writes inside that tree (`git fetch`,
`git checkout`, `git worktree add`, and sed-then-revert mutation testing), so a
full block would stop review outright rather than constrain it.

These tests pin BOTH directions, which is the point:

  * the bare deny must not come back, and the reasoning must survive with it;
  * no composed deny may reach a reviewer's own `projects/` tree — a NEGATIVE
    control on a path deliberately excluded, so a future edit that widens the
    anchor by accident fails loudly here;
  * and sibling isolation must still be emitted in the same composition, so the
    negative control cannot pass merely by nothing being denied at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.composer import compose_settings_local
from claudlobby.config import BotConfig, FleetConfig, ScopeConfig
from claudlobby.loader import parse_expertise_file
from claudlobby.paths import Paths

REVIEWER_EXPERTISE = "code-review"
REPO = Path(__file__).resolve().parents[1]
CODE_REVIEW_MD = REPO / "library" / "expertise" / "code-review.md"


def _fleet(*bots: BotConfig) -> FleetConfig:
    return FleetConfig(
        name="test-fleet",
        service_prefix="com.test",
        bots={b.bot_id: b for b in bots},
        mission="Ship.",
    )


def _bot(bot_id: str, expertise: list[str]) -> BotConfig:
    return BotConfig(
        bot_id=bot_id,
        name=bot_id.title(),
        expertise=expertise,
        scope=ScopeConfig(org="acme", repos=["acme/widget"]),
    )


@pytest.fixture
def paths() -> Paths:
    """Rooted at the REAL repo, and asserts it resolved.

    A `tmp_path` root has no `library/`, so `find_library_file` returns None and
    `_resolve_expertise_permissions` silently `continue`s past the profile
    (composer.py:2275-2280). Every test below would then pass identically if
    `code-review.md` were deleted outright — coverage that certifies nothing,
    which is the exact object this PR argues against, one layer up.

    The assert is the load-bearing half. Pointing at the real root fixes it
    today; asserting the lookup resolved is what stops a future path change from
    silently restoring the decoy, since the failure mode reads as a pass.
    """
    p = Paths(root=REPO, fleet_dir=None)
    assert p.find_library_file("expertise", REVIEWER_EXPERTISE, ".md") is not None, (
        "fixture cannot resolve the expertise file, so composition would silently "
        "skip it and these tests would assert nothing"
    )
    return p


def _deny(bot, fleet, paths) -> list[str]:
    # `deny` is absent, not empty, when a bot composes no deny rules at all.
    return compose_settings_local(bot, fleet, paths)["permissions"].get("deny", [])


class TestReviewerProjectsTreeStaysWritable:
    """The negative control. `projects/` is deliberately excluded — a reviewer
    that cannot write there cannot fetch, cannot add a worktree, and cannot run
    mutation testing, which is the instrument that catches vacuous tests."""

    def test_no_composed_deny_reaches_a_reviewers_own_projects_tree(self, paths):
        vera, otis = (
            _bot("vera", [REVIEWER_EXPERTISE]),
            _bot("otis", ["software-engineering"]),
        )
        fleet = _fleet(vera, otis)
        deny = _deny(vera, fleet, paths)
        # Match each BOT'S projects subtree explicitly, never a bare "/projects/"
        # substring: this repo is itself checked out under a bot's projects/ dir,
        # so a substring test matches the composed ROOT and reports a rule that
        # targets `<root>/runtime/bots/otis/**` as if it targeted a projects tree.
        targets = [str(paths.bot_runtime(b) / "projects") for b in fleet.bots]
        offenders = [d for d in deny if any(t in d for t in targets)]
        assert offenders == [], (
            "a deny now reaches a reviewer's own projects/ tree: "
            f"{offenders}. #1406 excluded it deliberately — worktree bookkeeping "
            "lands in the main checkout's .git wherever the worktree sits, and "
            "mutation testing is a deliberate write to source."
        )

    def test_sibling_isolation_is_still_emitted_in_the_same_composition(self, paths):
        """The positive half. Without this the negative control above would pass
        just as happily if NOTHING were ever denied — an inert control that
        cannot fail in the other direction."""
        vera, otis = (
            _bot("vera", [REVIEWER_EXPERTISE]),
            _bot("otis", ["software-engineering"]),
        )
        deny = _deny(vera, _fleet(vera, otis), paths)
        assert any(d.startswith("Edit(//") and d.endswith("/otis/**)") for d in deny), deny
        assert any(d.startswith("Read(//") and d.endswith("/otis/**)") for d in deny), deny

    def test_a_reviewer_composes_no_bare_tool_deny(self, paths):
        vera = _bot("vera", [REVIEWER_EXPERTISE])
        deny = _deny(vera, _fleet(vera), paths)
        assert not (set(deny) & {"Write", "Edit", "NotebookEdit"}), deny


class TestCodeReviewExpertiseDeclaresNoBareDeny:
    """A bare-tool-name deny reads as a constraint and enforces nothing against
    Bash. Reintroducing one here is how this defect comes back."""

    def test_the_expertise_file_declares_no_bare_tool_deny(self):
        item = parse_expertise_file(CODE_REVIEW_MD)
        assert item is not None and item.permissions is not None
        declared = set(item.permissions.deny)
        assert not (declared & {"Write", "Edit", "NotebookEdit"}), (
            f"code-review.md re-declares a bare deny {sorted(declared)} — bare "
            "tool names do not bind Bash (#1406)."
        )

    def test_the_reasoning_survives_in_the_file(self):
        """A silent removal invites the next reader to restore it, and a silent
        exclusion invites them to widen it. Both rationales must stay."""
        text = CODE_REVIEW_MD.read_text()
        assert "#1406" in text
        assert "not bind Bash" in text, "why the bare deny went"
        assert "worktree" in text, "fact (a): moving the worktree does not escape"
        assert "utation testing" in text, "fact (b): the vacuous-test detector"
