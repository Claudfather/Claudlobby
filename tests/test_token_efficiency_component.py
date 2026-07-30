"""#728 P1 — token-efficiency protocol component: contract + composed-size budget.

The component is re-paid in every context window of every bot that composes it,
so the size budget is enforced here by CI rather than by convention. The loader
is the same path the composer uses, so `item.body` is what actually composes.
"""

from pathlib import Path

from claudlobby.loader import load_library_item

REPO = Path(__file__).resolve().parents[1]
COMPONENT = REPO / "library" / "protocols" / "token-efficiency.md"

# Provisional per #728; #729 stage A's cost-weighted break-even may tighten it.
# This constant tracks the ratified number.
BODY_BUDGET_LINES = 35


class TestTokenEfficiencyComponent:
    def test_loads_with_valid_frontmatter_and_title(self):
        item = load_library_item(COMPONENT)
        assert item is not None, f"loader rejected {COMPONENT}"
        assert item.title == "Token-Efficiency"
        assert item.description, "description: is part of the component contract"

    def test_composed_body_within_budget(self):
        item = load_library_item(COMPONENT)
        n = len(item.body.splitlines())
        assert n <= BODY_BUDGET_LINES, (
            f"composed body is {n} lines > {BODY_BUDGET_LINES} (#728 budget) — "
            "every line is fleet-wide standing context overhead"
        )

    def test_load_bearing_clauses_present(self):
        # #728 names these three as load-bearing: wording may be tuned in
        # review, but the clauses themselves stay.
        body = load_library_item(COMPONENT).body
        assert "Rule zero" in body
        assert "Density, never frequency" in body
        assert "Never compress:" in body

    def test_h1_stripped_by_loader(self):
        # The loader strips the H1 (it would duplicate the composed section
        # heading); a body that still opens with the title indicates a
        # frontmatter/H1 mismatch.
        body = load_library_item(COMPONENT).body
        assert not body.lstrip().startswith("# Token-Efficiency")
