"""What the deadline watch tells an assignee (#2546).

An assignment past its deadline reads `overdue`, `orphaned` or `past_due` in its assignee's brief. Nothing told
the assignee what `orphaned` means, or that a progress report ends it for a while. This suite has three groups:

  1. THE CLAIMS the assignee-facing text makes, pinned against the real brief over a throwaway plane: what moves an
     assignment into `orphaned`, what takes it out, and what does not. They describe behavior that already existed;
     a text test that passed while one of these claims was false would be a decoy.
  2. THE SURFACES that state them: the report-back protocol every worker carries, the fleet-ops skill, the status
     skill's pointer, the help of `assignment progress` and `assignment show`, and the brief's own lines.
  3. THE NUMBERS those surfaces print (45 minutes, 24 hours) are the matcher's defaults, read from the matcher, and
     every status the brief can print is explained in the skill.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from claudlobby.__main__ import main
from claudlobby.brief import boot_provenance, build_brief, format_boot_brief, format_brief
from claudlobby.composer import compose_claude_md
from claudlobby.paths import Paths
from tests.conftest import install_real_template, load_lib_module, load_test_fleet, make_paths
from tests.test_brief import (  # noqa: F401 — the brief suite's fixtures and landing helpers
    FLEET, NOW, _SEQ, _dispatch, _fleet, _iso, _land, _plane_carrier, paths, root, tmp_path,
)

REPO = Path(__file__).resolve().parent.parent
WORKER = next(iter(_fleet().bots))      # the brief suite's fleet declares one worker and its manager;
MANAGER = _fleet().manager              # their names come from it, so this module adds none of its own
GRACE_S = 2700     # the matcher's DEFAULT_PROGRESS_GRACE_S: 45 minutes (group 3 reads it from the matcher)
CAP_S = 86400       # its DEFAULT_OVERDUE_MAX_AGE_S: 24 hours
DISPATCHED, DEADLINE = NOW - 9000, NOW - 3000   # dispatched two and a half hours ago, due fifty minutes ago


# --- the world ----------------------------------------------------------------


def _restart(paths: Paths, at: int = NOW - 100) -> None:
    """The worker's session starts again at *at*: start-bot.sh rewrites data/.spawn."""
    os.utime(paths.runtime_bots / WORKER / "data" / ".spawn", (at, at))


def _fact(paths: Paths, wi: str, asg: str, event: str, at: int, *, bot: str = WORKER,
          emitter: str = "report-back", **payload) -> None:
    """One task fact on an assignment, as the report door lands it (a progress report, a block, a return, a
    completion) or the deadline verb lands it (deadline_changed)."""
    from claudlobby.plane.emit_api import emit_batch
    _SEQ[0] += 1
    out = emit_batch(paths.root, [{
        "event_type": "task", "emitter": emitter, "fleet": FLEET, "source_ref": f"assignee-attention:{_SEQ[0]}",
        "occurred_at": _iso(at),
        "payload": {"work_item_id": wi, "assignment_id": asg, "event": event,
                    "actor": f"bot:{FLEET}/{bot}", **payload}}])
    assert all(o.status == "committed" for o in out), out


def _readings(paths: Paths, now: int = NOW) -> list[tuple[str, str]]:
    """(status, reason) of each of the worker's open assignments, as its brief reads them at *now*."""
    items = build_brief(_fleet(), paths, WORKER, now)["work"]["items"]
    return [(item["attention"]["status"], item["attention"]["reason"]) for item in items]


def _reading(paths: Paths, now: int = NOW) -> tuple[str, str]:
    (only,) = _readings(paths, now)
    return only


def _by_assignment(paths: Paths, now: int = NOW) -> dict[str, tuple[str, str]]:
    """The same readings keyed by assignment ID, for a claim about which assignment reads what."""
    items = build_brief(_fleet(), paths, WORKER, now)["work"]["items"]
    return {item["attention"]["assignment_id"]: (item["attention"]["status"], item["attention"]["reason"])
            for item in items}


ORPHANED = ("orphaned", "respawned_after_dispatch")
GRACE = ("past_due", "progress_grace")
CAPPED = ("past_due", "max_age_cap")


# --- 1. the claims ------------------------------------------------------------


def test_a_restart_after_the_dispatch_is_what_turns_overdue_into_orphaned(paths: Paths):
    _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    assert _reading(paths) == ("overdue", "none")           # the fixture's .spawn is older than the dispatch
    _restart(paths)
    assert _reading(paths) == ORPHANED


def test_a_progress_report_holds_the_flag_back_for_the_grace_window_and_not_one_second_longer(paths: Paths):
    wi, asg = _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _restart(paths)
    assert _reading(paths) == ORPHANED
    reported = NOW - 100
    _fact(paths, wi, asg, "progress", reported)
    assert _reading(paths, NOW) == GRACE
    assert _reading(paths, reported + GRACE_S) == GRACE     # the last second of the window
    assert _reading(paths, reported + GRACE_S + 1) == ORPHANED


