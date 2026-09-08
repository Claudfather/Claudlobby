"""The switch registry and the surfaces that render it (chunk N).

The defaults flip is only half a change. The other half — and the half these
tests are mostly about — is that whatever stays off has to be VISIBLE, with the
one line that flips it, at the three places an operator actually looks:
`claudlobby doctor`, `claudlobby plane doctor`, and the end of a setup run.

So the assertions here are deliberately about the SURFACE, not just the data:
a registry nobody renders is the same opacity in a tidier shape.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import switches as sw
from claudlobby.config import load_fleet
from claudlobby.paths import Paths

REPO = Path(__file__).resolve().parent.parent

_FLEET = """\
    fleet:
      name: sw-fleet
      service_prefix: com.sw
      bots:
        kev:
          expertise: [software-engineering]
"""


def _root(tmp_path: Path, env: str | None = None) -> Path:
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    # The REAL resolver, not a stub: the whole reason env_tiers shells out is
    # that a Python copy of the cascade drifts from the runtime (#1226), and a
    # test that stubs it certifies the copy instead of the contract.
    if not (root / "lib").exists():
        (root / "lib").symlink_to(REPO / "lib")
    (root / "fleet.yaml").write_text(dedent(_FLEET))
    if env is not None:
        (root / ".env").write_text(env)
    return root


def _resolve(tmp_path: Path, env: str | None = None):
    root = _root(tmp_path, env)
    fleet, _md = load_fleet(root / "fleet.yaml")
    return sw.resolve(Paths(root=root, fleet_dir=root), fleet)


def _state(rows, key):
    return next(r for r in rows if r.switch.key == key)


# ---------------------------------------------------------------------------
# the rule, as shipped
# ---------------------------------------------------------------------------


def test_exactly_the_four_categories_ship_off():
    """The rule has four reasons and no fifth. Every opt-in must NAME which one
    keeps it off — an unexplained off switch is how a default quietly becomes
    a habit, which is the state this chunk found the estate in."""
    opt_in = {s.key for s in sw.SWITCHES if s.polarity == sw.OPT_IN}
    assert opt_in == {"update-siblings", "session-digest", "code-audit-sweep",
                      "weekly-worker-restart"}
    for s in sw.SWITCHES:
        if s.polarity == sw.OPT_IN:
            assert s.why_opt_in, f"{s.key} ships off with no stated reason"
        else:
            assert not s.why_opt_in


def test_every_switch_carries_both_directions():
    """A knob you can only turn one way is not a knob. Every row states the arm
    AND the disarm line, because the reader who needs the table is equally
    likely to be turning something off."""
    for s in sw.SWITCHES:
        assert s.arm and s.disarm and s.what, s.key
        assert s.env or s.job or s.scope == sw.FLEET_JOB, s.key


def test_the_target_workflow_doors_are_the_reaction_chain():
    """Scoped narrowly on purpose: status's header names these and nothing
    else. A header that listed every off switch would be scrolled past, which
    is the same failure as printing nothing.

    `pane-send-chunking` joined the set in the chunk-O fold and earns it the
    same way the other three do: with chunking off, a dispatch over 1 KB
    arrives TAIL ONLY, so the envelope and the task id are gone, the worker
    cannot report against an id it never received, and the row never closes.
    The loop stops closing rather than merely slowing down."""
    assert {s.key for s in sw.SWITCHES if s.target_workflow} == {
        "task-recheck", "plane-expire", "plane-recording",
        "pane-send-chunking"}


def test_a_fresh_fleet_has_everything_but_the_opt_ins_on(tmp_path):
    rows = _resolve(tmp_path)
    on = {r.switch.key for r in rows if r.on}
    off = {r.switch.key for r in rows if not r.on}
    assert off == {s.key for s in sw.SWITCHES if s.polarity == sw.OPT_IN}
    assert {"task-recheck", "plane-expire", "plane-prune", "plane-daemon",
            "plane-view", "plane-host-probe", "registry-scan",
            "spindown-receipt", "plane-recording",
            "pane-send-chunking"} <= on


def test_the_shipped_tier_agrees_with_the_registry():
    """The registry and system.yaml are two spellings of one decision, and the
    estate's recurring defect is two spellings drifting (#892/#1143). Pinned
    together so a future `enroll: false` in the yaml with no registry row — or
    the reverse — fails here rather than on a host."""
    from claudlobby.config import load_host_jobs

    from claudlobby.config import _load_system_defaults

    host = load_host_jobs()
    fleet_jobs = (_load_system_defaults().get("defaults") or {}).get("jobs") or {}
    checked = 0
    for s in sw.SWITCHES:
        if not s.job:
            continue
        # F9: the FLEET tier is checked too. Skipping it left the shipped
        # `defaults.jobs` free to carry an `enroll: false` on task-recheck —
        # the reaction the whole loop is for — with the registry still
        # printing it as on, which is the drift this pin exists to catch.
        if s.scope in (sw.HOST_JOB, sw.HOST_SERVICE):
            cfg, where = host[s.job], "system.yaml host.jobs"
        elif s.scope == sw.FLEET_JOB:
            cfg, where = fleet_jobs[s.job], "system.yaml defaults.jobs"
        else:
            continue
        if s.scope == sw.HOST_SERVICE:
            enrolled = cfg.get("enroll") is True
        else:
            enrolled = cfg.get("enroll", True) is not False
        checked += 1
        assert enrolled is s.default_on, (
            f"{s.job}: {where} says enrolled={enrolled} while the registry"
            f" says default_on={s.default_on}")
    assert checked >= 7, "the sweep stopped covering the rows it used to"


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_a_fleet_env_that_turns_the_recheck_off_is_visible(tmp_path):
    """The pin the ruling asks for by name: a fleet with a `.env` turning
    task-recheck off SHOWS it, and names the tier that did."""
    rows = _resolve(tmp_path, "TASK_RECHECK_ENABLED=0\n")
    st = _state(rows, "task-recheck")
    assert st.on is False
    assert st.label == "off"
    assert "root .env" in st.source or ".env" in st.source
    assert sw.target_workflow_off(rows) == [st]
    assert "task-recheck" in sw.summary_line(rows)


def test_an_empty_assignment_is_not_an_off_switch(tmp_path):
    """`export FLAG=` wins at its tier (#1213) and is not a "0". The bash gate
    reads it the same way (`${FLAG:-1}` is empty, which is not 0), so the table
    and the door agree — which is the only thing that makes the table worth
    reading."""
    rows = _resolve(tmp_path, "TASK_RECHECK_ENABLED=\n")
    assert _state(rows, "task-recheck").on is True


def test_the_silencer_reads_inverted(tmp_path):
    rows = _resolve(tmp_path, "PLANE_EMIT_DISABLED=1\n")
    assert _state(rows, "plane-recording").on is False
    assert _state(rows, "plane-recording") in sw.target_workflow_off(rows)


def test_an_opt_in_nobody_armed_reads_as_the_DEFAULT_not_as_config(tmp_path):
    """Attribution answers "who decided this". An opt-in sitting off because
    nobody armed it was never turned off by anything — saying "fleet.yaml" there
    sends a reader to edit a file that has nothing to say."""
    rows = _resolve(tmp_path)
    assert _state(rows, "code-audit-sweep").source == "default"
    assert _state(rows, "weekly-worker-restart").source == "default"


def test_an_armed_opt_in_names_what_armed_it(tmp_path):
    rows = _resolve(tmp_path, "SESSION_DIGEST_ENABLED=1\n")
    st = _state(rows, "session-digest")
    assert st.on is True and ".env" in st.source


def test_an_unreachable_resolver_says_so_rather_than_guessing(tmp_path,
                                                             monkeypatch):
    """Unreachable is not empty (source_state's rule). A table that quietly
    printed the shipped defaults as if it had measured them would be a worse
    lie than the dormancy it replaced."""
    import claudlobby.env_tiers as et

    def boom(*a, **k):
        raise et.ResolverUnavailable("no resolver")

    monkeypatch.setattr(et, "read_tiers", boom)
    rows = _resolve(tmp_path)
    st = _state(rows, "task-recheck")
    assert st.unknown and st.label == "unknown"
    assert "resolver unreachable" in st.detail
    assert "resolver unreachable" in sw.summary_line(rows)
    # ...and an UNKNOWN state never reaches status's header: naming a door as
    # off when we could not read it is the fabrication this rule exists to stop.
    assert st not in sw.target_workflow_off(rows)


# ---------------------------------------------------------------------------
# the surfaces
# ---------------------------------------------------------------------------


def test_the_table_leads_with_the_opt_ins(tmp_path):
    """Ordering is the surface. Three off switches buried under nine on ones
    have been named without being shown."""
    rows = _resolve(tmp_path)
    text = sw.format_table(rows)
    assert text.index("opt-in — ships OFF") < text.index("on by default")
    for s in sw.SWITCHES:
        if s.polarity == sw.OPT_IN:
            assert s.key in text and s.arm in text
            assert s.why_opt_in in text


def _cli(root: Path, *argv):
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root), *argv],
        capture_output=True, text=True, timeout=180, cwd=str(REPO))


def test_doctor_switches_prints_the_table_alone(tmp_path):
    r = _cli(_root(tmp_path, "TASK_RECHECK_ENABLED=0\n"), "doctor", "--switches")
    assert r.returncode == 0, r.stderr
    assert "=== switches ===" in r.stdout
    assert "task-recheck" in r.stdout and "off" in r.stdout
    # A row shows the line the reader needs FROM WHERE IT IS: an off door
    # prints how to get it back, an on door prints how to stop it. Printing
    # both would double the table and halve the chance either is read.
    assert "arm: unset TASK_RECHECK_ENABLED" in r.stdout
    assert "turn off: PLANE_EXPIRE_ENABLED=0" in r.stdout
    assert "update-siblings" in r.stdout          # the opt-ins are always listed
    assert "session-digest" in r.stdout
    assert "code-audit-sweep" in r.stdout
    # ...and ONLY the table: the shell setup doors call this, and a health
    # command's service probes / credential curls have no business in a
    # setup summary.
    assert "npx" not in r.stdout and "=== claudlobby doctor ===" not in r.stdout


def test_the_doctor_rung_exists_and_never_fails(tmp_path):
    """Reported, never flagged. A fleet that turned the re-check off did so on
    purpose; warning about it would train operators to ignore the rung, and a
    surface people ignore is not a surface."""
    from claudlobby.doctor import DoctorReport, check_switches

    root = _root(tmp_path, "TASK_RECHECK_ENABLED=0\n")
    fleet, _md = load_fleet(root / "fleet.yaml")
    report = DoctorReport()
    check_switches(fleet, Paths(root=root, fleet_dir=root), report)
    rung = next(c for c in report.checks if c.name == "switches")
    assert rung.status == "pass"
    assert "task-recheck" in rung.detail
    assert report.switch_rows


def test_status_header_names_a_disabled_reaction(tmp_path):
    from claudlobby.status import switches_off_note

    rows = _resolve(tmp_path, "TASK_RECHECK_ENABLED=0\n")
    note = switches_off_note("artemis-data", rows)
    assert "task-recheck off on artemis-data" in note
    assert "doctor --switches" in note
    assert switches_off_note("artemis-data", _resolve(tmp_path / "b")) == ""


@pytest.mark.parametrize("door", ["lib/setup-fleet", "lib/setup-system"])
def test_both_setup_doors_end_by_printing_the_table(door):
    """One renderer, called by both — never a bash copy. A second table in
    shell is how the printed truth and the actual truth drift."""
    body = (REPO / door).read_text()
    assert "doctor --switches" in body


# ---------------------------------------------------------------------------
# the composed side
# ---------------------------------------------------------------------------


def test_host_timer_dormancy_is_COMPOSE_TIME_now(tmp_path):
    """REWRITTEN by the fold (F7): the contract this pinned has changed.

    The chunk shipped `enroll: false` on a host timer as composed-but-dormant
    plus a DORMANT manifest setup-system skipped. The fold deletes the
    manifest and does not compose the unit at all, for the reason the SERVICE
    branch already did it that way: setup-system enrolls every composed
    claudlobby-* unit it finds, so "compose it and remember not to enroll it"
    is a second mechanism that can only disagree with the first. No manifest
    for what is not composed.

    #1385 is still what is being closed — update-siblings MUTATES OPERATOR
    SOURCE — just one rung lower."""
    from claudlobby.composer import compose_host_timers

    root = tmp_path / "h"
    root.mkdir(parents=True)
    out = compose_host_timers(Paths(root=root))
    names = {p.name for p in out.iterdir()}
    assert not [n for n in names if n.startswith("claudlobby-update-siblings")]
    assert "DORMANT" not in names, "a manifest for units nobody composed"
    # ...and the armed jobs beside it are unaffected.
    assert "claudlobby-notify-behind.timer" in names


def test_an_armed_to_unarmed_host_timer_is_PRUNED(tmp_path):
    """The leftover half. A composed unit from an earlier generate must not
    survive the flip, or the next setup run enrols a job this host has
    declared dormant — which is exactly how #1385 stayed invisible."""
    from claudlobby.composer import compose_host_timers

    root = tmp_path / "h"
    root.mkdir(parents=True)
    out = compose_host_timers(Paths(root=root))
    stale = out / "claudlobby-update-siblings.timer"
    stale.write_text("[Unit]\n")
    (out / "DORMANT").write_text("claudlobby-update-siblings\n")
    compose_host_timers(Paths(root=root))
    assert not stale.exists()
    assert not (out / "DORMANT").exists()


def test_setup_system_WALKS_BACK_a_unit_nothing_composes(tmp_path, monkeypatch):
    """F4: dormancy that only applies forward is not dormancy.

    A host that ran the installer before this chunk has
    claudlobby-update-siblings ENABLED and firing weekly, while every surface
    added by the chunk calls it off. Compose-time dormancy cannot reach it —
    the unit is already installed — so setup-system disables and removes
    every installed claudlobby-* unit the current compose does not emit."""
    home = tmp_path / "home"
    installed = home / "Library" / "LaunchAgents"
    installed.mkdir(parents=True)
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    composed = tmp_path / "timers"
    composed.mkdir()
    # one job this host still composes, one it no longer does
    (composed / "claudlobby-plane-prune.timer").write_text("x")
    (composed / "claudlobby-plane-prune.service").write_text("x")
    # each supervisor's own shape in its own dir — LaunchAgents holds plists,
    # ~/.config/systemd/user holds .timer/.service
    for job in ("plane-prune", "update-siblings"):
        (installed / f"claudlobby-{job}.plist").write_text("x")
        for ext in ("timer", "service"):
            (home / ".config" / "systemd" / "user"
             / f"claudlobby-{job}.{ext}").write_text("x")
    out = _walk_back(tmp_path, home, composed)
    assert "update-siblings" in out.stdout, out.stderr
    # The helper is platform-scoped by design — it disables through the
    # supervisor this host actually runs — so assert on THIS platform's dir.
    import platform
    d = (home / ".config" / "systemd" / "user"
         if platform.system() == "Linux" else installed)
    stale = "claudlobby-update-siblings"
    assert not list(d.glob(f"{stale}.*")), sorted(x.name for x in d.iterdir())
    assert list(d.glob("claudlobby-plane-prune.*"))


def test_the_walk_back_REFUSES_on_an_empty_compose(tmp_path):
    """source_state's rule, on a destructive door. A generate that emitted
    nothing is an unreachable instrument, not a host where every job is
    dormant — and reading it the second way tears down a healthy host."""
    home = tmp_path / "home"
    for sub in ("Library/LaunchAgents", ".config/systemd/user"):
        (home / sub).mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "claudlobby-plane-prune.plist").write_text("x")
    (home / ".config" / "systemd" / "user" / "claudlobby-plane-prune.timer").write_text("x")
    composed = tmp_path / "timers"
    composed.mkdir()
    out = _walk_back(tmp_path, home, composed)
    assert "refusing" in out.stderr
    assert (home / "Library" / "LaunchAgents" / "claudlobby-plane-prune.plist").exists()
    assert (home / ".config" / "systemd" / "user" / "claudlobby-plane-prune.timer").exists()


def _walk_back(tmp_path: Path, home: Path, composed: Path):
    """Drive the real bash helper with launchctl/systemctl stubbed out, so the
    DECISION is under test and no host state is touched."""
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    for tool in ("launchctl", "systemctl"):
        f = binp / tool
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    script = tmp_path / "drive.sh"
    script.write_text(
        f'. "{REPO}/lib/lib-common.sh"\n'
        'set +e\n'
        f'walk_back_uncomposed_host_units "{composed}"\n'
    )
    import os
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{binp}:{os.environ.get('PATH', '')}"}
    return subprocess.run(["/bin/bash", str(script)], capture_output=True,
                          text=True, env=env, timeout=60)


def test_the_plane_services_compose_by_default(tmp_path):
    from claudlobby.composer import compose_host_timers

    root = tmp_path / "h"
    root.mkdir(parents=True)
    out = compose_host_timers(Paths(root=root))
    names = {p.name for p in out.iterdir()}
    for job in ("plane-daemon", "plane-view"):
        assert f"claudlobby-{job}.service" in names
        assert f"claudlobby-{job}.plist" in names
        assert f"claudlobby-{job}.timer" not in names   # resident, not timed
    for job in ("plane-expire", "plane-prune", "plane-host-probe"):
        assert f"claudlobby-{job}.timer" in names


def test_the_view_is_NOT_composed_without_its_extra(tmp_path, monkeypatch):
    """F1, the fold's blocking finding. `plane-view.enroll: true` composes a
    Restart=always / KeepAlive unit, and `claudlobby plane view` exits 1
    without fastapi+uvicorn — which live in an optional extra the documented
    install did not include. Every host that followed the README would have
    enrolled a service that crash-loops every 5s, forever.

    "The unit exits saying so" is an honest failure for a hand run and a crash
    loop under supervision, and enrolling by default is what turns the first
    into the second. So the extra is a COMPOSE-time gate: no extra, no unit."""
    from claudlobby import switches as _sw
    from claudlobby.composer import compose_host_timers

    monkeypatch.setattr(_sw, "extra_available", lambda extra: False)
    root = tmp_path / "h"
    root.mkdir(parents=True)
    out = compose_host_timers(Paths(root=root))
    names = {p.name for p in out.iterdir()}
    assert "claudlobby-plane-view.service" not in names
    assert "claudlobby-plane-view.plist" not in names
    # ...the daemon, which needs no extra, is untouched.
    assert "claudlobby-plane-daemon.service" in names
    # ...and a unit composed before the extra was removed is pruned, or the
    # next setup run enrols the crash loop from stale files.
    (out / "claudlobby-plane-view.service").write_text("[Unit]\n")
    compose_host_timers(Paths(root=root))
    assert not (out / "claudlobby-plane-view.service").exists()


def test_extra_available_reads_the_REAL_interpreter_both_ways():
    """The gate pins above monkeypatch the decision; this pins the DETECTOR,
    against a module that genuinely is not importable and one that genuinely
    is. A gate fed by a detector nobody exercised is the shape where the
    compositor would keep composing a unit that cannot start."""
    import pytest as _pytest

    assert sw.extra_available("plane-ui") is True   # the dev venv has it
    assert sw.extra_available("no-such-extra") is True   # nothing declared
    _pytest.MonkeyPatch().context()
    mp = _pytest.MonkeyPatch()
    try:
        mp.setitem(sw._EXTRA_MODULES, "plane-ui",
                   ("claudlobby_definitely_not_installed_xyz",))
        assert sw.extra_available("plane-ui") is False
        assert sw.missing_extra("plane-view") == "plane-ui"
        assert sw.missing_extra("plane-daemon") == ""
    finally:
        mp.undo()


def test_the_table_arms_the_view_with_PIP_when_the_extra_is_missing(
        tmp_path, monkeypatch):
    """The other half of F1: the switch table stops saying "on". A row that
    claims a service is running when no unit exists sends a reader to debug
    supervision instead of installing two wheels."""
    from claudlobby import switches as _sw

    monkeypatch.setattr(_sw, "extra_available", lambda extra: False)
    rows = _resolve(tmp_path)
    st = _state(rows, "plane-view")
    assert st.on is False and st.unknown is False
    assert "plane-ui" in st.source
    assert "pip install -e '.[plane-ui]'" in st.arm
    assert "pip install -e '.[plane-ui]'" in _sw.format_table(rows)


def test_the_composer_arming_tables_are_derived_not_listed():
    """The registry is the one place a knob's spelling lives. Four copies of
    "what knobs exist" is how the estate ended up with a `.env` full of flags
    nothing read."""
    from claudlobby.composer import FLEET_JOB_ARMING, HOST_JOB_ARMING

    assert FLEET_JOB_ARMING == sw.jobs_with_env(sw.FLEET_JOB)
    assert HOST_JOB_ARMING == sw.jobs_with_env(sw.HOST_JOB, sw.HOST_SERVICE)
    assert set(HOST_JOB_ARMING) == {"plane-expire", "plane-prune"}


def test_the_validator_namespaces_come_from_the_registry():
    """Derived, so a door deleted tomorrow warns without anyone touching the
    validator — and a fleet's own MYTOOL_ENABLED never does."""
    assert sw.namespaces() == {"TASK", "PLANE", "SESSION", "SPINDOWN", "PANE"}
    assert "PLANE_SHADOW_ENABLED" not in sw.env_names()
    assert "PLANE_SHADOW_ENABLED" in sw.RETIRED


def test_doctor_switches_works_with_NO_fleet_at_all(tmp_path):
    """lib/setup-system runs once per HOST, and a host with overlay fleets has
    no root `fleet.yaml` — so requiring one here would have made the host door
    print nothing, which is the exact silence this chunk exists to remove. The
    host rows are still true without a fleet; the fleet-scoped ones fall back
    to their shipped defaults."""
    root = tmp_path / "hostonly"
    root.mkdir()
    (root / "lib").symlink_to(REPO / "lib")
    r = _cli(root, "doctor", "--switches")
    assert r.returncode == 0, r.stderr
    assert "plane-daemon" in r.stdout and "update-siblings" in r.stdout


# ---------------------------------------------------------------------------
# the fold: a switch names its REAL carrier (F2)
# ---------------------------------------------------------------------------


def test_every_switch_declares_a_carrier_and_derives_its_two_lines():
    """The first build hand-wrote arm/disarm, and four rows named a carrier
    that cannot reach the door they gate — which is worse than no table: you
    write the flag, watch nothing happen, and conclude the door is broken. The
    carrier is declared; the lines are derived from it."""
    for s in sw.SWITCHES:
        assert s.carrier, s.key
        assert s.arm == sw._carrier_lines(s)[0]
        assert s.disarm == sw._carrier_lines(s)[1]
        if s.carrier in (sw.ENV_FLEET, sw.ENV_HOST, sw.BOT_CONF):
            assert s.env, f"{s.key}: an env carrier with no variable"
            assert s.env in s.arm and s.env in s.disarm


def test_the_four_corrected_carriers():
    """Each was measured, not argued:

    * `session-digest` / `spindown-receipt` run INSIDE a bot session, and
      start-bot.sh sources the .env tiers before `set -a` — so a tier line is
      assigned unexported and never reaches them. bot.conf is the carrier.
    * `plane-host-probe` is a host timer with no flag of its own; naming the
      estate silencer as its arm pointed at a variable no unit carries.
    * `plane-recording` keeps the .env tier — and the composer now carries the
      resolved value to BOTH places that cannot read a tier themselves.
    """
    assert sw.by_key("session-digest").carrier == sw.BOT_CONF
    assert sw.by_key("spindown-receipt").carrier == sw.BOT_CONF
    assert sw.by_key("plane-host-probe").carrier == sw.ENROLL_HOST
    assert "PLANE_EMIT_DISABLED" not in sw.by_key("plane-host-probe").arm
    assert sw.by_key("plane-recording").carrier == sw.ENV_FLEET
    for key in ("session-digest", "spindown-receipt"):
        # whichever direction is the non-default one names the carrier: an
        # opt-in says it in `arm`, an opt-out in `disarm`.
        sd = sw.by_key(key)
        assert "bots.NAME.env:" in (sd.arm if sd.polarity == sw.OPT_IN
                                    else sd.disarm)


def test_the_silencer_reaches_the_bot_conf_AND_the_timer_units(tmp_path):
    """F2's live half. A fleet that silences the plane silenced `generate` and
    nothing else: a session sees no unexported tier assignment and a timer
    sources no .env at all, so every door the fleet actually runs kept
    recording. An off switch that reaches one of its doors is not an off
    switch."""
    from claudlobby.composer import compose_bot_conf, compose_fleet_timers
    from claudlobby.config import load_fleet

    root = _root(tmp_path, "PLANE_EMIT_DISABLED=1\n")
    fleet, merged = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=root)
    conf = compose_bot_conf(fleet.bots["kev"], fleet, paths)
    assert [ln for ln in conf.splitlines()
            if ln.startswith("export PLANE_EMIT_DISABLED=")
            and ln.rstrip().endswith("1")], conf
    timers = compose_fleet_timers(fleet, paths, merged)
    unit = (timers / "com.sw.task-recheck.service").read_text()
    assert "Environment=PLANE_EMIT_DISABLED=1" in unit
    # ...and a fleet that says nothing stamps nothing: the door's own default
    # stays the ONE place the answer lives.
    quiet = _root(tmp_path / "q")
    qfleet, qmerged = load_fleet(quiet / "fleet.yaml")
    qpaths = Paths(root=quiet, fleet_dir=quiet)
    assert "PLANE_EMIT_DISABLED" not in compose_bot_conf(
        qfleet.bots["kev"], qfleet, qpaths)


