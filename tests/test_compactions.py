"""#2206 proposal 2: each Claude Code compaction becomes one plane event.

A compaction leaves one ``compact_boundary`` row in the bot's session
transcript: its trigger, the context before it and, once it ends, after it.
The row's keys come from a live 2.1 transcript; every value below is invented.
The recorder reads what each declared bot's transcripts appended since its
cursor and emits each row once. These tests drive it with an emitter that
answers as the plane does, then through a real scratch plane, the CLI door,
and the pulse that runs the door.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import _scrubbed_env, read_fleet_events, write_jsonl

REPO = Path(__file__).resolve().parent.parent
FLEET_PULSE = REPO / "claudlobby/_runtime_scripts" / "fleet-pulse.sh"


def _recorder():
    # Reached by name in each test, so a run without the module fails each
    # test on its own line instead of failing collection.
    return importlib.import_module("claudlobby.compactions")


def _boundary(ts, pre, post, uuid, *, trigger="manual", session="s1"):
    meta = {"trigger": trigger, "preTokens": pre}
    if post is not None:
        meta["postTokens"] = post
    return {"parentUuid": None, "logicalParentUuid": "p0", "isSidechain": False,
            "type": "system", "subtype": "compact_boundary",
            "content": "Conversation compacted", "level": "info", "compactMetadata": meta,
            "uuid": uuid, "timestamp": ts, "sessionId": session}


def _said(ts, text="go on", session="s1"):
    return {"type": "user", "isSidechain": False, "sessionId": session, "timestamp": ts,
            "message": {"role": "user", "content": text}}


def _fleet(tmp_path, *bots):
    fleet = SimpleNamespace(name="f", bots={b: SimpleNamespace(account=b) for b in bots},
                            accounts={b: str(tmp_path / "acct" / b) for b in bots})
    paths = SimpleNamespace(root=tmp_path, fleet_state=tmp_path / "fleet-state",
                            bot_runtime=lambda b: tmp_path / "runtime" / b)
    return fleet, paths


def _transcripts(tmp_path, paths, bot):
    from claudlobby.isolation import transcript_slug

    directory = tmp_path / "acct" / bot / "projects" / transcript_slug(paths.bot_runtime(bot))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _append(path, *rows):
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _pre(plane):
    return [event["payload"]["data"]["data"]["pre_tokens"] for event in plane.sent]


class Plane:
    """An emitter that keeps each event it is handed and answers as the plane
    does: an event id it already holds is a duplicate, stored once."""

    def __init__(self):
        self.sent, self.stored = [], {}

    def __call__(self, raws):
        outcomes = []
        for raw in raws:
            self.sent.append(raw)
            status = "duplicate" if raw["event_id"] in self.stored else "committed"
            self.stored.setdefault(raw["event_id"], raw)
            outcomes.append(SimpleNamespace(status=status))
        return outcomes


def _bot(result, name):
    return next(row for row in result["bots"] if row["bot"] == name)


def test_a_boundary_row_becomes_one_compaction_event_with_its_tokens_and_time(tmp_path):
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_said("2026-10-06T20:30:00Z"),
                                 _boundary("2026-10-06T20:33:29.519Z", 503386, 17666, "u1"),
                                 _said("2026-10-06T20:33:30Z")])
    plane = Plane()
    result = rec.record_compactions(paths, fleet, emit=plane)
    assert len(plane.sent) == 1
    event = plane.sent[0]
    assert event["event_type"] == "system" and event["fleet"] == "f"
    assert event["occurred_at"] == "2026-10-06T20:33:29.519000+00:00"
    # `event list` serves only fleet-events provenance (#1643): any other
    # source_ref would leave the event invisible to the caller it is for.
    assert event["source_ref"].startswith("fleet-events:")
    payload = event["payload"]
    assert (payload["event"], payload["subject_kind"], payload["subject"]) == (
        "compaction", "actor", "bot:f/b")
    assert payload["data"]["source"] == "compactions"
    assert payload["data"]["legacy_ts"] == "2026-10-06T20:33:29.519000+00:00"
    assert payload["data"]["data"] == {"trigger": "manual", "pre_tokens": 503386,
                                       "post_tokens": 17666, "session": "s1"}
    row = _bot(result, "b")
    assert (row["recorded"], row["reason"]) == (1, None)


def test_each_row_is_emitted_once_across_passes(tmp_path):
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_boundary("2026-10-06T15:39:04Z", 250174, 18456, "u1")])
    plane = Plane()
    rec.record_compactions(paths, fleet, emit=plane)
    rec.record_compactions(paths, fleet, emit=plane)      # nothing appended: nothing sent
    assert _pre(plane) == [250174]
    _append(d / "s1.jsonl", _said("2026-10-06T16:00:00Z"),
            _boundary("2026-10-06T17:12:34Z", 348328, 19254, "u2"))
    rec.record_compactions(paths, fleet, emit=plane)
    assert _pre(plane) == [250174, 348328]
    # A lost cursor reads the rows again under the same ids: the plane holds
    # each one once.
    (paths.fleet_state / rec.CURSOR_FILE).unlink()
    rec.record_compactions(paths, fleet, emit=plane)
    assert len(plane.sent) == 4 and len(plane.stored) == 2


def test_after_the_first_pass_only_the_appended_bytes_are_read(tmp_path):
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_said("2026-10-06T20:00:00Z", "x" * 10_000)])
    plane = Plane()
    first = _bot(rec.record_compactions(paths, fleet, emit=plane), "b")
    assert first["bytes_read"] == (d / "s1.jsonl").stat().st_size
    line = json.dumps(_boundary("2026-10-06T20:33:29Z", 1000, 10, "u1")) + "\n"
    with open(d / "s1.jsonl", "a", encoding="utf-8") as fh:
        fh.write(line)
    second = _bot(rec.record_compactions(paths, fleet, emit=plane), "b")
    assert second["bytes_read"] == len(line.encode())
    assert second["recorded"] == 1


def test_a_line_still_being_written_waits_for_the_next_pass(tmp_path):
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    d = _transcripts(tmp_path, paths, "b")
    line = json.dumps(_boundary("2026-10-06T20:33:29Z", 1000, 10, "u1"))
    (d / "s1.jsonl").write_text(json.dumps(_said("2026-10-06T20:00:00Z")) + "\n" + line[:40])
    plane = Plane()
    rec.record_compactions(paths, fleet, emit=plane)
    assert plane.sent == []
    with open(d / "s1.jsonl", "a", encoding="utf-8") as fh:
        fh.write(line[40:] + "\n")
    rec.record_compactions(paths, fleet, emit=plane)
    assert _pre(plane) == [1000]


def test_a_restart_is_followed_into_the_new_session_and_the_old_one_finished(tmp_path):
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_said("2026-10-06T20:00:00Z")])
    plane = Plane()
    rec.record_compactions(paths, fleet, emit=plane)
    _append(d / "s1.jsonl", _boundary("2026-10-06T20:10:00Z", 600000, 20000, "u1"))
    write_jsonl(d / "s2.jsonl", [_boundary("2026-10-06T20:20:00Z", 700000, 21000, "u2",
                                           session="s2")])
    rec.record_compactions(paths, fleet, emit=plane)
    assert sorted(e["payload"]["data"]["data"]["session"] for e in plane.sent) == ["s1", "s2"]


def test_before_the_first_pass_only_the_newest_transcript_is_read(tmp_path):
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "old.jsonl", [_boundary("2026-10-01T00:00:00Z", 1, 1, "u0", session="old")])
    os.utime(d / "old.jsonl", (1_000, 1_000))
    write_jsonl(d / "s1.jsonl", [_boundary("2026-10-06T20:00:00Z", 2, 2, "u1")])
    plane = Plane()
    rec.record_compactions(paths, fleet, emit=plane)
    rec.record_compactions(paths, fleet, emit=plane)
    assert [e["payload"]["data"]["data"]["session"] for e in plane.sent] == ["s1"]


def test_an_unreadable_transcript_records_nothing_and_says_why(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b", "gone")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_boundary("2026-10-06T20:00:00Z", 2, 2, "u1")])
    (d / "s1.jsonl").chmod(0)
    plane = Plane()
    try:
        result = rec.record_compactions(paths, fleet, emit=plane)
    finally:
        (d / "s1.jsonl").chmod(0o600)
    assert plane.sent == []
    assert _bot(result, "b")["reason"] == "unreadable_transcript_file"
    assert _bot(result, "gone")["reason"] == "transcript_directory_missing_or_untrusted"
    # Nothing was passed over: once it reads, the row is recorded.
    rec.record_compactions(paths, fleet, emit=plane)
    assert _pre(plane) == [2]


def test_a_failed_emit_moves_no_cursor(tmp_path):
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_boundary("2026-10-06T20:00:00Z", 2, 2, "u1")])

    def refused(raws):
        raise OSError("the plane is not there")

    failed = rec.record_compactions(paths, fleet, emit=refused)
    assert "the plane is not there" in failed["error"]
    plane = Plane()
    rec.record_compactions(paths, fleet, emit=plane)
    assert _pre(plane) == [2]


def test_bytes_past_the_pass_cap_are_skipped_and_counted(tmp_path):
    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_said("2026-10-06T20:00:00Z")])
    plane = Plane()
    rec.record_compactions(paths, fleet, emit=plane, read_cap=4096)
    _append(d / "s1.jsonl", _boundary("2026-10-06T20:10:00Z", 9, 9, "u-lost"),
            _said("2026-10-06T20:11:00Z", "y" * 8000),
            _boundary("2026-10-06T20:20:00Z", 7, 7, "u-kept"))
    row = _bot(rec.record_compactions(paths, fleet, emit=plane, read_cap=4096), "b")
    assert row["skipped_bytes"] > 0
    assert _pre(plane) == [7]


def test_events_land_on_the_plane_once_and_read_back_as_fleet_events(tmp_path):
    from claudlobby.plane.db import connect, db_file
    from claudlobby.plane.emit_api import emit_batch
    from tests.plane_setup import initialize_plane

    rec = _recorder()
    fleet, paths = _fleet(tmp_path, "b")
    initialize_plane(tmp_path)
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_boundary("2026-10-06T20:33:29.519Z", 503386, 17666, "u1")])

    def plane(raws):
        return emit_batch(tmp_path, raws, conn_factory=lambda: connect(db_file(tmp_path)))

    rec.record_compactions(paths, fleet, emit=plane)
    (paths.fleet_state / rec.CURSOR_FILE).unlink()      # read the row a second time
    rec.record_compactions(paths, fleet, emit=plane)
    rows = [json.loads(line) for line in read_fleet_events(tmp_path).splitlines()
            if '"type":"compaction"' in line]
    assert len(rows) == 1
    assert rows[0]["bot"] == "b"
    assert rows[0]["data"] == {"trigger": "manual", "pre_tokens": 503386,
                               "post_tokens": 17666, "session": "s1"}


def test_the_door_records_for_the_selected_fleet_and_names_each_bot(tmp_path, monkeypatch):
    from claudlobby import activation_state
    from claudlobby.commands import checkin

    door = importlib.import_module("claudlobby.commands.compactions_record")
    fleet, paths = _fleet(tmp_path, "b", "gone")
    d = _transcripts(tmp_path, paths, "b")
    write_jsonl(d / "s1.jsonl", [_boundary("2026-10-06T20:00:00Z", 2, 2, "u1")])
    selected = {"release_id": "r1"}
    monkeypatch.setattr(checkin, "_scope", lambda _args: (
        SimpleNamespace(paths=paths, fleet=fleet), None, selected, None))
    monkeypatch.setattr(activation_state, "read_selection", lambda _root: selected)
    plane = Plane()
    monkeypatch.setattr("claudlobby.plane.emit_api.emit_batch", lambda root, raws: plane(raws))
    out = door.dispatch(SimpleNamespace(public_command="fleet.compactions.record"))
    assert [row["bot"] for row in out.data["bots"]] == ["b", "gone"]
    assert _bot(out.data, "gone")["reason"] == "transcript_directory_missing_or_untrusted"
    assert _pre(plane) == [2]


def _stub_cli(tmp_path):
    log = tmp_path / "cli-argv.log"
    stub = tmp_path / "bin" / "claudlobby"
    stub.parent.mkdir()
    stub.write_text("#!/bin/bash\n"
                    f"printf '%s\\n' \"$*\" >> '{log}'\n"
                    "echo 'stub refusal' >&2\n"
                    "exit \"${STUB_RC:-0}\"\n")
    stub.chmod(0o755)
    return stub, log


@pytest.mark.parametrize("rc", [0, 6], ids=["recorded", "refused"])
def test_each_pulse_records_the_fleets_compactions_through_the_cli(tmp_path, rc):
    root, fleet = tmp_path / "root", "cfleet"
    bot = root / "local" / fleet / "runtime" / "bots" / "solo"
    (bot / "data").mkdir(parents=True)
    (bot / "bot.conf").write_text("TMUX_SOCKET=compactions2206-none\n")
    (root / "local" / fleet / "fleet.yaml").write_text(
        "fleet:\n  manager: solo\n  bots:\n    solo:\n")
    stub, log = _stub_cli(tmp_path)
    env = _scrubbed_env(HOME=str(root / "home"), CLAUDLOBBY_ROOT=str(root),
                        CLAUDLOBBY_CLI=str(stub), STUB_RC=str(rc), PLANE_EMIT_DISABLED="1",
                        TMUX_TMPDIR=os.environ["TMPDIR"])
    proc = subprocess.run(["bash", str(FLEET_PULSE), fleet], env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr          # a refusal never stops the sweep
    assert (f"--root {root} --fleet {fleet} --json fleet compactions record"
            in log.read_text().splitlines())
    if rc:
        assert "compactions not recorded" in proc.stderr and "stub refusal" in proc.stderr