def test_a_block_is_not_progress(paths: Paths):
    wi, asg = _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _restart(paths)
    _fact(paths, wi, asg, "blocked_waiting", NOW - 90)
    item = build_brief(_fleet(), paths, WORKER, NOW)["work"]["items"][0]
    assert item["state"] == "blocked"                       # the block is recorded, and the assignment is still the worker's
    assert (item["attention"]["status"], item["attention"]["reason"]) == ORPHANED


@pytest.mark.parametrize("event", ["returned_blocked", "completed", "failed"])
def test_returning_completing_and_failing_take_the_assignment_off_the_open_list(paths: Paths, event: str):
    wi, asg = _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _restart(paths)
    assert _readings(paths) == [ORPHANED]
    _fact(paths, wi, asg, event, NOW - 50)
    assert _readings(paths) == []


def test_the_expiry_cap_ends_the_flag_even_for_a_worker_that_keeps_reporting(paths: Paths):
    wi, asg = _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _restart(paths)
    _fact(paths, wi, asg, "progress", DISPATCHED + CAP_S - 50)
    assert _reading(paths, DISPATCHED + CAP_S) == GRACE     # a report 50 s old: the window holds, the cap has not struck
    assert _reading(paths, DISPATCHED + CAP_S + 1) == CAPPED
    assert _reading(paths, DISPATCHED + CAP_S + GRACE_S + 10) == CAPPED


def test_with_no_report_at_all_the_expiry_cap_still_ends_the_flag(paths: Paths):
    _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _restart(paths)
    assert _reading(paths, DISPATCHED + CAP_S) == ORPHANED      # never reported: flagged to the cap's last second
    assert _reading(paths, DISPATCHED + CAP_S + 1) == CAPPED    # then aged out, though nobody reported


def test_progress_on_one_assignment_holds_back_every_past_due_assignment_of_the_bot(paths: Paths):
    _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    wi_b, asg_b = _land(paths, _dispatch(WORKER, DISPATCHED + 1, DEADLINE, task_id="t-b"))
    _restart(paths)
    assert _readings(paths) == [ORPHANED, ORPHANED]
    _fact(paths, wi_b, asg_b, "progress", NOW - 100)
    assert _readings(paths) == [GRACE, GRACE]


def test_a_progress_report_holds_back_only_the_assignments_dispatched_before_it(paths: Paths):
    wi, asg = _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _fact(paths, wi, asg, "progress", NOW - 2000)
    _, asg_b = _land(paths, _dispatch(WORKER, NOW - 1900, NOW - 1000, task_id="t-b"))   # after that report
    assert _by_assignment(paths) == {asg: GRACE, asg_b: ("overdue", "none")}   # the report predates t-b
    _fact(paths, wi, asg, "progress", NOW - 50)
    assert _by_assignment(paths) == {asg: GRACE, asg_b: GRACE}                 # a newer one holds back both


def test_a_moved_deadline_clears_the_flag_and_moves_the_expiry_cap_with_it(paths: Paths):
    wi, asg = _land(paths, _dispatch(WORKER, NOW - 172800, NOW - 170000, task_id="t-a"))   # two days old, long past due
    _restart(paths)
    assert _reading(paths) == CAPPED
    new_deadline = NOW + 3600
    _fact(paths, wi, asg, "deadline_changed", NOW - 10, bot=MANAGER, emitter="claudlobby.tasks.v1",
          by=f"bot:{FLEET}/{MANAGER}", reason="waits on the operator", deadline=_iso(new_deadline))
    assert _reading(paths, NOW) == ("not_due", "none")
    assert _reading(paths, new_deadline + 1) == ORPHANED    # past the new deadline, the cap counts from it
    assert _reading(paths, new_deadline + CAP_S) == ORPHANED
    assert _reading(paths, new_deadline + CAP_S + 1) == CAPPED


def test_a_deadline_moved_into_the_past_starts_the_expiry_cap_at_the_move(paths: Paths):
    wi, asg = _land(paths, _dispatch(WORKER, NOW - 172800, NOW - 170000, task_id="t-a"))   # two days old
    _restart(paths)
    moved_at = NOW - 10
    _fact(paths, wi, asg, "deadline_changed", moved_at, bot=MANAGER, emitter="claudlobby.tasks.v1",
          by=f"bot:{FLEET}/{MANAGER}", reason="waits on the operator", deadline=_iso(NOW - 3600))
    assert _reading(paths, NOW) == ORPHANED                 # past the new deadline, and the cap counts from the move
    assert _reading(paths, moved_at + CAP_S) == ORPHANED
    assert _reading(paths, moved_at + CAP_S + 1) == CAPPED


