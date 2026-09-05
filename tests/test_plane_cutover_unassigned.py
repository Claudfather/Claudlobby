"""Cutover chunk 7a → F18 closure R2a: the idle-worker check (`--unassigned`)
answers from the plane — the bot's newest report of any status, terminal or
not, against its newest dispatch — so the watchdog's idle list needs no
ledger.

Deleted with the shadow and the legacy side (F18 closure, R2a):
test_unassigned_answers_the_same_from_both_sources (→
test_unassigned_lists_the_idle_worker_from_the_plane),
test_a_dispatch_after_the_report_clears_it_on_both_sides (→ ..._clears_it),
test_the_flag_and_declaration_flip_the_idle_check_and_the_shadow_grades_it
(its unreachable half → test_the_idle_check_refuses_an_unreachable_plane),
test_a_plane_only_completion_makes_the_flipped_check_answer_from_the_plane
(asymmetric by design; its plane half is the first test),
test_a_terminal_note_that_resolved_nothing_is_idle_on_both_sides (→ ..._is_idle).
"""

from __future__ import annotations

from claudlobby.plane import cutover as cut
from tests.plane_fixtures import F, NOW_EPOCH, REPO, _cli, _declare, _epoch, _matcher, _report, _scene
from tests.test_plane_cutover_parity import _live_dispatch

WI2, ASG2 = f"wi_{'2':0>32}", f"asg_{'2':0>32}"
WI3, ASG3 = f"wi_{'3':0>32}", f"asg_{'3':0>32}"


def _idle(root):
    r = _matcher(root, "--unassigned", str(NOW_EPOCH), "--fleet", F)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_unassigned_lists_the_idle_worker_from_the_plane(tmp_path):
    """w1 finishes t-2 and is never re-tasked: idle; w2 holds an open task and
    reported nothing: not listed."""
    root, paths, _, _ = _scene(tmp_path)
    assert _idle(root) == ""                                        # nobody has reported since their dispatch
    done = "2026-09-02T13:00:00Z"
    _report(root, WI2, ASG2, done)
    out = _idle(root)
    assert out.startswith("w1 ") and "w2" not in out
    _, rts, idle, tid, status = out.split()
    assert (tid, status) == ("t-2-bbbb", "completed")
    assert int(rts) == _epoch(done) and int(idle) == NOW_EPOCH - int(rts)


