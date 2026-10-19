"""#2243: the stop door's local record, and the unit-file fact it is read beside.

The unit file says THAT a bot is stopped (`svc_is_registered`, the fact activation,
keepalive and fleet-pulse read); the record answers whether the stop was meant. fleet-pulse
reads the record with sed, so its shape is pinned here: one JSON line, keys sorted, `by` a
plain alias. Python reads the unit-file fact through `unit_installed`, a mirror of the bash
predicate, and the parity test below holds the two together on the same HOME.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from pathlib import Path

import pytest

from claudlobby import stop_record

LIB = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts"


def _bot(tmp_path):
    bot = tmp_path / "bot"
    (bot / "data").mkdir(parents=True)
    return bot


def test_a_written_record_reads_back_and_is_one_sorted_json_line(tmp_path):
    bot = _bot(tmp_path)
    written = stop_record.write_stop(bot, by="bot:f/m", reason="parked", request_id="r-1")
    raw = (bot / "data" / ".stopped").read_text()
    assert raw.endswith("\n") and raw.count("\n") == 1
    assert raw == json.dumps(written, sort_keys=True) + "\n"
    assert stop_record.read_stop(bot) == written
    assert written["by"] == "bot:f/m" and written["reason"] == "parked"
    assert abs(written["stopped_epoch"] - time.time()) < 60
    assert written["stopped_at"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(written["stopped_epoch"]))
    assert (bot / "data" / ".stopped").stat().st_mode & 0o077 == 0
    assert stop_record.clear_stop(bot) is True and stop_record.read_stop(bot) is None
    assert stop_record.clear_stop(bot) is False


def test_a_missing_data_directory_is_created(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    stop_record.write_stop(bot, by="operator", reason=None, request_id="r-2")
    assert stop_record.read_stop(bot)["by"] == "operator"


@pytest.mark.parametrize("by", ["", "has space", 'quo"te', "x" * 200])
def test_by_is_a_plain_alias_the_sweep_can_read(tmp_path, by):
    with pytest.raises(stop_record.StopRecordError):
        stop_record.write_stop(_bot(tmp_path), by=by, reason=None, request_id="r")


@pytest.mark.parametrize("reason", ["two\nlines", "tab\there", "x" * 2001])
def test_a_reason_is_one_printable_line(tmp_path, reason):
    with pytest.raises(stop_record.StopRecordError):
        stop_record.write_stop(_bot(tmp_path), by="operator", reason=reason, request_id="r")


def test_a_symlinked_data_directory_is_refused(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (bot / "data").symlink_to(elsewhere)
    with pytest.raises(stop_record.StopRecordError):
        stop_record.write_stop(bot, by="operator", reason=None, request_id="r")
    assert list(elsewhere.iterdir()) == []


def test_an_unreadable_record_still_answers_that_a_stop_was_recorded(tmp_path):
    """The sweep keeps a bot silent on the file's presence; a reader that cannot parse it
    must not turn a recorded stop into no stop."""
    bot = _bot(tmp_path)
    (bot / "data" / ".stopped").write_text("not json\n")
    assert stop_record.read_stop(bot) == {}


@pytest.mark.skipif(platform.system() != "Linux", reason="the systemd branch of the predicate")
@pytest.mark.parametrize("installed", [True, False])
def test_unit_installed_is_svc_is_registered(tmp_path, installed):
    home = tmp_path / "home"
    label = "com.t.parity2243"
    if installed:
        unit = home / ".config/systemd/user" / f"{label}.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\n")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
           "PLANE_EMIT_DISABLED": "1", "LANG": "C.UTF-8"}
    rc = subprocess.run(["bash", "-c", f'. "{LIB}/lib-common.sh"; svc_is_registered "$1" "$2"', "_",
                         str(tmp_path), label], env=env, capture_output=True, text=True, timeout=60).returncode
    assert (rc == 0) is installed
    assert stop_record.unit_installed(label, home=home) is installed
