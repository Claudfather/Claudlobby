"""Freeze existing operation identities without minting or asserting authority.

Generated origin and selected destination are distinct. Activation must seed
host fleet/bot keyframes through the registry owner before public mutations;
missing identities are never repaired by this reader. Human actors are portable
identities explicitly selected by a trusted local operator, not host-owned bots.
Runtime admission and grants remain the public caller's responsibility. ``--by``
is operation provenance, deliberately not an input to this identity resolver.
"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import stat

from .context import Context, generated_selectors, resolve_context
from .plane.db import connect_ro, db_file
from .plane.ids import ID_PATTERNS
from .plane.registry_read import current_entities
from .plane.schema_state import require_current_schema
from .resources import PackageResources
from .task_operations import TaskActor, TaskOperationContext
from .task_queries import TaskQueryError


class OperationContextError(TaskQueryError):
    code = "conflict"


def _host_uid(root: Path) -> str:
    try:
        if (root / "state").is_symlink():
            raise ValueError("redirected state")
        fd = os.open(root / "state/host-uid", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("host identity is not an owned private file")
            value = stream.read().strip()
        if not re.fullmatch(ID_PATTERNS["host"], value):
            raise ValueError("malformed host identity")
        return value
    except (OSError, ValueError, UnicodeError) as exc:
        raise OperationContextError("existing host identity is unavailable or invalid") from exc


def _identity(conn, kind, alias, *, parent=None, human=False):
    rows = conn.execute("SELECT uid, parent_uid, provisional FROM identity_registry "
                        "WHERE kind=? AND alias=?", (kind, alias)).fetchall()
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
    elif not isinstance(operator_alias, str) or not re.fullmatch(r"human:[^\s:/]+", operator_alias):
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
        raise OperationContextError("existing Plane identity registry is unavailable") from exc


def resolve_task_context(*, root: Path | None = None, fleet: str | None = None,
                         operator_alias: str | None = None,
                         package: PackageResources | None = None) -> TaskOperationContext:
    """Resolve public selectors without replacing the generated caller origin.

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
        origin = resolve_context(root=Path(os.environ["CLAUDLOBBY_ROOT"]), fleet=origin_fleet,
                                 bot=origin_bot, package=package)
        for key, expected in (("FLEET_ROOT", origin.paths.fleet_config_dir),
                              ("BOT_DIR", origin.paths.bot_runtime(origin_bot))):
            if key in os.environ and (not os.environ[key].strip()
                    or Path(os.environ[key]).expanduser().resolve() != expected.resolve()):
                raise OperationContextError(f"generated {key} conflicts with caller origin")
    elif "BOT_DIR" in os.environ:
        raise OperationContextError("generated bot directory has no caller identity")
    destination = resolve_context(root=root, fleet=fleet if fleet is not None else origin_fleet,
                                  package=package)
    return bind_task_context(destination, origin=origin, operator_alias=operator_alias)
