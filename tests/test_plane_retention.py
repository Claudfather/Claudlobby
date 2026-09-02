"""Metric-sample retention (chunk 3a) — the family-scoped, ledger-safe
30-day DELETE.

The load-bearing laws (spec §F20/§10): retention deletes metric_samples
and NOTHING else; the ingest_ledger is NEVER touched (it is the dedupe
horizon); aging is by ingested_at (the ledger's forward clock, skew-safe),
never occurred_at. The pure logic is pinned without a timer; one CLI pin
over the real db proves the door; two composition pins prove the dormant
host-job wiring.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.retention import (
    DEFAULT_RETENTION_DAYS, prune_metric_samples,
)

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _sample(root: Path, subj_alias="bot:f/erlich"):
    emit_batch(root, [{
        "event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
        "payload": {"subject_kind": "bot_instance", "subject": subj_alias,
                    "metric": "bot.heartbeat", "value": {"state": "IDLE"}}}])


def _backdate_all(root: Path, days_old: float):
    """Rewrite metric_samples.ingested_at to <days_old> in the past — the
    field retention ages by."""
    db = connect(db_path(root))
    old = (NOW - timedelta(days=days_old)).isoformat()
    try:
        db.execute("UPDATE metric_samples SET ingested_at = ?", (old,))
    finally:
        db.close()


def _counts(root: Path):
    db = sqlite3.connect(db_path(root))
    try:
        s = db.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
        led = db.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0]
        return s, led
    finally:
        db.close()


def test_old_samples_age_out_by_ingested_at(tmp_path):
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=40)         # older than the 30d window
    conn = connect(db_path(root))
    try:
        res = prune_metric_samples(conn, now=NOW)
    finally:
        conn.close()
    assert res.deleted == 1
    assert _counts(root)[0] == 0


def test_fresh_samples_are_kept(tmp_path):
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=5)          # inside the window
    conn = connect(db_path(root))
    try:
        res = prune_metric_samples(conn, now=NOW)
    finally:
        conn.close()
    assert res.deleted == 0
    assert _counts(root)[0] == 1


def test_the_ledger_is_never_touched(tmp_path):
    """THE invariant: retention deletes family rows only. The ledger is the
    dedupe horizon and its rows outlive every family row — deleting one
    would let a replayed old event re-ingest as new."""
    root = _root(tmp_path)
    _sample(root)
    _sample(root)
    _backdate_all(root, days_old=99)
    _, led_before = _counts(root)
    conn = connect(db_path(root))
    try:
        prune_metric_samples(conn, now=NOW)
    finally:
        conn.close()
    samples_after, led_after = _counts(root)
    assert samples_after == 0                 # both aged out
    assert led_after == led_before            # ledger untouched
    assert led_after == 2


def test_a_backfilled_row_is_kept_by_ingestion_not_occurrence(tmp_path):
    """Aging is by ingested_at, never occurred_at: a sample with an ANCIENT
    occurred_at but a recent ingested_at (a backfill / RTC-skewed carrier)
    stays — it is in the join window from when we LEARNED it. Deleting by
    occurred_at would silently drop live data."""
    root = _root(tmp_path)
    _sample(root)
    db = connect(db_path(root))
    try:
        # ancient occurrence, fresh ingestion
        db.execute("UPDATE metric_samples SET occurred_at = ?, ingested_at = ?",
                   ((NOW - timedelta(days=400)).isoformat(),
                    (NOW - timedelta(days=1)).isoformat()))
    finally:
        db.close()
    conn = connect(db_path(root))
    try:
        res = prune_metric_samples(conn, now=NOW)
    finally:
        conn.close()
    assert res.deleted == 0                   # kept: learned yesterday
    assert _counts(root)[0] == 1


def test_dry_run_reports_without_deleting(tmp_path):
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=40)
    conn = connect(db_path(root))
    try:
        res = prune_metric_samples(conn, now=NOW, dry_run=True)
    finally:
        conn.close()
    assert res.candidates == 1 and res.deleted == 0
    assert _counts(root)[0] == 1              # still there


def test_custom_window_and_negative_refused(tmp_path):
    import pytest
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=45)
    conn = connect(db_path(root))
    try:
        assert prune_metric_samples(conn, now=NOW, days=60).deleted == 0
        assert prune_metric_samples(conn, now=NOW, days=30).deleted == 1
        with pytest.raises(ValueError):
            prune_metric_samples(conn, now=NOW, days=-1)
    finally:
        conn.close()


# --- CLI door + composition ------------------------------------------------

def _cli(root: Path, *argv, armed=True):
    import os
    env = dict(os.environ)
    if armed:
        env["PLANE_PRUNE_ENABLED"] = "1"   # the launcher self-gate
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root),
         "plane", *argv], capture_output=True, text=True, timeout=120, env=env)


def test_cli_prune_ages_out_and_dry_run_is_safe(tmp_path):
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=40)
    dry = _cli(root, "prune", "--dry-run")
    assert dry.returncode == 0
    assert "would delete 1" in dry.stdout
    assert _counts(root)[0] == 1              # dry run kept it
    live = _cli(root, "prune")
    assert live.returncode == 0
    assert "deleted 1" in live.stdout
    assert _counts(root)[0] == 0
    # a db that never existed is a no-op, not an error
    empty = _cli(tmp_path / "nope", "prune")
    assert empty.returncode == 0


def test_prune_job_composes_dormant_and_reads_root():
    import yaml
    sysyaml = yaml.safe_load(
        (REPO / "claudlobby" / "system.yaml").read_text())
    job = sysyaml["host"]["jobs"]["plane-prune"]
    assert job["enroll"] is False             # a DELETE door never auto-arms
    assert "plane-prune.sh" in job["script"]


def _launcher(root: Path, *argv, armed):
    import os
    # the throwaway root has no .venv; the launcher resolves the CLI via
    # its PATH rung, so put the repo venv there (how the estate resolves)
    env = dict(os.environ, CLAUDLOBBY_ROOT=str(root),
               PATH=f"{REPO / '.venv' / 'bin'}:" + os.environ.get("PATH", ""))
    if armed:
        env["PLANE_PRUNE_ENABLED"] = "1"
    return subprocess.run(
        ["bash", str(REPO / "lib" / "plane-prune.sh"), *argv],
        capture_output=True, text=True, timeout=120, env=env)


def test_launcher_self_gates_on_the_arming_flag(tmp_path):
    """r-gauntlet: enroll:false is NOT enforced for host timers
    (setup-system enrolls every composed unit), so for a DELETE door the
    launcher self-gates — unarmed, it no-ops loudly and deletes nothing;
    armed, it runs. Defense in depth beyond the dormant manifest."""
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=40)
    dormant = _launcher(root, "--dry-run", armed=False)
    assert dormant.returncode == 0
    assert "dormant" in dormant.stderr
    assert _counts(root)[0] == 1              # unarmed touched nothing
    armed = _launcher(root, armed=True)
    assert armed.returncode == 0
    assert _counts(root)[0] == 0              # armed pruned


def test_cli_negative_window_is_a_clean_refusal(tmp_path):
    """r-gauntlet: --days -1 (a future cutoff that would delete
    EVERYTHING) is a ContractViolation → rc 2, never a raw traceback."""
    root = _root(tmp_path)
    _sample(root)
    r = _cli(root, "prune", "--days", "-1")
    assert r.returncode == 2
    assert "Traceback" not in r.stderr
    assert _counts(root)[0] == 1              # nothing deleted


def test_prune_launcher_is_thin_and_root_flag_precedes_subcommand():
    body = (REPO / "lib" / "plane-prune.sh").read_text()
    # --root is global and MUST precede the subcommand (the plane-daemon
    # smoke caught the inverted order as a real argparse refusal)
    assert 'ARGS=(--root "$ROOT" plane prune "$@")' in body
    assert body.count("exec") >= 3            # the venv/PATH/python3 ladder


def test_host_prune_timer_arms_from_the_host_tier(tmp_path, monkeypatch):
    """Chunk 3a.1: a host timer starts with a CLOSED env, so the
    self-gated prune door needs PLANE_PRUNE_ENABLED stamped as an
    Environment= line — resolved from the host tier cascade, on the
    plane-prune job only, unarmed by default (the safe default for a
    DELETE door)."""
    from claudlobby.composer import compose_host_timers
    from claudlobby.paths import Paths
    import claudlobby.composer as comp

    root = tmp_path / "root"
    (root / "claudlobby").mkdir(parents=True)
    # a minimal host.jobs with plane-prune + a neighbor
    (root / "claudlobby" / "system.yaml").write_text(
        "host:\n  jobs:\n"
        "    plane-prune:\n      enroll: false\n"
        "      script: \"$CLAUDLOBBY_ROOT/lib/plane-prune.sh\"\n"
        "      schedule: \"*-*-* 05:15:00\"\n      type: oneshot\n"
        "    claude-update:\n"
        "      script: \"$CLAUDLOBBY_ROOT/lib/update-claude-code.sh\"\n"
        "      schedule: \"*-*-* 04:00:00\"\n      type: oneshot\n")
    paths = Paths(root=root)

    import claudlobby.env_tiers as et
    # armed: the host tier resolves PLANE_PRUNE_ENABLED=1
    from claudlobby.env_tiers import Resolution
    monkeypatch.setattr(et, "read_tiers",
                        lambda paths, bot_name=None, fleet_name=None: [])
    monkeypatch.setattr(et, "cascade", lambda tiers: {
        "PLANE_PRUNE_ENABLED": Resolution(
            name="PLANE_PRUNE_ENABLED", value="1", tier="host", path=None)})
    out = compose_host_timers(paths)
    svc = (out / "claudlobby-plane-prune.service").read_text()
    assert "Environment=PLANE_PRUNE_ENABLED=1" in svc
    # the neighbor job never gets it
    upd = (out / "claudlobby-claude-update.service").read_text()
    assert "PLANE_PRUNE_ENABLED" not in upd
    # unarmed: no flag stamped
    monkeypatch.setattr(et, "cascade", lambda tiers: {})
    out2 = compose_host_timers(paths)
    assert "PLANE_PRUNE_ENABLED" not in (
        out2 / "claudlobby-plane-prune.service").read_text()
