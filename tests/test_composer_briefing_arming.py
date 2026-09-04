"""Gauntlet-round pin: briefing timers carry the plane arming (C11).

Pre-fix, the briefing door's plane emission was UNREACHABLE in production:
briefing-trigger gates on PLANE_EMIT_ENABLED, but composed timer units carry
a CLOSED env list and the fleet-tier .env a bot session sources never reaches
a scheduler process — so the "five doors dual-write" claim was four doors and
one dead block. The composer now resolves the fleet's arming through the
runtime's own tier cascade (env_tiers — NOT Paths.env_file, whose docstring
names that misuse as the #1226 defect) and emits it as an Environment= line /
plist key on the briefing units, with systemd/launchd parity.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from claudlobby.composer import compose_fleet_timers
from claudlobby.config import load_fleet
from claudlobby.env_tiers import Resolution, ResolverUnavailable
from claudlobby.paths import Paths

_FLEET = """\
    fleet:
      name: arm-fleet
      service_prefix: com.test
      system_defaults: false
      bots:
        kev:
          expertise: [eng]
          mcp: [github]
          briefing:
            slots:
              morning: "*-*-* 08:30:00"
"""


def _compose(tmp_path: Path, monkeypatch, armed: str):
    root = tmp_path / "f"
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(dedent(_FLEET))
    fleet, md = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=root)

    # The composer does `from . import env_tiers` at the call site, so
    # patching the module's functions is exactly what it will see.
    import claudlobby.env_tiers as env_tiers_mod

    res = Resolution(
        name="PLANE_EMIT_ENABLED", value=armed, tier="fleet", path=None,
    )
    monkeypatch.setattr(env_tiers_mod, "read_tiers",
                        lambda paths, fleet_name=None, bot_name=None: [])
    monkeypatch.setattr(env_tiers_mod, "cascade",
                        lambda tiers: {"PLANE_EMIT_ENABLED": res})
    return compose_fleet_timers(fleet, paths, md)


def test_armed_fleet_composes_the_env_on_both_platforms(tmp_path, monkeypatch):
    timers = _compose(tmp_path, monkeypatch, armed="1")
    service = (timers / "com.test.briefing-kev-morning.service").read_text()
    plist = (timers / "com.test.briefing-kev-morning.plist").read_text()
    assert "Environment=PLANE_EMIT_ENABLED=1" in service
    assert "<key>PLANE_EMIT_ENABLED</key>" in plist
    assert "<string>1</string>" in plist


def test_unarmed_fleet_composes_no_plane_env(tmp_path, monkeypatch):
    timers = _compose(tmp_path, monkeypatch, armed="0")
    service = (timers / "com.test.briefing-kev-morning.service").read_text()
    plist = (timers / "com.test.briefing-kev-morning.plist").read_text()
    assert "PLANE_EMIT_ENABLED" not in service
    assert "PLANE_EMIT_ENABLED" not in plist


def test_resolver_unavailable_composes_unarmed_not_crashed(tmp_path, monkeypatch):
    import claudlobby.env_tiers as env_tiers_mod

    def unavailable(paths, fleet_name=None, bot_name=None):
        raise ResolverUnavailable("no resolver in this fixture")

    monkeypatch.setattr(env_tiers_mod, "read_tiers", unavailable)
    root = tmp_path / "f"
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(dedent(_FLEET))
    fleet, md = load_fleet(root / "fleet.yaml")
    timers = compose_fleet_timers(fleet, Paths(root=root, fleet_dir=root), md)
    service = (timers / "com.test.briefing-kev-morning.service").read_text()
    assert "PLANE_EMIT_ENABLED" not in service


def _compose_with_defaults(tmp_path: Path, monkeypatch, armed: str):
    # the base fixture disables system defaults, so no job units compose —
    # the keepalive pin needs a defaults-on fleet of its own
    fl = dedent(_FLEET).replace("system_defaults: false", "system_defaults: true")
    root = tmp_path / "fd"
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(fl)
    fleet, md = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=root)
    import claudlobby.env_tiers as env_tiers_mod
    res = Resolution(
        name="PLANE_EMIT_ENABLED", value=armed, tier="fleet", path=None,
    )
    monkeypatch.setattr(env_tiers_mod, "read_tiers",
                        lambda paths, fleet_name=None, bot_name=None: [])
    monkeypatch.setattr(env_tiers_mod, "cascade",
                        lambda tiers: {"PLANE_EMIT_ENABLED": res})
    return compose_fleet_timers(fleet, paths, md)


def test_keepalive_unit_carries_the_arming_too(tmp_path, monkeypatch):
    """The presence door (keepalive-as-a-door) runs from the keepalive job
    unit — the SAME closed-scheduler-env class briefing hit. The one
    hoisted derivation stamps the emission flag on EVERY fleet job unit
    (Phase B1 follow-up: any script that sources lib-common can land a
    fleet event, and fleet-pulse — the fleet's main emitter — composed
    without it on the live estate, so a whole sweep reached only the JSONL);
    unarmed composes unarmed."""
    timers = _compose_with_defaults(tmp_path, monkeypatch, armed="1")
    service = (timers / "com.test.keepalive.service").read_text()
    assert "Environment=PLANE_EMIT_ENABLED=1" in service
    pulse = (timers / "com.test.fleet-pulse.service").read_text()
    assert "Environment=PLANE_EMIT_ENABLED=1" in pulse
    unarmed = _compose_with_defaults(tmp_path, monkeypatch, armed="0")
    assert "PLANE_EMIT_ENABLED" not in (
        unarmed / "com.test.keepalive.service").read_text()


def test_keepalive_unit_abandons_its_children(tmp_path, monkeypatch):
    """The keepalive sweep backgrounds each bot's plane emit; the supervisor's
    control-group kill at job exit dropped the LAST bot's heartbeat every
    tick (live: 1 sample in 7 days vs ~1300 per sibling; a manual tick landed
    at once). The composed unit lets children outlive the job on both
    platforms; a job without the knob is untouched."""
    timers = _compose_with_defaults(tmp_path, monkeypatch, armed="1")
    svc = (timers / "com.test.keepalive.service").read_text()
    plist = (timers / "com.test.keepalive.plist").read_text()
    assert "KillMode=process" in svc
    assert "<key>AbandonProcessGroup</key>" in plist and "<true/>" in plist
    other = next(p for p in timers.glob("com.test.*.service")
                 if "keepalive" not in p.name)
    assert "KillMode" not in other.read_text()
    other_plist = timers / other.name.replace(".service", ".plist")
    if other_plist.exists():
        assert "AbandonProcessGroup" not in other_plist.read_text()


def _compose_shadow(tmp_path: Path, monkeypatch, armed: str):
    """The shadow comparison (cutover chunk 3) rides its OWN carrier,
    PLANE_SHADOW_ENABLED, stamped on the plane-shadow unit only — the
    defaults-on fleet of the keepalive pin, with the cascade answering the
    shadow flag and NOT the emission flag."""
    fl = dedent(_FLEET).replace("system_defaults: false", "system_defaults: true")
    root = tmp_path / "fs"
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(fl)
    fleet, md = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=root)
    import claudlobby.env_tiers as env_tiers_mod
    res = Resolution(name="PLANE_SHADOW_ENABLED", value=armed, tier="fleet", path=None)
    monkeypatch.setattr(env_tiers_mod, "read_tiers",
                        lambda paths, fleet_name=None, bot_name=None: [])
    monkeypatch.setattr(env_tiers_mod, "cascade",
                        lambda tiers: {"PLANE_SHADOW_ENABLED": res})
    return compose_fleet_timers(fleet, paths, md)


def test_shadow_arming_stamps_only_the_shadow_unit(tmp_path, monkeypatch):
    timers = _compose_shadow(tmp_path, monkeypatch, armed="1")
    service = (timers / "com.test.plane-shadow.service").read_text()
    plist = (timers / "com.test.plane-shadow.plist").read_text()
    assert "Environment=PLANE_SHADOW_ENABLED=1" in service
    assert "<key>PLANE_SHADOW_ENABLED</key>" in plist
    assert "PLANE_SHADOW_ENABLED" not in (timers / "com.test.keepalive.service").read_text()
    assert "PLANE_EMIT_ENABLED" not in service        # emission on does not arm the record
    assert "com.test.plane-shadow" in (timers / "DORMANT").read_text()


def test_unarmed_shadow_composes_no_shadow_env(tmp_path, monkeypatch):
    timers = _compose_shadow(tmp_path, monkeypatch, armed="0")
    assert "PLANE_SHADOW_ENABLED" not in (timers / "com.test.plane-shadow.service").read_text()
