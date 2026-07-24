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

import pytest

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


def _vault_pin_ref() -> str | None:
    """The pinned git ref (``@<ref>``) or PyPI version bound from the single
    claudron requirement, or None if neither shape is present."""
    for req in _vault_requirements():
        m = re.search(r"@(\S+)$", req) or re.search(r"(?:==|>=|~=)\s*(\S+)", req)
        if m:
            return m.group(1)
    return None


def _pin_is_sha(ref: str | None) -> bool:
    """A git SHA pin (7–40 hex chars) is legitimately version-less — the floor
    check can only SKIP it, never silently pass. Detected POSITIVELY so that a
    ref which merely *fails* to parse as a version (an rc/pre-release suffix) is
    NOT mistaken for a SHA and waved through (the #692 review's finding 1)."""
    return bool(ref) and re.fullmatch(r"[0-9a-f]{7,40}", ref) is not None


def _version_from_ref(ref: str | None) -> tuple[int, ...] | None:
    """A ref → version tuple, or None when it is not a plain ``X.Y[.Z]`` — i.e.
    a SHA, a branch name, OR a suffixed pre-release like ``v0.4.0rc1`` /
    ``0.4.0.dev0``. Fully anchored so a trailing suffix does NOT parse to its
    numeric core (an rc BELOW the floor must not masquerade as a clean release)."""
    if ref is None:
        return None
    m = re.fullmatch(r"v?(\d+(?:\.\d+){1,2})", ref)
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _pinned_vault_version() -> tuple[int, ...] | None:
    """The version tuple the [vault] extra pins, or None when the ref is not a
    plain ``X.Y[.Z]`` release (SHA / branch / pre-release)."""
    return _version_from_ref(_vault_pin_ref())


def _release_tuple(release: str) -> tuple[int, ...] | None:
    """A COMPAT_FLOOR row's release as a version tuple, or None if it is not a
    plain ``X.Y[.Z]`` (a SHA-valued release, or ``unbuilt — demand-gated``)."""
    m = re.match(r"v?(\d+(?:\.\d+){1,2})", release)
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _floor_from_rows(rows) -> tuple[int, ...]:
    """Highest ``default_order_release`` among LIVE (non-parked) rows. A live row
    whose release does not parse (a SHA-valued release, per the schema's own docs)
    is a loud AssertionError, never a silent drop that would understate the floor
    (#692 review, finding 2)."""
    live: list[tuple[int, ...]] = []
    for cap in rows:
        if getattr(cap, "parked", ""):
            continue
        rel = _release_tuple(cap.default_order_release)
        assert rel is not None, (
            f"live COMPAT_FLOOR row {cap.feature!r} has a non-version "
            f"default_order_release {cap.default_order_release!r} — the pin-floor "
            f"check cannot include it. Give it a comparable release or park it."
        )
        live.append(rel)
    assert live, "no live floor rows carry a comparable release"
    return max(live)


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
    constraint. Parked rows are excluded — they are demand-gated, not shipped.

    Hardened (#692 review): a SHA pin legitimately SKIPS (no version to compare);
    anything else that fails to parse — an rc/pre-release suffix — FAILS LOUD
    rather than no-op'ing; and a live floor row whose release does not parse
    (a SHA-valued release) FAILS LOUD rather than silently dropping out of the
    max() (which would understate the floor). Both were "a bad pin passes
    silently," the exact failure this guard exists to prevent."""
    ref = _vault_pin_ref()
    if _pin_is_sha(ref):
        return  # version-less SHA pin — the floor comparison cannot apply
    pin = _pinned_vault_version()
    assert pin is not None, (
        f"[vault] pin ref {ref!r} is neither a SHA nor a plain X.Y[.Z] version "
        f"(e.g. an rc / pre-release / branch) — the floor check cannot verify it. "
        f"Pin a released tag or a full SHA."
    )
    # Every LIVE row must carry a comparable release: an unparseable one (a
    # SHA-valued release, per the schema's own docs) is a loud "reconcile this"
    # signal, never a silent drop that understates the floor.
    floor = _floor_from_rows(COMPAT_FLOOR)
    assert pin >= floor, (
        f"[vault] pin v{'.'.join(map(str, pin))} is below the compat floor "
        f"v{'.'.join(map(str, floor))} — a vault-wired bot would compose a "
        f"capability the pinned engine cannot provide (see #680/#692)"
    )


def test_integration_doc_version_claim_matches_pin():
    """The doc's "What works today (at vX.Y.Z)" headline must name the pinned
    version — the exact doc/pin divergence #685 left behind. A SHA pin has no
    version headline to match (skip); an rc/pre-release pin is already rejected
    loudly by test_vault_pin_satisfies_compat_floor, so skip here to avoid
    double-reporting the same defect."""
    ref = _vault_pin_ref()
    if _pin_is_sha(ref):
        return
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


# Direct parser tests (#692 review): exercise the version/SHA parsers against
# synthetic refs rather than relying solely on the repo's current pin as the one
# input — that reliance is exactly what let the rc-suffix / SHA-row gaps through.
@pytest.mark.parametrize(
    "ref,expected",
    [
        ("v0.4.0", (0, 4, 0)),
        ("0.4.0", (0, 4, 0)),
        ("v0.4", (0, 4)),
        ("v0.10.0", (0, 10, 0)),        # int compare, not lexicographic ("0.10">"0.9")
        ("v0.4.0rc1", None),            # pre-release suffix must NOT parse to 0.4.0
        ("v0.4.0-rc1", None),
        ("0.4.0.dev0", None),
        ("main", None),                 # branch pin — not a version
        ("6004421", None),              # short SHA
        ("6004421" + "0" * 33, None),   # 40-char all-hex SHA
        (None, None),
    ],
)
def test_version_from_ref_parser(ref, expected):
    assert _version_from_ref(ref) == expected


@pytest.mark.parametrize(
    "ref,is_sha",
    [
        ("6004421", True),                 # 7 hex
        ("6004421" + "0" * 33, True),      # 40 hex
        ("abcdef0", True),
        ("v0.4.0", False),
        ("0.4.0", False),
        ("v0.4.0rc1", False),
        ("main", False),
        ("abcdefg", False),                # 'g' is not hex
        (None, False),
    ],
)
def test_pin_is_sha(ref, is_sha):
    assert _pin_is_sha(ref) is is_sha


@pytest.mark.parametrize(
    "release,expected",
    [
        ("0.4.0", (0, 4, 0)),
        ("0.2.0", (0, 2, 0)),
        ("0.10.0", (0, 10, 0)),
        ("unbuilt — demand-gated", None),
        ("6004421" + "0" * 33, None),      # SHA-valued release → unparseable → None
    ],
)
def test_release_tuple_parser(release, expected):
    assert _release_tuple(release) == expected


def test_floor_rejects_unparseable_live_row():
    """A live floor row with a non-version release fails LOUD instead of dropping
    out of max() and understating the floor (#692 review, finding 2)."""
    from types import SimpleNamespace

    def _row(feature, release, parked=""):
        return SimpleNamespace(feature=feature, default_order_release=release, parked=parked)

    clean = [_row("a", "0.2.0"), _row("b", "0.4.0"), _row("c", "unbuilt", parked="decision C")]
    assert _floor_from_rows(clean) == (0, 4, 0)  # parked SHA/unbuilt rows are fine

    with_sha_live = clean + [_row("sha-row", "6004421" + "0" * 33)]  # LIVE + unparseable
    with pytest.raises(AssertionError):
        _floor_from_rows(with_sha_live)


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
