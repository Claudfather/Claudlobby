"""Git backup/restore invariants retained from the retired activation suite."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from claudlobby import activation_state as state
from claudlobby.plane.db import db_file
from tests.fixtures.current_runtime import current_runtime  # noqa: F401

@pytest.fixture
def git_backup_host(current_runtime, monkeypatch):
    """Existing private DB and snapshot, with a real isolated runtime-use lock."""
    from uuid import uuid4
    from claudlobby import host_backup
    from claudlobby.installation import write_installation, install_launcher
    root, _, _, host = current_runtime
    package, snapshot = host.package, host.snapshot
    write_installation(package.installation)
    install_launcher(package.installation, interpreter=Path(sys.executable).resolve())
    request_id = str(uuid4())
    update = {"schema": 1, "installation": str(package.installation.directory), "data_root": str(root),
        "request_id": request_id, "before": snapshot["revision"], "target": "b" * 40,
        "config_fingerprint": "c" * 64, "scope": {}, "quiet": {}, "phase": "quiesce",
        "maintenance": False, "git_complete": False,
        "dependencies": {"path": "requirements-runtime.lock", "before": "d" * 64, "target": "d" * 64,
                         "prepared": True, "receipt_sha256": None},
        "consumers": {"example/worker": {"checkpoint_operation": request_id, "checkpoint": None,
                                            "stop": None, "handoff_outcome": "unknown"}}}
    path = root / "state/update.json"
    path.write_text(json.dumps(update, indent=2) + "\n")
    path.chmod(0o600)
    monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", str(root / "host-system.yaml"))
    return root, package, snapshot, update


@pytest.mark.parametrize("with_update", [True, False])
def test_git_backup_restore_keeps_authoring_and_inert_original_recovery_evidence(git_backup_host, tmp_path, with_update):
    from claudlobby.host_backup import create_backup, inspect_backup, restore_backup, restore_status
    from claudlobby.plane.owner_source import bind_source, inspect_source, SourceNeedsBinding
    root, package, snapshot, update = git_backup_host
    bind_source(root)
    original_config = (root / "state/config.json").read_bytes()
    original_update = (root / "state/update.json").read_bytes()
    if not with_update:
        (root / "state/update.json").unlink()
    authored = b"pending authoring is distinct from committed fleet text\n"
    (root / "fleet.yaml").write_bytes(authored)
    (root / "library").mkdir()
    (root / "library/recovery.md").write_text("Observed local authoring\n")
    (root / "host-system.yaml").write_text("host: {jobs: {}}\n")
    (root / ".env").write_text("EXAMPLE_SECRET=excluded\n")
    (root / "state/requests").mkdir(exist_ok=True)
    (root / "state/requests/excluded.json").write_text('{"status":"unknown"}')
    bundle, restored = tmp_path / "git-backup", tmp_path / "replacement"
    manifest = create_backup(root, bundle, package=package)
    assert manifest["schema"] == 2 and manifest["release_id"] is None and manifest["plan_id"] is None
    assert manifest["configuration_provenance"] == "authored_at_backup"
    assert manifest["source_revision"] == snapshot["revision"]
    assert manifest["config_fingerprint"] == snapshot["fingerprint"]
    assert (bundle / "configuration/fleet.yaml").read_bytes() == authored
    assert (bundle / "recovery/config.json").read_bytes() == original_config
    if with_update:
        assert (bundle / "recovery/update.json").read_bytes() == original_update
    else:
        assert not (bundle / "recovery/update.json").exists()
    assert all(".env" not in name and "requests" not in name for name in manifest["files"])
    # Inspection/restore never relies on the original host still being present.
    root.rename(root.with_name(root.name + "-retired"))
    assert inspect_backup(bundle) == manifest
    marker = restore_backup(bundle, restored, replacing_retired_host=True)
    assert marker["schema"] == 2
    assert (restored / "fleet.yaml").read_bytes() == authored
    assert (restored / "library/recovery.md").read_text() == "Observed local authoring\n"
    assert not (restored / ".env").exists()
    assert not (restored / "state/config.json").exists() and not (restored / "state/update.json").exists()
    assert (restored / "state/restored-backup/config.json").read_bytes() == original_config
    if with_update:
        assert (restored / "state/restored-backup/update.json").read_bytes() == original_update
        retained = json.loads(original_update)
        assert retained["request_id"] == update["request_id"]
        assert retained["consumers"]["example/worker"]["handoff_outcome"] == "unknown"
    else:
        assert not (restored / "state/restored-backup/update.json").exists()
    status = restore_status(restored)
    assert status["runtime_reinstallation_required"] and status["update_resumed"] is False
    assert status["access_restored"] is False
    with pytest.raises(SourceNeedsBinding):
        inspect_source(restored)
    with pytest.raises(state.ActivationRefusal, match="new absolute"):
        restore_backup(bundle, restored, replacing_retired_host=True)


@pytest.mark.parametrize("changed", ["config", "update", "authoring"])
def test_git_backup_refuses_changes_during_sqlite_copy(git_backup_host, tmp_path, monkeypatch, changed):
    from claudlobby import active_config, host_backup
    root, package, snapshot, _ = git_backup_host
    backup = host_backup._backup
    def copy_then_change(*args):
        backup(*args)
        if changed == "config":
            following = deepcopy(snapshot)
            following["fleets"]["example"]["fleet"] += "\n# Next configuration\n"
            following["fingerprint"] = active_config._fingerprint(following)
            active_config.publish_current_config(following, installation=package.installation,
                                                 expected_previous=snapshot["fingerprint"])
        else:
            path = root / ("state/update.json" if changed == "update" else "fleet.yaml")
            path.write_bytes(path.read_bytes() + b"\n")
    monkeypatch.setattr(host_backup, "_backup", copy_then_change)
    destination = tmp_path / "changed-backup"
    with pytest.raises(state.ActivationRefusal, match="changed while being copied"):
        host_backup.create_backup(root, destination, package=package)
    assert not destination.exists()


def test_git_backup_refuses_schema_mismatch_and_tampered_inert_record(git_backup_host, tmp_path):
    from claudlobby.host_backup import create_backup, inspect_backup, restore_backup
    from claudlobby.runtime_versions import SQL_SCHEMA_VERSION
    root, package, _, _ = git_backup_host
    with sqlite3.connect(db_file(root)) as conn:
        conn.execute(f"PRAGMA user_version={SQL_SCHEMA_VERSION + 1}")
    bundle = tmp_path / "git-backup"
    with pytest.raises(state.ActivationRefusal, match="runtime schema"):
        create_backup(root, bundle, package=package)
    assert not bundle.exists()
    with sqlite3.connect(db_file(root)) as conn:
        conn.execute(f"PRAGMA user_version={SQL_SCHEMA_VERSION}")
    manifest = create_backup(root, bundle, package=package)
    manifest["database_schema"] += 1
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(state.ActivationRefusal, match="matching the recorded database schema"):
        restore_backup(bundle, tmp_path / "replacement", replacing_retired_host=True)
    (bundle / "recovery/update.json").write_bytes(b"{}\n")
    with pytest.raises(state.ActivationRefusal, match="digest"):
        inspect_backup(bundle)
