"""Bind operation identities; only mutation entry points register local humans.

Generated origin and selected destination are distinct. Activation must seed
host fleet/bot keyframes through the registry owner before public mutations;
missing identities are never repaired by this reader. Human actors are portable
identities of a trusted local operator, not host-owned bots. A mutation may
record that operator's first contact through the Plane ingest owner.
Runtime admission and grants remain the public caller's responsibility. ``--by``
is operation provenance, deliberately not an input to this identity resolver.
"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import pwd
import re
import sqlite3

from .active_config import resolve_active_context
from .context import Context, generated_selectors
from .plane.db import connect_ro, db_file
from .plane.ids import ID_PATTERNS, derive_uid, read_host_uid
from .plane.registry_read import current_entities
from .plane.schema_state import require_current_schema
from .resources import PackageResources
from .task_operations import TaskActor, TaskOperationContext
from .task_queries import TaskQueryError


class OperationContextError(TaskQueryError):
    code = "conflict"


class OperationContextUnavailableError(OperationContextError):
    code = "unavailable"


class _MissingHumanIdentity(OperationContextError):
    """The sole read-boundary failure that a mutation may register."""


_HUMAN_ALIAS = re.compile(r"human:[^\s:/]+")


def _valid_human_alias(alias: object) -> bool:
    return isinstance(alias, str) and _HUMAN_ALIAS.fullmatch(alias) is not None


def _host_uid(root: Path) -> str:
    try:
        return read_host_uid(root / "state")
    except ValueError as exc:
        raise OperationContextError("existing host identity is unavailable or invalid") from exc


def _identity(conn, kind, alias, *, parent=None, human=False):
    rows = conn.execute("SELECT uid, parent_uid, provisional FROM identity_registry "
                        "WHERE kind=? AND alias=?", (kind, alias)).fetchall()
    if not rows and human:
        raise _MissingHumanIdentity(f"existing {kind} identity is missing: {alias}")
    if len(rows) != 1 or not re.fullmatch(ID_PATTERNS[kind], rows[0]["uid"]):
        raise OperationContextError(f"existing {kind} identity is missing or ambiguous: {alias}")
    row = rows[0]
    if human:
        # A human's first-contact fleet is historical provenance, not ownership.
        if row["parent_uid"] is not None and (not re.fullmatch(ID_PATTERNS["fleet"], row["parent_uid"])
                or not conn.execute("SELECT 1 FROM identity_registry WHERE kind='fleet' AND uid=?",
                                    (row["parent_uid"],)).fetchone()):
            raise OperationContextError("human identity has an invalid parent binding")
    elif row["parent_uid"] not in (None, parent) or row["provisional"] != 0:
        raise OperationContextError(f"unconfirmed or foreign {kind} identity: {alias}")
    return row["uid"]


def _local_entity(entities, host_uid, kind, alias, uid):
    rows = [r for r in entities if r["entity_type"] == kind and r["entity_alias"] == alias]
    if (len(rows) != 1 or rows[0]["host_uid"] != host_uid or rows[0]["entity_uid"] != uid
            or not isinstance(rows[0]["payload"], dict) or rows[0]["payload"].get("alias") != alias):
        raise OperationContextError(f"missing, ambiguous or foreign host registry binding: {alias}")


def _fleet_identities(conn, entities, host_uid, context):
    fleet = context.fleet
    uid = _identity(conn, "fleet", fleet.name, parent=host_uid)
    _local_entity(entities, host_uid, "fleet", fleet.name, uid)
    bots = {}
    for bot in fleet.bots:
        alias = f"bot:{fleet.name}/{bot}"
        instance = _identity(conn, "bot_instance", alias, parent=uid)
        _local_entity(entities, host_uid, "bot", alias, instance)
        bots[bot] = TaskActor(_identity(conn, "actor", alias, parent=uid), alias)
    return uid, bots


def bind_task_context(destination: Context, *, origin: Context | None = None,
                      operator_alias: str | None = None) -> TaskOperationContext:
    """Read one snapshot of existing IDs for already resolved config contexts."""
    if destination.paths.seed:
        raise OperationContextError("seed configuration has no operational identity")
    if origin is not None:
        if (operator_alias is not None or origin.bot_id not in origin.fleet.bots
                or origin.paths.root != destination.paths.root or origin.paths.seed):
            raise OperationContextError("caller origin conflicts with the selected operation context")
    elif not _valid_human_alias(operator_alias):
        raise OperationContextError("identify an existing human: actor outside a generated bot context")
    root = destination.paths.root
    host_uid = _host_uid(root)
    try:
        with closing(connect_ro(db_file(root))) as conn:
            conn.execute("BEGIN")
            require_current_schema(conn)
            entities = current_entities(conn)
            fleet_uid, bots = _fleet_identities(conn, entities, host_uid, destination)
            caller_fleet_uid = None
            if origin is None:
                caller = TaskActor(_identity(conn, "actor", operator_alias, human=True), operator_alias)
            else:
                caller_fleet_uid, callers = _fleet_identities(conn, entities, host_uid, origin)
                caller = callers[origin.bot_id]
            if _host_uid(root) != host_uid:
                raise OperationContextError("host identity changed during operation resolution")
            return TaskOperationContext(destination, host_uid, fleet_uid, caller, bots, caller_fleet_uid)
    except (OSError, sqlite3.Error) as exc:
        raise OperationContextUnavailableError("existing Plane identity registry is unavailable") from exc


def canonical_task_provenance_alias(ctx: TaskOperationContext, alias: str | None) -> str:
    """Validate the claimed actor spelling before a request can replay."""
    if alias is None:
        return ctx.caller.alias
    if not isinstance(alias, str):
        raise OperationContextError("provenance actor must be a canonical actor alias")
    if alias == ctx.caller.alias:
        return alias
    if _valid_human_alias(alias) or any(actor.alias == alias for actor in ctx.bots.values()):
        return alias
    raise OperationContextError("provenance actor is not an existing selected-fleet actor")


def resolve_task_provenance(ctx: TaskOperationContext, alias: str | None,
                            conn: sqlite3.Connection) -> TaskActor:
    """Resolve a claimed `--by` actor without changing the operation caller.

    A named human must already exist in the identity registry. A named bot must
    be a selected-fleet member; the operation's later identity check rechecks
    the frozen UID under its request and task locks. This read never registers
    a third party or grants the claimed actor's authority to the caller.
    """
    canonical = canonical_task_provenance_alias(ctx, alias)
    if canonical == ctx.caller.alias:
        return ctx.caller
    if _valid_human_alias(canonical):
        try:
            return TaskActor(_identity(conn, "actor", canonical, human=True), canonical)
        except (OSError, sqlite3.Error) as exc:
            raise OperationContextUnavailableError("existing provenance identity registry is unavailable") from exc
    return next(actor for actor in ctx.bots.values() if actor.alias == canonical)


def _selected_task_contexts(*, root: Path | None, fleet: str | None,
                            package: PackageResources | None) -> tuple[Context, Context | None]:
    """Resolve selectors before either read or mutation can choose an actor.

    Environment selectors are read before applying explicit destination flags,
    so malformed bot context cannot be bypassed by naming another fleet/root.
    This is trusted local provenance binding, not authentication.
    """
    origin_fleet, origin_bot = generated_selectors(include_bot=True)
    if ("FLEET_NAME" in os.environ and "CLAUDLOBBY_FLEET" in os.environ
            and os.environ["FLEET_NAME"] != os.environ["CLAUDLOBBY_FLEET"]):
        raise OperationContextError("generated fleet selectors disagree")
    origin = None
    if origin_bot is not None:
        if not origin_fleet or not os.environ.get("CLAUDLOBBY_ROOT", "").strip():
            raise OperationContextError("generated bot origin requires its own root and fleet")
        origin = resolve_active_context(root=Path(os.environ["CLAUDLOBBY_ROOT"]), fleet=origin_fleet,
                                 bot=origin_bot, package=package)
        for key, expected in (("FLEET_ROOT", origin.paths.fleet_config_dir),
                              ("BOT_DIR", origin.paths.bot_runtime(origin_bot))):
            if key in os.environ and (not os.environ[key].strip()
                    or Path(os.environ[key]).expanduser().resolve() != expected.resolve()):
                raise OperationContextError(f"generated {key} conflicts with caller origin")
    elif "BOT_DIR" in os.environ:
        raise OperationContextError("generated bot paths have no caller identity")
    elif "FLEET_ROOT" in os.environ:
        # A generated fleet timer has a fleet/root carrier but no bot actor.
        # Bind that carrier to its recorded active fleet before selecting the
        # destination; it cannot claim a different fleet through CLI flags.
        if not origin_fleet or not os.environ.get("CLAUDLOBBY_ROOT", "").strip():
            raise OperationContextError("generated fleet path requires its own root and fleet")
        fleet_origin = resolve_active_context(root=Path(os.environ["CLAUDLOBBY_ROOT"]),
                                              fleet=origin_fleet, package=package)
        declared = os.environ["FLEET_ROOT"]
        if (not declared.strip() or Path(declared).expanduser().resolve()
                != fleet_origin.paths.fleet_config_dir.resolve()):
            raise OperationContextError("generated FLEET_ROOT conflicts with caller fleet")
        destination = resolve_active_context(root=root, fleet=fleet if fleet is not None else origin_fleet,
                                             package=package)
        if (destination.paths.root != fleet_origin.paths.root
                or destination.fleet.name != fleet_origin.fleet.name):
            raise OperationContextError("generated fleet carrier conflicts with destination")
        return destination, None
    destination = resolve_active_context(root=root, fleet=fleet if fleet is not None else origin_fleet,
                                  package=package)
    return destination, origin


def resolve_operation_scope(*, root: Path | None = None, fleet: str | None = None,
                            package: PackageResources | None = None) -> tuple[Context, Context | None]:
    """Select frozen operation scope and validate any generated caller origin.

    A read has no human-actor prerequisite and never registers an identity.
    """
    return _selected_task_contexts(root=root, fleet=fleet, package=package)


def resolve_task_context(*, root: Path | None = None, fleet: str | None = None,
                         operator_alias: str | None = None,
                         package: PackageResources | None = None) -> TaskOperationContext:
    """Read existing identities only; a missing human is never registered."""
    destination, origin = _selected_task_contexts(root=root, fleet=fleet, package=package)
    return bind_task_context(destination, origin=origin, operator_alias=operator_alias)


def _local_operator_alias() -> str:
    """Use the OS account database, never USER/LOGNAME or a `--by` label."""
    try:
        name = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError) as exc:
        raise OperationContextError("local operator account is unavailable") from exc
    if not isinstance(name, str) or not _valid_human_alias(f"human:{name}"):
        raise OperationContextError("local operator account name is invalid")
    return f"human:{name}"


def resolve_task_mutation_context(*, root: Path | None = None, fleet: str | None = None,
                                  operator_alias: str | None = None,
                                  package: PackageResources | None = None) -> TaskOperationContext:
    """Bind a task mutation, registering a cold local human through Plane ingest.

    Generated bot origin always wins; `operator_alias` cannot replace it. A
    human's first-contact fact is committed before any task operation receives
    its context. Subsequent calls read the same actor from the registry.
    """
    destination, origin = _selected_task_contexts(root=root, fleet=fleet, package=package)
    if origin is not None:
        return bind_task_context(destination, origin=origin, operator_alias=operator_alias)
    alias = operator_alias if operator_alias is not None else _local_operator_alias()
    try:
        return bind_task_context(destination, operator_alias=alias)
    except _MissingHumanIdentity:
        pass

    # The read binder already verified the selected fleet and local host before
    # finding this one missing actor. Ingest, not this module, owns its UID and
    # the idempotent alias registry write. A deterministic event ID also makes
    # an uncertain acknowledgment safe to reconcile on the next call.
    root_path = destination.paths.root
    event_id = derive_uid("ev", f"task-operator-first-contact:v1:{_host_uid(root_path)}:{alias}")
    event = {"event_id": event_id, "event_type": "system", "emitter": "task-operation-context",
             "fleet": destination.fleet.name, "source_ref": f"task-operator-first-contact:{event_id}",
             "payload": {"event": "operator_first_seen", "subject_kind": "actor", "subject": alias}}
    try:
        from .plane.emit_api import emit_batch
        outcome = emit_batch(root_path, [event], require_commit=True)[0]
    except Exception as exc:
        raise OperationContextUnavailableError("local operator identity could not be recorded") from exc
    if outcome.status not in ("committed", "duplicate"):
        raise OperationContextUnavailableError("local operator identity was not committed")
    return bind_task_context(destination, operator_alias=alias)
