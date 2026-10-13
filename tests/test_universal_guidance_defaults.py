"""#1168: the guidance every fleet copied by hand is a framework default.

Five guardrails, three protocols and the `timezone` resource were declared in
each fleet's own `defaults:`. They are registry entries now, and three
properties are pinned here:

* a fleet that declares nothing composes them;
* a fleet that still declares them by hand composes each once, in its own
  order, so registering them changes nothing that fleet composes;
* the opt-out removes them.

Guardrail and resource defaults merge AHEAD of the fleet's own list, so a fleet
that lists them first composes identical bytes after removing its copies.
Protocol defaults compose AFTER the declared list, so the same removal reorders
a fleet's protocol sections. The last test class pins that difference, because
it is why the protocol copies stay. Names are obviously fake; the repo is public.
"""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import defaults
from claudlobby.composer import compose_bot, resolve_effective_protocols
from claudlobby.config import DEFAULT_GUARDRAILS, DEFAULT_RESOURCES, load_fleet
from claudlobby.paths import Paths
from tests.conftest import install_real_template
from tests.package_fixtures import source_package

GUARDRAILS = [
    "claudlobby-dev-in-projects",
    "no-push-main",
    "no-destructive-git",
    "pii-protection",
    "no-fabrication",
    "confirm-before-send",
]
TELEGRAM_PROTOCOLS = ["telegram-routing", "telegram-formatting"]
PROTOCOLS = ["context-management", *TELEGRAM_PROTOCOLS]

NAKED = """\
    fleet:
      name: test-fleet
      manager: lead
      service_prefix: com.test
      bots:
        lead:
          expertise: [eng]
        worker-1:
          expertise: [eng]
"""

#: The shape every fleet had before the registry carried these entries: the
#: five guardrails first and in registry order, the install rule further down,
#: `timezone` first in every resources list.
WITH_COPIES = """\
    fleet:
      name: test-fleet
      manager: lead
      service_prefix: com.test
      defaults:
        guardrails: [no-push-main, no-destructive-git, pii-protection, no-fabrication, confirm-before-send, extra-guard, claudlobby-dev-in-projects]
        resources: [timezone]
      bots:
        lead:
          expertise: [eng]
        worker-1:
          expertise: [eng]
          guardrails: [stanza-guard]
          resources: [timezone, extra-res]
"""

#: The same fleet with its copies of the registered entries removed.
WITHOUT_COPIES = """\
    fleet:
      name: test-fleet
      manager: lead
      service_prefix: com.test
      defaults:
        guardrails: [extra-guard]
      bots:
        lead:
          expertise: [eng]
        worker-1:
          expertise: [eng]
          guardrails: [stanza-guard]
          resources: [extra-res]
"""


def _fleet(root: Path, body: str) -> Path:
    """A one-file fleet: `load_fleet` reads nothing else for these lists."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "fleet.yaml"
    path.write_text(dedent(body))
    return path


def _with(body: str, block: str) -> str:
    """Insert *block* (already at fleet-key indent) ahead of `bots:`, once."""
    marker = "      bots:\n"
    assert body.count(marker) == 1
    return body.replace(marker, block + marker)


def _edit(fleet_dir: Path, old: str, new: str) -> None:
    """Replace one exact span of the fixture's fleet.yaml, refusing a miss."""
    path = fleet_dir / "fleet.yaml"
    text = path.read_text()
    assert text.count(old) == 1, f"fixture edit matched {text.count(old)} times: {old!r}"
    path.write_text(text.replace(old, new))


def _paths(fleet_dir: Path) -> Paths:
    return Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package())


def _effective_protocols(fleet_dir: Path, bot_id: str) -> list[str]:
    """The bot's effective protocols, less the shared-documentation slot.

    The fixture fleet has a shared-docs tree, so that slot is appended too;
    it is not what these tests are about, and its own tests pin it.
    """
    fleet, _ = load_fleet(fleet_dir / "fleet.yaml")
    bot = fleet.bots[bot_id]
    effective = resolve_effective_protocols(
        bot, fleet, _paths(fleet_dir), is_manager=bot_id in fleet.manager_bots()
    )
    assert len(effective) == len(set(effective)), f"duplicate protocol: {effective}"
    return [p for p in effective if not p.startswith("shared-documentation")]


