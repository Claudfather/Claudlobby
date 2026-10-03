"""A refreshed bot whose capture is old still reaches its IDs at boot (#2094).

The refresh no longer makes an old handoff resume. The IDs it adopted reach a
booting bot through the boot brief (#2049), the canonical door for them.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3

from claudlobby.activation_handoffs import persist_canonical_handoffs
from claudlobby.plane.db import db_file
from claudlobby.task_audit import audit_tasks
from tests.package_fixtures import source_package
from tests.test_activation import cold, tmp_path  # noqa: F401 — short private activation root
from tests.test_activation_handoffs import STALE_HANDOFF, _resume_gate
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_read_cli import active  # noqa: F401 — active private Plane fixture
from tests.test_task_state import _assignment, _task


def test_a_bot_refreshed_with_an_old_capture_reaches_its_assignment_ids_at_boot(  # noqa: F811
        active, capsys, monkeypatch):
    from claudlobby import brief, env_tiers
    from claudlobby.__main__ import main
    from claudlobby.activation_identity import read_selected_identity_bindings
    from claudlobby.paths import load_lib_module

    root, host = active
    bindings = read_selected_identity_bindings(root, "example", package=host.package)
    with sqlite3.connect(db_file(root)) as conn:
        _task(conn, "wi_refreshed", fleet_uid=bindings["fleet_uid"])
        conn.execute("UPDATE work_items SET project_key=NULL, workstream_id=NULL "
                     "WHERE work_item_id='wi_refreshed'")
        _assignment(conn, "asg_refreshed", "wi_refreshed", fleet_uid=bindings["fleet_uid"])
        conn.execute("UPDATE assignments SET assignee_uid=? WHERE assignment_id='asg_refreshed'",
                     (bindings["bots"]["worker"],))
        expected = asdict(audit_tasks(conn))
    bot_dirs = {("example", bot): root / "runtime/bots" / bot for bot in ("manager", "worker")}
    for directory in bot_dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    handoff = bot_dirs["example", "worker"] / ".claude/session.md"
    handoff.parent.mkdir(exist_ok=True)
    handoff.write_bytes(STALE_HANDOFF)

    persist_canonical_handoffs(root, roster={"example": ("manager", ("manager", "worker"))},
                               bot_dirs=bot_dirs, expected_audit=expected)
    assert _resume_gate(handoff) == 1  # the 2020 capture stays too old to resume

    monkeypatch.setattr(brief, "load_dispatch_doors",
                        lambda paths: load_lib_module(source_package().native, "dispatch-overdue.py"))
    monkeypatch.setattr(env_tiers, "resolve", lambda paths, bot_name=None, fleet_name=None: {})
    for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
                       "FLEET_NAME": "example", "BOT_ID": "worker",
                       "BOT_DIR": str(bot_dirs["example", "worker"])}.items():
        monkeypatch.setenv(key, value)
    assert main(["--root", str(root), "--json", "brief"]) == 0
    work = json.loads(capsys.readouterr().out)["data"]["brief"]["work"]
    assert [(item["task_id"], (item["assignment"] or {}).get("assignment_id"))
            for item in work["items"]] == [("wi_refreshed", "asg_refreshed")]
