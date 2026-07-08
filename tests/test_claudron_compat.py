"""Guards for the Claudron integration floor (plan 2026-07-07-claudron-consumption, 1c).

Three invariants: the [vault] extra stays pinned (never a bare git URL), the
compat table stays well-formed, and the integration doc stays in sync with the
table it renders.
"""

import re
from pathlib import Path

from claudlobby.claudron_compat import COMPAT_FLOOR

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INTEGRATION_DOC = ROOT / "documentation" / "integrations" / "claudron-integration.md"


def _vault_requirement() -> str:
    text = PYPROJECT.read_text()
    match = re.search(r'^vault\s*=\s*\["([^"]+)"\]', text, re.MULTILINE)
    assert match, "pyproject.toml [vault] extra not found"
    return match.group(1)


def test_vault_extra_is_pinned():
    """The claudron dependency must name a SHA, tag, or version range — never
    a bare git URL tracking HEAD (F1 lock)."""
    req = _vault_requirement()
    if "git+" in req:
        assert re.search(
            r"@(?:[0-9a-f]{40}|v?\d+\.\d+(?:\.\d+)?)$", req
        ), f"[vault] extra tracks an unpinned git HEAD: {req}"
    else:
        # PyPI form — require a version specifier.
        assert re.search(
            r"(?:==|>=|~=)\s*\d", req
        ), f"[vault] extra has no version bound: {req}"


def test_compat_floor_well_formed():
    assert COMPAT_FLOOR, "compat floor must not be empty"
    features = [c.feature for c in COMPAT_FLOOR]
    assert len(features) == len(set(features)), "duplicate feature entries"
    for cap in COMPAT_FLOOR:
        assert cap.feature and cap.requires and cap.default_order_release


def test_integration_doc_renders_compat_floor():
    """The doc is the human rendering of COMPAT_FLOOR — every capability and
    slated release must appear there, and the doc must name the module as SSOT."""
    doc = INTEGRATION_DOC.read_text()
    assert "claudlobby/claudron_compat.py" in doc
    for cap in COMPAT_FLOOR:
        assert cap.requires in doc, f"doc missing capability: {cap.requires!r}"
        assert (
            cap.default_order_release in doc
        ), f"doc missing release annotation: {cap.default_order_release!r}"