# ---------------------------------------------------------------------------
# the chunk-O fold: a knob that restores a data-losing send is NAMED (F8)
# ---------------------------------------------------------------------------


def test_pane_send_chunking_is_a_registered_switch(tmp_path):
    """`PANE_SEND_CHUNK_BYTES=0` restores the pre-#1493 single send — the one
    that lost the head of 94 of 180 large dispatches in a week — and it reaches
    a live bot through `fleet.yaml env:` -> bot.conf like any other session
    knob. "Nobody would set that" is a hope, not a mechanism; the registry is
    the mechanism.

    It is an opt-OUT whose value is a BYTE CAP rather than a boolean, which the
    polarity already handles correctly: only an exact `0` is off, so `900`,
    `450` and an empty assignment all leave chunking on."""
    s = sw.by_key("pane-send-chunking")
    assert s.polarity == sw.OPT_OUT and s.scope == sw.DOOR
    assert s.env == "PANE_SEND_CHUNK_BYTES"
    # bot.conf, for session-digest's reason: the door runs inside a bot session
    # and start-bot.sh sources the .env tiers before `set -a`.
    assert s.carrier == sw.BOT_CONF
    assert "bots.NAME.env:" in s.disarm
    # ...and the `what` says BOTH carriers, because a host-side sender (a hand
    # run, a timer's dispatch) reads the host or root .env instead.
    assert "byte cap" in s.what and "host or root .env" in s.what

    rows = _resolve(tmp_path)
    assert _state(rows, "pane-send-chunking").on is True
    # a byte cap that is not 0 is still ON — the polarity must not read "set"
    # as "armed"
    assert _state(_resolve(tmp_path / "cap", "PANE_SEND_CHUNK_BYTES=450\n"),
                  "pane-send-chunking").on is True
    off = _state(_resolve(tmp_path / "off", "PANE_SEND_CHUNK_BYTES=0\n"),
                 "pane-send-chunking")
    assert off.on is False and off.label == "off"


