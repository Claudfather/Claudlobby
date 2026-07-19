"""Tests for the equippable briefing feature (#627 P3).

Covers config coercion (bot-level `briefing:` stanza) with parse-time hard
rejects (non-shell-ident slot names, 5-field cron), the BRIEFING_* env block in
the equipped bot's bot.conf, per-(bot,slot) timer emission via
compose_fleet_timers, the generate-side stale-unit reconciler with its
abort-on-degenerate guard, and the validator source-coverage warning.

Mirrors tests/test_code_audit_sweep.py — one feature, one cohesive test module.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.composer import (
    _reconcile_briefing_units,
    compose_bot_conf,
    compose_fleet_timers,
)
from claudlobby.config import BriefingConfig, _coerce_bot, load_fleet
from claudlobby.paths import Paths
from claudlobby.validator import validate


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


def _write(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(dedent(body))
    return root / "fleet.yaml"


def _env_val(conf: str, key: str) -> str | None:
    prefix = f"export {key}="
    for line in conf.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return " ".join(shlex.split(line[len(prefix) :]))
    return None


# A fleet equipping bot "kev" with two briefing slots, per-slot sections, and
# sources, plus an mcp source so the coverage validator stays quiet.
_BRIEFING_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      system_defaults: false
      bots:
        kev:
          expertise: [eng]
          mcp: [github]
          briefing:
            slots:
              morning: "*-*-* 08:30:00"
              evening: "*-*-* 18:30:00"
            sections:
              morning: [overnight, calendar, overdue]
            sources: [github, gmail]
        mason:
          expertise: [eng]
"""


# ---------------------------------------------------------------------------
# Component A — config parse + parse-time hard rejects
# ---------------------------------------------------------------------------
class TestBriefingConfigCoercion:
    def test_none_when_absent(self):
        bot = _coerce_bot("kev", {"expertise": ["eng"]}, {})
        assert bot.briefing is None

    def test_slots_parsed(self):
        bot = _coerce_bot(
            "kev",
            {
                "expertise": ["eng"],
                "briefing": {"slots": {"morning": "*-*-* 08:30:00"}},
            },
            {},
        )
        assert isinstance(bot.briefing, BriefingConfig)
        assert bot.briefing.slots == {"morning": "*-*-* 08:30:00"}

    def test_sections_and_sources_parsed(self):
        bot = _coerce_bot(
            "kev",
            {
                "expertise": ["eng"],
                "briefing": {
                    "slots": {"morning": "*-*-* 08:30:00"},
                    "sections": {"morning": ["overnight", "calendar"]},
                    "sources": ["github", "gmail"],
                },
            },
            {},
        )
        assert bot.briefing.sections == {"morning": ["overnight", "calendar"]}
        assert bot.briefing.sources == ["github", "gmail"]

    def test_empty_slots_rejected(self):
        with pytest.raises(ValueError, match="slots"):
            _coerce_bot("kev", {"expertise": ["eng"], "briefing": {"slots": {}}}, {})

    def test_slots_not_a_map_rejected(self):
        with pytest.raises(ValueError):
            _coerce_bot(
                "kev",
                {"expertise": ["eng"], "briefing": {"slots": ["morning"]}},
                {},
            )

    @pytest.mark.parametrize("bad_slot", ["week-end", "9am", "analytics-pm", "a.b"])
    def test_non_shell_ident_slot_rejected(self, bad_slot):
        # BRIEFING_SECTIONS_<SLOT> is a shell var — a non-identifier slot would
        # break bot.conf sourcing, so it must be rejected at parse time.
        with pytest.raises(ValueError, match="slot"):
            _coerce_bot(
                "kev",
                {
                    "expertise": ["eng"],
                    "briefing": {"slots": {bad_slot: "*-*-* 08:30:00"}},
                },
                {},
            )

    @pytest.mark.parametrize("cron", ["30 8 * * *", "0 */2 * * 1-5", "* * * * *"])
    def test_five_field_cron_rejected(self, cron):
        # The timer chain speaks systemd OnCalendar, not 5-field cron.
        with pytest.raises(ValueError, match="[Cc]ron|OnCalendar"):
            _coerce_bot(
                "kev",
                {"expertise": ["eng"], "briefing": {"slots": {"morning": cron}}},
                {},
            )

    @pytest.mark.parametrize(
        "cal", ["*-*-* 08:30:00", "Mon *-*-* 08:30:00", "08:30", "daily"]
    )
    def test_oncalendar_values_accepted(self, cal):
        bot = _coerce_bot(
            "kev",
            {"expertise": ["eng"], "briefing": {"slots": {"morning": cal}}},
            {},
        )
        assert bot.briefing.slots["morning"] == cal

    def test_load_fleet_parses_briefing(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _BRIEFING_FLEET))
        assert fleet.bots["kev"].briefing is not None
        assert set(fleet.bots["kev"].briefing.slots) == {"morning", "evening"}
        assert fleet.bots["mason"].briefing is None

    def test_briefing_enabled_true_when_equipped(self, tmp_path):
        # The predicate the generate CLI guards compose_fleet_timers on — without
        # it, a system_defaults:false briefing-only fleet composes no timers.
        fleet, _md = load_fleet(_write(tmp_path / "f", _BRIEFING_FLEET))
        assert fleet.briefing_enabled() is True

    def test_briefing_enabled_false_when_none(self, tmp_path):
        body = """\
            fleet:
              name: t
              service_prefix: com.test
              system_defaults: false
              bots:
                solo:
                  expertise: [eng]
        """
        fleet, _md = load_fleet(_write(tmp_path / "f", body))
        assert fleet.briefing_enabled() is False


