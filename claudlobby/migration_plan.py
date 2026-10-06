"""Read-only migration preview; repeat under quiescence before activation.

No replay, quarantine, schema writes, database creation or release selection.
Historical queue classification is deliberately not a second wire validator:
preserved telemetry still passes the canonical ingest validator on replay.
Unknown shapes/versions are blockers, never an empty queue or discard consent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import UUID

from .plane.db import connect_ro, db_file
from .plane.ids import ID_PATTERNS
from .plane.migrations import _MIGRATION_RE
from .plane.queue_paths import scan_spool, spool_path, staged_dir, staged_payload
from .releases import ReleaseManifest, read_release
from .request_receipts import decode_receipt
from .runtime_versions import SUPPORTED_PLANE_SCHEMA_VERSIONS
from .source_state import SOURCE_UNREADABLE, scan_dir
from .task_audit import TaskAuditError, audit_tasks
from .task_state import TASK_EMITTER


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class MigrationManifest:
    data_root: str
    source: dict
    target: dict
    database: dict
    task_audit: dict | None
    migrations: tuple[dict, ...]
    queues: dict
    operational: dict
    blockers: tuple[str, ...]
    proposed_steps: tuple[str, ...]
    rollback: dict
    preview_only: bool = True

    def payload(self) -> dict:
        return {"schema": 1, **asdict(self)}

    @property
    def manifest_id(self) -> str:
        return "m-" + _digest(_json(self.payload()))

    def readability_blockers(self, compatibility, *, after_target_writes: bool = False) -> tuple[str, ...]:
        """Recovery must read every retained version, not merely its maximum."""
        key = "after_target_write_versions" if after_target_writes else "after_sql_versions"
        return _readability_blockers(compatibility, self.rollback[key], self.operational)


def _readability_blockers(compatibility, floor: dict, operational: dict) -> tuple[str, ...]:
    blockers = list(compatibility.blockers(floor))
    for name, versions in (("receipt_format", operational["receipts"]["versions"]),
                           ("task_model", operational["task_model_versions"])):
        if versions is None:
            blockers.append(f"unknown retained {name}")
        else:
            blockers.extend(f"unsupported {name}: {v}" for v in versions
                            if not getattr(compatibility, name).supports(v))
    return tuple(dict.fromkeys(blockers))


def _file(path: Path) -> tuple[dict, bytes | None]:
    record = {"path": str(path), "state": "ok", "sha256": None, "bytes": None}
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("not a regular file")
        content = path.read_bytes()
        record.update(sha256=_digest(content), bytes=len(content))
        return record, content
    except FileNotFoundError:
        record["state"] = "absent"
    except (OSError, ValueError) as exc:
        record.update(state="unreadable", error=str(exc))
    return record, None


def _database(root: Path, initialize_empty: bool) -> tuple[dict, dict | None, list[str]]:
    path = db_file(root)
    # WAL participates in the stored state; SHM is lock/index scratch, not a
    # durable data version. Byte hashes are observation bindings, not backups.
    files = [_file(Path(str(path) + suffix))[0] for suffix in ("", "-wal")]
    state = {"path": str(path), "state": files[0]["state"], "user_version": None,
             "files": files, "consistent_backup": False, "initialize_empty": initialize_empty,
             "task_model_versions": None}
    blockers, audit = [], None
    if initialize_empty:
        if all(item["state"] == "absent" for item in files):
            state["task_model_versions"] = [0]
            return state, audit, blockers
        blockers.append("empty initialization requires both database and WAL to be absent")
    if state["state"] != "ok":
        blockers.append(f"plane database {state['state']}: explicit initialization or repair required")
        return state, audit, blockers
    try:
        conn = connect_ro(path)
        try:
            conn.execute("BEGIN")
            state["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
            report = audit_tasks(conn)
            audit = asdict(report)
            # A0 has already checked every producer with the semantic owner.
            # Empty history needs only legacy/empty readability. Do not infer
            # retained versions from the source release's declared writer.
            emitters = [] if not report.schema_version else conn.execute(
                "SELECT emitter FROM work_items UNION SELECT emitter FROM assignments "
                "UNION SELECT emitter FROM events WHERE kind='task'").fetchall()
            state["task_model_versions"] = sorted({1 if row[0] == TASK_EMITTER else 0
                                                   for row in emitters} or {0})
            for issue in report.blockers:
                targets = ", ".join((*issue.task_ids, *issue.assignment_ids))
                remedy = ("; withdraw in the owning fleet and re-admit in the worker fleet"
                          if issue.code == "foreign_fleet_assignee" else "")
                blockers.append(f"task audit: {issue.code} ({targets}){remedy}")
        finally:
            conn.close()
    except (OSError, sqlite3.Error, TaskAuditError) as exc:
        blockers.append("historical task audit unavailable: "
                        + (str(exc) if isinstance(exc, TaskAuditError) else type(exc).__name__))
        state["state"] = "uninterpretable"
    after = [_file(Path(item["path"]))[0] for item in files]
    # On a quiesced WAL-mode database, SQLite's first read can create an empty
    # WAL sidecar. It contains no durable pages, but it must be bound as the
    # observed file state. Every other byte or state change remains a blocker.
    empty_wal_created = (
        after[0] == files[0]
        and files[1]["state"] == "absent"
        and after[1]["state"] == "ok"
        and after[1]["bytes"] == 0
        and after[1]["sha256"] == _digest(b"")
    )
    if after != files and not empty_wal_created:
        blockers.append("database bytes changed during preview; repeat under quiescence")
        state["state"] = "changing"
    elif empty_wal_created:
        state["files"] = after
    if any(item["state"] == "unreadable" for item in after):
        blockers.append("database/WAL inventory is unreadable")
    return state, audit, blockers


def _receipts(root: Path) -> tuple[dict, list[str]]:
    """Read private receipt nodes, never creating locks or retaining bodies.

    Quiescence belongs to the caller. Empty request locks are metadata, not
    evidence that a send did not happen. Unknown nodes (including interrupted
    temporary files) require an explicit repair, never implicit deletion.
    """
    directory = root / "state/requests"
    result = {"path": str(directory), "state": "absent", "files": [],
              "versions": [0], "file_count": 0}
    blockers, versions = [], set()
    try:
        for ancestor in (root / "state", directory):
            info = ancestor.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("receipt directory is redirected or not owned")
        result["state"] = "ok"
        for fleet in sorted(directory.iterdir()):
            info = fleet.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or not re.fullmatch(ID_PATTERNS["fleet"], fleet.name)):
                raise ValueError("invalid receipt fleet directory")
            for path in sorted(fleet.iterdir()):
                record = {"path": str(path), "sha256": None, "bytes": None,
                          "mode": None, "format_version": None}
                result["files"].append(record)
                try:
                    if path.suffix not in {".json", ".lock"} or str(UUID(path.stem)) != path.stem:
                        raise ValueError("invalid receipt filename")
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                                or stat.S_IMODE(info.st_mode) != 0o600):
                            raise ValueError("receipt file must be owned and private")
                        content = stream.read()
                    record.update(sha256=_digest(content), bytes=len(content), mode=0o600)
                    if path.suffix == ".lock":
                        if content:
                            raise ValueError("request lock has unexpected content")
                        continue
                    receipt = decode_receipt(json.loads(content, object_pairs_hook=_unique,
                                                        parse_constant=_invalid_number),
                                             request_id=path.stem, fleet_uid=fleet.name)
                    record["format_version"] = receipt.format_version
                    versions.add(receipt.format_version)
                except (OSError, ValueError, UnicodeError):
                    # Codec failures can contain caller-provided data; keep
                    # only fixed wording and the scoped node in the preview.
                    blockers.append(f"invalid, unsupported or unreadable receipt node: {path}")
    except FileNotFoundError:
        if result["state"] != "absent":
            blockers.append("receipt inventory changed during preview")
    except (OSError, ValueError):
        blockers.append("receipt directory inventory unavailable or invalid")
    result["state"] = "blocked" if blockers else result["state"]
    result["versions"] = None if blockers else sorted(versions or {0})
    result["file_count"] = None if blockers else sum(
        item["format_version"] is not None for item in result["files"])
    return result, blockers


# Historical v0 family shapes from plane/contracts.py and ingest._family_values.
# This only establishes an inventory category, not full field/enum validation.
_REQUIRED = {
    "work_item": ("work_item_id", "title", "created_by"),
    "assignment": ("assignment_id", "work_item_id", "assignee", "assigned_by"),
    "task": ("work_item_id", "event"),
    "communication": ("msg_id", "sender", "message_class"),
    "transmission": ("msg_id", "attempt_no", "carrier", "destination", "state"),
    "system": ("event",), "workstream": ("workstream_id", "title", "opened_by"),
    "workstream_event": ("workstream_id", "event"),
    "registry_snapshot": ("entity_type", "entity_alias", "cause", "scan_id"),
    "metric_sample": ("subject_kind", "subject", "metric", "value"),
    "declaration": ("event", "subject_kind", "subject"),
}


def _classify(request: dict, *, raw: bool, source, target) -> dict:
    if not isinstance(request, dict) or not isinstance(request.get("payload"), dict):
        raise ValueError("event and payload must be objects")
    family, payload = request.get("event_type"), request["payload"]
    if not isinstance(family, str) or family not in _REQUIRED:
        # Issue text becomes operator-visible refusal text; never echo payload values.
        raise ValueError("unknown event family")
    if any(not isinstance(request.get(key), str) or not request[key]
           for key in ("event_id", "occurred_at", "emitter")):
        raise ValueError("pending event lacks finalized identity/time/emitter")
    if any(key not in payload or payload[key] is None for key in _REQUIRED[family]):
        raise ValueError(f"unrecognized {family} payload shape")
    for key in _REQUIRED[family]:
        value = payload[key]
        if key == "value":  # Metric values intentionally accept any JSON value except null.
            continue
        if key == "attempt_no":
            valid = type(value) is int and value >= 1
        else:
            valid = isinstance(value, str) and bool(value)
        if not valid:
            raise ValueError(f"unrecognized {family}.{key} payload shape")
    version = request.get("schema_version")
    if version is None and raw:
        # The stdlib shim stamps IDs/time but emit_batch stamps the envelope
        # version. An unstamped old batch must not silently acquire a new one.
        version = source.envelope.write
        if version != target.envelope.write:
            raise ValueError("unstamped raw envelope needs an explicit old-reader drain decision")
    if version not in SUPPORTED_PLANE_SCHEMA_VERSIONS:
        raise ValueError("unknown pending envelope version")
    if not target.envelope.supports(version):
        raise ValueError("target cannot read pending envelope version")
    category = "ordinary_telemetry"
    if family == "workstream_event" and "waiting_on" in payload:
        # Optional on the new wire, but an older strict payload codec cannot
        # replay a pending blocked-wait event. Drain or name a replay decision.
        category = "additive_workstream_wait"
    elif family in ("work_item", "assignment", "task"):
        category = "conditional_legacy_task_mutation"
    elif (family == "transmission" or family == "communication" and (
            payload.get("work_item_id") or payload.get("assignment_id")
            or payload.get("message_class") in ("task_request", "report"))):
        # queries.TASK_STATUS_SQL joins dispatch_msg_id to transmission facts;
        # linked reports/dispatches cannot be waved through as unrelated logs.
        category = "legacy_task_evidence"
    elif family == "system" and payload.get("event") in ("report_status", "reports_acked"):
        # queries' progress grace and report cursor both consume these markers.
        category = "legacy_task_cursor_or_status"
    if request["emitter"].startswith("claudlobby.tasks."):
        raise ValueError("versioned task producer requires its task-state decoder")
    return {"event_id": request["event_id"], "family": family, "classification": category,
            "envelope_version": version, "replay_validated": False}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_number(value):
    raise ValueError("invalid JSON number")


def _pending(path: Path, queue: str, source, target) -> dict:
    result, content = _file(path)
    result.update(records=[], issues=[], disposition="preserve for canonical ingest validation")
    if content is None:
        result["issues"].append(f"pending file {result['state']}")
        return result
    try:
        if source.pending_format.write != 0 or not target.pending_format.supports(0):
            raise ValueError("unsupported historical pending format")
        data = json.loads(content, object_pairs_hook=_unique, parse_constant=_invalid_number)
        if not isinstance(data, dict) or any(k in data for k in ("version", "format_version", "pending_format")):
            raise ValueError("unknown pending envelope shape/version")
        raw = queue == "staged" or queue == "quarantine" and "events" in data
        if "events" in data and "requests" in data:
            raise ValueError("ambiguous raw/finalized pending envelope")
        requests = data.get("events" if raw else "requests")
        if not isinstance(requests, list) or not requests:
            raise ValueError("pending batch needs a non-empty event list")
        for request in requests:
            result["records"].append(_classify(request, raw=raw, source=source, target=target))
        ids = [record["event_id"] for record in result["records"]]
        if len(ids) != len(set(ids)) or not raw and data.get("event_ids") != ids:
            raise ValueError("pending event identity list is missing, duplicated or inconsistent")
        if any(r["classification"] != "ordinary_telemetry" for r in result["records"]):
            result["disposition"] = "explicit old-semantics drain or named quarantine/replay decision required"
    except UnicodeError:
        result["issues"].append("pending file is not UTF-8 JSON")
    except TypeError:
        result["issues"].append("pending payload has an invalid type")
    except ValueError as exc:
        # Product-authored messages above, or JSON syntax positions; no values.
        result["issues"].append(str(exc))
    if queue == "quarantine":
        result["disposition"] = "retain outside replay paths; no replay/discard permission inferred"
    return result


def _queues(root: Path, source, target) -> tuple[dict, list[str]]:
    spool = scan_spool(root)
    probe, entries = scan_dir(staged_dir(root))
    selections = {"spool": (spool.spool_state, spool.pending),
                  "inflight": (spool.spool_state, spool.inflight),
                  "quarantine": (spool.quarantine_state, spool.quarantined),
                  "staged": (probe.state, sorted(p for p in entries if staged_payload(p)))}
    result, blockers = {}, []
    for queue, (state, paths) in selections.items():
        location = staged_dir(root) if queue == "staged" else spool_path(root)
        if queue == "quarantine":
            location = location / "quarantine"
        try:
            if not stat.S_ISDIR(location.lstat().st_mode):
                state = SOURCE_UNREADABLE
        except FileNotFoundError:
            pass
        except OSError:
            state = SOURCE_UNREADABLE
        files = [_pending(path, queue, source, target) for path in paths]
        unreadable = state == SOURCE_UNREADABLE
        result[queue] = {"path": str(location), "state": state,
                         "file_count": None if unreadable else len(files), "files": files,
                         "sha256": None if unreadable or any(f["sha256"] is None for f in files)
                         else _digest(_json(files))}
        if unreadable:
            blockers.append(f"{queue} queue unreadable; pending count is unknown")
        if queue == "inflight" and files:
            blockers.append("inflight spool claims require a quiesced ownership/recovery check")
        for item in files:
            if queue == "quarantine":
                # Retained evidence, never replayed or interpreted: a malformed
                # payload is normal. Its exact bytes stay bound in the digest,
                # and a vanished or unreadable file still refuses.
                if item["state"] != "ok":
                    blockers.append(f"{item['path']}: retained quarantine file is {item['state']}")
                continue
            blockers.extend(f"{item['path']}: {issue}" for issue in item["issues"])
            if queue != "quarantine" and any(r["classification"] != "ordinary_telemetry"
                                             for r in item["records"]):
                blockers.append(f"{item['path']}: non-telemetry pending records need a drain/quarantine decision")
    return result, blockers


def _release(root: Path, selected: ReleaseManifest) -> ReleaseManifest:
    verified = read_release(root, selected.release_id)
    if verified.seal_sha256 != selected.seal_sha256:
        raise ValueError("selected release changed before migration preview")
    return verified


def _binding(release: ReleaseManifest) -> dict:
    return {"release_id": release.release_id, "seal_sha256": release.seal_sha256,
            "artifact_id": release.inputs.artifact_id, "source_revision": release.inputs.source_revision,
            "runtime_sha256": release.runtime_sha256, "versions": release.compatibility.to_dict()}


def build_migration_manifest(data_root: Path, source: ReleaseManifest | None,
                             target: ReleaseManifest, *, initialize_empty: bool = False) -> MigrationManifest:
    """Inventory releases and data; None denotes an observed unsealed source.

    First adoption asserts no old executable compatibility or rollback path.
    Pending legacy batches are refused rather than guessed or discarded.
    """
    if type(initialize_empty) is not bool:
        raise ValueError("initialize_empty must be an explicit boolean")
    root = Path(data_root).expanduser().resolve()
    source, target = (_release(root, source) if source is not None else None), _release(root, target)
    database, audit, blockers = _database(root, initialize_empty)
    if source is None and (initialize_empty or database["state"] != "ok"):
        blockers.append("legacy adoption requires an existing readable Plane database")
    queues, queue_blockers = _queues(root, (source or target).compatibility, target.compatibility)
    blockers.extend(queue_blockers)
    if source is None and any(row["file_count"] != 0 for name, row in queues.items() if name != "quarantine"):
        # Quarantine is retained outside replay and bound by its inventory digest.
        blockers.append("legacy adoption requires empty pending, inflight and staged queues")
    receipts, receipt_blockers = _receipts(root)
    blockers.extend(receipt_blockers)
    operational = {"receipts": receipts, "task_model_versions": database["task_model_versions"],
                   "wire_additions": [{"family": "workstream_event", "field": "waiting_on",
                                       "location": "event.detail", "classification": "optional_metadata",
                                       "old_reader": "blocked row remains visible with unknown addressee"},
                                      {"family": "registry_snapshot", "field": "agent_cli",
                                       "location": "payload (entity bot)", "classification": "optional_metadata",
                                       "old_reader": "a keyframe without it reads as agent_cli claude"}]}
    observed = {"receipt_format": receipts["versions"],
                "task_model": operational["task_model_versions"]}
    for name, versions in observed.items():
        if versions is not None:
            blockers.extend(f"target cannot read retained {name}: {version}"
                            for version in versions if not getattr(target.compatibility, name).supports(version))
    current, goal = database["user_version"], target.compatibility.schema.write
    migrations = []
    directory = (target.directory / target.paths.artifact).parent / "plane/migrations"
    try:
        for path in sorted(directory.iterdir()):
            match = _MIGRATION_RE.fullmatch(path.name)
            if match:
                record, _ = _file(path)
                if record["state"] != "ok":
                    raise ValueError(f"unreadable migration SQL: {path}")
                migrations.append({"version": int(match[1]), **record,
                                   "proposed": (current if current is not None else 0) < int(match[1]) <= goal})
        if [m["version"] for m in migrations] != list(range(1, goal + 1)):
            raise ValueError("candidate migration sequence does not match its declared schema")
    except (OSError, ValueError) as exc:
        blockers.append("migration SQL inventory incomplete: "
                        + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
    if current is not None and current > goal:
        blockers.append("forward-only SQL owner cannot downgrade the current database")
    for name, version in target.compatibility.write_versions.items():
        if source is not None and name != "schema" and version != source.compatibility.write_versions[name]:
            if (name in observed and source.compatibility.write_versions[name] == 0 and version == 1
                    and observed[name] is not None
                    and all(getattr(target.compatibility, name).supports(v) for v in (0, 1))
                    and (name != "task_model" or not any(b.startswith("task audit:") for b in blockers))):
                continue  # Implemented decoders read both; no conversion or row rewrite.
            blockers.append(f"explicit {name} conversion/rehearsal required before changing its write version")
    after_sql = {**(source or target).compatibility.write_versions, "schema": max(current or 0, goal)}
    for name, versions in observed.items():
        if versions is not None:
            after_sql[name] = max(versions)
    after_writes = {**target.compatibility.write_versions, "schema": max(current or 0, goal)}
    sql_floor = (_readability_blockers(source.compatibility, after_sql, operational)
                 if source is not None else ("unsealed legacy runtime is not a rollback target",))
    write_floor = (_readability_blockers(source.compatibility, after_writes, operational)
                   if source is not None else ("unsealed legacy runtime is not a rollback target",))
    rollback = {"after_sql_versions": after_sql, "after_target_write_versions": after_writes,
                "source_after_sql_blockers": sql_floor, "source_after_target_writes_blockers": write_floor,
                "compatible_recovery_release_required_before_sql": bool(sql_floor) and source is not None,
                "backup_restoration_over_accepted_work_permitted": False}
    steps = ("repeat database/task/queue/receipt inventory under the host activation lock after quiescence",
             "resolve named conditional records under old semantics; retain ordinary telemetry for validated replay",
             *(("name a state-compatible recovery release before irreversible SQL",) if sql_floor and source is not None else ()),
             "record a consistent database backup and explicit migration/recovery approval",
             *(f"explicit SQL apply {m['version']:04d}, sha256 {m['sha256']}" for m in migrations if m["proposed"]),
             "verify schema, task linkage, cursor preservation and pending formats before candidate daemon start")
    binding = (_binding(source) if source is not None else
               {"kind": "legacy-unsealed", "root": str(root), "user_version": current,
                "database_files": database["files"]})
    return MigrationManifest(str(root), binding, _binding(target), database, audit,
                             tuple(migrations), queues, operational, tuple(sorted(set(blockers))), steps, rollback)


def verify_pending_queues(data_root: Path, manifest: MigrationManifest) -> None:
    """Recheck preserved queues and receipts under quiescence, even after SQL.

    The apply owner verifies the now-changed DB against its recorded SQL result.
    This grants no replay, quarantine, receipt mutation or discard permission.
    """
    root = Path(data_root).expanduser().resolve()
    if str(root) != manifest.data_root:
        raise ValueError("migration manifest belongs to another data root")
    legacy = manifest.source.get("kind") == "legacy-unsealed"
    source = None if legacy else read_release(root, manifest.source["release_id"])
    target = read_release(root, manifest.target["release_id"])
    legacy_binding = {"kind": "legacy-unsealed", "root": str(root),
                      "user_version": manifest.database["user_version"],
                      "database_files": manifest.database["files"]}
    if ((legacy and manifest.source != legacy_binding)
            or (not legacy and _binding(source) != manifest.source)
            or _binding(target) != manifest.target):
        raise ValueError("migration release bindings changed")
    queues, blockers = _queues(root, (source or target).compatibility, target.compatibility)
    if _json(queues) != _json(manifest.queues):
        raise ValueError("pending queue inventory changed; repeat the migration preview")
    if blockers:
        raise ValueError("pending queues remain blocked: " + "; ".join(blockers))
    receipts, blockers = _receipts(root)
    if _json(receipts) != _json(manifest.operational["receipts"]):
        raise ValueError("operational receipt inventory changed; repeat the migration preview")
    if blockers:
        raise ValueError("operational receipts remain blocked: " + "; ".join(blockers))