def test_chunking_off_reaches_status_header_and_the_table(tmp_path):
    """The other half of F8: named where the operator looks. A dispatch that
    arrives TAIL ONLY has lost its envelope and its task id, so the row can
    never be reported against and never closes — which is a reaction that does
    not happen, not a slower one."""
    from claudlobby.status import switches_off_note

    rows = _resolve(tmp_path, "PANE_SEND_CHUNK_BYTES=0\n")
    st = _state(rows, "pane-send-chunking")
    assert st in sw.target_workflow_off(rows)
    assert "pane-send-chunking off on demo" in switches_off_note("demo", rows)
    assert "pane-send-chunking" in sw.summary_line(rows)
    assert "pane-send-chunking" in sw.format_table(rows)


def test_the_lib_door_and_the_registry_spell_the_knob_the_same_way():
    """The shell reads the variable and the table describes it; a rename on one
    side and not the other is how the estate ended up with flags nothing read.
    Pinned against the door's own source, not a second list."""
    body = (REPO / "lib" / "lib-common.sh").read_text()
    assert "PANE_SEND_CHUNK_BYTES" in body
    # the loud line the fold added, so an off switch is visible in the logs of
    # the host it is off on
    assert "chunking OFF (PANE_SEND_CHUNK_BYTES=0)" in body
    assert "pane-send-chunking" in body   # the door points at its registry row