# ---------------------------------------------------------------------------
# Component B — BRIEFING_* bot.conf emission (F4)
# ---------------------------------------------------------------------------
class TestBriefingEnvEmission:
    def _conf(self, tmp_path, bot_id):
        fleet, _md = load_fleet(_write(tmp_path / "f", _BRIEFING_FLEET))
        return compose_bot_conf(fleet.bots[bot_id], fleet, _make_paths(tmp_path / "f"))

    def test_equipped_bot_gets_slots(self, tmp_path):
        conf = self._conf(tmp_path, "kev")
        slots = (_env_val(conf, "BRIEFING_SLOTS") or "").split()
        assert set(slots) == {"morning", "evening"}

    def test_equipped_bot_gets_sources(self, tmp_path):
        conf = self._conf(tmp_path, "kev")
        assert _env_val(conf, "BRIEFING_SOURCES") == "github gmail"

    def test_per_slot_sections_uppercased(self, tmp_path):
        # Env-var convention: BRIEFING_SECTIONS_<SLOT> with SLOT upper-cased.
        conf = self._conf(tmp_path, "kev")
        assert (
            _env_val(conf, "BRIEFING_SECTIONS_MORNING") == "overnight calendar overdue"
        )

    def test_slot_without_sections_has_no_section_var(self, tmp_path):
        conf = self._conf(tmp_path, "kev")
        assert "BRIEFING_SECTIONS_EVENING" not in conf

    def test_non_briefing_bot_has_no_env(self, tmp_path):
        conf = self._conf(tmp_path, "mason")
        assert "BRIEFING_SLOTS" not in conf


# ---------------------------------------------------------------------------
# Component C — per-(bot,slot) timer composition (F3)
# ---------------------------------------------------------------------------
class TestBriefingTimerComposition:
    def _compose(self, tmp_path):
        fleet, md = load_fleet(_write(tmp_path / "f", _BRIEFING_FLEET))
        paths = _make_paths(tmp_path / "f")
        timers = compose_fleet_timers(fleet, paths, md)
        return timers

    def test_units_emitted_per_slot(self, tmp_path):
        timers = self._compose(tmp_path)
        for slot in ("morning", "evening"):
            for ext in ("service", "timer", "plist"):
                unit = timers / f"com.test.briefing-kev-{slot}.{ext}"
                assert unit.exists(), f"missing {unit.name}"

    def test_timer_carries_oncalendar(self, tmp_path):
        timers = self._compose(tmp_path)
        body = (timers / "com.test.briefing-kev-morning.timer").read_text()
        assert "OnCalendar=*-*-* 08:30:00" in body

    def test_execstart_passes_fleet_bot_slot(self, tmp_path):
        timers = self._compose(tmp_path)
        svc = (timers / "com.test.briefing-kev-morning.service").read_text()
        assert "lib/briefing-trigger.sh" in svc
        assert "test-fleet kev morning" in svc

    def test_plist_passes_fleet_bot_slot(self, tmp_path):
        timers = self._compose(tmp_path)
        plist = (timers / "com.test.briefing-kev-evening.plist").read_text()
        # launchd ProgramArguments carry each arg as its own <string>.
        for arg in ("test-fleet", "kev", "evening"):
            assert f"<string>{arg}</string>" in plist

    def test_non_briefing_bot_gets_no_units(self, tmp_path):
        timers = self._compose(tmp_path)
        assert not list(timers.glob("com.test.briefing-mason-*"))

    def test_no_briefing_bots_no_units(self, tmp_path):
        body = """\
            fleet:
              name: t
              service_prefix: com.test
              system_defaults: false
              bots:
                solo:
                  expertise: [eng]
        """
        fleet, md = load_fleet(_write(tmp_path / "f", body))
        timers = compose_fleet_timers(fleet, _make_paths(tmp_path / "f"), md)
        assert not list(timers.glob("com.test.briefing-*"))


