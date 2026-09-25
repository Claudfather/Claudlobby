"""alert_delivery_failed leads with WHY nothing was delivered (#1771).

The row's detail is tg-post's captured output, cut to 300 characters. In a timer
env there is no FLEET_NAME, so tg-post first announces that it is skipping its
own plane record — and a manager read that benign line as the cause of the
failure, when the real rejection came second. The verdict now leads.

End to end, so the benign line comes from the real code rather than from the
fixture: the throwaway root's lib/ IS the repo's lib/ (the real tg-post and
lib-common), a stub curl answers the rejection a quoted token earned in
production (404 Not Found), the env is constructed with no FLEET_NAME, and the
row is read back from the root's own plane.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    _write_exec,
    constructed_env,
    read_fleet_events,
)

REPO = Path(__file__).resolve().parent.parent

CURL_REJECTS = (
    "#!/bin/bash\n"
    'printf \'%s\' \'{"ok":false,"error_code":404,"description":"Not Found"}\'\n'
)


def _host(tmp_path, *, scratch_plane_env):
    root = tmp_path / "root"
    root.mkdir()
    # The real lib, never written to: the alert path runs ${CLAUDLOBBY_ROOT}/lib/tg-post.sh.
    (root / "lib").symlink_to(REPO / "lib")
    chan = tmp_path / "chan"
    chan.mkdir()
    (chan / ".env").write_text("TELEGRAM_BOT_TOKEN=123:ABC\n")
    bot = root / "runtime" / "bots" / "tbot"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text(
        'export TELEGRAM_GROUP_CHAT_ID="-1001234567890"\n'
        f'export TELEGRAM_STATE_DIR="{chan}"\n'
    )
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_exec(stubs / "curl", CURL_REJECTS)
    env = constructed_env(
        PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
        HOME=tmp_path / "home",
        # Every event is a cold emit with no daemon; a loaded host can outrun
        # the 10s production bound and reap the row this test reads.
        FLEET_EVENT_EMIT_TIMEOUT_S="120",
        **scratch_plane_env(root),
    )
    return root, env


def _fire(root, env):
    driver = (
        f'. "{REPO}/lib/lib-common.sh"; '
        f'emit_failure_alert "{root}/runtime/bots" probe_alert "a probe"'
    )
    return subprocess.run(
        ["bash", "-c", driver], env=env, capture_output=True, text=True, timeout=300
    )


def test_the_rejection_leads_the_recorded_detail(tmp_path, *, scratch_plane_env):
    root, env = _host(tmp_path, scratch_plane_env=scratch_plane_env)
    r = _fire(root, env)
    assert r.returncode == 0, r.stderr

    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    failed = [row for row in rows if row["type"] == "alert_delivery_failed"]
    assert len(failed) == 1, rows
    data = failed[0]["data"]
    detail = data["detail"]
    assert data["exit"] == 3, data
    # Precondition: the real tg-post DID announce the benign skip, so this test
    # exercises the reorder rather than an output that never had the problem.
    assert "FLEET_NAME is empty" in detail, detail
    assert detail.startswith("tg-post: send REJECTED"), detail
    assert "error: Not Found" in detail.split("FLEET_NAME is empty")[0], detail


def _failed_row(root):
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    failed = [row["data"] for row in rows if row["type"] == "alert_delivery_failed"]
    assert len(failed) == 1, rows
    return rows, failed[0]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_token_file_is_a_verdict_not_a_script_error(tmp_path, *, scratch_plane_env):
    # The shared parser returns non-zero when it cannot open the file; unguarded
    # inside tg-post's command substitution that tripped the ERR trap, landing
    # critical script_error rows for what is simply a missing token.
    root, env = _host(tmp_path, scratch_plane_env=scratch_plane_env)
    token_file = tmp_path / "chan" / ".env"
    token_file.chmod(0)
    try:
        _fire(root, env)
    finally:
        token_file.chmod(0o600)
    rows, data = _failed_row(root)
    assert "script_error" not in [row["type"] for row in rows], rows
    assert data["exit"] == 1, data
    assert data["detail"].startswith("tg-post: no TELEGRAM_BOT_TOKEN"), data["detail"][:120]