# ---------------------------------------------------------------------------
# the fold: the host table admits what it did not read (F5)
# ---------------------------------------------------------------------------


def test_a_host_run_says_UNKNOWN_for_a_fleet_it_never_read(tmp_path):
    """`resolve(paths, fleet=None)` reported fleet-scoped switches as their
    shipped default with `set by: shipped default` — an assertion about a
    scope nobody opened, which is the unreachable-is-not-empty defect wearing
    a table. The host rows are still true; the fleet rows say so."""
    root = _root(tmp_path)
    rows = sw.resolve(Paths(root=root, fleet_dir=root), None)
    st = _state(rows, "task-recheck")
    assert st.unknown and st.unknown_reason == "no-fleet"
    assert st.source == "?" and "no fleet named" in st.detail
    # the host-scoped rows are NOT degraded — they were read
    assert _state(rows, "plane-prune").unknown is False
    assert _state(rows, "plane-daemon").unknown is False
    # ...an unknown never reaches status's header (naming a door off when we
    # could not read it is the fabrication the rule exists to stop) and never
    # counts as "turned off here".
    assert sw.target_workflow_off(rows) == []
    assert "turned off here" not in sw.summary_line(rows)
    assert "no fleet named" in sw.summary_line(rows)
    assert "set by: ?" in sw.format_table(rows)


