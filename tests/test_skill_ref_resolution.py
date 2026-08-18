"""Resolution gate for backticked ``/name`` references in composable content (#1253).

A denylist of dead names has no stopping rule: you can only search names you
already suspect, and nobody was ever going to grep ``/tech-debt``. This guard
inverts it — every backticked ``/name`` must RESOLVE to something invocable, so
a name nobody thought of is caught by construction rather than by a sweep.

Companion to ``tests/test_no_dead_claudna_refs.py``, which stays: that guard
denylists *known* renames and can say "use ``/claudna:audit tech-debt``
instead". This one catches the unknown-unknowns and cannot suggest a target.
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.commands.core import cmd_generate
from claudlobby.known_values import CLAUDNA_LIVE_SKILLS

from .conftest import load_test_fleet, make_paths

from claudlobby.skill_refs import (
    BUILTIN_COMMANDS,
    EXTERNAL_ALLOWLIST,
    KNOWN_UNRESOLVED,
    Resolution,
    fleet_skill_names,
    iter_refs,
    resolve_ref,
    scan_composable,
    scan_library,
)

FLEET_SKILLS = frozenset({"dispatch", "briefing", "adversarial-review"})


# ── the predicate ────────────────────────────────────────────────


def test_finds_a_bare_backticked_ref():
    assert [t for _, t in iter_refs("then run `/dispatch` to send it")] == ["/dispatch"]


def test_finds_a_ref_that_carries_arguments():
    """`/claudna:audit tech-debt` is one invocation, not a miss.

    A whole-span predicate sees only argument-less refs, which silently drops
    the post-consolidation verb forms — the exact shape the rename map produces.
    """
    assert [t for _, t in iter_refs("run `/claudna:audit tech-debt` now")] == [
        "/claudna:audit"
    ]


def test_ignores_a_multi_segment_filesystem_path():
    text = "see `/bin/bash` and `/tmp/tmux-$(id -u)/default` and `/migrations/`"
    assert [t for _, t in iter_refs(text)] == []


def test_ignores_a_span_that_does_not_start_with_a_slash():
    """Pass 1 of the hand sweeps over-matched here and was abandoned."""
    assert [t for _, t in iter_refs("`dist/index.js` and `publish.json`")] == []


def test_ignores_placeholder_refs():
    assert [t for _, t in iter_refs("`/<skill-name>` and `/claudna:<name>`")] == []


def test_an_unbackticked_reference_is_invisible():
    """A stated bound, asserted so it cannot be quietly widened later."""
    assert [t for _, t in iter_refs("then run /dispatch to send it")] == []


def test_reports_the_line_number_of_each_ref():
    assert list(iter_refs("intro\nrun `/dispatch`\nmore")) == [(2, "/dispatch")]


# ── resolution ───────────────────────────────────────────────────


def test_resolves_a_fleet_local_skill():
    r = resolve_ref("/dispatch", FLEET_SKILLS)
    assert r.resolved and r.rung == "fleet-skill"


def test_resolves_a_claude_code_builtin():
    r = resolve_ref("/compact", FLEET_SKILLS)
    assert r.resolved and r.rung == "builtin"


def test_resolves_an_allowlisted_external():
    r = resolve_ref("/setprivacy", FLEET_SKILLS)
    assert r.resolved and r.rung == "allowlist"


def test_resolves_a_live_plugin_skill():
    r = resolve_ref("/claudna:audit", FLEET_SKILLS)
    assert r.resolved and r.rung == "plugin-skill"


def test_does_not_resolve_a_retired_plugin_skill():
    """The class this gate exists for: retired, absorbed into `/claudna:audit`."""
    assert not resolve_ref("/claudna:tech-debt", FLEET_SKILLS).resolved


def test_prefixing_a_retired_name_does_not_make_it_resolve():
    """A normaliser would 'fix' `/tech-debt` into `/claudna:tech-debt`.

    Both must fail, or the text looks repaired while naming a command nobody
    can type. This is why the gate checks resolution and not shape.
    """
    bare = resolve_ref("/tech-debt", FLEET_SKILLS)
    prefixed = resolve_ref("/claudna:tech-debt", FLEET_SKILLS)
    assert not bare.resolved
    assert not prefixed.resolved


def test_an_unknown_bare_name_does_not_resolve():
    assert not resolve_ref("/nonexistent-thing", FLEET_SKILLS).resolved


def test_an_unknown_plugin_namespace_does_not_resolve():
    assert not resolve_ref("/otherplugin:whatever", FLEET_SKILLS).resolved


# ── deferral is not an allowlist ─────────────────────────────────


def test_a_deferred_ref_is_unresolved_and_flagged_as_deferred():
    """Deferral means 'broken, tracked'. Allowlist means 'not an invocation'.

    Collapsing the two would let real debt read as a pass, which is the
    'text now looks fixed' failure this gate is supposed to prevent.
    """
    r = resolve_ref("/ironclad", FLEET_SKILLS)
    assert not r.resolved
    assert r.deferred_to


def test_no_token_is_both_allowlisted_and_deferred():
    assert not (set(EXTERNAL_ALLOWLIST) & set(KNOWN_UNRESOLVED))


def test_every_allowlist_entry_carries_a_reason():
    assert all(v.strip() for v in EXTERNAL_ALLOWLIST.values())


def test_every_deferral_names_a_tracking_issue():
    assert all("#" in v for v in KNOWN_UNRESOLVED.values())


def test_builtins_are_not_silently_empty():
    """A positive control: an empty rung would resolve nothing and pass anyway."""
    assert "/compact" in BUILTIN_COMMANDS


@pytest.mark.parametrize("tok", sorted(EXTERNAL_ALLOWLIST))
def test_allowlist_entries_are_well_formed_tokens(tok):
    assert [t for _, t in iter_refs(f"`{tok}`")] == [tok]


# ── the gate over library/ ───────────────────────────────────────

REPO_DIR = Path(__file__).resolve().parent.parent
LIBRARY = REPO_DIR / "library"


def test_library_has_no_unresolvable_refs():
    """TEST LEVEL FAILS. Pre-merge, no live blast radius, guards the shared library."""
    offenders = [
        f"{Path(f.path).relative_to(REPO_DIR).as_posix()}:{f.lineno}: {f.token}"
        for f in scan_library(LIBRARY)
        if not f.deferred_to
    ]
    assert not offenders, (
        "backticked /refs that resolve to nothing invocable (#1253).\n"
        "Fix the reference, or — if it is not an invocation — add it to "
        "EXTERNAL_ALLOWLIST with a reason:\n  " + "\n  ".join(offenders)
    )


def test_every_deferral_is_still_live_in_library():
    """A deferral that outlives its defect becomes its own untruth.

    Without this, a fixed ref leaves a permanent entry that quietly excuses the
    next occurrence of the same token.
    """
    present = {f.token for f in scan_library(LIBRARY)}
    stale = sorted(set(KNOWN_UNRESOLVED) - present)
    assert not stale, (
        "KNOWN_UNRESOLVED entries no longer occur in library/ — delete them:\n  "
        + "\n  ".join(stale)
    )


# ── the compositor rung: warns, never fails ──────────────────────
#
# This is the ONLY layer that can see a fleet overlay. `local/*/library/` is
# gitignored, so CI is blind to it by design — and overlays are where the
# least-reviewed content lives. A test-only gate leaves every overlay
# permanently unguarded.


def _tree(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def test_scan_composable_sees_a_ref_only_present_in_the_overlay(tmp_path):
    base = tmp_path / "library"
    overlay = tmp_path / "overlay"
    _tree(base, "skills/dispatch/SKILL.md", "---\nname: dispatch\n---\n")
    _tree(overlay, "protocols/routing.md", "Then run `/nope-not-a-skill`.\n")
    tokens = {f.token for f in scan_composable(base, overlay)}
    assert "/nope-not-a-skill" in tokens


def test_a_skill_declared_only_in_the_overlay_resolves_a_base_reference(tmp_path):
    """Skills union across both trees, or every overlay skill reads as broken."""
    base = tmp_path / "library"
    overlay = tmp_path / "overlay"
    _tree(base, "protocols/p.md", "Then run `/fleet-local-only`.\n")
    _tree(overlay, "skills/fleet-local-only/SKILL.md", "---\nname: fleet-local-only\n---\n")
    assert [f.token for f in scan_composable(base, overlay)] == []


def test_scan_composable_tolerates_a_missing_overlay(tmp_path):
    base = tmp_path / "library"
    _tree(base, "protocols/p.md", "nothing here\n")
    assert scan_composable(base, None) == []


def test_scan_composable_tolerates_an_overlay_path_that_does_not_exist(tmp_path):
    """Fail open: a warn-only rung must never be the thing that breaks generate."""
    base = tmp_path / "library"
    _tree(base, "protocols/p.md", "nothing here\n")
    assert scan_composable(base, tmp_path / "absent") == []


class TestGenerateNeverFails:
    """`generate` runs against live fleets. A docs typo must not block it."""

    def _args(self, fleet_dir, strict=False):
        return SimpleNamespace(
            root=str(fleet_dir),
            fleet=None,
            seed=False,
            bot=None,
            strict=strict,
            verbose=False,
        )

    def test_generate_returns_zero_despite_an_unresolvable_ref(self, fleet_dir):
        _tree(fleet_dir / "library", "protocols/bad.md", "Run `/definitely-not-real`.\n")
        assert cmd_generate(self._args(fleet_dir)) == 0

    def test_generate_warns_and_names_the_ref(self, fleet_dir, caplog):
        _tree(fleet_dir / "library", "protocols/bad.md", "Run `/definitely-not-real`.\n")
        with caplog.at_level(logging.WARNING):
            cmd_generate(self._args(fleet_dir))
        assert "/definitely-not-real" in caplog.text

    def test_a_ref_finding_never_enters_the_validator_report(self, fleet_dir):
        """--strict turns validator warnings into a hard refusal, so a ref
        finding must never be routed there. That is the real mechanism, and it
        is what this asserts.

        An earlier version asserted `cmd_generate(strict=True) == 0` instead.
        It passed locally and failed in CI, because it needed the stock fixture
        to emit ZERO other validator warnings and which warnings it emits is
        host-dependent. Asserting on the report is deterministic everywhere;
        asserting on the exit code was measuring the fixture's ambient noise.
        """
        from claudlobby.validator import validate

        _tree(fleet_dir / "library", "protocols/bad.md", "Run `/definitely-not-real`.\n")
        paths = make_paths(fleet_dir)
        fleet = load_test_fleet(fleet_dir)
        report = validate(fleet, paths)
        leaked = [
            w
            for w in list(report.warnings) + list(report.errors)
            if "definitely-not-real" in str(w)
        ]
        assert not leaked, (
            "ref findings reached the validator report — --strict will now "
            f"refuse to generate on a docs typo: {leaked}"
        )


def test_no_deferral_is_also_resolvable_through_a_rung():
    """A deferral must never shadow a name that has since become valid.

    ``resolve_ref`` checks deferrals first, so a token left in KNOWN_UNRESOLVED
    after gaining a real skill would keep reporting broken forever — and the
    staleness test above cannot see it, because the token still occurs.
    """
    every_skill = set(fleet_skill_names(LIBRARY))
    masked = [
        tok
        for tok in KNOWN_UNRESOLVED
        if tok in BUILTIN_COMMANDS
        or tok in CLAUDNA_LIVE_SKILLS
        or tok.removeprefix("/") in every_skill
    ]
    assert not masked, f"deferred but now resolvable — delete the deferral: {masked}"
