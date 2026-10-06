"""#2165: the plane's quarantine is listed, and its counted losses are shown.

Before this, `plane doctor` and `plane status` counted the quarantine and the
trust panel named its newest five; nothing listed the rest. And the
`.emit-losses` rows (#2169's `stage_empty` among them) were read by
`plane doctor` alone, which nothing schedules.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.plane_setup import initialize_plane


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root), *args],
        capture_output=True,
        text=True,
    )


def _json(r: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise AssertionError(f"not JSON (rc {r.returncode}): {r.stdout!r} {r.stderr!r}")


def _quarantine(
    root: Path,
    name: str,
    content: str,
    reason: str | None,
    written_ago: float,
    quarantined_ago: float | None = None,
) -> Path:
    q = root / "state" / "plane" / "spool" / "quarantine"
    q.mkdir(parents=True, exist_ok=True)
    entry = q / name
    entry.write_text(content)
    t = time.time() - written_ago
    os.utime(entry, (t, t))
    if reason is not None:
        sidecar = q / (name + ".reason")
        sidecar.write_text(reason + "\n")
        t = time.time() - (
            quarantined_ago if quarantined_ago is not None else written_ago
        )
        os.utime(sidecar, (t, t))
    return entry


EMPTY_REASON = "malformed batch on replay: Expecting value: line 1 column 1 (char 0)"


@pytest.fixture()
def plane(tmp_path: Path) -> Path:
    initialize_plane(tmp_path)
    return tmp_path


def _three(root: Path) -> None:
    _quarantine(root, "1-ev_a.batch.11.json", "", EMPTY_REASON, 3 * 3600, 2 * 3600)
    _quarantine(
        root,
        "2-ev_b.json",
        '{"events": [1]}\n',
        "contract violation on replay: x",
        3600,
        1800,
    )
    _quarantine(root, "ev_c.json", '{"requests": []}\n', None, 600)


def _tree(root: Path) -> list:
    spool = root / "state" / "plane" / "spool"
    return sorted(
        (str(p.relative_to(spool)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in spool.rglob("*")
    )


# --- the list door ------------------------------------------------------------


def test_the_quarantine_list_names_every_entry_with_its_times_reason_and_emptiness(
    plane,
):
    _three(plane)
    before = _tree(plane)

    r = _run(plane, "plane", "spool", "list", "--quarantined", "--json")

    out = _json(r)
    assert r.returncode == 0 and out["ok"], out
    items = out["data"]["items"]
    assert [i["name"] for i in items] == [
        "ev_c.json",
        "2-ev_b.json",
        "1-ev_a.batch.11.json",
    ]
    empty = items[2]
    assert empty["empty"] is True and empty["size"] == 0
    assert empty["reason"] == EMPTY_REASON
    assert empty["written_at"] < empty["quarantined_at"], empty
    assert items[1]["empty"] is False and items[1]["reason"].startswith(
        "contract violation"
    )
    assert items[0]["reason"] is None and items[0]["quarantined_at"] is None
    assert out["data"]["next_cursor"] is None
    assert out["data"]["coverage"] == {
        "state": "ok",
        "total": 3,
        "returned": 3,
        "vanished": 0,
        "reasons_unreadable": 0,
    }
    assert _tree(plane) == before, "listing the quarantine changed the store"


def test_the_quarantine_list_pages_with_a_cursor_bound_to_its_root(
    plane, tmp_path_factory
):
    _three(plane)

    first = _json(
        _run(plane, "plane", "spool", "list", "--quarantined", "--limit", "2", "--json")
    )
    assert [i["name"] for i in first["data"]["items"]] == ["ev_c.json", "2-ev_b.json"]
    cursor = first["data"]["next_cursor"]
    assert cursor

    second = _json(
        _run(
            plane,
            "plane",
            "spool",
            "list",
            "--quarantined",
            "--limit",
            "2",
            "--cursor",
            cursor,
            "--json",
        )
    )
    assert [i["name"] for i in second["data"]["items"]] == ["1-ev_a.batch.11.json"]
    assert second["data"]["next_cursor"] is None

    other = tmp_path_factory.mktemp("other")
    initialize_plane(other)
    _three(other)
    moved = _json(
        _run(
            other,
            "plane",
            "spool",
            "list",
            "--quarantined",
            "--cursor",
            cursor,
            "--json",
        )
    )
    assert not moved["ok"] and moved["error"]["code"] == "invalid_argument", moved


def test_an_unreadable_quarantine_is_unavailable_never_an_empty_list(plane):
    q = plane / "state" / "plane" / "spool" / "quarantine"
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text("a file where the quarantine directory belongs")

    out = _json(_run(plane, "plane", "spool", "list", "--quarantined", "--json"))

    assert not out["ok"] and out["error"]["code"] == "unavailable", out


def test_the_pending_list_is_unchanged_without_the_flag(plane):
    _three(plane)
    out = _json(_run(plane, "plane", "spool", "list", "--json"))
    assert out["ok"] and out["data"] == {"entries": []}, out


# --- the counted losses -------------------------------------------------------


def _losses(root: Path, *rows: str) -> Path:
    path = root / "state" / "plane" / ".emit-losses"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(row + "\n" for row in rows))
    return path


def _rows(now: int) -> list[str]:
    return [
        f"{now - 60}\treap\temit_fleet_event\tbound=10s",
        f"{now - 120}\treap\temit_fleet_event\tbound=10s",
        f"{now - 30}\tstage_empty\t-\t.1-ev_x.batch.9.tmp",
        f"{now - 200}\tstaged_full\tbackground\t2000 batches",
        f"{now - 90000}\tstage_empty\t-\t.0-ev_old.batch.8.tmp",
    ]  # past the 24 h window


def test_the_loss_summary_counts_known_losses_in_the_window_apart_from_reaps(tmp_path):
    from claudlobby.plane.health import emit_losses_summary

    now = 2_000_000_000
    _losses(tmp_path, *_rows(now))
    s = emit_losses_summary(tmp_path, now)
    assert s["state"] == "ok" and s["window_s"] == 86400
    assert s["reaped"] == 2 and s["reap_doors"] == ["emit_fleet_event"]
    assert s["not_recorded"] == {"stage_empty": 1, "staged_full": 1}
    assert s["not_recorded_total"] == 2


def test_an_absent_loss_counter_is_zero_and_an_unreadable_one_is_withheld(tmp_path):
    from claudlobby.plane.health import emit_losses_summary

    absent = emit_losses_summary(tmp_path, 2_000_000_000)
    assert (
        absent["state"] == "ok"
        and absent["reaped"] == 0
        and absent["not_recorded_total"] == 0
    )
    (tmp_path / "state" / "plane" / ".emit-losses").mkdir(parents=True)
    unreadable = emit_losses_summary(tmp_path, 2_000_000_000)
    assert unreadable["state"] == "unreadable"
    assert unreadable["reaped"] is None and unreadable["not_recorded"] is None
    assert unreadable["not_recorded_total"] is None


def test_plane_status_carries_the_emit_losses(plane):
    _losses(plane, *_rows(int(time.time())))
    out = _json(_run(plane, "plane", "status", "--json"))
    assert out["ok"], out
    block = out["data"]["emit_losses"]
    assert block["state"] == "ok" and block["reaped"] == 2
    assert block["not_recorded"] == {"stage_empty": 1, "staged_full": 1}


def test_plane_doctor_counts_only_the_window_it_names(plane):
    """The rung says "in the last 24h" and read every row in the file. It now
    reads the shared summary, so a row past the window no longer counts."""
    _losses(plane, *_rows(int(time.time())))
    out = _json(_run(plane, "plane", "doctor", "--json"))
    rung = next(r for r in out["data"]["rungs"] if r["name"] == "emit losses")
    assert rung["status"] == "attention"
    assert rung["detail"].startswith(
        "2 reaped emit(s) in the last 24h — doors: emit_fleet_event."
    ), rung
    assert rung["detail"].endswith(
        "; 2 emit(s) NOT recorded (stage_empty, staged_full) — see state/plane/.emit-losses"
    ), rung
