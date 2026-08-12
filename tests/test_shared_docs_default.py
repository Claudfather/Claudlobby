"""#1168 — the `shared-documentation` INSTRUCT default, and its opt-out.

This protocol composed on every overlay-mode bot for as long as the compositor
has had shared docs, from a literal `append()` in `composer.py` that sat
downstream of the merge `defaults.REGISTRY` feeds. Nothing declared it and
nothing could switch it off — not `system_defaults.guardrails`, not the
`system_defaults: false` kill switch.

Bringing it under the registry has one hard requirement, and it is what most of
this file asserts: **the default path must not move a byte.** A registry entry
that also changed what bots are told would be the silent estate-wide edit the
tier split exists to prevent, so "it still ships" is pinned here as tightly as
"it can now be switched off".

The negatives all carry a positive control. A bot composing no
shared-documentation section proves nothing on its own — a broken fixture
composes nothing either — so each opt-out case also asserts that the
`guardrails` default still lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby import defaults
from claudlobby.composer import compose_bot
from claudlobby.config import BotConfig, FleetConfig, SystemDefaultsConfig
from claudlobby.paths import Paths

#: Sentinel body, not the real protocol text. The assertion is about the
#: composition pathway; pinning it to real prose would make this file fail on an
#: unrelated copy-edit.
PROTOCOL_SENTINEL = "SHARED_DOCS_PROTOCOL_SENTINEL"
GUARDRAIL_SENTINEL = "GUARDRAIL_CONTROL_SENTINEL"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "claudlobby"
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "protocols").mkdir(parents=True)
    (root / "library" / "guardrails").mkdir(parents=True)
    (root / "runtime" / "bots").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "voices").mkdir()
    (root / "templates").mkdir()

    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (root / "library" / "protocols" / "shared-documentation.md").write_text(
        f"---\ntitle: Shared Documentation\ndescription: d\n---\n\n"
        f"# Shared Documentation\n\n{PROTOCOL_SENTINEL}\n"
    )
    # The registry's RESTRICT entry, as the positive control for every negative.
    for name in defaults.resolve("guardrails"):
        (root / "library" / "guardrails" / f"{name}.md").write_text(
            f"---\ntitle: {name}\ndescription: d\n---\n\n# {name}\n\n{GUARDRAIL_SENTINEL}\n"
        )
    # Renders the bodies directly rather than calling the real template's
    # `render_section` macro: this file is asserting which items reach the
    # template, not how the template lays them out.
    (root / "templates" / "claude.md.j2").write_text(
        "{% for p in protocols %}## {{ p.title }}\n{{ p.body }}\n{% endfor %}"
        "{% for g in guardrails %}## {{ g.title }}\n{{ g.body }}\n{% endfor %}"
    )
    return root


def _compose(
    tmp_path: Path,
    *,
    system_defaults: SystemDefaultsConfig | None = None,
    declared_protocols: list[str] | None = None,
    root_mode: bool = False,
) -> str:
    root = _root(tmp_path)
    paths = Paths(root=root, fleet_dir=None if root_mode else root)
    bot = BotConfig(
        bot_id="worker",
        name="worker",
        expertise=["eng"],
        protocols=declared_protocols or [],
        guardrails=list(defaults.resolve("guardrails")),
    )
    fleet = FleetConfig(
        name="t",
        service_prefix="p",
        bots={"worker": bot},
        system_defaults=system_defaults or SystemDefaultsConfig(),
    )
    bot_dir = compose_bot(bot, fleet, paths, log=lambda _m: None)
    return (bot_dir / "CLAUDE.md").read_text()


class TestTheDefaultPathDoesNotMove:
    """The requirement the whole change is gated on."""

    def test_a_fleet_declaring_no_protocols_still_receives_it(self, tmp_path):
        # If this ever fails, the registry has silently REMOVED an instruction
        # from every bot on the estate — the same class of harm as adding one.
        assert PROTOCOL_SENTINEL in _compose(tmp_path)

    def test_it_is_not_duplicated_when_the_fleet_also_declares_it(self, tmp_path):
        out = _compose(tmp_path, declared_protocols=["shared-documentation"])
        assert out.count(PROTOCOL_SENTINEL) == 1


class TestTheOptOut:
    """What did not exist before: a way to say no."""

    def test_system_defaults_protocols_false_removes_it(self, tmp_path):
        out = _compose(tmp_path, system_defaults=SystemDefaultsConfig(protocols=False))
        assert PROTOCOL_SENTINEL not in out
        assert GUARDRAIL_SENTINEL in out, "control: the fleet stopped composing at all"

    def test_the_kill_switch_removes_it(self, tmp_path):
        # `system_defaults: false`. Measured on all 16 naked-bot arms before this
        # change: the kill switch did NOT remove it. That is the defect.
        out = _compose(tmp_path, system_defaults=SystemDefaultsConfig(enabled=False))
        assert PROTOCOL_SENTINEL not in out

    def test_an_explicit_declaration_survives_the_opt_out(self, tmp_path):
        # Opting out of the DEFAULT must not strip a protocol the fleet asked
        # for by name. The two are different statements and the merge must not
        # collapse them.
        out = _compose(
            tmp_path,
            system_defaults=SystemDefaultsConfig(protocols=False),
            declared_protocols=["shared-documentation"],
        )
        assert PROTOCOL_SENTINEL in out


class TestTheAvailabilityGate:
    """The bound the registry cannot state on its own."""

    def test_root_mode_composes_no_shared_documentation(self, tmp_path):
        # `Paths.shared_docs` is None without a fleet_dir, so there is no doc
        # tree for the protocol to describe. Measured on a real root-mode
        # compose before this change: the section was absent then too, which is
        # why the gate has to stay.
        out = _compose(tmp_path, root_mode=True)
        assert PROTOCOL_SENTINEL not in out
        assert GUARDRAIL_SENTINEL in out, "control: the fleet stopped composing at all"

    def test_the_gate_is_keyed_by_entry_not_by_type(self, tmp_path):
        # A second protocols default must NOT inherit the shared-docs condition.
        # Keyed by type, this test's entry would vanish in root mode along with
        # shared-documentation, and nothing would say why.
        root = _root(tmp_path)
        (root / "library" / "protocols" / "ungated.md").write_text(
            "---\ntitle: Ungated\ndescription: d\n---\n\n# Ungated\n\nUNGATED_SENTINEL\n"
        )
        entry = defaults.REGISTRY["protocols"]
        defaults.REGISTRY["protocols"] = type(entry)(
            tier=entry.tier,
            reason=entry.reason,
            entries=entry.entries + ("ungated",),
            settled=entry.settled,
            grandfathered=entry.grandfathered,
        )
        try:
            paths = Paths(root=root, fleet_dir=None)
            bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
            fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
            out = (
                compose_bot(bot, fleet, paths, log=lambda _m: None) / "CLAUDE.md"
            ).read_text()
            assert "UNGATED_SENTINEL" in out, (
                "an ungated entry was suppressed in root mode — the availability "
                "gate is keyed by type rather than by entry name"
            )
            assert PROTOCOL_SENTINEL not in out
        finally:
            defaults.REGISTRY["protocols"] = entry


class TestTheRegistryIsTheSource:
    """No second place names the entry."""

    def test_the_composer_names_no_protocol_literal(self):
        source = (
            Path(__file__).resolve().parent.parent / "claudlobby" / "composer.py"
        ).read_text()
        # The string may appear in a comment explaining the history; what must
        # not come back is a literal append into the composed list.
        assert 'protocol_names.append("shared-documentation")' not in source, (
            "the hardcoded append is back — the registry is no longer the source"
        )

    @pytest.mark.parametrize("entry", sorted(defaults.resolve("protocols")))
    def test_every_registered_protocol_exists_in_the_library(self, entry):
        # A registry entry naming a file that does not exist would fail at
        # compose time on a real fleet, not here, and only for fleets that had
        # not opted out.
        lib = Path(__file__).resolve().parent.parent / "library" / "protocols"
        assert (lib / f"{entry}.md").is_file(), f"{entry} is registered but absent"