# ---------------------------------------------------------------------------
# Component D — generate-side reconcile + abort-on-degenerate guard
# (the mandated TDD target — a bug can NOT prune all live briefing timers)
# ---------------------------------------------------------------------------
class TestBriefingReconcile:
    def _seed(self, timers_dir: Path, *basenames: str) -> None:
        timers_dir.mkdir(parents=True, exist_ok=True)
        for base in basenames:
            for ext in ("service", "timer", "plist"):
                (timers_dir / f"{base}.{ext}").write_text("# seed\n")

    def test_prunes_stale_keeps_composed(self, tmp_path):
        self._seed(
            tmp_path,
            "com.test.briefing-kev-morning",
            "com.test.briefing-kev-noon",  # renamed-away slot
        )
        composed = {"com.test.briefing-kev-morning"}
        pruned = _reconcile_briefing_units(tmp_path, "com.test", composed, 1)
        assert pruned == ["com.test.briefing-kev-noon"]
        assert (tmp_path / "com.test.briefing-kev-morning.timer").exists()
        assert not (tmp_path / "com.test.briefing-kev-noon.timer").exists()

    def test_glob_bounded_never_touches_other_units(self, tmp_path):
        self._seed(
            tmp_path,
            "com.test.briefing-kev-noon",  # stale briefing
            "com.test.code-audit-sweep",  # unrelated fleet job
            "com.test.fleet-pulse",
        )
        # Composed set empty is fine here because n_declared==0 (full removal).
        _reconcile_briefing_units(tmp_path, "com.test", set(), 0)
        assert (tmp_path / "com.test.code-audit-sweep.timer").exists()
        assert (tmp_path / "com.test.fleet-pulse.timer").exists()

    def test_full_removal_prunes_all_briefing(self, tmp_path):
        self._seed(
            tmp_path,
            "com.test.briefing-kev-morning",
            "com.test.briefing-kev-evening",
        )
        pruned = _reconcile_briefing_units(tmp_path, "com.test", set(), 0)
        assert set(pruned) == {
            "com.test.briefing-kev-morning",
            "com.test.briefing-kev-evening",
        }
        assert not list(tmp_path.glob("com.test.briefing-*"))

    def test_abort_on_degenerate_composed_set(self, tmp_path, caplog):
        # THE guard: config declares briefing bots but the composed set came back
        # empty (a composition bug) — the reconciler must NOT prune the live units.
        self._seed(
            tmp_path,
            "com.test.briefing-kev-morning",
            "com.test.briefing-kev-evening",
        )
        with caplog.at_level("WARNING"):
            pruned = _reconcile_briefing_units(tmp_path, "com.test", set(), 2)
        assert pruned == []
        assert (tmp_path / "com.test.briefing-kev-morning.timer").exists()
        assert (tmp_path / "com.test.briefing-kev-evening.timer").exists()
        assert any(
            "degenerate" in r.message.lower() or "skipping" in r.message.lower()
            for r in caplog.records
        )

    def test_abort_on_partial_composed_set(self, tmp_path, caplog):
        # navi's #630 gap: config declares 4 (bot,slot) units but only 3 composed
        # (an interrupted/torn generate leaves runtime/fleet/timers/ short one) —
        # the reconciler must NOT prune the live 4th, exactly as it refuses a
        # fully-empty set. Empty is just partial's limit case; any shortfall is
        # the same composition-bug signal, so the guard is quantitative.
        self._seed(
            tmp_path,
            "com.test.briefing-kev-morning",
            "com.test.briefing-kev-evening",
            "com.test.briefing-ari-morning",
            "com.test.briefing-ari-evening",  # live 4th — absent from composed
        )
        composed = {
            "com.test.briefing-kev-morning",
            "com.test.briefing-kev-evening",
            "com.test.briefing-ari-morning",
        }
        with caplog.at_level("WARNING"):
            pruned = _reconcile_briefing_units(tmp_path, "com.test", composed, 4)
        assert pruned == []
        assert (tmp_path / "com.test.briefing-ari-evening.timer").exists()
        assert any(
            "partial" in r.message.lower() or "skipping" in r.message.lower()
            for r in caplog.records
        )

    def test_generate_reconcile_prunes_renamed_slot(self, tmp_path):
        # End-to-end through compose_fleet_timers: pre-seed a stale slot unit,
        # then compose the current fleet — the stale one is gone, current remain.
        fleet, md = load_fleet(_write(tmp_path / "f", _BRIEFING_FLEET))
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        self._seed(timers, "com.test.briefing-kev-midday")  # since-removed slot
        compose_fleet_timers(fleet, paths, md)
        assert not (timers / "com.test.briefing-kev-midday.timer").exists()
        assert (timers / "com.test.briefing-kev-morning.timer").exists()

    def test_generate_writes_briefing_expected_manifest(self, tmp_path):
        # generate emits a config-truth BRIEFING_EXPECTED manifest (DORMANT
        # precedent) listing every declared (bot,slot) unit, so setup-fleet's
        # reconcile has an independent count to catch a partial/torn timers dir.
        fleet, md = load_fleet(_write(tmp_path / "f", _BRIEFING_FLEET))
        paths = _make_paths(tmp_path / "f")
        compose_fleet_timers(fleet, paths, md)
        manifest = paths.runtime_fleet / "timers" / "BRIEFING_EXPECTED"
        listed = {
            ln
            for ln in manifest.read_text().splitlines()
            if ln.startswith("com.test.briefing-")
        }
        assert listed == {
            "com.test.briefing-kev-morning",
            "com.test.briefing-kev-evening",
        }

    def test_briefing_manifest_removed_when_stanza_gone(self, tmp_path):
        # A fleet that once equipped briefing but no longer declares any: the
        # manifest must report zero expected units so setup-fleet allows the
        # full teardown (composed 0 == expected 0 → prune, not abort).
        fleet, md = load_fleet(
            _write(
                tmp_path / "f",
                """\
                fleet:
                  name: t
                  service_prefix: com.test
                  system_defaults: false
                  bots:
                    kev:
                      expertise: [eng]
                """,
            )
        )
        paths = _make_paths(tmp_path / "f")
        timers = paths.runtime_fleet / "timers"
        self._seed(timers, "com.test.briefing-kev-morning")  # leftover from before
        compose_fleet_timers(fleet, paths, md)
        manifest = timers / "BRIEFING_EXPECTED"
        assert manifest.exists()
        assert not any(
            ln.startswith("com.test.briefing-")
            for ln in manifest.read_text().splitlines()
        )