def test_an_unknown_row_prints_BOTH_directions(tmp_path):
    """We do not know which way it is set, so printing one line would be
    picking a side."""
    root = _root(tmp_path)
    text = sw.format_table(sw.resolve(Paths(root=root, fleet_dir=root), None))
    block = text[text.index("task-recheck"):]
    assert "arm: unset TASK_RECHECK_ENABLED" in block
    assert "turn off: TASK_RECHECK_ENABLED=0" in block


def test_plane_doctor_and_doctor_switches_agree_on_one_fleet(tmp_path):
    """The plane subset discarded the --fleet it was given, so the two doors
    answered differently about the same fleet — one read the fleet tier, the
    other reported the shipped defaults."""
    root = _root(tmp_path, "PLANE_EMIT_ENABLED=0\n")
    doc = _cli(root, "doctor", "--switches")
    pln = _cli(root, "plane", "doctor")
    import re

    assert doc.returncode == 0, doc.stderr
    row = re.compile(r"^\s+registry-scan\s+(\S+)", re.M)
    for out in (doc.stdout, pln.stdout):
        states = row.findall(out)
        assert states, out
        assert set(states) == {"off"}, out


# ---------------------------------------------------------------------------
# the fold: one gate helper (F6)
# ---------------------------------------------------------------------------


