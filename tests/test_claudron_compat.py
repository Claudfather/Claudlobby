"""Guards for the Claudron integration floor (plan 2026-07-07-claudron-consumption, 1c).

Invariants: the [vault] extra stays pinned (never a bare git URL), the compat
table stays well-formed, the integration doc stays in sync with the table it
renders, and — added in boundary phase L4 (deliverable 4) — the module docstring
names a *real* consumer (L1 wired ``doctor.check_claudron``; the docstring must
not out-live it).
"""

import inspect
import re
from pathlib import Path

import claudlobby.claudron_compat as claudron_compat
import claudlobby.doctor as doctor
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


def _pinned_vault_version() -> tuple[int, ...] | None:
    """The version tuple the [vault] extra pins, or None for a SHA/rangeless pin.

    Parses the trailing ``@v0.4.0`` (git tag) or a ``==0.4.0`` / ``>=0.4`` PyPI
    bound from the single claudron requirement.
    """
    for req in _vault_requirements():
        m = re.search(r"@v?(\d+(?:\.\d+){1,2})$", req) or re.search(
            r"(?:==|>=|~=)\s*v?(\d+(?:\.\d+){0,2})", req
        )
        if m:
            return tuple(int(p) for p in m.group(1).split("."))
    return None


def _release_tuple(release: str) -> tuple[int, ...] | None:
    m = re.match(r"v?(\d+(?:\.\d+){1,2})", release)
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def test_vault_pin_satisfies_compat_floor():
    """The pinned engine must actually PROVIDE every capability claudlobby
    composes — i.e. the pin ≥ the highest ``default_order_release`` among live
    (non-parked) floor rows.

    This is the guard #692 added after #685 half-landed: the docs + COMPAT_FLOOR
    were bumped to v0.4.0 (the fleet session loop's floor) while ``pyproject.toml``
    still pinned v0.2.0 — an engine with no per-bot hook dispatch — so a
    vault-wired bot composed a session loop against an engine that could not run
    it (#680's false-green, re-shipped on every fresh install). ``default_order_release``
    is an annotation Doctor never triggers on at *runtime* (it probes), but the
    *install-time* pin is a static decision and this floor is exactly its binding
    constraint. Parked rows are excluded — they are demand-gated, not shipped."""
    pin = _pinned_vault_version()
    if pin is None:  # SHA pin — version can't be compared; test_vault_extra_is_pinned covers form
        return
    live = [
        rel
        for cap in COMPAT_FLOOR
        if not cap.parked and (rel := _release_tuple(cap.default_order_release)) is not None
    ]
    assert live, "no live floor rows carry a comparable release"
    floor = max(live)
    assert pin >= floor, (
        f"[vault] pin v{'.'.join(map(str, pin))} is below the compat floor "
        f"v{'.'.join(map(str, floor))} — a vault-wired bot would compose a "
        f"capability the pinned engine cannot provide (see #680/#692)"
    )


def test_integration_doc_version_claim_matches_pin():
    """The doc's "What works today (at vX.Y.Z)" headline must name the pinned
    version — the exact doc/pin divergence #685 left behind."""
    pin = _pinned_vault_version()
    if pin is None:
        return
    doc = INTEGRATION_DOC.read_text()
    m = re.search(r"What works today \(at v(\d+(?:\.\d+){1,2})\)", doc)
    assert m, "integration doc is missing its 'What works today (at vX.Y.Z)' headline"
    doc_ver = tuple(int(p) for p in m.group(1).split("."))
    assert doc_ver == pin, (
        f"integration doc claims v{m.group(1)} but [vault] pins "
        f"v{'.'.join(map(str, pin))} — bump both together"
    )


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


def test_docstring_names_a_real_consumer():
    """Docstring truth (L4, deliverable 4): the module docstring promises
    ``doctor.check_claudron`` reads this table (the check L1 wired). Freeze that
    the named consumer is real — exists, is callable, and actually reads
    COMPAT_FLOOR — so the docstring cannot describe a consumer that drifted away
    (rename or delete ``check_claudron`` and this fails, forcing both in sync)."""
    docstring = claudron_compat.__doc__ or ""
    assert "doctor.check_claudron" in docstring, (
        "docstring names a consumer that is not doctor.check_claudron"
    )
    consumer = getattr(doctor, "check_claudron", None)
    assert callable(consumer), "docstring names doctor.check_claudron but it is gone"

    doctor_src = inspect.getsource(doctor)
    assert "from .claudron_compat import" in doctor_src, (
        "doctor no longer imports the compat table the docstring says it reads"
    )
    assert "COMPAT_FLOOR" in inspect.getsource(consumer), (
        "doctor.check_claudron no longer reads COMPAT_FLOOR — the docstring's "
        "'reads this table' claim is stale"
    )
