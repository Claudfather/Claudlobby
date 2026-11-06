"""Bounded Git initialization/update, coordinated by one UpdateStore.

The stable runner owns process replacement and the exclusive maintenance fence.
This application-side owner prepares, saves/quiets, publishes and restores the
frozen consumer roster. Explicit initialization alone may create a new database;
existing database migrations remain outside this operation.
"""
from __future__ import annotations

import ast
import hashlib
import os
import stat
import tempfile
import platform
from pathlib import Path

from .active_config import context_from_plan, read_current_config, snapshot_from_plan, publish_current_config
from .activation_identity import (read_registry_identity_bindings, identity_bindings_from_registry,
                                  preflight_registry_scan, scan_registry)
from .config_install import prepare_publication, apply_publication
from .config_plan import read_rendered
from .git_update import UpdateError, _git, _head, _sync, assert_conversion_retired
from .installation import install_launcher, install_updater
from .plane.db import db_file, connect_ro
from .runtime_versions import SQL_SCHEMA_VERSION
from .supervision_inventory import _catalog
from .update_services import (
    freeze_service_intent, consumer_scope, validate_candidate_consumers,
    quiesce_consumer, observe_quiet, restore_consumer, native_state_matches,
)


def _operation(store, *phases):
    store._assert_locked()
    record = store.load()
    if record is None or (phases and record["phase"] not in phases):
        raise UpdateError("update operation is not in the required phase")
    if "candidate_config" not in record or "consumers" not in record:
        raise UpdateError("update application intent is incomplete; resume preparation")
    return record


def _schema(store, revision, *, initialize=False):
    """Read declared target version without importing candidate code."""
    source = _git(store.installation, "show", f"{revision}:claudlobby/runtime_versions.py").stdout
    try:
        tree = ast.parse(source)
        values = [ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                     and target.id == "SQL_SCHEMA_VERSION" for target in node.targets)]
    except (SyntaxError, ValueError, TypeError) as exc:
        raise UpdateError("candidate SQL schema declaration is unavailable") from exc
    if len(values) != 1 or type(values[0]) is not int:
        raise UpdateError("candidate SQL schema declaration is ambiguous")
    if values[0] != SQL_SCHEMA_VERSION:
        raise UpdateError("SQL schema change requires the canonical migration owner before this update")
    if initialize:
        return values[0]
    path = db_file(store.root)
    if not path.is_file() or path.is_symlink():
        raise UpdateError("existing Plane database is unavailable; update cannot initialize it")
    conn = connect_ro(path)
    try:
        actual = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    if values[0] != SQL_SCHEMA_VERSION or actual != SQL_SCHEMA_VERSION:
        raise UpdateError("SQL schema change requires the canonical migration owner before this update")
    return actual


def _initial_database(store, record):
    """An initialized final DB must be the inode this operation created, never a foreign DB."""
    progress = record.get("initialization", {}).get("database")
    if not isinstance(progress, dict) or progress.get("state") not in {"ready", "published"}:
        raise UpdateError("initialization found an unowned Plane database")
    path = db_file(store.root)
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            or (info.st_dev, info.st_ino) != (progress["device"], progress["inode"])):
        raise UpdateError("initialization Plane database differs from its owned staged file")
    return progress