class TestTheRegistry:
    def test_the_guardrail_defaults_lead_with_the_install_rule(self):
        assert DEFAULT_GUARDRAILS == GUARDRAILS

    def test_timezone_is_the_resource_default_at_the_instruct_tier(self):
        d = defaults.REGISTRY["resources"]
        assert DEFAULT_RESOURCES == ["timezone"]
        assert d.tier is defaults.Tier.INSTRUCT
        assert d.grandfathered == ("timezone",)

    def test_the_three_protocols_are_grandfathered_global_entries(self):
        d = defaults.REGISTRY["protocols"]
        for name in PROTOCOLS:
            assert name in d.entries, name
            assert name in d.grandfathered, name

    @pytest.mark.parametrize("channel", [True, False])
    def test_only_the_telegram_protocols_wait_for_the_channel(self, channel):
        facts = defaults.Facts(shared_docs=False, vault_wired=False, telegram_channel=channel)
        assert defaults.available("context-management", facts) is True
        for name in TELEGRAM_PROTOCOLS:
            assert defaults.available(name, facts) is channel, name

    def test_a_facts_built_without_the_channel_gates_them_off(self):
        facts = defaults.Facts(shared_docs=True, vault_wired=False)
        assert not any(defaults.available(n, facts) for n in TELEGRAM_PROTOCOLS)


class TestGuardrailAndResourceDefaults:
    def test_a_fleet_that_declares_nothing_composes_them(self, tmp_path):
        fleet, _ = load_fleet(_fleet(tmp_path, NAKED))
        for bot in fleet.bots.values():
            assert bot.guardrails == GUARDRAILS
            assert bot.resources == ["timezone"]

    def test_copies_in_place_compose_each_entry_once_in_order(self, tmp_path):
        fleet, _ = load_fleet(_fleet(tmp_path, WITH_COPIES))
        assert fleet.bots["lead"].guardrails == [*GUARDRAILS, "extra-guard"]
        worker = fleet.bots["worker-1"]
        assert worker.guardrails == [*GUARDRAILS, "extra-guard", "stanza-guard"]
        assert worker.resources == ["timezone", "extra-res"]

    def test_removing_the_copies_composes_the_same_lists(self, tmp_path):
        a, _ = load_fleet(_fleet(tmp_path / "a", WITH_COPIES))
        b, _ = load_fleet(_fleet(tmp_path / "b", WITHOUT_COPIES))
        assert sorted(a.bots) == sorted(b.bots)
        for name in a.bots:
            assert a.bots[name].guardrails == b.bots[name].guardrails, name
            assert a.bots[name].resources == b.bots[name].resources, name

    @pytest.mark.parametrize(
        "block",
        [
            "      system_defaults:\n        guardrails: false\n        resources: false\n",
            "      system_defaults: false\n",
        ],
        ids=["per-category", "kill-switch"],
    )
    def test_the_opt_out_removes_them(self, tmp_path, block):
        fleet, _ = load_fleet(_fleet(tmp_path, _with(NAKED, block)))
        for bot in fleet.bots.values():
            assert bot.guardrails == []
            assert bot.resources == []

    def test_an_entry_the_bot_lists_itself_survives_the_opt_out(self, tmp_path):
        body = _with(
            WITHOUT_COPIES,
            "      system_defaults:\n        guardrails: false\n        resources: false\n",
        ).replace("resources: [extra-res]", "resources: [timezone, extra-res]")
        fleet, _ = load_fleet(_fleet(tmp_path, body))
        worker = fleet.bots["worker-1"]
        assert worker.resources == ["timezone", "extra-res"]
        assert worker.guardrails == ["extra-guard", "stanza-guard"]


