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
a fleet's protocol sections; the last protocol test pins that difference,
because it is why the protocol copies stay. Names are obviously fake; the repo
is public.
"""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import defaults
from claudlobby.composer import render_bot_files, resolve_effective_protocols
from claudlobby.config import DEFAULT_GUARDRAILS, DEFAULT_RESOURCES, load_fleet
from tests.conftest import install_real_template, load_test_fleet, make_paths

GUARDRAILS = list(DEFAULT_GUARDRAILS)
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
    """Replace one exact span of the fixture's fleet.yaml, refusing a miss.

    A replacement that matches nothing leaves the fixture unchanged and the test
    then measures the default fleet, so the count is asserted.
    """
    path = fleet_dir / "fleet.yaml"
    text = path.read_text()
    assert text.count(old) == 1, f"fixture edit matched {text.count(old)} times: {old!r}"
    path.write_text(text.replace(old, new))


def _effective_protocols(fleet_dir: Path, bot_id: str) -> list[str]:
    """The bot's effective protocols, less the shared-documentation slot.

    The fixture fleet has a shared-docs tree, so that slot is appended too; it is
    not what these tests are about, and its own tests pin it.
    """
    fleet = load_test_fleet(fleet_dir)
    bot = fleet.bots[bot_id]
    effective = resolve_effective_protocols(
        bot, fleet, make_paths(fleet_dir), is_manager=bot_id in fleet.manager_bots()
    )
    assert len(effective) == len(set(effective)), f"duplicate protocol: {effective}"
    return [p for p in effective if not p.startswith("shared-documentation")]


class TestTheRegistry:
    def test_timezone_is_the_resource_default_at_the_instruct_tier(self):
        d = defaults.REGISTRY["resources"]
        assert DEFAULT_RESOURCES == ["timezone"]
        assert d.tier is defaults.Tier.INSTRUCT
        assert d.grandfathered == ("timezone",)

    def test_the_three_protocols_are_grandfathered_global_entries(self):
        grandfathered = defaults.REGISTRY["protocols"].grandfathered
        assert set(PROTOCOLS) <= set(grandfathered)

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


class TestRenderedBytes:
    """Byte-level: each bot's rendered CLAUDE.md, settings and bot.conf.

    `bot.conf` records the manifest's own digest (`FLEET_MANIFEST_SHA256`), which
    any edit to fleet.yaml changes, comments included. That line is the one
    allowed to differ, and it must differ, or the second render did not read the
    edited file.
    """

    FILES = ("CLAUDE.md", ".claude/settings.local.json", "bot.conf")
    DIGEST = re.compile(r"^export FLEET_MANIFEST_SHA256=.*$", re.M)

    def _render(self, fleet_dir: Path) -> dict[str, dict[str, str]]:
        fleet = load_test_fleet(fleet_dir)
        paths = make_paths(fleet_dir)
        out = {}
        for bot in fleet.bots.values():
            files = render_bot_files(bot, fleet, paths)
            out[bot.bot_id] = {name: files[name].content for name in self.FILES}
        return out

    def test_guardrail_and_timezone_copies_come_out_with_identical_bytes(
        self, fleet_dir, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        install_real_template(fleet_dir)
        (fleet_dir / "library" / "guardrails" / "extra-guard.md").write_text(
            "---\ntitle: extra-guard\n---\n\nA fleet's own guardrail.\n"
        )
        copies = (
            "    guardrails: [no-push-main, no-destructive-git, pii-protection, "
            "no-fabrication, confirm-before-send, extra-guard]\n"
            "    resources: [timezone]\n"
        )
        _edit(fleet_dir, "    guardrails: [no-push-main]\n", copies)
        with_copies = self._render(fleet_dir)
        # Positive control, so the comparison below is not two empty states:
        # every default rendered, each exactly once.
        headings = [f"### {g}" for g in GUARDRAILS if g != "no-push-main"]
        headings += ["### No push to main", "### timezone", "### extra-guard"]
        for bot_id, files in with_copies.items():
            for heading in headings:
                assert files["CLAUDE.md"].count(heading + "\n") == 1, f"{bot_id}: {heading!r}"
        _edit(fleet_dir, copies, "    guardrails: [extra-guard]\n")
        without_copies = self._render(fleet_dir)
        assert sorted(without_copies) == sorted(with_copies)
        for bot_id, files in with_copies.items():
            for name in self.FILES:
                a, b = files[name], without_copies[bot_id][name]
                if name == "bot.conf":
                    assert self.DIGEST.search(a) and a != b, f"{bot_id}: digest did not move"
                    a, b = self.DIGEST.sub("", a), self.DIGEST.sub("", b)
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

    def test_the_opt_out_removes_them(self, fleet_dir):
        _edit(
            fleet_dir,
            "  accounts:\n",
            "  system_defaults:\n    protocols: false\n\n  accounts:\n",
        )
        assert not set(PROTOCOLS) & set(_effective_protocols(fleet_dir, "worker-1"))

    def test_copies_keep_their_order_and_removing_them_moves_the_three_last(self, fleet_dir):
        """Why the protocol copies stay: protocol defaults are appended.

        With the copies in place the declared order is kept, each entry once. With
        them removed the three compose after the declared list. A registry
        position cannot fix that in general: two bots whose remaining lists both
        start with `report-back` can need these three before it in one fleet and
        after it in another.
        """
        with_copies = f"    protocols: [report-back, {', '.join(PROTOCOLS)}, extra-proto]\n"
        _edit(fleet_dir, "    protocols: [report-back]\n", with_copies)
        assert _effective_protocols(fleet_dir, "worker-1") == [
            "report-back", *PROTOCOLS, "extra-proto",
        ]
        _edit(fleet_dir, with_copies, "    protocols: [report-back, extra-proto]\n")
        assert _effective_protocols(fleet_dir, "worker-1") == [
            "report-back", "extra-proto", *PROTOCOLS,
        ]
