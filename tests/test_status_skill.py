"""Tests for library/skills/status and library/skills/selfcheck.

Written because the PR that introduced them (#1375) originally cited
`test_printify_skill.py test_shopify_skill.py test_status.py` -- 69 passing tests
-- as its evidence. None of the three touches either file. `test_status.py` tests
`claudlobby.status`, the CLI fleet-health dashboard; the filename coincided with
the subject, the content did not. A malformed frontmatter or a broken tool_grants
line would have sailed through that citation undetected.

So these tests exist to fail on the specific ways THIS change can regress.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILLS = REPO / "library" / "skills"
STATUS = SKILLS / "status" / "SKILL.md"
SELFCHECK = SKILLS / "selfcheck" / "SKILL.md"


def _frontmatter(p: pathlib.Path) -> dict[str, str]:
    text = p.read_text()
    assert text.startswith("---\n"), f"{p.name}: no frontmatter block"
    block = text.split("---", 2)[1]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if not line or line[0] in " -\t":
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


@pytest.mark.parametrize("path,name", [(STATUS, "status"), (SELFCHECK, "selfcheck")])
def test_frontmatter_name_matches_directory(path: pathlib.Path, name: str) -> None:
    """Claude Code routes /<name> by the frontmatter, the composer by the directory.

    If they disagree the skill is invoked under one name and composed under another,
    which is silent -- nothing errors, the slash command just does not exist.
    """
    assert path.is_file(), f"{path} missing"
    fm = _frontmatter(path)
    assert fm.get("name") == name, f"{path.name}: name={fm.get('name')!r}, dir={name!r}"
    assert fm.get("description"), f"{path.name}: description is required"


def test_selfcheck_is_the_diagnostic_and_status_is_not() -> None:
    """The rename's whole point. If these swap back, /status silently becomes a
    PID-and-memory dump again and the human's readout disappears with no error."""
    assert "MCP Server Connectivity" in SELFCHECK.read_text()
    status = STATUS.read_text()
    assert "MCP Server Connectivity" not in status
    assert "what moved" in status.lower()


def test_degraded_procedure_covers_BOTH_modes_with_worked_examples() -> None:
    """The rung the skill rests on.

    Review found the original shipped a worked example for `labeled` only, while
    `omitted` got a bare negative constraint -- 'must not appear as a number' with
    no instruction for what to write instead. Half a contract with no example is
    prose, not a procedure.
    """
    text = STATUS.read_text()
    assert "labeled" in text and "omitted" in text
    # each mode needs a real example line, not just a mention of the mode name
    assert "#903" in text, "no worked example for the labeled mode"
    assert "#891" in text, "no worked example for the omitted mode"


def test_the_degraded_check_is_reachable_from_the_list_building_steps() -> None:
    """It used to live only under 'Non-negotiables', structurally disconnected from
    the steps that build the output. An agent could follow Steps 1-3 faithfully and
    never cross-reference degraded[]. The check has to BE a step."""
    text = STATUS.read_text()
    assert re.search(r"^#+ Step 3c", text, re.M), "degraded reconciliation is not a step"
    body = text.split("## Non-negotiables")[0]
    assert "degraded" in body, "degraded[] is never mentioned in the procedure itself"


def test_output_contract_admits_the_disclosure_footer() -> None:
    """Self-contradiction guard. The skill says 'exactly two questions, nothing else',
    but a standing degraded disclosure is neither a MOVED line nor an OPEN-FOR-YOU
    line. If the contract does not admit the footer, the skill forbids its own rule."""
    text = STATUS.read_text()
    assert "NOT SHOWN" in text, "no home defined for a standing disclosure"
    two_q = re.search(r"exactly two questions[^\n]*", text)
    assert two_q and "footer" in two_q.group(0), (
        "the two-questions contract does not admit the footer it requires"
    )


def test_status_consumes_the_shipped_read_door_not_raw_state() -> None:
    """A hand-rolled reader silently disagrees with the framework's own."""
    text = STATUS.read_text()
    assert "claudlobby" in text and "brief" in text
    assert "state/" not in text.replace("`state/`", ""), "reaches into raw state files"


@pytest.mark.parametrize("path", [STATUS, SELFCHECK])
def test_no_jinja_placeholders_because_skills_are_symlinked_not_rendered(
    path: pathlib.Path,
) -> None:
    """composer.py::link_skills only symlinks -- it never parses or renders SKILL.md.
    A {{BOT_NAME}} here would reach the agent as literal text."""
    assert "{{" not in path.read_text(), f"{path.name}: Jinja placeholder will not be substituted"


def test_shared_partial_link_resolves_from_the_skill_directory() -> None:
    """library/skills/README.md cites status/SKILL.md as THE example of the relative
    `../_telegram-formatting.md` link working through the symlink. If the link breaks,
    the README's worked example silently becomes false."""
    text = STATUS.read_text()
    for rel in re.findall(r"\]\((\.\./[^)]+)\)", text):
        assert (STATUS.parent / rel).resolve().is_file(), f"dangling partial link: {rel}"


def test_tool_grants_are_wellformed() -> None:
    """A malformed grant line is exactly what the original test citation would miss."""
    block = STATUS.read_text().split("---", 2)[1]
    grants = [
        ln.strip().lstrip("-").strip().strip('"')
        for ln in block.splitlines()
        if ln.startswith("  - ")
    ]
    assert grants, "status declares no tool_grants"
    for g in grants:
        assert g, "empty grant entry"
        if g.startswith("mcp__"):
            assert "*" not in g[:-1], f"mcp glob must be trailing-only: {g}"
        else:
            assert re.match(r"^[A-Z][A-Za-z]*(\(.*\))?$", g), f"malformed grant: {g}"
