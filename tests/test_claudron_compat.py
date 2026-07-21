"""Guards for the Claudron integration floor (plan 2026-07-07-claudron-consumption, 1c).

Three invariants: the [vault] extra stays pinned (never a bare git URL), the
compat table stays well-formed, and the integration doc stays in sync with the
table it renders.
"""

import re
from pathlib import Path

from claudlobby.claudron_compat import COMPAT_FLOOR, PROBE_API, PROBE_VERB_PREFIX

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INTEGRATION_DOC = ROOT / "documentation" / "integrations" / "claudron-integration.md"

try:
    import tomllib
except ModuleNotFoundError:  # requires-python floor is 3.10; tomllib is 3.11+
    import tomli as tomllib  # type: ignore[no-redef]


def _vault_requirements() -> list[str]:
    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["optional-dependencies"]["vault"]


def test_vault_extra_is_pinned():
    """Every claudron requirement must name a SHA, tag, or version range — never
    a bare git URL tracking HEAD (F1 lock)."""
    reqs = _vault_requirements()
    assert reqs, "pyproject.toml [vault] extra is empty"
    for req in reqs:
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


def test_parked_rows_are_never_probed():
    """A parked row is demand-gated by decision, not missing capability —
    probing one is how it would come back as "unmet" (boundary phase L1)."""
    for cap in COMPAT_FLOOR:
        if cap.parked:
            assert cap.probe == "", f"parked row carries a probe: {cap.feature}"


def test_live_rows_declare_a_probe():
    """Doctor decides met/unmet by probing the capability, never by comparing
    the release annotation — so an un-parked row without a probe is a row
    doctor cannot honestly report on."""
    for cap in COMPAT_FLOOR:
        if not cap.parked:
            assert cap.probe, f"live row has no probe: {cap.feature}"


def test_integration_doc_renders_compat_floor():
    """The doc table is the human rendering of COMPAT_FLOOR — each capability
    must appear as a full row (**every** column bound, so no cell can drift
    independently), and the doc must name the module as SSOT.

    The Doctor-state cell is derived, not asserted as prose: a parked row must
    render its parked marker, a live row must name the probe it actually runs.
    Without this the fourth column was a hand-maintained copy of `cap.parked` /
    `cap.probe` with no gate — the exact rendered-copy drift the table's own
    SSOT claim forbids."""
    doc = INTEGRATION_DOC.read_text()
    assert "claudlobby/claudron_compat.py" in doc
    for cap in COMPAT_FLOOR:
        row = f"| {cap.feature} | {cap.requires} | {cap.default_order_release} |"
        assert row in doc, f"doc table missing or stale row: {row}"

        # Locate the rendered row and pin its Doctor-state cell to the data.
        # Backticks are markdown, not content — strip them so the gate tracks
        # what the cell *says*, not how it is formatted.
        line = next(ln for ln in doc.splitlines() if ln.startswith(row))
        state = line[len(row):].replace("`", "")
        if cap.parked:
            assert "parked" in state, f"parked row not rendered as parked: {cap.feature}"
            assert "unmet" not in state.replace('never "unmet"', ""), (
                f"parked row rendered as a deficiency: {cap.feature}"
            )
        else:
            probe = cap.probe.removeprefix(PROBE_VERB_PREFIX) if cap.probe != PROBE_API else "[vault] extra"
            assert probe in state, (
                f"live row's Doctor-state cell does not name its probe "
                f"({probe!r}): {cap.feature}"
            )
