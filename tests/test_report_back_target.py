"""Where a report-back is DELIVERED (#1754): REPORTS_TO first, MANAGER_TMUX as
the fallback, and a self-addressed send refused (rc 4, nothing sent, nothing
recorded). The full account is the comment block in lib/report-back.sh.

These drive the REAL door against a REAL scratch plane with the tmux binary
stubbed (`/usr/bin/true`): the pane send is not the fact under test, the
recipient the plane records is.
"""

from __future__ import annotations

from pathlib import Path

from tests.plane_fixtures import F as FLEET
from tests.plane_fixtures import ro as _ro
from tests.test_task_id_dispatch import LIB_DIR, _bash, plane_env, plane_report_rows


def _run(tmp_path: Path, bot: str, args: str = 'completed "widget shipped"', **env: str):
    e = plane_env(tmp_path)
    e["TMUX_BIN"] = "/usr/bin/true"
    e.update(env)
    return _bash(f'"{LIB_DIR}/report-back.sh" {bot} {args}', env=e)


# --- a manager reports UPWARD, never to itself ---------------------------------


def test_a_manager_with_reports_to_delivers_to_the_upward_target(tmp_path):
    """The live proof case, fixed: MANAGER_TMUX is the manager's own id (the
    marker bot_is_manager keys on, untouched) and REPORTS_TO names who it
    reports to. The plane's recipient is the upward target, not the sender."""
    r = _run(tmp_path, "mgr", BOT_ID="mgr", MANAGER_TMUX="mgr", REPORTS_TO="cto")
    assert r.returncode == 0, r.stderr
    rows = plane_report_rows(tmp_path)
    assert len(rows) == 1, rows
    assert rows[0]["sender"] == f"bot:{FLEET}/mgr"
    assert rows[0]["recipient_raw"] == "cto"
    assert rows[0]["recipient"] == f"bot:{FLEET}/cto"
    assert rows[0]["recipient"] != rows[0]["sender"], "a self-addressed row is the defect"


def test_a_manager_with_no_upward_target_is_refused_loudly(tmp_path):
    """No REPORTS_TO and the manager marker points at itself: the only delivery
    the old door could make was into its own pane, with the plane closing
    green. Now it refuses -- rc 4, the remedy on stderr, NOTHING recorded: the
    refusal precedes the batch and the send -- so the row stays open until
    the report reaches someone."""
    r = _run(tmp_path, "mgr", BOT_ID="mgr", MANAGER_TMUX="mgr")
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert "mgr" in r.stderr and "reports_to" in r.stderr, r.stderr
    assert "own pane" in r.stderr, r.stderr
    assert plane_report_rows(tmp_path) == [], "a refused report must not close the row green"
    assert _report_back_rows(tmp_path) == 0, "nothing of this report reached the plane"


def _report_back_rows(root: Path) -> int:
    """Every row the report-back door emits under its own source_ref -- the
    communication, its task legs, its marker, its transmission. The plane db
    itself may exist: a peer lookup for a fleet with no local/<fleet> dir trips
    lib-common's ERR trap into a script_error row (pre-existing -- the original
    door landed two per report on this shape), which is not this door's record.
    """
    if not (root / "state" / "plane" / "plane.db").exists():
        return 0
    with _ro(root) as conn:
        return sum(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_ref LIKE 'report-back:%'"
            ).fetchone()[0]
            for table in ("communications", "events")
        )


def test_reports_to_naming_the_sender_itself_is_refused(tmp_path):
    """A declared upward target that IS the sender is the same self-send with a
    different carrier; the refusal keys on the resolved target, not on which
    variable supplied it."""
    r = _run(tmp_path, "w1", BOT_ID="w1", REPORTS_TO="w1", MANAGER_TMUX="lead")
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert plane_report_rows(tmp_path) == []


def test_self_detection_reads_the_positional_sender_name(tmp_path):
    """The sender is the positional name, the alias the plane stamps; a hand
    caller may run with no BOT_ID exported and the check must not depend on
    the export."""
    r = _run(tmp_path, "w1", MANAGER_TMUX="w1")
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert plane_report_rows(tmp_path) == []


def test_a_caller_driving_the_door_for_another_bot_is_not_self(tmp_path):
    """The environment carries the CALLER's id (a manager, a harness) while the
    positional names the worker reporting: the two aliases differ, so this is
    not a self-addressed send -- a bare BOT_ID compare would refuse it."""
    r = _run(tmp_path, "w1", BOT_ID="lead", BOT_NAME="lead", MANAGER_TMUX="lead")
    assert r.returncode == 0, r.stderr
    rows = plane_report_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["recipient_raw"] == "lead", rows