# --- 2. the surfaces ----------------------------------------------------------


def _text(rel: str) -> str:
    return (REPO / rel).read_text()


def _span(text: str, first: str, last: str) -> str:
    """From the start of the paragraph that opens with *first* to the end of the paragraph that opens with *last*."""
    assert first in text, f"no paragraph opens with {first}"
    start = text.index(first)
    assert last in text[start:], f"no paragraph opens with {last}"
    tail = text.index(last, start)
    end = text.find("\n\n", tail)
    return text[start:end if end != -1 else len(text)]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _report_back() -> str:
    return _flat(_span(_text("library/protocols/report-back.md"), "**Past its deadline.**", "**Past its deadline.**"))


def _fleet_ops() -> str:
    return _flat(_span(_text("library/skills/fleet-ops/SKILL.md"), "**The deadline watch.**", "**Ending it.**"))


def test_report_back_tells_a_worker_what_orphaned_means_and_what_clears_it():
    para = _report_back()
    assert "`orphaned`" in para and "`overdue`" in para and "restarted after the dispatch" in para
    assert "still open and still yours" in para
    assert "`assignment progress`" in para and "holds the flag back for" in para     # the number: the numbers test below
    assert "`block` does not" in para
    assert "`/fleet-ops` says what each status means and what ends it" in para      # the rest of the explanation is there
    assert "task nudge" not in para        # a worker's composed grants hold no `task nudge`: the fleet manager's do


def test_a_workers_composed_instructions_carry_the_paragraph(fleet_dir):
    """The protocol as the compositor renders it into a worker's CLAUDE.md, from the real template and library."""
    (fleet_dir / "library" / "protocols" / "report-back.md").unlink()   # the fixture's stub would shadow the real file
    manifest = fleet_dir / "fleet.yaml"
    text = manifest.read_text()
    assert text.count("expertise: [software-engineering]") == 1
    manifest.write_text(text.replace("expertise: [software-engineering]",
                                     "expertise: [software-engineering]\n      protocols: [report-back]"))
    install_real_template(fleet_dir)
    fleet = load_test_fleet(fleet_dir)
    composed = _flat(compose_claude_md(fleet.bots["worker-1"], fleet, make_paths(fleet_dir)))
    paragraph = _report_back()
    assert composed.count(paragraph) == 1
    assert composed.index("Report-Back Protocol") < composed.index(paragraph)


def test_fleet_ops_explains_every_attention_status():
    section = _fleet_ops()
    for cell in ("`not_due`", "`overdue`", "`orphaned` (`respawned_after_dispatch`)", "`past_due` (`progress_grace`)",
                 "`past_due` (`max_age_cap`)", "`unknown`"):
        assert cell in section, cell
    assert "on any assignment of yours" in section          # the clock is the bot's, not the assignment's
    assert "past-deadline assignments dispatched before it" in section     # and only those (the claims above)
    assert "one dispatched after it waits for a newer report" in section
    assert "no progress since its dispatch in the last 45 minutes" in section
    assert "held back by a progress report made since its dispatch and under 45 minutes old" in section
    assert "`block` is not progress" in section
    assert "`complete`, `fail` and `return` end the assignment" in section
    assert "`assignment reschedule`" in section and "`block --reason`" in section
    assert "task nudge" not in section


def test_the_status_skill_points_at_that_explanation():
    para = _flat(_text("library/skills/status/SKILL.md"))
    assert "overdue/orphaned or an explicit unknown; `/fleet-ops` says what each status means" in para


def _help(capsys, *argv: str) -> str:
    with pytest.raises(SystemExit) as exit_info:
        main([*argv, "--help"])
    assert exit_info.value.code == 0
    return _flat(capsys.readouterr().out)


def test_assignment_progress_help_explains_the_deadline_watch(capsys):
    text = _help(capsys, "assignment", "progress")
    for needle in ("what is done and what is next", "then notify the fleet manager", "orphaned", "past_due",
                   "45 minutes", "complete, fail and return end the assignment", "block does not clear the watch",
                   "More than 24 hours after the dispatch", "past_due with or without a report"):
        assert needle in text, needle


def test_assignment_show_help_says_where_the_watch_is(capsys):
    text = _help(capsys, "assignment", "show")
    assert "not part of this record" in text and "claudlobby brief" in text and "orphaned" in text


def _two_orphaned_rows(paths: Paths) -> str:
    for n in (1, 2):
        _land(paths, _dispatch(WORKER, DISPATCHED + n, DEADLINE, task_id=f"t-{n}"))
    _restart(paths)
    return format_boot_brief(build_brief(_fleet(), paths, WORKER, NOW), boot_provenance(paths, NOW))


