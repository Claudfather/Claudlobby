"""F18 closure R3 — the cutover machinery is gone: no `plane cutover`, no
`plane parity`, no `plane import`, no doctor rung about flags or declarations.
The plane is the only source, so there is no flag to read and no rollback
lever to lose. The two epochs the transition RECORDED stay registered — the
estate's rows must keep their severity."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.plane_fixtures import F, _cli, _scene, ro

CLI = Path(sys.executable).parent / "claudlobby"


def test_the_three_doors_are_unknown_subcommands(tmp_path):
    root, _paths, _, _ = _scene(tmp_path)
    for door in ("cutover", "parity", "import"):
        r = _cli(root, door)
        assert r.returncode == 2 and "invalid choice" in r.stderr, (door, r.returncode, r.stderr[-300:])
    assert "cutover" not in subprocess.run([str(CLI), "plane", "--help"], capture_output=True,
                                           text=True).stdout


def _old_epochs(root):
    """The rows the transition recorded on an estate, as the deleted door landed them."""
    out = emit_batch(root, [
        {"event_type": "system", "emitter": "cutover", "fleet": F,
         "payload": {"event": "cutover_declared", "subject_kind": "fleet", "subject": F,
                     "data": {"reader": "open", "forced": None, "shadowed": False}}},
        {"event_type": "system", "emitter": "cutover", "fleet": F,
         "payload": {"event": "legacy_write_retired", "subject_kind": "fleet", "subject": F,
                     "data": {"flags": {"dispatch": "PLANE_LEGACY_WRITE_DISPATCH=0"}}}}])
    assert all(o.status == "committed" for o in out), out


def test_the_recorded_epochs_still_classify_and_the_doctor_carries_no_rung_for_them(tmp_path):
    root, _paths, _, _ = _scene(tmp_path)
    _old_epochs(root)
    (root / "home").mkdir(exist_ok=True)
    with ro(root) as conn:
        sev = dict(conn.execute("SELECT event, severity FROM events WHERE kind = 'system'"
                                " AND event IN ('cutover_declared', 'legacy_write_retired')").fetchall())
    assert sev == {"cutover_declared": "notice", "legacy_write_retired": "notice"}
    assert SYSTEM_EVENT_SEVERITY["cutover_declared"] == "notice"
    d = _cli(root, "doctor")
    assert "cutover" not in d.stdout.lower() and "legacy write" not in d.stdout.lower(), d.stdout
