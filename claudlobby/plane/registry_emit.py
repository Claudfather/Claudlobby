"""The generate-time registry emitter (Phase 2b — spec §9b/§18).

One SCAN per generate: assemble the §9b entity payloads from the composed
fleet state, emit them as `registry_snapshot` events (cause=generate, one
scan_id), diff the db's prior entity set against what this scan enumerated
to emit TOMBSTONES (deletion is the one underivable operation), then close
with the `scan_completed` declaration that makes those tombstones valid
(round-3 F11: the join is by scan_id, never by time).

Assembly is DETERMINISTIC by construction — sorted lists, content hashes,
no timestamps inside payloads — because the ingest hash gate depends on it:
an unchanged estate re-scanned must suppress every row, or every generate
would write a full keyframe set.

Scope honesty: a scan enumerates ONE fleet plus the host-global surfaces
(host, vault, shared library). Tombstone eligibility is limited to that
scope — another fleet's bots are NOT missing just because this fleet's scan
did not name them. Scope membership is derivable from alias conventions
(``bot:<fleet>/…``, ``<fleet>/<key>``, ``shared/…``) and the scan_completed
declaration names its scope string.

Dormancy (estate rule): armed per fleet by ``PLANE_EMIT_ENABLED=1`` in the
fleet's env — a root pull must never switch on new emission. The generate
hook is NON-BLOCKING: a scan failure logs and never breaks generate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import shutil
import subprocess
import uuid
from pathlib import Path

from .canonical import canonical_hash

log = logging.getLogger("claudlobby.plane.registry")

_LIBRARY_KINDS = (
    "expertise", "skills", "mcp", "guardrails", "protocols", "tools",
    "voices", "resources", "lessons", "principles", "permissions",
    "post_actions",
)

_SCHEMA = "1"


def _file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree_hash(d: Path) -> str:
    pairs = []
    for f in sorted(d.rglob("*")):
        if f.is_file():
            pairs.append([str(f.relative_to(d)), _file_hash(f)])
    return canonical_hash(pairs)


def _hash_or_none(p: Path) -> str | None:
    try:
        return _file_hash(p)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Payload assembly (§9b — deterministic, volatile facts excluded per F12)
# ---------------------------------------------------------------------------

def host_payload(paths) -> dict:
    import os as _os

    disk = shutil.disk_usage("/")
    try:
        page = _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES")
        ram_mb = int(page / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        ram_mb = 0
    claude_bin = shutil.which("claude")
    claude_version = "unavailable"
    if claude_bin:
        try:
            claude_version = subprocess.run(
                [claude_bin, "--version"], capture_output=True, text=True,
                timeout=10).stdout.strip() or "unavailable"
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        clv = subprocess.run(
            ["git", "-C", str(paths.root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        clv = ""
    system_yaml = paths.root / "claudlobby" / "system.yaml"
    declared_fleets = sorted(
        d.name for d in (paths.root / "local").iterdir()
        if d.is_dir() and (d / "fleet.yaml").is_file()
    ) if (paths.root / "local").is_dir() else []
    return {
        "aliases": {"hostname": platform.node()},
        "os": "darwin" if platform.system() == "Darwin" else "linux",
        "arch": platform.machine(),
        "kernel": platform.release(),
        "ram_total_mb": ram_mb,
        "disk_total_gb": int(disk.total / (1024 ** 3)),
        "system": {
            "claudlobby_version": clv or "unknown",
            "claude_version": claude_version,
            "python_version": platform.python_version(),
            # host_jobs/plugins/emitters: the daily PROBE facet owns the
            # enrolled-state walk (cause=probe); generate records the
            # install identity only — an empty list here is "not scanned
            # by this cause", disclosed by the cause column itself.
            "host_jobs": [],
            "plugins": [],
            "emitters": [],
            "defaults_tier_hash": _hash_or_none(system_yaml) or "absent",
        },
        "declared_fleets": declared_fleets,
        "schema_version": _SCHEMA,
    }


def vault_payload(paths, fleet) -> dict | None:
    vp = getattr(fleet, "claudron_vault_path", None) or next(
        (b.claudron_vault_path for b in fleet.bots.values()
         if b.claudron_vault_path), None)
    if not vp:
        return None
    vpath = Path(vp).expanduser()
    if not vpath.is_dir():
        return None
    try:
        remote = subprocess.run(
            ["git", "-C", str(vpath), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        remote = ""
    gitignore = vpath / ".gitignore"
    safe = False
    try:
        gi = gitignore.read_text()
        safe = ("runtime" in gi) and (".env" in gi)
    except OSError:
        pass
    from .. import claudron_compat
    floor = str(getattr(claudron_compat, "FLOOR", "")) or "unset"
    return {
        "alias": vpath.name,
        "role": "primary",
        "mount_path": str(vpath),
        "remote": remote,                      # sensitive (§11) at render
        "compat": {"floor": floor, "ok": True},
        "carries_fleets": (vpath / "local").is_dir()
                          or "local" in vpath.parts,
        "gitignore_safe": safe,
        "schema_version": _SCHEMA,
    }


def _mission_file(paths, rel: str | None) -> dict | None:
    if not rel:
        return None
    p = Path(rel) if Path(rel).is_absolute() else paths.fleet_dir / rel \
        if getattr(paths, "fleet_dir", None) else Path(rel)
    h = _hash_or_none(p)
    return {"path": str(p), "content_hash": h} if h else None


def fleet_payload(paths, fleet, vault_rev: str | None) -> dict:
    managers = sorted({t.manager for t in fleet.teams.values() if t.manager})
    defaults = fleet.defaults or {}
    tier_lists = {}
    for k in ("skills", "mcp", "guardrails", "protocols", "expertise",
              "permissions", "hooks"):
        tier_lists[k] = canonical_hash(defaults.get(k) or [])
    return {
        "alias": fleet.name,
        "service_prefix": fleet.service_prefix,
        "mission": fleet.mission,
        "mission_file": _mission_file(paths, fleet.mission_file),
        "manager": managers[0] if len(managers) == 1 else managers,
        "groups": [
            {"name": t.name, "manager": t.manager,
             "members": sorted(t.workers), "mission": None}
            for t in sorted(fleet.teams.values(), key=lambda t: t.name)
        ],
        "org_edges": [
            {"bot": b, "reports_to": fleet.bots[b].reports_to}
            for b in sorted(fleet.bots)
        ],
        "roster": sorted(fleet.bots),
        "defaults_summary": {
            "model": str(defaults.get("model") or ""),
            "effort": defaults.get("effort"),
            "account": str(defaults.get("account") or "default"),
            "list_tier_hashes": tier_lists,
        },
        "env_keys": sorted((defaults.get("env") or {}).keys()),
        "jobs": [],                           # probe facet (see host note)
        "plugins_additional": sorted(
            getattr(fleet.plugins, "additional", []) or []),
        "vault_binding": {
            "vault_uid": None,
            "path": str(getattr(fleet, "claudron_vault_path", "") or ""),
        },
        "telegram": ({"group_alias": "fleet-group"}
                     if fleet.telegram_group_chat_id else None),
        "declared_hash": _hash_or_none(paths.fleet_yaml) or "unknown",
        "vault_rev": vault_rev,
        "schema_version": _SCHEMA,
    }


def project_payload(fleet, proj, vault_rev: str | None) -> dict:
    validation = getattr(proj, "validation", None)
    tier = getattr(validation, "tier", None) or "review"
    return {
        "key": proj.key,
        "title": proj.title,
        "repos": sorted(proj.repos),
        "tier": tier,
        "validation_hash": canonical_hash(
            getattr(proj, "raw", {}).get("validation") or {}),
        "mission_file": None,
        "declared_hash": canonical_hash(getattr(proj, "raw", {}) or {}),
        "vault_rev": vault_rev,
        "schema_version": _SCHEMA,
    }


def bot_payload(paths, fleet, bot, vault_rev: str | None) -> dict:
    tp = bot.tool_permissions
    posture = {
        "permissions_mode": bot.permission_mode or (
            "dangerously_skip" if bot.dangerously_skip_permissions
            else "acceptEdits"),
        "tool_allow": sorted(getattr(tp, "allow", []) or []),
        "tool_deny": sorted(getattr(tp, "deny", []) or []),
        "sandbox": {
            "enabled": getattr(bot.sandbox, "enabled", None),
            "auto_allow_bash": getattr(bot.sandbox, "auto_allow_bash", None),
            "config_hash": canonical_hash(
                getattr(bot.sandbox, "__dict__", {}) and {
                    k: v for k, v in vars(bot.sandbox).items()
                    if not k.startswith("_")} or {}),
        },
        "permissions_grants": {
            "count": len(bot.permissions),
            "hash": canonical_hash(sorted(bot.permissions)),
        },
        "hooks": [
            {"event": event, "matcher": h.get("matcher"),
             "cmd_hash": canonical_hash(h)}
            for event in sorted(bot.hooks)
            for h in bot.hooks[event]
        ],
        "env_keys": sorted(bot.env.keys()),
        "rc_enabled": bot.remote_control,
        "telegram": {
            "chat_alias": None,
            "require_mention": bot.telegram.require_mention,
        },
        "git_credentials_profile": (
            sorted(bot.git_credentials)[0] if bot.git_credentials else None),
    }
    group = next(
        (t.name for t in fleet.teams.values()
         if bot.bot_id in t.workers or t.manager == bot.bot_id), None)
    equipment = {
        "expertise": sorted(bot.expertise),
        "voice": bot.voice,
        "skills": sorted(bot.skills),
        "mcp": sorted(getattr(m, "name", str(m)) for m in bot.mcp),
        "integrations": sorted(bot.integrations),
        "guardrails": sorted(bot.guardrails),
        "protocols": sorted(bot.protocols),
        "resources": sorted(bot.resources),
        "lessons": sorted(bot.lessons),
        "principles": sorted(bot.principles),
        "post_actions": sorted(bot.post_actions),
        "tools": sorted(getattr(t, "name", str(t)) for t in bot.tools),
        "plugins": [],
    }
    bot_dir = paths.runtime_bots / bot.bot_id
    composed = {}
    for key, rel in (("claude_md", "CLAUDE.md"), ("bot_conf", "bot.conf"),
                     ("mcp_json", ".mcp.json"),
                     ("settings_local", ".claude/settings.local.json")):
        composed[key] = _hash_or_none(bot_dir / rel) or "absent"
    org = {
        "mission": bot.mission,
        "reports_to": bot.reports_to,
        "manages": sorted(bot.manages or []),
        "group": group,
        "scope_hash": canonical_hash(vars(bot.scope)) if bot.scope else None,
    }
    briefing = bot.briefing
    schedule = {
        "briefings": sorted(getattr(briefing, "slots", []) or [])
                     if briefing else [],
        "sprint": None,
    }
    payload = {
        "alias": f"bot:{fleet.name}/{bot.bot_id}",
        "display_name": bot.name if bot.name != bot.bot_id else None,
        "account": bot.account,
        "service": f"{fleet.service_prefix}.{bot.bot_id}",
        "model": bot.model or str((fleet.defaults or {}).get("model") or ""),
        "effort": bot.effort,
        "org": org,
        "equipment": equipment,
        "posture": posture,
        "schedule": schedule,
        "vault_binding": ({"vault_uid": None,
                           "path": bot.claudron_vault_path}
                          if bot.claudron_vault_path else None),
        "composed_hashes": composed,
        "vault_rev": vault_rev,
        "schema_version": _SCHEMA,
    }
    # declared = the declaration's stable projection (equipment + org +
    # posture + model), independent of composed artifacts
    payload["declared_hash"] = canonical_hash(
        {"org": org, "equipment": equipment, "posture": posture,
         "model": payload["model"], "effort": bot.effort})
    return payload


def library_items(paths, fleet_name: str, vault_rev: str | None):
    """Return ([(alias, payload)…], skipped). Raises nothing — unreadable
    items are SKIPPED and counted, and a nonzero count marks the scan
    incomplete (F11: a partial enumeration must never validate
    tombstones)."""
    items: list[tuple[str, dict]] = []
    skipped = 0
    shared_root = paths.root / "library"
    for kind in _LIBRARY_KINDS:
        for base in paths.library_search_dirs(kind):
            if not base.is_dir():
                continue
            is_shared = str(base).startswith(str(shared_root))
            tier = "shared" if is_shared else "fleet-overlay"
            prefix = "shared" if is_shared else fleet_name
            for entry in sorted(base.iterdir()):
                if entry.name.startswith(".") or entry.name == "README.md":
                    continue
                try:
                    if entry.is_dir():
                        chash = _tree_hash(entry)
                        name = entry.name
                    elif entry.suffix in (".md", ".json", ".yaml", ".sh"):
                        chash = _file_hash(entry)
                        name = entry.stem
                    else:
                        continue
                except OSError:
                    skipped += 1
                    continue
                alias = f"{prefix}/{kind}/{name}"
                items.append((alias, {
                    "category": kind,
                    "name": name,
                    "source_tier": tier,
                    "content_hash": chash,
                    "title": None,
                    "description": None,
                    "declared_hash": chash,
                    "vault_rev": vault_rev if tier == "fleet-overlay" else None,
                    "schema_version": _SCHEMA,
                }))
    return items, skipped


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def _vault_rev(paths) -> str | None:
    fleet_dir = getattr(paths, "fleet_dir", None)
    if not fleet_dir:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(fleet_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _in_scope(entity_type: str, alias: str, fleet_name: str) -> bool:
    """Tombstone scope: what THIS scan is authoritative for. Host, vault and
    shared library are host-global (any fleet's scan enumerates them); bots/
    projects/overlay items belong to their fleet by alias convention."""
    if entity_type in ("host", "vault"):
        return True
    if entity_type == "fleet":
        return alias == fleet_name
    if entity_type == "bot":
        return alias.startswith(f"bot:{fleet_name}/")
    if entity_type == "project":
        return alias.startswith(f"{fleet_name}/")
    if entity_type == "library_item":
        return alias.startswith(("shared/", f"{fleet_name}/"))
    return False


def run_generate_scan(paths, fleet) -> dict | None:
    """Emit one generate-cause registry scan for *fleet*. Returns the summary
    dict, or None when the fleet is UNARMED (dormancy rule). Raises only
    upward through the non-blocking hook in cmd_generate."""
    defaults_env = (fleet.defaults or {}).get("env") or {}
    import os
    armed = str(defaults_env.get("PLANE_EMIT_ENABLED")
                or os.environ.get("PLANE_EMIT_ENABLED") or "") == "1"
    if not armed:
        return None

    from .emit_api import emit_batch

    root = paths.root
    scan_id = f"scan-{uuid.uuid4().hex[:12]}"
    vault_rev = _vault_rev(paths)
    complete = True

    entities: list[tuple[str, str, dict]] = []
    entities.append(("host", platform.node(), host_payload(paths)))
    vp = vault_payload(paths, fleet)
    if vp:
        entities.append(("vault", vp["alias"], vp))
    entities.append(("fleet", fleet.name,
                     fleet_payload(paths, fleet, vault_rev)))
    for key in sorted(fleet.projects):
        entities.append((
            "project", f"{fleet.name}/{key}",
            project_payload(fleet, fleet.projects[key], vault_rev)))
    for bot_id in sorted(fleet.bots):
        p = bot_payload(paths, fleet, fleet.bots[bot_id], vault_rev)
        entities.append(("bot", p["alias"], p))
    lib_items, skipped = library_items(paths, fleet.name, vault_rev)
    for alias, payload in lib_items:
        entities.append(("library_item", alias, payload))
    if skipped:
        complete = False   # F11: a partial enumeration invalidates tombstones

    def snap(etype, alias, payload, tombstone=False):
        body = {"entity_type": etype, "entity_alias": alias,
                "cause": "generate", "scan_id": scan_id,
                "vault_rev": vault_rev}
        if tombstone:
            body["tombstone"] = True
        else:
            body["payload"] = payload
        return {"event_type": "registry_snapshot", "emitter": "generate",
                "fleet": fleet.name, "payload": body}

    events = [snap(t, a, p) for t, a, p in entities]

    # Tombstones: prior in-scope entities this COMPLETE enumeration did not
    # name. Read-only db peek; a missing db = first scan = nothing to
    # tombstone. Never against an incomplete enumeration (F11).
    tombstoned = 0
    if complete:
        from .db import db_path
        import sqlite3
        db = Path(root) / "state" / "plane" / "plane.db"
        seen = {(t, a) for t, a, _ in entities}
        if db.is_file():
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT entity_type, entity_uid, entity_alias, tombstone,"
                    " MAX(ingest_seq) FROM registry_snapshots"
                    " GROUP BY entity_type, entity_uid").fetchall()
                conn.close()
                for r in rows:
                    if r["tombstone"]:
                        continue
                    key = (r["entity_type"], r["entity_alias"])
                    if key in seen:
                        continue
                    if _in_scope(r["entity_type"], r["entity_alias"],
                                 fleet.name):
                        events.append(snap(r["entity_type"],
                                           r["entity_alias"], None,
                                           tombstone=True))
                        tombstoned += 1
            except sqlite3.Error as exc:
                log.warning("registry scan: tombstone diff skipped (%s)", exc)
                complete = False

    if vault_rev:
        events.append({
            "event_type": "declaration", "emitter": "generate",
            "fleet": fleet.name,
            "payload": {"event": "revision_seen", "subject_kind": "vault",
                        "subject": (vp or {}).get("alias", "vault"),
                        "vault_rev": vault_rev}})

    counts: dict[str, int] = {}
    for t, _a, _p in entities:
        counts[t] = counts.get(t, 0) + 1
    events.append({
        "event_type": "declaration", "emitter": "generate",
        "fleet": fleet.name,
        "payload": {"event": "scan_completed", "subject_kind": "host",
                    "subject": platform.node(), "scan_id": scan_id,
                    "scope": f"host+shared+fleet:{fleet.name}",
                    "counts": {**counts, "tombstoned": tombstoned},
                    "complete": complete, "source_rev": vault_rev}})

    outcomes = []
    for i in range(0, len(events), 50):
        outcomes.extend(emit_batch(root, events[i:i + 50]))
    by_status: dict[str, int] = {}
    for o in outcomes:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    return {"scan_id": scan_id, "entities": len(entities),
            "tombstoned": tombstoned, "complete": complete,
            "outcomes": by_status}