def _fresh_root(store, record=None, *, committed=False):
    """Reject adoption/recovery evidence, while preserving validated owner preparation."""
    from .plane.owner_source import authority_only_plane
    root = store.root
    state = root / "state"
    if root.resolve() != root or state.resolve() != state:
        raise UpdateError("initialization data root is redirected")
    config = state / "config.json"
    if os.path.lexists(config):
        if not committed or read_current_config(root, installation=store.installation) != record["candidate_config"]:
            raise UpdateError("initialization requires no existing active configuration")
    for name in ("selected-release.json", "restored-backup.json", "restored-backup", "activations", "migrations"):
        path = state / name
        if os.path.lexists(path):
            raise UpdateError("initialization refuses existing activation or recovery evidence")
    if not os.path.lexists(db_file(root)):
        authority_only_plane(root, allow_absent=True)
        if record and record.get("initialization", {}).get("database", {}).get("state") == "published":
            raise UpdateError("initialization owned Plane database disappeared")
        return
    if record is None:
        raise UpdateError("initialization requires no existing Plane database")
    _initial_database(store, record)
    directory = db_file(root).parent
    allowed = {"plane.db", "plane.db-wal", "plane.db-shm", "owner-access.db",
               "owner-access.db-wal", "owner-access.db-shm", "owner-access.db-journal"}
    if (directory.resolve() != directory or directory.stat().st_uid != os.getuid()
            or stat.S_IMODE(directory.stat().st_mode) != 0o700):
        raise UpdateError("initialization Plane storage changed")
    for path in directory.iterdir():
        info = path.lstat()
        if (path.name not in allowed or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise UpdateError("initialization refuses foreign Plane data or queues")
    if (directory / "owner-access.db").exists():
        from .plane.owner_access import OwnerAccess
        access = OwnerAccess(root)
        # Only a published DB proven above to belong to this same committing
        # initialization may retain already-retired first-owner preparation.
        resume_owner = (committed and record.get("initialize") is True
                        and record.get("phase") == "commit"
                        and record["initialization"]["database"]["state"] == "published")
        access.validate_storage(allow_retired_first_fleet=resume_owner)
        if resume_owner and not access.first_fleet_pending():
            from .plane.owner_source import inspect_source
            inspect_source(root)  # No re-attestation on an interrupted completion.


def conversion_baseline(store, package, *, require_authored=True):
    """Re-read only the exact original sealed evidence of this current operation."""
    from .update_conversion import read_sealed_baseline
    from .config_plan import path_state
    record = store.load()
    scope = record.get('conversion_scope') if record is not None else None
    if scope is None:
        raise UpdateError('sealed conversion has no original scope')
    retirement = record.get('conversion_selector')
    archived = None
    if retirement is not None:
        path = Path(retirement['path'])
        if path.exists() and not (store.state / 'selected-release.json').exists():
            archived = {key: retirement[key] for key in ('path', 'sha256')}
    baseline = read_sealed_baseline(store.root, package=package,
        expected_selection=scope['selection'], archived_selector=archived)
    if baseline.selector_sha256 != scope['selector_sha256'] or baseline.evidence != scope['evidence']:
        raise UpdateError('original sealed conversion evidence changed')
    if require_authored:
        authored = getattr(baseline, "authored_plan", None) or baseline.plan
        for sources in authored.effects['fleet_sources'].values():
            for item in sources.values():
                path = Path(item['path'])
                if path_state(path, source=True) != authored.inputs[str(path)]['state']:
                    raise UpdateError('authored configuration differs from the frozen sealed baseline; review it before conversion')
    return baseline


def prepare_operation(store, rendered, adapter, install_directory, interpreter, request_id, *, initialize=False):
    """Prepare immutable inputs/dependencies; explicit initialization writes no schema here."""
    store._assert_locked()
    if type(initialize) is not bool:
        raise UpdateError("initialize scope must be explicit boolean intent")
    existing = store.load()
    if existing is not None and existing["phase"] != "done":
        if ((existing["target"], existing["request_id"]) != (rendered.revision, request_id)
                or existing.get("finalized", True)
                and existing["config_fingerprint"] != rendered.fingerprint
                or "candidate_config" in existing and existing.get("initialize", False) != initialize):
            raise UpdateError("continue the existing frozen update operation")
        if existing["phase"] != "prepare":
            return _operation(store)
    elif initialize and existing is not None:
        raise UpdateError("initialization refuses prior update recovery evidence")
    rendered.check_inputs()
    baseline = conversion_baseline(store, adapter.package) if existing is not None and existing.get('conversion_scope') else None
    if baseline is not None and initialize:
        raise UpdateError('sealed conversion cannot initialize existing storage')
    if initialize:
        if not rendered.fleets:
            raise UpdateError("initialization requires a frozen first-team fleet")
        _fresh_root(store)
        current = None
        preflight_registry_scan(context_from_plan(rendered, fleet, package=adapter.package)
                                for fleet in rendered.fleets)
        identities = None
    elif baseline is not None:
        current = None
        from .composer import account_dir
        for fleet, old in baseline.contexts.items():
            new = context_from_plan(rendered, fleet, package=adapter.package)
            if (old.paths.fleet_yaml != new.paths.fleet_yaml or old.fleet.service_prefix != new.fleet.service_prefix
                    or set(old.fleet.bots) != set(new.fleet.bots)
                    or any((old.fleet.bots[name].agent_cli, account_dir(old.fleet.bots[name], old.fleet))
                           != (new.fleet.bots[name].agent_cli, account_dir(new.fleet.bots[name], new.fleet))
                           for name in old.fleet.bots)):
                raise UpdateError("sealed conversion cannot change native session identity/account bindings")
        identities = read_registry_identity_bindings(rendered, package=adapter.package)
        if identities != baseline.identities:
            raise UpdateError('sealed conversion cannot change existing identities')
    else:
        current = read_current_config(store.root, installation=store.installation)
        identities = read_registry_identity_bindings(rendered, package=adapter.package)
    manager, domain, _, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
    if manager != platform.system():
        raise UpdateError("native manager differs from current host")
    placement = {'manager': manager, 'domain': baseline.domain if baseline is not None else domain,
                 'directory': str(install_directory)}
    if baseline is not None and (baseline.manager != manager or baseline.domain != domain or baseline.install_directory != install_directory):
        raise UpdateError('sealed conversion cannot change native placement')
    if current is not None and current.get("supervision") not in (None, placement):
        raise UpdateError("native install directory differs from committed placement")
    candidate = snapshot_from_plan(rendered, store.installation, rendered.revision,
                                   identities=identities, supervision=placement)
    validate_candidate_consumers(current, candidate, platform.system(), baseline=baseline)
    if current is not None and current["identities"] != candidate["identities"]:
        raise UpdateError("identity changes require an explicit configuration scope")
    schema = _schema(store, rendered.revision, initialize=True) if initialize else _schema(store, rendered.revision)
    previous = current["fingerprint"] if current is not None else None
    if existing is not None and existing["phase"] == "prepare" and "consumers" in existing:
        record = existing
        if record["candidate_config"] != candidate or record["previous_config"] != previous:
            raise UpdateError("prepared effective configuration changed")
    else:
        rows = freeze_service_intent(current, adapter, install_directory,
                                     candidate_snapshot=candidate, rendered=rendered, baseline=baseline)
        scope = consumer_scope(rows)
        record = store.prepare(rendered.revision, config_fingerprint=rendered.fingerprint,
                               scope=scope, request_id=request_id)
        record.update(candidate_config=candidate, previous_config=previous, initialize=initialize,
                      sql_schema=schema, consumers={row["target"]: {"intent": row} for row in rows})
        store.save(record)
    prepare_publication(rendered, store)
    store.prepare_dependencies(interpreter=interpreter)
    record = store.load()
    record["phase"] = "quiesce"
    store.save(record)
    return record


def _schedule(row):
    return row["target"].endswith(".timer") or row["manager"] == "Darwin" and row["scheduled"]


def _recorder(row):
    return row["phase"] == "ingest"


def _checkpoint(store, key, adapter):
    from .bot_operations import save_planned_checkpoint
    record = store.load()
    progress = record["consumers"][key]
    row = progress["intent"]
    # A persisted save is not proof that a still-running bot did no later work.
    # Re-enter the shared owner before an unattempted stop: observe the same
    # process-bound native session without sending a model request.
    if progress.get("checkpoint_operation") is None:
        progress["checkpoint_operation"] = record["request_id"]
        store.save(record)
    directory, label, tmpdir = row["bot_binding"]
    progress["checkpoint_state"] = "waiting_for_native_idle"
    store.save(record)
    options = {}
    if record.get("conversion_scope") is not None:
        from .active_config import context_from_plan
        baseline = conversion_baseline(store, adapter.package)
        declaration = row["declaration"]
        options["context"] = context_from_plan(baseline.plan, declaration["fleet"],
                                               bot=declaration["bot"], package=adapter.package)
    proof = save_planned_checkpoint(Path(directory), label, tmpdir, adapter,
                                    operation_id=progress["checkpoint_operation"], **options)
    # Interrupted observation resumes the same operation without model sends.
    record = store.load()
    record["consumers"][key]["checkpoint"] = proof
    record["consumers"][key]["checkpoint_state"] = "native_session_verified"
    store.save(record)


def check_conversion_before_retirement(store, adapter):
    """Read-only last check while the old selector and both legacy EX locks remain."""
    record = _operation(store, "quiesce")
    baseline = conversion_baseline(store, adapter.package)
    rendered = read_rendered(store.root, record["config_fingerprint"])
    rendered.check_inputs()
    rows = freeze_service_intent(None, adapter, Path(record["native_install_directory"]),
                                 candidate_snapshot=record["candidate_config"], rendered=rendered, baseline=baseline)
    # Scheduled one-shots can finish while legacy EX drains. Use exactly the
    # stop owner's state rule, then compare every other binding byte unchanged.
    from copy import deepcopy
    rows = deepcopy(rows)
    for row in rows:
        original = record["consumers"].get(row["target"], {}).get("intent")
        if original is not None and original.get("scheduled") and row.get("scheduled"):
            if not native_state_matches(original, row["saved_native"]):
                raise UpdateError("sealed conversion native intent changed since preview; original selector retained")
            row["saved_native"] = original["saved_native"]
            row["restore_native"] = original["restore_native"]
            row["properties"] = [(key, dict(original["properties"])[key] if key == "ActiveState" else value)
                                 for key, value in row["properties"]]
    if consumer_scope(rows) != record["scope"]:
        raise UpdateError("sealed conversion native intent changed since preview; original selector retained")


def quiesce_operation(store, adapter):
    """Schedules first; keep the recorder available until all bots save/stop."""
    record = _operation(store, "quiesce")
    store.verify_dependencies()
    if record.get("initialize", False):
        _fresh_root(store)
    elif record.get('conversion_scope') is not None:
        assert_conversion_retired(store)
        if not any(item.get('stop') or item.get('park') for item in record['consumers'].values()):
            conversion_baseline(store, adapter.package)
    elif read_current_config(store.root, installation=store.installation)["fingerprint"] != record["previous_config"]:
        raise UpdateError("effective configuration changed before quiescence")
    if not any(item.get("stop") for item in record["consumers"].values()):
        read_rendered(store.root, record["config_fingerprint"]).check_inputs()
    def order(key):
        row = record["consumers"][key]["intent"]
        return (0 if _schedule(row) else 1 if row["scheduled"] else
                2 if row["bot_binding"] else 4 if _recorder(row) else 3, key)
    for key in sorted(record["consumers"], key=order):
        progress = store.load()["consumers"][key]
        row = progress["intent"]
        if progress.get("stop") == "done":
            continue
        if (row.get("kind") != "added" and row["bot_binding"] and row["disposition"] == "running"
                and progress.get("stop") is None):
            _checkpoint(store, key, adapter)
        quiesce_consumer(store, key, adapter)
    return store.load()


def assert_operation_quiet(store, adapter):
    """Caller holds EX; capture proof and native quiet are freshly rechecked."""
    from .bot_operations import verify_checkpoint_after_stop
    record = _operation(store, "quiesce")
    if record.get('conversion_scope') is not None:
        assert_conversion_retired(store)
    for key, progress in record["consumers"].items():
        row = progress["intent"]
        if progress.get("stop") != "done":
            raise UpdateError("consumer stop has not completed")
        if row.get("kind") != "added" and row["bot_binding"] and row["disposition"] == "running":
            if progress.get("checkpoint") is None:
                raise UpdateError("running bot lacks its original checkpoint proof")
            verify_checkpoint_after_stop(Path(row["bot_binding"][0]), progress["checkpoint"])
        observe_quiet(store, key, adapter)
    if record.get("initialize", False):
        _fresh_root(store)
        actual_schema = _schema(store, record["target"], initialize=True)
    else:
        actual_schema = _schema(store, record["target"])
    if actual_schema != record["sql_schema"]:
        raise UpdateError("Plane schema changed during quiescence")
    return store.load()


def _initialize_database(store):
    """Migrate only an owned staged DB, then atomically publish without overwrite."""
    from .plane.db import connect, db_path
    from .plane.migrations import migrate
    record = store.load()
    _fresh_root(store, record, committed=True)
    progress = record.setdefault("initialization", {}).get("database")
    if progress is None:
        store.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        if store.work.resolve() != store.work:
            raise UpdateError("initialization staging directory is redirected")
        fd, name = tempfile.mkstemp(prefix="initialize-plane-", suffix=".db", dir=store.work)
        try:
            info = os.fstat(fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        _sync(store.work)
        progress = {"path": name, "device": info.st_dev, "inode": info.st_ino, "state": "created"}
        record["initialization"]["database"] = progress
        store.save(record)  # Durable ownership precedes canonical schema writes.
    staged = Path(progress["path"])
    if staged.parent != store.work or not staged.name.startswith("initialize-plane-"):
        raise UpdateError("initialization staged database path changed")
    if progress["state"] != "published":
        info = staged.lstat()
        if (staged.resolve() != staged or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()
                or (info.st_dev, info.st_ino) != (progress["device"], progress["inode"])):
            raise UpdateError("initialization staged database changed")
        if progress["state"] == "created":
            conn = connect(staged)
            try:
                if migrate(conn) != record["sql_schema"] or conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise UpdateError("initialization schema did not complete")
            finally:
                conn.close()
            _sync(staged)
            progress.update(state="ready", sha256=hashlib.sha256(staged.read_bytes()).hexdigest())
            store.save(record)
        if hashlib.sha256(staged.read_bytes()).hexdigest() != progress["sha256"]:
            raise UpdateError("initialization staged schema bytes changed")
        final = db_path(store.root)
        try:
            os.link(staged, final)
        except FileExistsError:
            _initial_database(store, record)
        _sync(final.parent)
        progress["state"] = "published"
        store.save(record)
    _initial_database(store, record)
    if os.path.lexists(staged):
        if staged.is_symlink() or not os.path.samefile(staged, db_file(store.root)):
            raise UpdateError("initialization staged database identity changed")
        staged.unlink()
        _sync(store.work)
    if _schema(store, record["target"]) != record["sql_schema"]:
        raise UpdateError("initialized Plane schema changed")


def _initialize_identities(store, rendered):
    """A pending scan is re-observed, never blindly sent again after interruption."""
    from .resources import get_resources
    from .operation_context import OperationContextError
    from .activation_state import ActivationError
    from .plane.registry_emit import RegistryScanError
    from .plane.ids import ensure_host_uid
    record = store.load()
    progress = record["initialization"].get("registry")
    if progress is not None and progress.get("state") == "complete":
        return
    package = get_resources(installation=store.installation)
    if progress is None:
        # Existing owner preparation may already own the host UID. The canonical
        # minter validates/preserves it; this coordinator never creates IDs itself.
        ensure_host_uid(store.root / "state")
        record["initialization"]["registry"] = {"state": "pending"}
        store.save(record)
        try:
            rows = scan_registry(rendered, package=package)
        except RegistryScanError as exc:
            record = store.load()
            record["initialization"]["registry"]["summary"] = exc.summary
            store.save(record)
            raise
        identities = identity_bindings_from_registry(rendered, rows, package=package)
    else:
        try:
            identities = read_registry_identity_bindings(rendered, package=package)
        except (ActivationError, OperationContextError) as exc:
            raise UpdateError("initial registry scan remains unverified; original unknown scan is not resent") from exc
    candidate = snapshot_from_plan(rendered, store.installation, record["target"], identities=identities,
                                   supervision=record["candidate_config"]["supervision"])
    # Identity capture alone may refine the prepared snapshot after schema creation.
    before = dict(candidate, identities=None)
    before["fingerprint"] = record["candidate_config"]["fingerprint"]
    if before != record["candidate_config"]:
        raise UpdateError("initial configuration changed while binding registry identities")
    record["candidate_config"] = candidate
    record["initialization"]["registry"]["state"] = "complete"
    store.save(record)


def publish_operation(store):
    """Commit the effective snapshot before native restoration is admitted."""
    record = _operation(store, "environment", "publish", "commit", "restore")
    if record["phase"] == "restore":
        current = read_current_config(store.root, installation=store.installation, revision=record["target"])
        if current != record["candidate_config"]:
            raise UpdateError("committed configuration differs from update")
        return record
    if (not record["maintenance"] or not record["git_complete"]
            or not record["dependencies"].get("environment_complete")
            or _head(store.installation) != record["target"]):
        raise UpdateError("publication requires verified target code/environment under maintenance")
    if record.get("initialize", False):
        _fresh_root(store, record, committed=True)
        actual_schema = _schema(store, record["target"], initialize=True)
    else:
        actual_schema = _schema(store, record["target"])
    if actual_schema != record["sql_schema"]:
        raise UpdateError("Plane schema changed before publication")
    if record.get('conversion_scope') is not None:
        assert_conversion_retired(store)
    rendered = read_rendered(store.root, record['config_fingerprint'])
    if record['phase'] in {'environment', 'publish'}:
        record["phase"] = "publish"
        store.save(record)
        apply_publication(rendered, store)
        record = store.load()
        record["phase"] = "commit"
        store.save(record)
    if record.get("initialize", False):
        _initialize_database(store)
        # The initializer has just proved this operation's published DB inode;
        # bind existing first-owner preparation before any registry/native writer.
        from .plane.owner_source import complete_first_fleet_source
        complete_first_fleet_source(store.root)
        _initialize_identities(store, rendered)
        record = store.load()
    interpreter = Path(record["host_interpreter"])
    install_launcher(store.installation, interpreter=interpreter)
    install_updater(store.installation, interpreter=interpreter)
    publish_current_config(record["candidate_config"], installation=store.installation,
                           expected_previous=record["previous_config"])
    record = store.load()
    record.update(phase="restore", maintenance=False, config_committed=True)
    store.save(record)
    return record


def restore_operation(store, adapter):
    """Record per-consumer health without revoking the committed cutover."""
    record = _operation(store, "restore", "done")
    current = read_current_config(store.root, installation=store.installation, revision=record["target"])
    if not record.get("config_committed") or current != record["candidate_config"]:
        raise UpdateError("restore requires the exact committed configuration")
    if record["phase"] == "done" and not record.get("health_errors"):
        return record
    def order(key):
        row = record["consumers"][key]["intent"]
        return (3 if _schedule(row) else 2 if row["bot_binding"] else 0 if _recorder(row) else 1, key)
    for key in sorted(record["consumers"], key=order):
        progress = store.load()["consumers"][key]
        if progress.get("restore") == "done" and not progress.get("restore_error"):
            continue
        try:
            # A bot this update left paused stays a health error until it runs
            # again or its stop is recorded, so every pass reads it again (#2699).
            if progress.get("restore") != "done" or progress["intent"].get("preserved_mask"):
                restore_consumer(store, key, adapter, current)
            if progress.get("checkpoint") is not None:
                from .bot_operations import retire_planned_checkpoint
                retire_planned_checkpoint(Path(progress["intent"]["bot_binding"][0]), progress["checkpoint"])
        except (OSError, ValueError, RuntimeError) as exc:
            latest = store.load()
            latest["consumers"][key]["restore_error"] = str(exc)
            store.save(latest)
        else:
            latest = store.load()
            latest["consumers"][key].pop("restore_error", None)
            store.save(latest)
    record = store.load()
    record["health_errors"] = {key: item["restore_error"] for key, item in record["consumers"].items()
                               if "restore_error" in item}
    record["phase"] = "done"
    store.save(record)
    return record