def test_the_boot_brief_says_once_what_orphaned_means(paths: Paths):
    boot = _two_orphaned_rows(paths)
    lines = boot.splitlines()
    hints = [line for line in lines if line.startswith("  orphaned:")]
    assert len(hints) == 1, boot
    hint = hints[0]
    assert "past its deadline" in hint and "restarted" in hint and "assignment progress" in hint
    assert lines.index(hint) < min(i for i, line in enumerate(lines) if line.startswith("  ORPHANED "))
    assert "re-issue" not in boot                           # the old per-row note said the manager had to send it again


def test_the_boot_brief_without_an_orphaned_assignment_has_no_such_line(paths: Paths):
    _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))   # the fixture's .spawn is older: overdue
    boot = format_boot_brief(build_brief(_fleet(), paths, WORKER, NOW), boot_provenance(paths, NOW))
    assert "OVERDUE" in boot and "orphaned:" not in boot and "assignment progress" not in boot


def _item_block(text: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if "assignment asg_" in line and "attention " in line)
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith("      ")), len(lines))
    return lines[start:end]


def _scenario_orphaned(paths):
    _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _restart(paths)
    return NOW


def _scenario_grace(paths):
    wi, asg = _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _restart(paths)
    _fact(paths, wi, asg, "progress", NOW - 100)
    return NOW


def _scenario_capped(paths):
    _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    _restart(paths)
    return DISPATCHED + CAP_S + 1


def _scenario_overdue(paths):
    _land(paths, _dispatch(WORKER, DISPATCHED, DEADLINE, task_id="t-a"))
    return NOW


def _scenario_not_due(paths):
    _land(paths, _dispatch(WORKER, NOW - 500, NOW + 5000, task_id="t-a"))
    _restart(paths)
    return NOW


@pytest.mark.parametrize(("scenario", "status", "note"), [
    (_scenario_orphaned, "orphaned", ("orphaned: past its deadline", "restarted", "assignment progress", "45 minutes")),
    (_scenario_grace, "past_due", ("past_due: held back by a progress report", "45 minutes")),
    (_scenario_capped, "past_due", ("past_due: more than 24 hours", "not resolved")),
    (_scenario_overdue, "overdue", None),
    (_scenario_not_due, "not_due", None),
])
def test_the_text_brief_explains_the_deferred_states(paths: Paths, scenario, status, note):
    now = scenario(paths)
    block = _item_block(format_brief(build_brief(_fleet(), paths, WORKER, now)))
    assert f"attention {status}" in block[0]
    extra = [line for line in block[1:] if line.strip().startswith(("orphaned:", "past_due:"))]
    if note is None:
        assert extra == []                                  # the control: a state whose name says it needs no note
    else:
        assert len(extra) == 1, block
        assert all(part in extra[0] for part in note), extra[0]


def test_the_json_brief_carries_status_and_reason_but_no_note(paths: Paths):
    _scenario_orphaned(paths)
    attention = build_brief(_fleet(), paths, WORKER, NOW)["work"]["items"][0]["attention"]
    assert set(attention) == {"task_id", "assignment_id", "assignee_uid", "bot", "dispatched_at", "expected_by",
                              "last_progress_at", "spawn_at", "past_due", "elapsed_past_deadline_s", "status", "reason"}


# --- 3. the numbers, and the statuses -----------------------------------------


def test_the_numbers_the_surfaces_print_are_the_matchers_defaults(capsys):
    from claudlobby.brief import _WATCH_NOTES, BOOT_ORPHANED_HINT      # new names: imported here so main fails this test, not the module
    doors = load_lib_module("dispatch-overdue")
    assert (doors.DEFAULT_PROGRESS_GRACE_S, doors.DEFAULT_OVERDUE_MAX_AGE_S) == (GRACE_S, CAP_S)
    minutes, hours = f"{GRACE_S // 60} minutes", f"{CAP_S // 3600} hours"
    assert minutes in _report_back() and minutes in _fleet_ops() and hours in _fleet_ops()
    progress_help = _help(capsys, "assignment", "progress")
    assert minutes in progress_help and hours in progress_help
    assert minutes in BOOT_ORPHANED_HINT
    assert minutes in _WATCH_NOTES[ORPHANED] and minutes in _WATCH_NOTES[GRACE] and hours in _WATCH_NOTES[CAPPED]


def test_every_attention_status_the_brief_can_print_is_in_the_skill():
    source = _text("claudlobby/_runtime_scripts/dispatch-overdue.py")
    body = source[source.index("def assignment_attention"):source.index("def overdue_all")]
    statuses = set(re.findall(r'status="(\w+)"', body)) | set(re.findall(r'"status": "(\w+)"', body))
    assert statuses == {"not_due", "past_due", "overdue", "orphaned", "unknown"}, statuses   # the scan sees all five
    section = _fleet_ops()
    for status in sorted(statuses):
        assert f"`{status}`" in section, status
