"""A wrong queue node must not certify an empty drain (#1934 hosted failure)."""

from claudlobby.plane.queue_paths import scan_queue_dir, scan_spool, spool_path


def test_queue_inventory_distinguishes_absence_from_wrong_or_redirected_nodes(tmp_path):
    queue = spool_path(tmp_path)
    queue.parent.mkdir(parents=True)
    assert scan_queue_dir(queue)[0].state == "absent"
    assert scan_spool(tmp_path).spool_state == "ok"
    queue.write_text("cannot enumerate this queue")
    scan = scan_spool(tmp_path)
    assert scan.spool_state == scan.quarantine_state == "unreadable"
    assert scan.pending == []
    queue.unlink()
    target = tmp_path / "elsewhere"
    target.mkdir()
    queue.symlink_to(target, target_is_directory=True)
    assert scan_queue_dir(queue)[0].state == "unreadable"
    queue.unlink()
    queue.mkdir()
    pending = queue / "pending.json"
    pending.write_text("pending")
    scan = scan_spool(tmp_path)
    assert scan.spool_state == scan.quarantine_state == "ok"
    assert scan.pending == [pending]
