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
    # Opt-OUT since the defaults flip: absence RUNS, only an exact 0 stops it.
    env["PLANE_PRUNE_ENABLED"] = "1" if armed else "0"   # the launcher self-gate
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


def test_prune_job_ships_enrolled_and_reads_root():
    """Chunk N: retention ships ON. The rule says a door that DELETES DATA
    stays opt-in, and this is the one argued exception: what it deletes is raw
    per-minute SAMPLES past the 30-day incident-join window — the retention
    every metrics store does, and the reason the per-minute host probe is safe
    to ship on. The ledger is out of reach by construction (family-scoped
    DELETE, pinned elsewhere in this file), and the window stays the knob."""
    import yaml
    sysyaml = yaml.safe_load(
        (REPO / "claudlobby" / "system.yaml").read_text())
    job = sysyaml["host"]["jobs"]["plane-prune"]
    assert job.get("enroll", True) is True
    assert "plane-prune.sh" in job["script"]


def _launcher(root: Path, *argv, armed):
    import os
    # the throwaway root has no .venv; the launcher resolves the CLI via
    # its PATH rung, so put the repo venv there (how the estate resolves)
    env = dict(os.environ, CLAUDLOBBY_ROOT=str(root),
               PATH=f"{REPO / '.venv' / 'bin'}:" + os.environ.get("PATH", ""))
    # Opt-OUT since the defaults flip: absence RUNS, only an exact 0 stops it.
    env["PLANE_PRUNE_ENABLED"] = "1" if armed else "0"
    return subprocess.run(
        ["bash", str(REPO / "lib" / "plane-prune.sh"), *argv],
        capture_output=True, text=True, timeout=120, env=env)


def test_launcher_runs_by_default_and_its_off_switch_is_LOUD(tmp_path):
    """Opt-OUT since chunk N. The off half still deletes nothing AND says so:
    a plane growing without bound because a flag was set two months ago and
    forgotten is precisely what a silent skip buys."""
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=40)
    off = _launcher(root, "--dry-run", armed=False)
    assert off.returncode == 0
    # REWRITTEN by the fold (F6): the loud line comes from the shared gate
    # (lib-common `switch_is_on`) now — same three facts, one definition.
    assert "plane-prune: OFF here" in off.stderr
    assert "PLANE_PRUNE_ENABLED=0" in off.stderr
    assert "accumulate without bound" in off.stderr
    assert _counts(root)[0] == 1              # off touched nothing
    on = _launcher(root, armed=True)
    assert on.returncode == 0
    assert _counts(root)[0] == 0              # on pruned


def test_launcher_prunes_with_no_flag_at_all(tmp_path):
    """Absence is ON — the flip itself. Fails if `${FLAG:-0}` comes back."""
    import os
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=40)
    env = {k: v for k, v in os.environ.items() if k != "PLANE_PRUNE_ENABLED"}
    env.update(CLAUDLOBBY_ROOT=str(root),
               PATH=f"{REPO / '.venv' / 'bin'}:" + os.environ.get("PATH", ""))
    r = subprocess.run(["bash", str(REPO / "lib" / "plane-prune.sh")],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stderr
    assert _counts(root)[0] == 0


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


# --- #1751: the metric_sample lane shipped with no watermark --------------
#
# Same defect #1744 fixed for the system lane beside this one (kept in
# test_system_event_retention.py): prune_metric_samples deletes family rows
# and correctly never touches the ledger, which left a ledger row with no
# family row -- exactly the state `_verify_duplicates` treats as integrity
# damage, refusing the WHOLE BATCH on the first divergence. This lane has
# shipped ON by default for months; #1744's fix was for a lane that ships
# OFF. Reuses `prune_watermarks` -- does not invent a second mechanism.
#
# The blast radius (whole-batch loss on ANY unexplained divergence) is
# pre-existing, not caused by pruning, and not this PR's to fix -- three
# people independently established that tonight. This closes one way of
# REACHING it.

def _watermark(root: Path, family="metric_sample"):
    db = sqlite3.connect(db_path(root))
    try:
        return db.execute(
            "SELECT pruned_before, pruned_at FROM prune_watermarks"
            " WHERE family = ?", (family,)).fetchone()
    finally:
        db.close()


def test_a_prune_that_deletes_records_a_watermark(tmp_path):
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=40)
    conn = connect(db_path(root))
    try:
        res = prune_metric_samples(conn, now=NOW)
    finally:
        conn.close()
    assert res.deleted == 1
    wm = _watermark(root)
    assert wm is not None
    assert wm[0] == res.cutoff


def test_a_prune_that_deletes_nothing_writes_no_watermark(tmp_path):
    """A watermark asserts rows were removed behind it -- writing one for a
    no-op prune would excuse an absence this lane never caused."""
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=5)           # inside the window
    conn = connect(db_path(root))
    try:
        prune_metric_samples(conn, now=NOW)
    finally:
        conn.close()
    assert _watermark(root) is None


