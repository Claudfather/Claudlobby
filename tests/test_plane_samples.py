"""`claudlobby plane samples` (#1644): one metric family for one subject over a
window, through the read-only connection, with the plane released before
anything prints (the #1905/#1912 rule)."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch

WINDOW = ("--since", "2026-09-29T07:30:00-04:00", "--until", "2026-09-29T07:47:30-04:00")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _host_sample(root: Path, at: str, metric: str, value, host: str = "probe-host") -> None:
    emit_batch(root, [{
        "event_type": "metric_sample", "emitter": "host-probe", "fleet": "_host", "occurred_at": at,
        "payload": {"subject_kind": "host", "subject": host, "metric": metric, "value": value}}])


@pytest.fixture
def plane(tmp_path: Path) -> Path:
    root = _root(tmp_path)
    for hhmm, mem, load in (("11:29", 5000, 3.0), ("11:30", 4800, 4.0), ("11:40", 900, 20.0),
                            ("11:47", 150, 56.0), ("11:49", 6000, 2.0)):
        at = f"2026-09-29T{hhmm}:06.500000+00:00"
        _host_sample(root, at, "host.mem_available_mb", mem)
        _host_sample(root, at, "host.load", {"one": load, "five": load / 2, "fifteen": load / 4})
    return root


def _cli(root: Path, *argv: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDLOBBY_ROOT", "FLEET_NAME", "BOT_ID")}
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root), "plane", "samples", *argv],
                          capture_output=True, text=True, timeout=120, env=env)


def test_a_window_prints_its_samples_oldest_first_bounds_included(plane: Path) -> None:
    run = _cli(plane, "host.mem_available_mb", *WINDOW)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert lines[0].startswith("host.mem_available_mb (MB) for host probe-host,") and lines[0].endswith(": 3 sample(s)")
    assert lines[1:] == ["  2026-09-29T11:30:06Z  4800", "  2026-09-29T11:40:06Z  900", "  2026-09-29T11:47:06Z  150"]
    edge = _cli(plane, "host.mem_available_mb", "--since", "2026-09-29T11:30:06.5Z", "--until", "2026-09-29T11:30:06.5Z")
    assert edge.stdout.splitlines()[1:] == ["  2026-09-29T11:30:06Z  4800"]


def test_an_object_value_reads_as_pairs_and_json_keeps_it_whole(plane: Path) -> None:
    run = _cli(plane, "host.load", *WINDOW)
    assert run.stdout.splitlines()[-1] == "  2026-09-29T11:47:06Z  one=56.0 five=28.0 fifteen=14.0"
    out = json.loads(_cli(plane, "host.load", *WINDOW, "--json").stdout)
    assert (out["metric"], out["kind"], out["subject"], out["unit"]) == ("host.load", "host", "probe-host", "load")
    assert [s["value"]["one"] for s in out["samples"]] == [4.0, 20.0, 56.0]


def test_an_empty_window_is_an_answer(plane: Path) -> None:
    run = _cli(plane, "host.mem_available_mb", "--since", "2026-09-28T00:00:00Z", "--until", "2026-09-28T01:00:00Z")
    assert run.returncode == 0
    assert run.stdout.splitlines() == [run.stdout.splitlines()[0]] and run.stdout.rstrip().endswith(": 0 sample(s)")


def test_instants_are_compared_as_times_across_offsets(tmp_path: Path) -> None:
    # Ingest stores the offset it was given, so the window must compare times,
    # not text: 07:40-04:00 is 11:40Z and inside, 08:10-04:00 is 12:10Z and out.
    root = _root(tmp_path)
    _host_sample(root, "2026-09-29T07:40:00-04:00", "host.mem_available_mb", 111)
    _host_sample(root, "2026-09-29T11:35:00+00:00", "host.mem_available_mb", 222)
    _host_sample(root, "2026-09-29T08:10:00-04:00", "host.mem_available_mb", 333)
    run = _cli(root, "host.mem_available_mb", "--since", "2026-09-29T11:30:00Z", "--until", "2026-09-29T12:00:00Z")
    assert run.stdout.splitlines()[1:] == ["  2026-09-29T11:35:00Z  222", "  2026-09-29T11:40:00Z  111"]


@pytest.mark.parametrize(
    ("argv", "rc", "says"),
    [
        (("host.nope", *WINDOW), 2, "unknown metric 'host.nope'"),
        (("host.load", "--subject", "other-host", *WINDOW), 2, "no host subject named 'other-host'; recorded: probe-host"),
        (("host.load", "--since", "2026-09-29T12:00:00Z", "--until", "2026-09-29T11:00:00Z"), 2, "is after --until"),
        (("host.load", "--until", "yesterday"), 2, "cannot parse --until 'yesterday'"),
        (("bot.heartbeat", *WINDOW), 2, "name the subject's --kind"),
    ],
)
def test_a_bad_request_is_refused_with_a_reason(plane: Path, argv, rc, says) -> None:
    run = _cli(plane, *argv)
    assert run.returncode == rc and says in run.stderr, (run.returncode, run.stderr)


def test_two_hosts_and_no_subject_names_both(plane: Path) -> None:
    _host_sample(plane, "2026-09-29T11:31:00+00:00", "host.load", {"one": 1.0, "five": 1.0, "fifteen": 1.0}, host="b-host")
    run = _cli(plane, "host.load", *WINDOW)
    assert run.returncode == 2 and "name one with --subject: b-host, probe-host" in run.stderr
    assert _cli(plane, "host.load", "--subject", "b-host", *WINDOW).stdout.rstrip().endswith("11:31:00Z  one=1.0 five=1.0 fifteen=1.0")


def test_an_unreachable_plane_refuses_and_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "no-plane"
    run = _cli(root, "host.load")
    assert run.returncode == 3 and "the plane cannot answer: no plane db" in run.stderr
    assert not db_file(root).exists() and not (root / "state").exists()


def test_the_read_changes_nothing_in_the_plane(plane: Path) -> None:
    before = hashlib.sha256(db_file(plane).read_bytes()).hexdigest()
    assert _cli(plane, "host.load", *WINDOW).returncode == 0
    assert hashlib.sha256(db_file(plane).read_bytes()).hexdigest() == before


def test_the_plane_is_released_before_anything_prints(plane: Path, monkeypatch) -> None:
    import claudlobby.commands.plane as plane_cmd

    real_open_ro = plane_cmd.open_ro
    state = {"closed": False, "writes_while_open": 0}

    class Watched:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args):
            return self._conn.execute(*args)

        def close(self):
            state["closed"] = True
            self._conn.close()

    def watched_open_ro(root, **kwargs):
        conn, why = real_open_ro(root, **kwargs)
        return (Watched(conn) if conn is not None else None), why

    class Stdout(io.StringIO):
        def write(self, text):
            if text and not state["closed"]:
                state["writes_while_open"] += 1
            return super().write(text)

    out = Stdout()
    monkeypatch.setattr(plane_cmd, "open_ro", watched_open_ro)
    monkeypatch.setattr(sys, "stdout", out)
    args = argparse.Namespace(root=str(plane), fleet=None, seed=False, metric="host.mem_available_mb", subject=None,
                              kind=None, since="2026-09-29T11:00:00Z", until="2026-09-29T12:00:00Z", json=False)
    assert plane_cmd.cmd_plane_samples(args) == 0
    assert state["closed"] and state["writes_while_open"] == 0
    assert out.getvalue().splitlines()[0].endswith(": 5 sample(s)")