# ---------------------------------------------------------------------------
# Component E2 — `claudlobby diff` must see briefing timers
# ---------------------------------------------------------------------------
class TestBriefingDiff:
    def test_diff_detects_briefing_timer_drift(self, tmp_path):
        # diff_fleet_timers must diff briefing units even on a system_defaults:false
        # briefing-only fleet — the guard historically skipped it (defaults/sweep
        # only), so briefing drift went silently undetected.
        from claudlobby.diff import diff_fleet_timers

        fleet, md = load_fleet(_write(tmp_path / "f", _BRIEFING_FLEET))
        paths = _make_paths(tmp_path / "f")
        compose_fleet_timers(fleet, paths, md)  # runtime now has briefing units
        # Introduce drift: a composed unit goes missing from runtime.
        (
            paths.runtime_fleet / "timers" / "com.test.briefing-kev-morning.timer"
        ).unlink()
        out = diff_fleet_timers(fleet, paths, md)
        assert "briefing-kev-morning" in out, f"drift not detected: {out!r}"


# ---------------------------------------------------------------------------
# Component F — validator source-coverage warning
# ---------------------------------------------------------------------------
class TestBriefingValidator:
    def test_briefing_without_source_warns(self, tmp_path):
        body = """\
            fleet:
              name: t
              service_prefix: com.test
              system_defaults: false
              bots:
                kev:
                  expertise: [eng]
                  briefing:
                    slots:
                      morning: "*-*-* 08:30:00"
        """
        fleet, _md = load_fleet(_write(tmp_path / "f", body))
        report = validate(fleet, _make_paths(tmp_path / "f"))
        assert any("briefing" in w and "source" in w.lower() for w in report.warnings)

    def test_briefing_with_mcp_source_no_warn(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _BRIEFING_FLEET))
        report = validate(fleet, _make_paths(tmp_path / "f"))
        assert not any(
            "briefing" in w and "source" in w.lower() for w in report.warnings
        )