def test_a_dispatch_after_the_report_clears_it(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    _report(root, WI2, ASG2, "2026-09-02T13:00:00Z")
    assert _idle(root).startswith("w1 ")
    _live_dispatch(root, "4", "t-4-dddd", ts="2026-09-02T14:00:00Z",    # re-tasked after reporting
                   expected_by="2026-09-03T00:00:00+00:00")
    assert _idle(root) == ""


def test_the_idle_check_refuses_an_unreachable_plane(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    _report(root, WI2, ASG2, "2026-09-02T13:00:00Z")
    nofleet = _matcher(root, "--unassigned", str(NOW_EPOCH))
    assert nofleet.returncode == 3 and nofleet.stdout == "" and "UNREACHABLE" in nofleet.stderr
    (root / "state" / "plane" / "plane.db").unlink()
    gone = _matcher(root, "--unassigned", str(NOW_EPOCH), "--fleet", F)
    assert gone.returncode == 3 and gone.stdout == "" and "UNREACHABLE" in gone.stderr


def test_the_fleet_pulse_unit_carries_the_unassigned_flag_and_names_its_fleet(tmp_path, monkeypatch):
    from claudlobby.composer import FLEET_JOB_ARMING
    from tests.test_plane_cutover_flip import _composed
    assert FLEET_JOB_ARMING["fleet-pulse"] == ("PLANE_READ_OVERDUE", "PLANE_READ_UNASSIGNED", "PLANE_READ_EVENTS")
    timers, _ = _composed(tmp_path, monkeypatch, {"PLANE_READ_UNASSIGNED": "1"})
    pulse = next(p for p in timers.iterdir() if "fleet-pulse" in p.name and p.suffix == ".service").read_text()
    assert "Environment=PLANE_READ_UNASSIGNED=1" in pulse
    src = (REPO / "lib" / "fleet-pulse.sh").read_text()
    line = next(l for l in src.splitlines() if 'dispatch-overdue.py" --unassigned' in l)
    assert '--fleet "$fleet"' in line
    assert "|| _unassigned_rc=$?" in src and "cannot be judged this pass" in src   # a refusal is disclosed, never an empty answer


def test_every_fleet_job_unit_carries_the_emission_flag_when_the_tier_arms_it(tmp_path, monkeypatch):
    """A timer unit sources no .env, and any script that sources lib-common can
    land a fleet event (the ERR trap alone) — measured on the live estate: the
    fleet-pulse unit composed with the read flags but not the emission flag,
    so a whole sweep's fleet events went unrecorded."""
    from tests.test_plane_cutover_flip import _composed
    armed, unarmed = tmp_path / "armed", tmp_path / "unarmed"
    armed.mkdir(); unarmed.mkdir()
    timers, _ = _composed(armed, monkeypatch, {"PLANE_EMIT_ENABLED": "1"})
    services = [p for p in timers.iterdir() if p.suffix == ".service"]
    assert services
    for svc in services:
        assert "Environment=PLANE_EMIT_ENABLED=1" in svc.read_text(), svc.name
    timers, _ = _composed(unarmed, monkeypatch, {"PLANE_EMIT_ENABLED": "0"})
    for svc in (p for p in timers.iterdir() if p.suffix == ".service"):
        assert "PLANE_EMIT_ENABLED" not in svc.read_text(), svc.name        # unarmed composes unarmed


def test_every_fleet_job_unit_carries_the_retirement_flags_when_the_tier_says_0(tmp_path, monkeypatch):
    """Phase C, found live: the sessions retired their events write but the
    fleet-pulse TIMER kept dual-writing — its unit was stamped with the read
    and emit flags, never the write flags. Every fleet job unit now carries
    each PLANE_LEGACY_WRITE_* that resolves to 0; at 1 nothing is stamped."""
    from tests.test_plane_cutover_flip import _composed
    on, off = tmp_path / "on", tmp_path / "off"
    on.mkdir(); off.mkdir()
    timers, _ = _composed(on, monkeypatch, {"PLANE_EMIT_ENABLED": "1", "PLANE_LEGACY_WRITE_EVENTS": "0",
                                            "PLANE_LEGACY_WRITE_DISPATCH": "0"})
    services = [p for p in timers.iterdir() if p.suffix == ".service"]
    assert services
    for svc in services:
        text = svc.read_text()
        assert "Environment=PLANE_LEGACY_WRITE_EVENTS=0" in text and "Environment=PLANE_LEGACY_WRITE_DISPATCH=0" in text, svc.name
        assert "PLANE_LEGACY_WRITE_REPORT" not in text                       # unset in the tier: nothing stamped
    timers, _ = _composed(off, monkeypatch, {"PLANE_EMIT_ENABLED": "1", "PLANE_LEGACY_WRITE_EVENTS": "1"})
    for svc in (p for p in timers.iterdir() if p.suffix == ".service"):
        assert "PLANE_LEGACY_WRITE" not in svc.read_text(), svc.name


def test_retire_writes_no_longer_names_a_frozen_reader(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    for reader in cut.READERS:
        _declare(root, reader)
    done = _cli(root, "cutover", "--retire-writes")
    assert done.returncode == 0 and "frozen" not in done.stdout.lower() and "--unassigned" in done.stdout


def test_a_progress_report_is_not_terminal_and_a_blocked_one_keeps_the_legacy_word(tmp_path):
    """A later `progress` (non-terminal) report means the worker is NOT idle;
    a `returned_blocked` task event prints as the legacy `blocked`."""
    root, paths, _, _ = _scene(tmp_path)
    _report(root, WI2, ASG2, "2026-09-02T13:00:00Z")
    _report(root, WI2, ASG2, "2026-09-02T13:30:00Z", event="progress", extra={"progress": 50})
    assert _idle(root) == ""                                        # progress: not idle
    _report(root, WI3, ASG3, "2026-09-02T14:00:00Z", bot="w2", event="returned_blocked")   # w2's open task ends blocked
    out = _idle(root)
    assert out.startswith("w2 ") and out.rstrip().endswith("t-3-cccc blocked") and "w1" not in out


def test_a_report_that_resolved_nothing_is_the_newest_report_but_not_terminal(tmp_path):
    """A bare note (the door lands its communication, no task event, no
    marker) after a completion: a report with no status — not idle, like a
    progress report."""
    root, paths, _, _ = _scene(tmp_path)
    _report(root, WI2, ASG2, "2026-09-02T13:00:00Z")
    _report(root, None, None, "2026-09-02T13:15:00Z", event=None)
    assert _idle(root) == ""


def test_a_peers_later_report_does_not_touch_this_bots_idleness(tmp_path):
    """The newest-report question is PER BOT: w2's later progress report
    leaves w1 idle (w1's own newest report is still its completion)."""
    root, paths, _, _ = _scene(tmp_path)
    _report(root, WI2, ASG2, "2026-09-02T13:00:00Z")
    _report(root, WI3, ASG3, "2026-09-02T13:45:00Z", bot="w2", event="progress", extra={"progress": 10})
    out = _idle(root)
    assert out.startswith("w1 ") and "w2" not in out


def test_a_terminal_note_that_resolved_nothing_is_idle(tmp_path):
    """The general lens's finding: a `completed` report with no task id and no
    open id-less dispatch lands only a communication — its status rode
    nowhere, so the idle check said busy. The door lands a report_status
    marker; the check reads it."""
    root, paths, _, _ = _scene(tmp_path)
    _report(root, None, None, "2026-09-02T13:15:00Z", event=None, status="completed")
    out = _idle(root)
    assert out.startswith("w1 ") and out.rstrip().endswith("- completed")


def test_a_case_variant_alias_does_not_lose_the_bots_report(tmp_path):
    """The adversarial lens: a dispatch to `bot:f/W1` and a report from
    `bot:f/w1` mint two actor uids; every per-bot read spans all of them."""
    root, paths, _, _ = _scene(tmp_path)
    _live_dispatch(root, "6", "t-6-ffff", ts="2026-09-02T12:30:00Z", bot="W1",
                   expected_by="2026-09-03T00:00:00+00:00")
    _report(root, f"wi_{'6':0>32}", f"asg_{'6':0>32}", "2026-09-02T13:00:00Z", bot="w1")   # the lower-case alias reports
    out = _idle(root)
    assert out.startswith("w1 ") and "t-6-ffff completed" in out