# --- a worker is unchanged ------------------------------------------------------


def test_a_worker_with_only_manager_tmux_still_delivers_to_its_manager(tmp_path):
    """No REPORTS_TO reached the session (a bot.conf composed before the field
    existed): the fallback is the old behaviour byte for byte."""
    r = _run(tmp_path, "w1", BOT_ID="w1", MANAGER_TMUX="lead")
    assert r.returncode == 0, r.stderr
    rows = plane_report_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["recipient_raw"] == "lead", rows
    assert rows[0]["recipient"] == f"bot:{FLEET}/lead"


def test_a_worker_with_reports_to_delivers_to_the_declared_target(tmp_path):
    """The declaration is THE report-to fact, for a worker too. On every real
    fleet the two agree (the composer resolves reports_to, else the team
    manager); when a worker declares someone else, the validator warns and
    the declaration wins."""
    r = _run(tmp_path, "w1", BOT_ID="w1", MANAGER_TMUX="lead", REPORTS_TO="lead2")
    assert r.returncode == 0, r.stderr
    rows = plane_report_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["recipient_raw"] == "lead2", rows


def test_an_empty_reports_to_is_absent_not_a_target(tmp_path):
    """An empty assignment is not a declaration of nobody: it falls through to
    MANAGER_TMUX exactly as an unset one does."""
    r = _run(tmp_path, "w1", BOT_ID="w1", MANAGER_TMUX="lead", REPORTS_TO="")
    assert r.returncode == 0, r.stderr
    rows = plane_report_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["recipient_raw"] == "lead", rows


# --- fleet-aware: a same bare name in another fleet is a different bot (#526) --


def _peer_dir(root: Path, fleet: str, bot: str) -> Path:
    """A peer's bot dir as the composer lays it out, so bot_dir_for_session
    (own-fleet fast path, then the cross-fleet glob) can find it; the fleet
    dir carries a fleet.yaml because resolve_fleet_dir requires one."""
    fdir = root / "local" / fleet
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "fleet.yaml").write_text(f"fleet:\n  name: {fleet}\n  bots: {{}}\n")
    d = fdir / "runtime" / "bots" / bot
    d.mkdir(parents=True)
    (d / "bot.conf").write_text(f"BOT_ID={bot}\nFLEET_NAME={fleet}\nBOT_SERVICE=svc.{fleet}.{bot}\n")
    return d


def test_a_same_named_bot_in_another_fleet_is_not_self(tmp_path):
    """#526: two fleets may each hold a bot named `mgr`. The sender is `mgr` of
    fleet F; the only `mgr` dir on the host belongs to another fleet, so the
    target resolves THERE and the recipient alias names that fleet. A bare-
    name compare would read this as self and refuse; the alias compare does
    not, and the row records which `mgr` was actually addressed."""
    _peer_dir(tmp_path, "otherfleet", "mgr")
    r = _run(tmp_path, "mgr", BOT_ID="mgr", REPORTS_TO="mgr")
    assert r.returncode == 0, r.stderr
    rows = plane_report_rows(tmp_path)
    assert len(rows) == 1, rows
    assert rows[0]["sender"] == f"bot:{FLEET}/mgr"
    assert rows[0]["recipient"] == "bot:otherfleet/mgr", rows
    assert rows[0]["recipient"] != rows[0]["sender"]


def test_the_senders_own_fleet_peer_wins_over_a_same_named_bot_elsewhere(tmp_path):
    """The other direction of #526: `lead` exists in the sender's fleet AND in
    another. The own-fleet fast path resolves the sender's own `lead`, and the
    alias carries the sender's fleet -- the report goes where the team wiring
    says, never to the namesake next door."""
    _peer_dir(tmp_path, FLEET, "lead")
    _peer_dir(tmp_path, "otherfleet", "lead")
    r = _run(tmp_path, "w1", BOT_ID="w1", REPORTS_TO="lead")
    assert r.returncode == 0, r.stderr
    rows = plane_report_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["recipient"] == f"bot:{FLEET}/lead", rows


def test_the_self_check_follows_the_resolved_peer_across_fleets(tmp_path):
    """And the refusal is on the RESOLVED identity: `mgr` of fleet F, whose own
    dir exists in F, naming `mgr` resolves to itself even though another
    fleet also has a `mgr` -- refused, because that is the bot it would land
    on."""
    _peer_dir(tmp_path, FLEET, "mgr")
    _peer_dir(tmp_path, "otherfleet", "mgr")
    r = _run(tmp_path, "mgr", BOT_ID="mgr", MANAGER_TMUX="mgr")
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert plane_report_rows(tmp_path) == []

