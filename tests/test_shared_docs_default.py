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

from dataclasses import replace
from pathlib import Path

from claudlobby import defaults
from claudlobby.composer import compose_bot
from claudlobby.config import BotConfig, FleetConfig, SystemDefaultsConfig
from claudlobby.paths import Paths

#: Sentinel body, not the real protocol text. The assertion is about the
#: composition pathway; pinning it to real prose would make this file fail on an
#: unrelated copy-edit.
PROTOCOL_SENTINEL = "SHARED_DOCS_PROTOCOL_SENTINEL"
GUARDRAIL_SENTINEL = "GUARDRAIL_CONTROL_SENTINEL"


def _protocol(name: str, sentinel: str) -> str:
    return f"---\ntitle: {name}\ndescription: d\n---\n\n# {name}\n\n{sentinel}\n"


def _root(tmp_path: Path, extra_protocols: dict[str, str] | None = None) -> Path:
    # exist_ok throughout: a caller that needs one more library file calls this
    # again rather than re-deriving Paths/BotConfig/FleetConfig inline. The
    # inline copy is what drifts — it silently stops matching `_compose`'s fleet
    # shape and then tests a fleet no other test in this file is describing.
    root = tmp_path / "claudlobby"
    for sub in (
        "library/expertise",
        "library/protocols",
        "library/guardrails",
        "runtime/bots",
        "lib",
        "voices",
        "templates",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)

    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (root / "library" / "protocols" / "shared-documentation.md").write_text(
        _protocol("Shared Documentation", PROTOCOL_SENTINEL)
    )
    for pname, sentinel in (extra_protocols or {}).items():
        (root / "library" / "protocols" / f"{pname}.md").write_text(
            _protocol(pname, sentinel)
        )
    # The registry's RESTRICT entry, as the positive control for every negative.
    for name in defaults.resolve("guardrails"):
        (root / "library" / "guardrails" / f"{name}.md").write_text(
            _protocol(name, GUARDRAIL_SENTINEL)
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
    extra_protocols: dict[str, str] | None = None,
) -> str:
    root = _root(tmp_path, extra_protocols)
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

    def test_the_gate_is_keyed_by_entry_not_by_type(self, tmp_path, monkeypatch):
        # A second protocols default must NOT inherit the shared-docs condition.
        # Keyed by type, this entry would vanish in root mode alongside
        # shared-documentation, and nothing would say why.
        #
        # `replace` rather than rebuilding the dataclass field by field: a
        # hand-listed reconstruction silently drops whatever it forgets, and
        # this PR added a field that had to be threaded through by hand. The
        # next one would not be, and nothing would fail.
        entry = defaults.REGISTRY["protocols"]
        monkeypatch.setitem(
            defaults.REGISTRY,
            "protocols",
            replace(entry, entries=entry.entries + ("ungated",)),
        )
        out = _compose(
            tmp_path,
            root_mode=True,
            extra_protocols={"ungated": "UNGATED_SENTINEL"},
        )
        assert "UNGATED_SENTINEL" in out, (
            "an ungated entry was suppressed in root mode — the availability "
            "gate is keyed by type rather than by entry name"
        )
        assert PROTOCOL_SENTINEL not in out


# NOTE — two tests deliberately do NOT live here.
#
# "the composer names no `shared-documentation` literal" was a source-text grep.
# It was removed rather than moved: measured, re-adding the hardcoded append
# fails `test_system_defaults_protocols_false_removes_it` and
# `test_the_kill_switch_removes_it` on their own, so the grep asserted nothing
# the behavioural tests did not — while breaking on a reformat or a rename of
# the local `protocol_names`, and passing on any re-add spelled differently.
#
# "every registered entry resolves to a library file" belongs to the registry,
# not to this entry, and lives in `test_defaults_registry.py` where the library
# path is already bound and it is asserted for all twelve types.
