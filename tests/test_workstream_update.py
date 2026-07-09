"""Tests for lib/workstream-update.sh — the single-writer registry mutator.

Drives the real bash helper against a scratch registry file (WORKSTREAMS_PATH),
mirroring how test_creds_check_telegram.py exercises the real script.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "lib" / "workstream-update.sh"

# The helper shells out to jq; skip cleanly on hosts without it rather than
# erroring the whole suite (matches the sibling bash-driving tests).
pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="workstream-update.sh requires jq"
)


def _run(tmp_path: Path, *args: str, env_extra: dict | None = None):
    """Run workstream-update.sh with an isolated registry. Returns CompletedProcess."""
    registry = tmp_path / "workstreams.json"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "CLAUDLOBBY_ROOT": str(tmp_path / "root"),
        "WORKSTREAMS_PATH": str(registry),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _registry(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "workstreams.json").read_text())


def _open(tmp_path: Path, title: str, *extra: str, env_extra: dict | None = None) -> str:
    r = _run(tmp_path, "open", title, *extra, env_extra=env_extra)
    assert r.returncode == 0, f"open failed: {r.stderr}"
    return r.stdout.strip()


class TestOpen:
    def test_open_creates_active_entry_with_full_schema(self, tmp_path: Path):
        ws_id = _open(
            tmp_path, "Ship the widget", "--owner", "alex", "--project", "acme", "--next", "spike"
        )
        assert ws_id == "ws-ship-the-widget"
        entry = _registry(tmp_path)["workstreams"][ws_id]
        assert entry["status"] == "active"
        assert entry["owner_bot"] == "alex"
        assert entry["project"] == "acme"
        assert entry["next"] == "spike"
        assert entry["task_ids"] == []
        assert entry["refs"] == {"issues": [], "prs": []}
        assert entry["renewals"] == []
        # opened/progress/lease timestamps all present
        for k in ("opened_ts", "last_progress_ts", "lease_expires_ts"):
            assert entry[k], f"missing {k}"

    def test_slug_dedup_is_deterministic(self, tmp_path: Path):
        a = _open(tmp_path, "Same Title")
        b = _open(tmp_path, "Same Title")
        c = _open(tmp_path, "Same Title")
        assert [a, b, c] == ["ws-same-title", "ws-same-title-2", "ws-same-title-3"]

    def test_explicit_id_collision_fails(self, tmp_path: Path):
        _open(tmp_path, "First", "--id", "ws-custom")
        r = _run(tmp_path, "open", "Second", "--id", "ws-custom")
        assert r.returncode != 0
        assert "already exists" in r.stderr

    def test_fleet_name_stamps_entry(self, tmp_path: Path):
        ws_id = _open(tmp_path, "Fleet-stamped", env_extra={"FLEET_NAME": "eng-team"})
        assert _registry(tmp_path)["workstreams"][ws_id]["fleet"] == "eng-team"

    def test_lease_is_days_after_open(self, tmp_path: Path):
        ws_id = _open(tmp_path, "Leased", env_extra={"WORKSTREAM_LEASE_DAYS": "14"})
        entry = _registry(tmp_path)["workstreams"][ws_id]
        opened = datetime.fromisoformat(entry["opened_ts"].replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(entry["lease_expires_ts"].replace("Z", "+00:00"))
        assert abs((expiry - opened).total_seconds() - 14 * 86400) < 120


class TestCap:
    def test_open_at_cap_fails_with_actionable_message(self, tmp_path: Path):
        env = {"WORKSTREAM_MAX_ACTIVE": "2"}
        _open(tmp_path, "one", env_extra=env)
        _open(tmp_path, "two", env_extra=env)
        r = _run(tmp_path, "open", "three", env_extra=env)
        assert r.returncode == 3
        assert "cap (2)" in r.stderr
        assert "max_active" in r.stderr  # names the knob
        assert "ws-one" in r.stderr  # names oldest active as a close candidate

    def test_blocked_and_closed_free_a_cap_slot(self, tmp_path: Path):
        env = {"WORKSTREAM_MAX_ACTIVE": "2"}
        a = _open(tmp_path, "one", env_extra=env)
        _open(tmp_path, "two", env_extra=env)
        # Blocking one drops it out of the active count -> a third can open.
        assert _run(tmp_path, "block", a, env_extra=env).returncode == 0
        assert _run(tmp_path, "open", "three", env_extra=env).returncode == 0


class TestProgressRenew:
    def test_progress_advances_last_progress_and_extends_lease(self, tmp_path: Path):
        ws_id = _open(tmp_path, "work", env_extra={"WORKSTREAM_LEASE_DAYS": "1"})
        before = _registry(tmp_path)["workstreams"][ws_id]
        r = _run(tmp_path, "progress", ws_id, "--next", "phase 2", env_extra={"WORKSTREAM_LEASE_DAYS": "30"})
        assert r.returncode == 0
        after = _registry(tmp_path)["workstreams"][ws_id]
        assert after["next"] == "phase 2"
        # lease pushed out from 1 day to 30 days
        assert after["lease_expires_ts"] > before["lease_expires_ts"]
        assert after["last_progress_ts"] >= before["last_progress_ts"]

    def test_renew_requires_note(self, tmp_path: Path):
        ws_id = _open(tmp_path, "needs-note")
        r = _run(tmp_path, "renew", ws_id)
        assert r.returncode != 0
        assert "--note is required" in r.stderr

    def test_renew_loophole_is_visible(self, tmp_path: Path):
        """renew extends the lease but must NOT credit progress — so serial
        renew-without-progress stays detectable by the stall check."""
        ws_id = _open(tmp_path, "loophole", env_extra={"WORKSTREAM_LEASE_DAYS": "1"})
        opened = _registry(tmp_path)["workstreams"][ws_id]
        r1 = _run(tmp_path, "renew", ws_id, "--note", "still waiting on review", env_extra={"WORKSTREAM_LEASE_DAYS": "30"})
        r2 = _run(tmp_path, "renew", ws_id, "--note", "still waiting again", env_extra={"WORKSTREAM_LEASE_DAYS": "30"})
        assert r1.returncode == 0 and r2.returncode == 0
        after = _registry(tmp_path)["workstreams"][ws_id]
        # lease extended, two renewals logged, but progress NOT credited
        assert after["lease_expires_ts"] > opened["lease_expires_ts"]
        assert len(after["renewals"]) == 2
        assert all(rn["note"] for rn in after["renewals"])
        assert after["last_progress_ts"] == opened["last_progress_ts"]


class TestCloseBlockPrune:
    def test_close_marks_done_and_stamps_closed_ts(self, tmp_path: Path):
        ws_id = _open(tmp_path, "finish")
        assert _run(tmp_path, "close", ws_id).returncode == 0
        entry = _registry(tmp_path)["workstreams"][ws_id]
        assert entry["status"] == "done"
        assert entry["closed_ts"]

    def test_close_abandoned(self, tmp_path: Path):
        ws_id = _open(tmp_path, "drop")
        assert _run(tmp_path, "close", ws_id, "--status", "abandoned").returncode == 0
        assert _registry(tmp_path)["workstreams"][ws_id]["status"] == "abandoned"

    def test_close_rejects_bad_status(self, tmp_path: Path):
        ws_id = _open(tmp_path, "bad")
        r = _run(tmp_path, "close", ws_id, "--status", "finished")
        assert r.returncode != 0
        assert "done|abandoned" in r.stderr

    def test_prune_archives_terminal_and_drops_from_registry(self, tmp_path: Path):
        keep = _open(tmp_path, "keep active")
        gone = _open(tmp_path, "will close")
        _run(tmp_path, "close", gone)
        r = _run(tmp_path, "prune")
        assert r.returncode == 0
        reg = _registry(tmp_path)["workstreams"]
        assert keep in reg and gone not in reg
        archive = tmp_path / "workstreams-archive.jsonl"
        lines = [json.loads(x) for x in archive.read_text().splitlines() if x.strip()]
        assert len(lines) == 1 and lines[0]["id"] == gone

    def test_prune_noop_when_nothing_terminal(self, tmp_path: Path):
        _open(tmp_path, "active only")
        r = _run(tmp_path, "prune")
        assert r.returncode == 0
        assert not (tmp_path / "workstreams-archive.jsonl").exists()


class TestErrors:
    @pytest.mark.parametrize("cmd", ["progress", "renew", "block", "close"])
    def test_missing_id_fails(self, tmp_path: Path, cmd: str):
        extra = ["--note", "x"] if cmd == "renew" else []
        r = _run(tmp_path, cmd, "ws-nonexistent", *extra)
        assert r.returncode == 1
        assert "no such workstream" in r.stderr

    def test_unknown_subcommand_fails(self, tmp_path: Path):
        r = _run(tmp_path, "frobnicate")
        assert r.returncode != 0
        assert "unknown subcommand" in r.stderr


class TestConcurrency:
    def test_parallel_opens_mint_distinct_ids(self, tmp_path: Path):
        # The single-writer guarantee, regression-protected: N concurrent opens
        # under the lock mint N distinct ids and never lose an update (the
        # id-mint-under-lock TOCTOU fix — previously only manually observed).
        n = 20
        # Cap raised above n so this isolates id-minting, not the cap (the cap
        # holding under concurrency is covered by the sequential cap tests).
        hi_cap = {"WORKSTREAM_MAX_ACTIVE": "50"}
        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(
                lambda i: _run(tmp_path, "open", f"work item {i}", env_extra=hi_cap), range(n)
            ))
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results if r.returncode]
        ids = [r.stdout.strip() for r in results]
        assert len(set(ids)) == n, f"id collision under concurrency: {sorted(ids)}"
        assert len(_registry(tmp_path)["workstreams"]) == n, "lost update: fewer entries than opens"

    @pytest.mark.parametrize("cmd,extra", [
        ("progress", []), ("renew", ["--note", "n"]), ("block", []), ("close", []),
    ])
    def test_mutator_never_autovivifies_a_missing_id(self, tmp_path: Path, cmd, extra):
        # B1 regression: the existence check runs INSIDE the lock, so a mutator
        # on an absent id errors and leaves the registry untouched — jq never
        # runs `.workstreams[$id].x = y` on a missing key (which would create a
        # partial zombie: no id/status/title).
        keeper = _open(tmp_path, "keeper")
        r = _run(tmp_path, cmd, "ws-ghost", *extra)
        assert r.returncode != 0
        workstreams = _registry(tmp_path)["workstreams"]
        assert "ws-ghost" not in workstreams, f"{cmd} auto-vivified a zombie entry"
        assert set(workstreams) == {keeper}, f"{cmd} disturbed the registry"

    def test_concurrent_mutate_and_prune_stay_wellformed(self, tmp_path: Path):
        # Stress the exact B1 interleaving: race a mutator against prune on a
        # terminal entry. Whatever the ordering, every surviving entry must be
        # a full record (its map key equals its .id) — no status-only zombie.
        for _ in range(15):
            ws = _open(tmp_path, "racer")
            _run(tmp_path, "close", ws)
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(_run, tmp_path, "prune")
                f2 = ex.submit(_run, tmp_path, "renew", ws, "--note", "race")
                f1.result(); f2.result()
            for wid, entry in _registry(tmp_path)["workstreams"].items():
                assert entry.get("id") == wid, f"zombie entry {wid!r}: {entry}"


class TestBadEnvBounds:
    # M2/M3: the single writer validates its own bounds; a bad env must _die
    # before any mutation, not silently disable the cap or write an empty lease.
    @pytest.mark.parametrize("var", ["WORKSTREAM_MAX_ACTIVE", "WORKSTREAM_LEASE_DAYS"])
    @pytest.mark.parametrize("bad", ["lots", "-3", "0"])
    def test_non_positive_int_bound_dies(self, tmp_path: Path, var: str, bad: str):
        r = _run(tmp_path, "open", "t", env_extra={var: bad})
        assert r.returncode != 0
        assert "positive integer" in r.stderr or "must be >= 1" in r.stderr
        reg_file = tmp_path / "workstreams.json"
        if reg_file.exists():
            assert _registry(tmp_path)["workstreams"] == {}, "entry created despite bad bound"