def _gate(tmp_path: Path, value: str | None):
    import os

    script = tmp_path / "g.sh"
    script.write_text(
        f'. "{REPO}/lib/lib-common.sh"\n'
        'set +e\n'
        'switch_is_on DEMO_ENABLED demo-door "nothing will happen" || echo OFF\n'
    )
    env = {k: v for k, v in os.environ.items() if k != "DEMO_ENABLED"}
    if value is not None:
        env["DEMO_ENABLED"] = value
    return subprocess.run(["/bin/bash", str(script)], capture_output=True,
                          text=True, env=env, timeout=60)


@pytest.mark.parametrize("value,off", [
    (None, False), ("", False), ("1", False), ("true", False),
    ("00", False), ("0", True),
])
def test_switch_is_on_owns_polarity(tmp_path, value, off):
    """Four launchers hand-rolled `[ "${X:-1}" = "0" ]` and its loud line. One
    definition, because a copy is how a flag comes to mean something different
    in bash from what the table an operator reads says it means. Empty is a
    win at its tier (#1213) and is NOT a 0 — same rule as resolves_to."""
    r = _gate(tmp_path, value)
    assert ("OFF" in r.stdout) is off
    assert ("demo-door: OFF here" in r.stderr) is off
    if off:
        assert "nothing will happen" in r.stderr