def test_the_metric_sample_watermark_never_walks_backwards(tmp_path):
    """A re-run with a narrower window must not re-expose rows an earlier,
    wider prune already explained. Mirrors the system lane's own pin
    (test_system_event_retention.py) over this file's real-schema
    fixtures, per #1751's ask for a round-trip test on a real schema."""
    root = _root(tmp_path)
    _sample(root)
    _backdate_all(root, days_old=400)          # only row so far -- safe
    conn = connect(db_path(root))
    try:
        prune_metric_samples(conn, now=NOW, days=100)
    finally:
        conn.close()
    wide = _watermark(root)[0]                 # the 400d row is gone now

    _sample(root)
    _backdate_all(root, days_old=40)           # only row left -- safe again
    conn = connect(db_path(root))
    try:
        prune_metric_samples(conn, now=NOW, days=30)
    finally:
        conn.close()
    narrow = _watermark(root)[0]
    assert narrow >= wide, (wide, narrow)


def test_a_replayed_batch_survives_when_one_of_its_rows_was_pruned(tmp_path):
    """THE behavioral proof. The real replay shape is an ACK-LOST RETRY: the
    client resends the IDENTICAL prior batch, same event_ids throughout
    (plane-emit.sh pre-mints ids before the first attempt precisely so a
    retry can do this) -- never a stray brand-new id bundled with an old
    one, which is a DIFFERENT, separate, pre-existing defect (`ingest_many`
    is one all-or-nothing transaction, so ANY intra-batch collision rolls
    the WHOLE call back and a genuinely-never-ingested id in that same call
    hits its own "missing from ledger -- mixed state" refusal regardless of
    this fix -- confirmed directly, not this PR's to close, matches dara's
    "not yours to fix" scope).

    So: one real batch, two samples (the shape a keepalive tick actually
    sends -- bot.heartbeat + bot.session_up together). One ages out and is
    explained only by the watermark; its sibling's family row never moved.
    Before this fix, `_verify_duplicates` raised on the FIRST divergence
    (the pruned one) and never reached the second AT ALL -- the sibling's
    otherwise-clean duplicate classification was lost beside it. That is
    the original #1659/#1751 harm, reproduced and closed here.

    Real schema, real ingest, real prune -- `ingest_many` via `emit_batch`,
    not `_explained_by_a_prune` against a hand-built table."""
    root = _root(tmp_path)
    original = emit_batch(root, [
        {"event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
         "payload": {"subject_kind": "bot_instance", "subject": "bot:f/erlich",
                     "metric": "bot.heartbeat", "value": {"state": "IDLE"}}},
        {"event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
         "payload": {"subject_kind": "bot_instance", "subject": "bot:f/erlich",
                     "metric": "bot.session_up", "value": True}},
    ])
    assert [o.status for o in original] == ["committed", "committed"]
    pruned_id, survives_id = original[0].event_id, original[1].event_id

    # Age out ONLY the heartbeat row -- its sibling stays inside the window,
    # so only one of the two needs the watermark's explanation.
    db = connect(db_path(root))
    try:
        old = (NOW - timedelta(days=40)).isoformat()
        db.execute("UPDATE metric_samples SET ingested_at = ? WHERE event_id = ?",
                   (old, pruned_id))
        # `_explained_by_a_prune` reads the LEDGER's ingested_at, not the
        # family row's -- a real ingest stamps both at the same instant, so
        # keep them in sync here too (the same gap test_the_metric_sample_
        # watermark_never_walks_backwards's own setup does not need, since it
        # only exercises prune_metric_samples, which never reads the ledger).
        db.execute("UPDATE ingest_ledger SET ingested_at = ? WHERE event_id = ?",
                   (old, pruned_id))
    finally:
        db.close()
    conn = connect(db_path(root))
    try:
        res = prune_metric_samples(conn, now=NOW)
    finally:
        conn.close()
    assert res.deleted == 1                    # only the heartbeat row is gone
    assert _watermark(root) is not None         # explained by a watermark now

    # The ack-lost retry: the SAME batch, byte-identical event_ids, resent
    # whole. Must not raise, and the sibling's own clean duplicate
    # classification must not be lost beside the pruned one's.
    replay = emit_batch(root, [
        {"event_id": pruned_id,
         "event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
         "payload": {"subject_kind": "bot_instance", "subject": "bot:f/erlich",
                     "metric": "bot.heartbeat", "value": {"state": "IDLE"}}},
        {"event_id": survives_id,
         "event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
         "payload": {"subject_kind": "bot_instance", "subject": "bot:f/erlich",
                     "metric": "bot.session_up", "value": True}},
    ])
    assert [o.status for o in replay] == ["duplicate", "duplicate"]
    # and the surviving row is still exactly the one row it always was
    db = sqlite3.connect(db_path(root))
    try:
        n = db.execute("SELECT COUNT(*) FROM metric_samples WHERE event_id = ?",
                       (survives_id,)).fetchone()[0]
    finally:
        db.close()
    assert n == 1
