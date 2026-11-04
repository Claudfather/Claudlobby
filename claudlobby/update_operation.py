"""Bounded existing-installation update, coordinated by one UpdateStore.

The stable runner owns process replacement and the exclusive maintenance fence.
This application-side owner prepares, saves/quiets, publishes and restores the
same existing consumer roster. It neither initializes nor migrates a database.
"""
from __future__ import annotations

import ast
import platform
from pathlib import Path

from .active_config import read_current_config, snapshot_from_plan, publish_current_config
from .activation_identity import read_registry_identity_bindings
from .config_install import prepare_publication, apply_publication
from .config_plan import read_rendered
from .git_update import UpdateError, _git, _head
from .plane.db import db_file, connect_ro
from .runtime_versions import SQL_SCHEMA_VERSION
from .update_services import (
    freeze_service_intent, consumer_scope, validate_candidate_consumers,
    quiesce_consumer, observe_quiet, restore_consumer,
)


def _operation(store, *phases):
    store._assert_locked()
    record = store.load()
    if record is None or (phases and record["phase"] not in phases):
        raise UpdateError("update operation is not in the required phase")
    if "candidate_config" not in record or "consumers" not in record:
        raise UpdateError("update application intent is incomplete; resume preparation")
    return record


def _schema(store, revision):
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


def prepare_operation(store, rendered, adapter, install_directory, interpreter, request_id):
    """Prepare all immutable inputs and dependencies before native effects."""
    store._assert_locked()
    existing = store.load()
    if existing is not None and existing["phase"] != "done":
        if ((existing["target"], existing["request_id"]) != (rendered.revision, request_id)
                or existing.get("finalized", True)
                and existing["config_fingerprint"] != rendered.fingerprint):
            raise UpdateError("continue the existing frozen update operation")
        if existing["phase"] != "prepare":
            return _operation(store)
    rendered.check_inputs()
    current = read_current_config(store.root, installation=store.installation)
    identities = read_registry_identity_bindings(rendered, package=adapter.package)
    candidate = snapshot_from_plan(rendered, store.installation, rendered.revision, identities=identities)
    validate_candidate_consumers(current, candidate, platform.system())
    if current["identities"] != candidate["identities"]:
        raise UpdateError("identity changes require an explicit configuration scope")
    schema = _schema(store, rendered.revision)
    if existing is not None and existing["phase"] == "prepare" and "consumers" in existing:
        record = existing
        if record["candidate_config"] != candidate or record["previous_config"] != current["fingerprint"]:
            raise UpdateError("prepared effective configuration changed")
    else:
        rows = freeze_service_intent(current, adapter, install_directory)
        metadata = {unit["source"]: unit for unit in current["units"]}
        for row in rows:
            row["phase"] = metadata[row["declaration"]["source"]]["phase"]
        scope = consumer_scope(rows)
        record = store.prepare(rendered.revision, config_fingerprint=rendered.fingerprint,
                               scope=scope, request_id=request_id)
        record.update(candidate_config=candidate, previous_config=current["fingerprint"],
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
    # Re-enter the shared owner before an unattempted stop: it verifies the
    # original request and live freshness without sending a replacement.
    if progress.get("checkpoint_operation") is None:
        progress["checkpoint_operation"] = record["request_id"]
        store.save(record)
    directory, label, tmpdir = row["bot_binding"]
    proof = save_planned_checkpoint(Path(directory), label, tmpdir, adapter,
                                    operation_id=progress["checkpoint_operation"])
    # The shared checkpoint owner retains the original request before sends;
    # an interrupted return simply re-observes that same owner/request.
    record = store.load()
    record["consumers"][key]["checkpoint"] = proof
    store.save(record)


def quiesce_operation(store, adapter):
    """Schedules first; keep the recorder available until all bots save/stop."""
    record = _operation(store, "quiesce")
    store.verify_dependencies()
    if read_current_config(store.root, installation=store.installation)["fingerprint"] != record["previous_config"]:
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
        if row["bot_binding"] and row["disposition"] == "running" and progress.get("stop") is None:
            _checkpoint(store, key, adapter)
        quiesce_consumer(store, key, adapter)
    return store.load()


def assert_operation_quiet(store, adapter):
    """Caller holds EX; capture proof and native quiet are freshly rechecked."""
    from .bot_operations import verify_checkpoint_after_stop
    record = _operation(store, "quiesce")
    for key, progress in record["consumers"].items():
        row = progress["intent"]
        if progress.get("stop") != "done":
            raise UpdateError("consumer stop has not completed")
        if row["bot_binding"] and row["disposition"] == "running":
            if progress.get("checkpoint") is None:
                raise UpdateError("running bot lacks its original checkpoint proof")
            verify_checkpoint_after_stop(Path(row["bot_binding"][0]), progress["checkpoint"])
        observe_quiet(store, key, adapter)
    if _schema(store, record["target"]) != record["sql_schema"]:
        raise UpdateError("Plane schema changed during quiescence")
    return store.load()


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
    if _schema(store, record["target"]) != record["sql_schema"]:
        raise UpdateError("Plane schema changed before publication")
    rendered = read_rendered(store.root, record["config_fingerprint"])
    if record["phase"] in {"environment", "publish"}:
        record["phase"] = "publish"
        store.save(record)
        apply_publication(rendered, store)
        record = store.load()
        record["phase"] = "commit"
        store.save(record)
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
            if progress.get("restore") != "done":
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
