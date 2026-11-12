"""#2145 Task 9b: `plane/fleet_events.fleet_event_request` is the one Python spelling
of the row bash `emit_fleet_event` writes — pinned byte for byte against the real
bash function (shell functions shadow the clocks, the host name and the emit), in
the style of tests/test_plane_session_hook.py's derivation parity."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from claudlobby.plane.contracts import validate_request
from claudlobby.plane.fleet_events import fleet_event_request
from claudlobby.plane.ids import ID_PATTERNS, derive_hex

LIB = Path(__file__).resolve().parent.parent / "claudlobby" / "_runtime_scripts" / "lib-common.sh"
T = "2026-10-05T00:00:00Z"


def _bash_row(tmp_path: Path, call: str, fleet: str | None) -> dict:
    capture = tmp_path / "batch.json"
    script = (
        f'. "{LIB}"\n'
        "plane_armed() { return 0; }\n"
        f"ts_iso() {{ printf '{T}'; }}\n"
        f"date() {{ printf '{T}\\n'; }}\n"
        "hostname() { printf 'h1'; }\n"
        'plane_emit_bounded() { printf \'%s\' "$3" > "$CAPTURE"; }\n'
        f"{call}\n"
    )
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "CAPTURE": str(capture)}
    if fleet is not None:
        env["FLEET_NAME"] = fleet
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    batch = json.loads(capture.read_text())
    assert len(batch["events"]) == 1
    return batch["events"][0]


@pytest.mark.parametrize("call, fleet, kwargs", [
    # actor anchor: an explicit bot id, no bot dir
    ("""emit_fleet_event vault_sync test-src '{"a":1}' "" bot1""", "f1",
     dict(fleet="f1", subject_kind="actor", subject="bot:f1/bot1", bot="bot1")),
    # fleet anchor: bot dir and bot id empty
    ("""emit_fleet_event vault_sync test-src '{"a":1}' "" "" """, "f1",
     dict(fleet="f1", subject_kind="fleet", subject="f1", bot="fleet")),
    # host anchor: no fleet anywhere
    ("""emit_fleet_event vault_sync test-src '{"a":1}' "" "" """, None,
     dict(fleet="_host", subject_kind="host", subject="h1", bot="host")),
], ids=["actor", "fleet", "host"])
def test_the_python_row_is_the_bash_row_byte_for_byte(tmp_path, call, fleet, kwargs):
    bash = _bash_row(tmp_path, call, fleet)
    python = fleet_event_request("vault_sync", source="test-src", data={"a": 1},
                                 occurred_at=T, legacy_ts=T, **kwargs)
    assert re.fullmatch(ID_PATTERNS["event"], python.pop("event_id"))  # bash mints none; the daemon does
    assert python == bash
    # The bot is derived from the anchor when the caller omits it.
    derived = fleet_event_request("vault_sync", source="test-src", data={"a": 1}, occurred_at=T,
                                  legacy_ts=T, **{k: v for k, v in kwargs.items() if k != "bot"})
    derived.pop("event_id")
    assert derived == bash


def _legacy_line_statement() -> tuple[str, str]:
    """emit_fleet_event's `printf -v _line` statement, found by name — never by line
    number (open branches shift lib-common.sh)."""
    text = LIB.read_text()
    body = text[text.index("\nemit_fleet_event() {"):]
    body = body[:body.index("\n}\n")]
    match = re.search(r"printf -v _line '([^']*)' \\\n\s*(.*)\n", body)
    assert match, "emit_fleet_event no longer composes its legacy line with printf -v _line"
    return match.group(1), match.group(2)


def test_source_ref_is_derive_hex_of_the_legacy_line():
    fmt, args = _legacy_line_statement()
    assert args.split() == ['"$ts"', '"$bot_id"', '"$event_type"', '"$event_source"', '"$data_json"']
    legacy = fmt % (T, "bot1", "vault_sync", "test-src", json.dumps({"a": 1}, separators=(",", ":")))
    assert legacy == '{"ts":"%s","bot":"bot1","type":"vault_sync","source":"test-src","data":{"a":1}}' % T
    row = fleet_event_request("vault_sync", fleet="f1", subject_kind="actor", subject="bot:f1/bot1",
                              bot="bot1", source="test-src", data={"a": 1}, occurred_at=T, legacy_ts=T)
    assert row["source_ref"] == "fleet-events:sha:" + derive_hex(legacy)


def test_a_caller_may_supply_its_own_key():
    row = fleet_event_request("fleet_notice", fleet="f1", subject_kind="fleet", subject="f1",
                              source="fleet-notify", data={}, key="req-123")
    assert row["source_ref"] == "fleet-events:sha:" + derive_hex("req-123")
    assert row["emitter"] == "fleet-notify"


def test_observed_at_rides_only_when_given():
    base = dict(fleet="f1", subject_kind="fleet", subject="f1", source="test-src", data={"a": 1},
                occurred_at=T, legacy_ts=T)
    assert "observed_at" not in fleet_event_request("vault_sync", **base)
    row = fleet_event_request("vault_sync", observed_at="2026-10-05T00:05:00Z", **base)
    assert row["observed_at"] == "2026-10-05T00:05:00Z" and row["occurred_at"] == T
    env, _payload = validate_request(row)
    assert env.observed_at is not None


def test_the_default_clock_is_utc_in_the_form_bash_stamps():
    row = fleet_event_request("vault_sync", fleet="f1", subject_kind="fleet", subject="f1",
                              source="test-src", data={})
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["occurred_at"])
    assert row["payload"]["data"]["legacy_ts"] == row["occurred_at"]
    validate_request(row)