class TestComposedBytes:
    """Byte-level: the composed CLAUDE.md, settings and bot.conf, both bots.

    `bot.conf` records the manifest's own digest (`FLEET_MANIFEST_SHA256`), which any
    edit to fleet.yaml changes, comments included. That line is the one allowed to
    differ, and it must differ, or the second compose did not read the edited file.
    """

    DIGEST = re.compile(rb"^export FLEET_MANIFEST_SHA256=.*$", re.M)

    FILES = ("CLAUDE.md", ".claude/settings.local.json", "bot.conf")

    def _compose(self, fleet_dir: Path, monkeypatch) -> dict[str, dict[str, bytes]]:
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        install_real_template(fleet_dir)
        fleet, _ = load_fleet(fleet_dir / "fleet.yaml")
        paths = _paths(fleet_dir)
        out = {}
        for bot in fleet.bots.values():
            compose_bot(bot, fleet, paths, log=lambda m: None)
            bot_dir = paths.bot_runtime(bot.bot_id)
            out[bot.bot_id] = {f: (bot_dir / f).read_bytes() for f in self.FILES}
        return out

    def test_guardrail_and_timezone_copies_come_out_with_identical_bytes(
        self, fleet_dir, monkeypatch
    ):
        (fleet_dir / "library" / "guardrails" / "extra-guard.md").write_text(
            "---\ntitle: extra-guard\n---\n\nA fleet's own guardrail.\n"
        )
        line = "    guardrails: [no-push-main]\n"
        _edit(
            fleet_dir,
            line,
            "    guardrails: [no-push-main, no-destructive-git, pii-protection, "
            "no-fabrication, confirm-before-send, extra-guard]\n"
            "    resources: [timezone]\n",
        )
        with_copies = self._compose(fleet_dir, monkeypatch)
        # Positive control, so the comparison below is not two empty states:
        # every default rendered, each exactly once.
        for bot_id, files in with_copies.items():
            text = files["CLAUDE.md"].decode()
            for heading in (*(f"### {g}" for g in GUARDRAILS if g != "no-push-main"),
                            "### No push to main", "### timezone", "### extra-guard"):
                assert text.count(heading + "\n") == 1, f"{bot_id}: {heading!r}"
        _edit(
            fleet_dir,
            "    guardrails: [no-push-main, no-destructive-git, pii-protection, "
            "no-fabrication, confirm-before-send, extra-guard]\n"
            "    resources: [timezone]\n",
            "    guardrails: [extra-guard]\n",
        )
        without_copies = self._compose(fleet_dir, monkeypatch)
        assert sorted(without_copies) == sorted(with_copies)
        for bot_id, files in with_copies.items():
            for name in self.FILES:
                a, b = files[name], without_copies[bot_id][name]
                if name == "bot.conf":
                    assert self.DIGEST.search(a) and a != b, f"{bot_id}: digest did not move"
                    a, b = self.DIGEST.sub(b"", a), self.DIGEST.sub(b"", b)
                assert a == b, f"{bot_id}: {name} differs"


class TestProtocolDefaults:
    def test_a_bot_with_a_telegram_channel_gets_all_three_after_its_own(self, fleet_dir):
        effective = _effective_protocols(fleet_dir, "worker-1")
        assert [p for p in effective if p in PROTOCOLS] == PROTOCOLS
        assert effective.index("report-back") < effective.index("context-management")

    def test_a_bot_without_a_channel_gets_context_management_only(self, fleet_dir):
        _edit(
            fleet_dir,
            "    worker-1:\n      expertise: [software-engineering]\n",
            "    worker-1:\n      expertise: [software-engineering]\n      channels: []\n",
        )
        effective = _effective_protocols(fleet_dir, "worker-1")
        assert "context-management" in effective
        assert not set(TELEGRAM_PROTOCOLS) & set(effective)
        # The other bot still has its channel and keeps both.
        assert set(TELEGRAM_PROTOCOLS) <= set(_effective_protocols(fleet_dir, "lead"))

    def test_declared_copies_keep_their_order_and_compose_once(self, fleet_dir):
        declared = ["report-back", *PROTOCOLS, "extra-proto"]
        _edit(
            fleet_dir,
            "    protocols: [report-back]\n",
            f"    protocols: [{', '.join(declared)}]\n",
        )
        assert _effective_protocols(fleet_dir, "worker-1") == declared

    def test_the_opt_out_removes_them(self, fleet_dir):
        _edit(
            fleet_dir,
            "  accounts:\n",
            "  system_defaults:\n    protocols: false\n\n  accounts:\n",
        )
        assert not set(PROTOCOLS) & set(_effective_protocols(fleet_dir, "worker-1"))


class TestWhyTheProtocolCopiesStay:
    def test_removing_them_moves_them_after_the_declared_list(self, fleet_dir):
        """Not byte-identical, by construction: the defaults are appended.

        A fleet that lists the three after `report-back` and before its own
        protocols composes them there; with the copies removed they compose at
        the end. A registry position cannot fix it in general: two bots whose
        remaining lists both start with `report-back` can need these three
        before it in one fleet and after it in another.
        """
        _edit(
            fleet_dir,
            "    protocols: [report-back]\n",
            f"    protocols: [report-back, {', '.join(PROTOCOLS)}, extra-proto]\n",
        )
        with_copies = _effective_protocols(fleet_dir, "worker-1")
        _edit(
            fleet_dir,
            f"    protocols: [report-back, {', '.join(PROTOCOLS)}, extra-proto]\n",
            "    protocols: [report-back, extra-proto]\n",
        )
        without_copies = _effective_protocols(fleet_dir, "worker-1")
        assert sorted(with_copies) == sorted(without_copies)
        assert without_copies == ["report-back", "extra-proto", *PROTOCOLS]
        assert with_copies != without_copies