@pytest.mark.parametrize("script,var", [
    ("lib/task-recheck.sh", "TASK_RECHECK_ENABLED"),
    ("lib/plane-expire.sh", "PLANE_EXPIRE_ENABLED"),
    ("lib/plane-prune.sh", "PLANE_PRUNE_ENABLED"),
    ("lib/spin-down-bot.sh", "SPINDOWN_RECEIPT_ENABLED"),
])
def test_the_four_launchers_call_the_shared_gate(script, var):
    body = (REPO / script).read_text()
    assert f"switch_is_on {var}" in body
    assert f'"${{{var}:-1}}" = "0"' not in body


# ---------------------------------------------------------------------------
# the fold: the docs are a rendered copy, not a hand-kept one (F8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", sorted(sw.DOC_BLOCKS))
def test_the_doc_switch_tables_ARE_the_registrys_render(doc):
    """Three hand-written tables were a fourth copy of the registry, and the
    estate's recurring defect is a copy drifting (#892/#1143) — two of them
    were already wrong about `session-digest`'s carrier. The block is
    generated; regenerate with `claudlobby doctor --switches --markdown`."""
    text = (REPO / doc).read_text()
    assert sw.DOC_BEGIN in text, f"{doc}: no generated block"
    body = text[text.index(sw.DOC_BEGIN):
                text.index(sw.DOC_END) + len(sw.DOC_END)]
    assert body == sw.format_markdown(**sw.DOC_BLOCKS[doc]), (
        f"{doc} is stale — regenerate:"
        " claudlobby doctor --switches --markdown")


def test_the_markdown_render_is_state_free(tmp_path):
    """A doc describes what SHIPS. Rendering this host's state into it would
    make every operator's checkout differ from the repo."""
    a = sw.format_markdown()
    (tmp_path / ".env").write_text("TASK_RECHECK_ENABLED=0\n")
    assert sw.format_markdown() == a
    assert "unknown" not in a and "shipped default" not in a


def test_doctor_switches_markdown_prints_every_block(tmp_path):
    r = _cli(_root(tmp_path), "doctor", "--switches", "--markdown")
    assert r.returncode == 0, r.stderr
    for doc in sw.DOC_BLOCKS:
        assert doc in r.stdout
    assert r.stdout.count(sw.DOC_BEGIN) == len(sw.DOC_BLOCKS)
