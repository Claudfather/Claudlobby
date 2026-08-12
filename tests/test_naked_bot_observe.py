"""Tests for the #1168 Phase 3 naked-bot observation gate.

Two layers, and the split is deliberate:

  * The harness's PURE logic (section parsing, artifact matching, drift
    diffing) is tested offline. A gate whose reader is wrong reports a clean
    baseline for the wrong reason, and that failure is a PASS.
  * The three properties the plan mandates are tested at the layer that can
    actually decide them for all twelve types. Only ONE type has a populated
    default today, so a compose-level assertion could only ever demonstrate the
    property for `guardrails`; the merge layer demonstrates the MECHANISM for
    every type, which is what Phase 2 needs before it populates the other
    eleven.

The real compose sweep is `lib/naked-bot-observe.py` itself, run against a
history-free export. It is not invoked here: it shells out to `git archive` and
runs twelve-plus full `generate` passes, which is a minutes-long job and belongs
in the gate, not in every suite run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_harness():
    """Import the hyphenated standalone module (`dispatch-overdue.py` precedent)."""
    path = REPO_ROOT / "lib" / "naked-bot-observe.py"
    spec = importlib.util.spec_from_file_location("naked_bot_observe", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["naked_bot_observe"] = mod
    spec.loader.exec_module(mod)
    return mod


nbo = _load_harness()


# ----------------------------------------------------------------- pure parsing


def test_parse_sections_maps_h2_to_its_h3_titles():
    md = "# Title\n\n## Protocols\n\n### One\n\ntext\n\n### Two\n\n## Guardrails\n\n### Three\n"
    assert nbo.parse_sections(md) == {
        "Protocols": ["One", "Two"],
        "Guardrails": ["Three"],
    }


def test_parse_sections_ignores_headings_inside_fenced_code():
    """A `#` comment in composed shell is not a section.

    Library content is full of fenced bash. Counting `# rebuild` as an H2 would
    invent sections no entity type produced and put them in a baseline that
    later diffs against reality.
    """
    md = "## Protocols\n\n### Real\n\n```bash\n## Not A Section\n### Also Not\n```\n\n### AlsoReal\n"
    assert nbo.parse_sections(md) == {"Protocols": ["Real", "AlsoReal"]}


def test_parse_sections_handles_tilde_fences():
    md = "## S\n\n~~~\n## Hidden\n~~~\n\n### Shown\n"
    assert nbo.parse_sections(md) == {"S": ["Shown"]}


def test_parse_sections_records_an_empty_section_as_present_but_empty():
    """`[]` and "absent" are different states and the gate turns on the difference.

    A section rendered with no entries means the type HAS an instruction surface
    and put nothing in it. A missing key means no surface at all. Phase 2 moves
    types from the first state to a populated one.
    """
    assert nbo.parse_sections("## Protocols\n\ntext only\n") == {"Protocols": []}


def test_inventory_dir_distinguishes_empty_dir_from_absent(tmp_path):
    """An empty `.claude/skills/` is what a naked bot gets; it must be visible."""
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("x")
    entries = nbo.inventory_dir(tmp_path)
    assert ".claude/skills/" in entries
    assert "CLAUDE.md" in entries


def test_inventory_dir_records_symlink_targets(tmp_path):
    """A skill arrives as a symlink; the link is the fact worth diffing."""
    (tmp_path / "target").write_text("x")
    (tmp_path / "link").symlink_to("target")
    assert "link -> target" in nbo.inventory_dir(tmp_path)


def test_match_artifacts_matches_the_path_not_the_symlink_suffix():
    entries = [".claude/skills/restart -> /lib/restart", ".mcp.json"]
    assert nbo.match_artifacts(entries, (".claude/skills/*",)) == [
        ".claude/skills/restart -> /lib/restart"
    ]


def test_match_artifacts_matches_a_trailing_slash_directory():
    assert nbo.match_artifacts(["tools/", "tools/x.sh"], ("tools/*",)) == ["tools/x.sh"]


# ------------------------------------------------------------------- the surfaces


def test_every_registry_type_has_a_declared_surface():
    """A thirteenth entity type must not be silently unobserved.

    P1's completeness test makes an unregistered type a failure. This is the
    same guard one layer out: a type in the registry with no `SURFACES` entry
    would compose freely and the gate would never look at it.
    """
    sys.path.insert(0, str(REPO_ROOT))
    import claudlobby.defaults as registry

    assert set(nbo.SURFACES) == set(registry.REGISTRY)


def test_surface_sections_match_the_template():
    """Pin the section labels against `render_section` in the real template.

    If the template renames a section and this map does not follow, the gate
    reads an absent key as "this type composed nothing" — a false clean, in the
    tier the gate exists for. The template is the source; this asserts the copy.
    """
    tmpl = (REPO_ROOT / "templates" / "claude.md.j2").read_text()
    declared = {s.section for s in nbo.SURFACES.values() if s.section}
    for label in declared:
        assert f"render_section('{label}'" in tmpl, (
            f"SURFACES claims a '{label}' section that the template never renders"
        )


def test_types_with_no_instruction_surface_are_recorded_as_none():
    """`skills`/`mcp`/`tools`/`expertise` add files, not sections.

    Recording them as `[]` would make "composed no instruction" and "cannot
    compose an instruction" read identically.
    """
    for t in ("skills", "mcp", "tools", "expertise"):
        assert nbo.SURFACES[t].section is None


# -------------------------------------------------- the three mandated properties


def test_all_twelve_types_have_an_explicit_disposition():
    """F4's junk-drawer guard, asserted from the gate's side as well.

    Duplicated deliberately: P1's copy lives beside the registry and this one
    beside the observer. If the registry module is ever refactored out from
    under the harness, the gate must fail rather than observe eleven types and
    report a clean twelve.
    """
    sys.path.insert(0, str(REPO_ROOT))
    import claudlobby.defaults as registry

    library_types = {
        p.name
        for p in (REPO_ROOT / "library").iterdir()
        if p.is_dir() and p.name != "__pycache__"
    }
    assert set(registry.REGISTRY) == library_types
    assert len(registry.REGISTRY) == 12


@pytest.mark.parametrize("etype", sorted(nbo.SURFACES))
def test_declaring_a_list_does_not_act_as_an_opt_out(etype, monkeypatch):
    """The `guardrails` property, generalised to every type (the plan's rule).

    A fleet that lists two skills has not said "and none of the defaults". This
    is asserted at the merge layer because only one type has a populated default
    today — the compose arm in the harness demonstrates it live for `guardrails`
    and cannot for the other eleven until Phase 2 populates them.

    Driven through the REAL `_coerce_bot`, per type, not through `_merge_lists`
    directly: `mcp` and `tools` resolve via `_merge_mcp_lists` /
    `_merge_tool_lists` and would be exempted by construction from a test that
    only exercised the shared merger. A parametrised test that asserted the same
    generic thing twelve times would name twelve types and check one.
    """
    sys.path.insert(0, str(REPO_ROOT))
    import claudlobby.config as config

    bot = config._coerce_bot(
        "probe",
        {"expertise": ["role"], etype: ["fleet-declared"]},
        {etype: ["compositor-default"]},
    )
    got = getattr(bot, etype)
    # `mcp`/`tools` merge into entry objects rather than bare strings; compare on
    # the name so the assertion is about membership, not representation.
    names = [g if isinstance(g, str) else g.name for g in got]
    assert "compositor-default" in names, (
        f"{etype}: declaring a list suppressed the compositor default — "
        "a fleet listing its own entries has not opted out of the defaults"
    )
    assert "fleet-declared" in names


#: Entity types that have a `system_defaults.<type>` opt-out key today.
#: Inverted one at a time as Phase 2 builds them — `guardrails` at Phase 1,
#: `protocols` when `shared-documentation` was admitted to the registry.
TYPES_WITH_AN_OPT_OUT = {"guardrails", "protocols"}


def test_the_opt_out_surface_does_not_exist_for_ten_of_twelve_types():
    """MEASURED, and this test records a GAP rather than blessing it.

    `SystemDefaultsConfig` reads a FIXED set of keys, not one per entity type.
    Ten of the twelve still have no opt-out, so the plan's checklist item — "for
    each of the 12 types, `system_defaults.<type>: false` demonstrably removes
    the default" — remains unsatisfiable for those ten.

    Worse, and UNCHANGED by Phase 2 so far: an unrecognised key is accepted
    silently. `_coerce_system_defaults` drops it and `generate` exits 0, so a
    fleet still cannot tell a working opt-out from a typo. Both confirmed on
    real composes by `lib/naked-bot-observe.py` (`optout:*` and
    `control:unknown-key` arms).

    This test FAILS every time Phase 2 adds a key. That is the intent: it is the
    todo, and going red is it telling you to move the type into
    `TYPES_WITH_AN_OPT_OUT` and re-record the baseline.
    """
    sys.path.insert(0, str(REPO_ROOT))
    import claudlobby.config as config

    cfg = config._coerce_system_defaults(
        {t: False for t in nbo.SURFACES if t not in TYPES_WITH_AN_OPT_OUT}
    )
    still_on = [
        t
        for t in nbo.SURFACES
        if t not in TYPES_WITH_AN_OPT_OUT
        and getattr(cfg, t, "NO SUCH FIELD") == "NO SUCH FIELD"
    ]
    assert len(still_on) == len(nbo.SURFACES) - len(TYPES_WITH_AN_OPT_OUT), (
        "the per-entity-type opt-out surface changed — re-run "
        "lib/naked-bot-observe.py and update the baseline record"
    )
    assert still_on and len(still_on) == 10, (
        f"expected ten types without an opt-out, got {len(still_on)}: {sorted(still_on)}"
    )


def test_every_declared_opt_out_key_actually_exists_on_the_config():
    """The other direction, and the one that fails quiet.

    A name in `TYPES_WITH_AN_OPT_OUT` that `SystemDefaultsConfig` does not carry
    would make the test above assert a smaller gap than really exists — the
    surface would look built while the key silently no-ops, which is the exact
    typo-indistinguishable-from-working failure this pair exists to expose.
    """
    sys.path.insert(0, str(REPO_ROOT))
    import claudlobby.config as config

    cfg = config._coerce_system_defaults({t: False for t in TYPES_WITH_AN_OPT_OUT})
    for t in sorted(TYPES_WITH_AN_OPT_OUT):
        assert getattr(cfg, t, "NO SUCH FIELD") is False, (
            f"{t} is declared to have an opt-out but SystemDefaultsConfig has no "
            "such field — the key is silently dropped"
        )


def test_the_guardrails_opt_out_does_exist_and_is_the_positive_control():
    """Without this, eleven no-ops read as eleven findings instead of one plus
    a broken instrument."""
    sys.path.insert(0, str(REPO_ROOT))
    import claudlobby.config as config

    assert config._coerce_system_defaults({"guardrails": False}).guardrails is False
    assert config._coerce_system_defaults({}).guardrails is True


# ------------------------------------------------------------------------- drift


def _report(**arms) -> dict:
    return {
        "schema": nbo.SCHEMA,
        "ref": "deadbeef",
        "arms": [{"label": k, "generate_rc": 0, "types": v} for k, v in arms.items()],
    }


def _t(instructions=None, entries=None, artifacts=None) -> dict:
    return {
        "tier": "instruct",
        "registry_entries": entries or [],
        "composed_instructions": instructions,
        "composed_artifacts": artifacts or [],
    }


def test_diff_reports_is_empty_for_an_identical_observation():
    r = _report(baseline={"protocols": _t(["A"])})
    assert nbo.diff_reports(r, r) == []


def test_diff_reports_names_a_newly_composed_instruction():
    """The Phase 2 case: a default lands and the gate must say which type."""
    old = _report(baseline={"skills": _t([])})
    new = _report(baseline={"skills": _t(["Restart"])})
    drift = nbo.diff_reports(old, new)
    assert len(drift) == 1
    assert "skills.composed_instructions" in drift[0]


def test_diff_reports_catches_an_artifact_only_change():
    """A skill symlink that adds no section still changed what the bot got."""
    old = _report(baseline={"skills": _t(None)})
    new = _report(baseline={"skills": _t(None, artifacts=[".claude/skills/x -> y"])})
    assert any("composed_artifacts" in d for d in nbo.diff_reports(old, new))


def test_diff_reports_refuses_to_compare_across_schema_versions():
    old = _report(baseline={})
    new = dict(_report(baseline={}), schema=nbo.SCHEMA + 1)
    drift = nbo.diff_reports(old, new)
    assert len(drift) == 1
    assert "not comparable" in drift[0]


def test_diff_reports_flags_a_generate_that_stopped_succeeding():
    old = _report(baseline={})
    new = _report(baseline={})
    new["arms"][0]["generate_rc"] = 1
    assert any("generate rc 0 -> 1" in d for d in nbo.diff_reports(old, new))


# ------------------------------------------- content probes and unwired defaults


def test_read_content_keys_returns_none_when_the_type_has_no_such_file(tmp_path):
    assert nbo.read_content_keys(tmp_path, None) is None


def test_read_content_keys_reads_a_nested_key(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"github": {}, "notion": {}}}')
    assert nbo.read_content_keys(tmp_path, (".mcp.json", "mcpServers")) == [
        "github",
        "notion",
    ]


def test_read_content_keys_distinguishes_empty_from_unreadable(tmp_path):
    """An empty config and a broken one must never record identically.

    `[]` is a real observation — the file said nothing is configured. A parse
    failure recording `[]` would let a corrupt compose read as a clean baseline.
    """
    (tmp_path / "a.json").write_text('{"k": {}}')
    (tmp_path / "b.json").write_text("{not json")
    assert nbo.read_content_keys(tmp_path, ("a.json", "k")) == []
    assert nbo.read_content_keys(tmp_path, ("b.json", "k")) == [
        "<UNREADABLE: JSONDecodeError>"
    ]
    assert nbo.read_content_keys(tmp_path, ("gone.json", "k")) == ["<ABSENT>"]


def test_the_two_always_present_files_have_a_content_probe():
    """`mcp` and `permissions` write into a file a naked bot already has.

    Without a content probe the gate would report "no change" for a default that
    landed inside one of them — blind for two of twelve, in the direction that
    reads clean.
    """
    for etype in ("mcp", "permissions"):
        assert nbo.SURFACES[etype].content_keys is not None


def test_inert_flags_a_registry_entry_that_composed_nothing():
    """The Phase 2 trap: the constant moves and no bot changes.

    `config.py` consumes only `DEFAULT_GUARDRAILS`; nothing feeds
    `resolve(<type>)` into the merge for the other eleven. Measured with a REAL
    skill (`doctor`): in the registry it composed no symlink, declared in
    fleet.yaml it composed one.
    """
    assert nbo.TypeObservation("instruct", ["doctor"], None, []).inert is True


def test_inert_is_false_when_the_entry_actually_composed():
    assert (
        nbo.TypeObservation("restrict", ["g"], ["Some Guardrail"], []).inert is False
    )
    assert nbo.TypeObservation("wire", ["t"], None, ["tools/t.sh"]).inert is False


def test_inert_is_false_for_an_empty_registry():
    """An unpopulated type is not "unwired" — there is nothing to wire yet."""
    assert nbo.TypeObservation("instruct", [], [], []).inert is False


def test_diff_survives_a_record_written_before_a_field_existed():
    """A missing field degrades to a reported difference, never a traceback.

    The schema guard is the first defence; this is the second, because
    forgetting to bump the schema is the likely slip — and it is the one that
    actually happened while building this harness.
    """
    old = _report(baseline={"mcp": {"tier": "wire", "registry_entries": []}})
    new = _report(baseline={"mcp": _t(None)})
    drift = nbo.diff_reports(old, new)
    assert drift and all(isinstance(d, str) for d in drift)


def test_scrub_removes_the_run_specific_export_path(tmp_path):
    """Two observations of the SAME commit must be byte-identical."""
    assert nbo.scrub(f"composed -> {tmp_path}/local/x", tmp_path) == (
        "composed -> $EXPORT/local/x"
    )
