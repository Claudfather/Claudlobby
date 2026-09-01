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

Dormancy (estate rule): armed per fleet through the runtime's .env tier
cascade (``PLANE_EMIT_ENABLED=1`` in the fleet-tier ``.env`` — the SAME
resolution the composer uses for briefing-timer arming). NOTE the carrier
split: ``fleet.yaml env:`` reaches composed ``bot.conf`` (runtime doors and
hooks read it) but does NOT arm generate-time scans — the .env tier arms
BOTH, so it is the recommended single carrier. A root pull must never
switch on new emission; the generate hook is NON-BLOCKING (a scan failure
logs and never breaks generate).

F11 boundary: this emitter enforces the PREVENTION half — incomplete
enumerations never tombstone, empty-but-complete tombstones its scope.
The VALIDATION half lives in ``queries.py`` (the shared effective-rows
CTE — see ``_F11_COMPLETION_JOIN``), and the write side consults the
same definition for suppression (``REG_CURRENT_KEYS_SQL`` below;
chunk B closed the IOU this paragraph used to carry).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from ..claudron_compat import COMPAT_FLOOR
from ..paths import _iter_fleet_dirs
from .canonical import CanonicalizationError, canonical_hash

log = logging.getLogger("claudlobby.plane.registry")

# Library categories are DISCOVERED, never hand-listed: the shipped list
# missed `integrations` (never keyframed, never tombstonable) and scanned a
# nonexistent `voices` dir — the #1009 hand-list class, made worse here by
# the hash gate freezing the omission forever (gauntlet round 1).
def _library_kinds(paths) -> list[str]:
    kinds: set[str] = set()
    for base in (paths.base_library,
                 getattr(paths, "overlay_library", None)):
        if base and base.is_dir():
            kinds.update(
                d.name for d in base.iterdir()
                if d.is_dir() and not d.name.startswith(".")
                and d.name != "__pycache__")
    return sorted(kinds)

_SCHEMA = "1"


def _file_hash(p: Path) -> str:
    # sha256:-prefixed like canonical_hash — ONE hash rendering estate-wide
    # (CANON_V1); a probe-facet emitter re-hashing the same item must
    # reproduce the exact string or every item flickers between emitters.
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


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


def _fallback_normalize(obj):
    """Total, deterministic normalization for the fallback hash: dict keys
    sorted by repr (mixed int/bool/str keys — YAML 1.1's `on:`/`2026:` — must
    never raise: the r1 fallback's own sorted() re-created the vaporize one
    level down, probed r2); sets sorted by repr (a raw set repr is
    PYTHONHASHSEED-nondeterministic across processes — keyframe churn)."""
    if isinstance(obj, dict):
        return [[repr(k), _fallback_normalize(v)]
                for k, v in sorted(obj.items(), key=lambda kv: repr(kv[0]))]
    if isinstance(obj, (list, tuple)):
        return [_fallback_normalize(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(repr(v) for v in obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


def _safe_hash(obj) -> str:
    """canonical_hash that DEGRADES instead of raising: CANON_V1 refuses
    floats and non-string keys, and one such value in a project's raw YAML
    must not vaporize the whole scan (gauntlet r1 measured; r2 hardened the
    fallback itself). The fallback is json-over-a-total-normalizer —
    deterministic across processes, which is all the hash gate needs."""
    try:
        return canonical_hash(obj)
    except (CanonicalizationError, TypeError, ValueError):
        blob = json.dumps(_fallback_normalize(obj), sort_keys=False,
                          ensure_ascii=False, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Payload assembly (§9b — deterministic, volatile facts excluded per F12)
# ---------------------------------------------------------------------------

def host_payload(paths) -> dict:
    disk = shutil.disk_usage("/")
    try:
        page = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
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
    # paths._iter_fleet_dirs — the ONE nested-aware fleet walk. The shipped
    # depth-1 glob measured [] on the live nested-vault host (gauntlet r1);
    # spec: "fleet aliases from manifests — NEVER process inference".
    declared_fleets = sorted(
        d.name for d in _iter_fleet_dirs(paths.root / "local")
        if (d / "fleet.yaml").is_file())
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


def _fleet_vault_path(fleet) -> str | None:
    # the fleet-tier value lives in defaults (config.py merges it there);
    # FleetConfig has no claudron_vault_path attr — the shipped getattr
    # returned None forever (gauntlet r1)
    return (fleet.defaults or {}).get("claudron_vault_path") or None


def vault_payload(paths, fleet) -> dict | None:
    vp = _fleet_vault_path(fleet) or next(
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
    # the floor summary = the highest slated release across the capability
    # tuple (deterministic; "pinned SHA" entries sort in but never win a
    # semver max). ok is None, NEVER a hardcoded True: no compat probe runs
    # here, and a fabricated verdict frozen by the hash gate is the §16 sin
    # this whole lane exists to kill (gauntlet r1 — the shipped True lied,
    # and the shipped getattr FLOOR fallback made floor permanently
    # "unset": under a hash gate, defaulted getattr is strictly worse than
    # the bare attribute).
    # semver-SHAPED entries only, compared as int tuples: the lexical max
    # returned 'unbuilt — demand-gated' on the live floor (it sorts after
    # every digit) and '0.10.0' < '0.9.0' lexically — both probed, and the
    # hash gate would have frozen the wrong floor forever (gauntlet r2).
    _semver = [tuple(int(x) for x in c.default_order_release.split("."))
               for c in COMPAT_FLOOR
               if re.fullmatch(r"\d+\.\d+\.\d+", c.default_order_release)]
    floor = ".".join(str(x) for x in max(_semver)) if _semver else "none"
    return {
        "alias": vpath.name,
        "role": "primary",
        "mount_path": str(vpath),
        "remote": remote,                      # sensitive (§11) at render
        "compat": {"floor": floor, "ok": None},
        "carries_fleets": (vpath / "local").is_dir()
                          or "local" in vpath.parts,
        "gitignore_safe": safe,
        "schema_version": _SCHEMA,
    }


def _mission_file(paths, rel: str | None) -> dict | None:
    if not rel:
        return None
    # fleet_config_dir / rel — the composer's own resolution (composer.py
    # resolves this exact field the same way), and Path(dir) / "/abs"
    # yields the absolute path anyway, so ONE expression covers both; the
    # stored path is absolute (CANON_V1 producer's duty). The shipped
    # CWD-dependent fallback flickered the hash per invocation dir
    # (gauntlet r1, measured).
    base = getattr(paths, "fleet_config_dir", None) or paths.root
    p = base / rel
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
            "path": str(_fleet_vault_path(fleet) or ""),
        },
        "telegram": ({"group_alias": "fleet-group"}
                     if fleet.telegram_group_chat_id else None),
        "declared_hash": _hash_or_none(paths.fleet_yaml) or "unknown",
        "vault_rev": vault_rev,
        "schema_version": _SCHEMA,
    }


def project_payload(paths, fleet, proj, vault_rev: str | None) -> dict:
    validation = getattr(proj, "validation", None)
    tier = getattr(validation, "tier", None) or "review"
    return {
        "key": proj.key,
        "title": proj.title,
        "repos": sorted(proj.repos),
        "tier": tier,
        "validation_hash": _safe_hash(
            getattr(proj, "raw", {}).get("validation") or {}),
        "mission_file": _mission_file(paths, proj.mission_file),
        "declared_hash": _safe_hash(getattr(proj, "raw", {}) or {}),
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
            "config_hash": _safe_hash(
                {k: v for k, v in vars(bot.sandbox).items()
                 if not k.startswith("_")}),
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
        "scope_hash": _safe_hash(vars(bot.scope)) if bot.scope else None,
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
    for kind in _library_kinds(paths):
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


def _in_scope(entity_type: str, alias: str, fleet_name: str,
              scanned: set) -> bool:
    """Tombstone scope: what THIS scan is authoritative for. Host and shared
    library are host-global (every scan enumerates them). The VAULT is in
    scope only for the EXACT alias this scan enumerated: vault enumeration
    is fleet-binding-dependent, so neither a vaultless fleet (r1, probed)
    nor a fleet bound to a DIFFERENT vault (r2, probed — the ping-pong one
    fleet over) may tombstone a sibling's vault. Disclosed trade-off: a
    fleet that re-binds to a new vault leaves its OLD vault keyframe
    standing — no generate scan owns that alias any more; an operator-cause
    emission (equip/migration) retires it. Bots/projects/overlay items
    belong to their fleet by alias convention (every prefix includes a
    separator, so prefix-name siblings cannot collide)."""
    if entity_type == "host":
        return True
    if entity_type == "vault":
        return ("vault", alias) in scanned
    if entity_type == "fleet":
        return alias == fleet_name
    if entity_type == "bot":
        return alias.startswith(f"bot:{fleet_name}/")
    if entity_type == "project":
        return alias.startswith(f"{fleet_name}/")
    if entity_type == "library_item":
        return alias.startswith(("shared/", f"{fleet_name}/"))
    return False


def assemble_entities(paths, fleet, vault_rev):
    """The scan's enumeration, PURE: ([(entity_type, alias, payload)…],
    complete). Extracted (chunk B) so hash-verification can re-derive the
    estate through the SAME assembly the emitter records — a second
    assembly would drift exactly like a second env cascade (#1226)."""
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
            project_payload(paths, fleet, fleet.projects[key], vault_rev)))
    for bot_id in sorted(fleet.bots):
        p = bot_payload(paths, fleet, fleet.bots[bot_id], vault_rev)
        entities.append(("bot", p["alias"], p))
    lib_items, skipped = library_items(paths, fleet.name, vault_rev)
    for alias, payload in lib_items:
        entities.append(("library_item", alias, payload))
    # F11: a partial enumeration invalidates tombstones
    return entities, not skipped


def run_generate_scan(paths, fleet) -> dict | None:
    """Emit one generate-cause registry scan for *fleet*. Returns the summary
    dict, or None when the fleet is UNARMED (dormancy rule). Raises only
    upward through the non-blocking hook in cmd_generate."""
    # Arming resolves through the runtime's OWN .env tier cascade —
    # env_tiers, exactly as the composer resolves the SAME variable for
    # briefing timers. The shipped check read fleet.defaults["env"], a tier
    # the estate does not use: measured armed-the-documented-way -> None —
    # dead in production while every test passed (gauntlet r1). A resolver
    # failure means UNARMED (dormancy fails closed); PLANE_EMIT_DISABLED=1
    # is the ruled harness exemption, honored here like every door.
    if os.environ.get("PLANE_EMIT_DISABLED") == "1":
        return None
    from .. import env_tiers as _env_tiers
    try:
        _res = _env_tiers.resolve(
            paths, fleet_name=fleet.name).get("PLANE_EMIT_ENABLED")
    except _env_tiers.ResolverUnavailable as exc:
        log.warning("registry scan: arming unresolved (%s) — UNARMED", exc)
        return None
    if _res is None or _res.value != "1":
        return None

    from .emit_api import emit_batch

    root = paths.root
    scan_id = f"scan-{uuid.uuid4().hex[:12]}"
    vault_rev = _vault_rev(paths)
    entities, complete = assemble_entities(paths, fleet, vault_rev)

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
    foreign_rows = 0
    if complete:
        import sqlite3
        db = Path(root) / "state" / "plane" / "plane.db"  # pure join (no
        # db_path(): its mkdir is a write this read path must not carry)
        seen = {(t, a) for t, a, _ in entities}
        if db.is_file():
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                # THIS host's envelope uid — the SAME value ingest stamps
                # rows with, READ from the uid file, never ensure_host_uid:
                # ensure_ MINTS on absence (mkdir+write — a write this read
                # path must not carry, r2 probed), and a fresh uid would
                # filter every row into a FALSE CLEAN. Absent/invalid file
                # -> the diff is skipped LOUDLY, never silently empty.
                uid_file = Path(root) / "state" / "host-uid"
                try:
                    this_host = uid_file.read_text().strip()
                except OSError:
                    this_host = ""
                if not this_host:
                    log.warning(
                        "registry scan: host-uid unreadable at %s —"
                        " tombstone diff SKIPPED (cannot scope rows to this"
                        " host)", uid_file)
                    raise sqlite3.Error("host-uid unreadable")
                rows = conn.execute(
                    "SELECT entity_type, entity_uid, entity_alias, tombstone,"
                    " MAX(ingest_seq) FROM registry_snapshots"
                    " WHERE origin = 'live' AND host_uid = ?"
                    " GROUP BY entity_type, entity_uid",
                    (this_host,)).fetchall()
                # rows recorded under OTHER hosts (legacy imports): counted
                # and DISCLOSED, never silently excluded (r2)
                foreign_rows = conn.execute(
                    "SELECT COUNT(*) FROM registry_snapshots"
                    " WHERE host_uid != ?", (this_host,)).fetchone()[0]
                # Tombstone eligibility asks the READER'S question — is the
                # entity still current? (chunk-B gauntlet SEV-1; the full
                # story lives at queries.REG_CURRENT_POINT_SQL.) One bulk
                # read of the reader's own SQL. Known residual, disclosed:
                # candidates are origin='live' while current is
                # origin-blind, so a legacy-origin row that is current can
                # never be tombstoned by a scan — operator-cause emission
                # is its only retirement (r3, probed).
                from .queries import REG_CURRENT_KEYS_SQL
                current_keys = {
                    (cr["entity_type"], cr["entity_uid"])
                    for cr in conn.execute(REG_CURRENT_KEYS_SQL,
                                           (this_host,))}
                conn.close()
                for r in rows:
                    if (r["entity_type"], r["entity_uid"]) not in current_keys:
                        continue   # effectively deleted — nothing to re-claim
                    key = (r["entity_type"], r["entity_alias"])
                    if key in seen:
                        continue
                    if _in_scope(r["entity_type"], r["entity_alias"],
                                 fleet.name, seen):
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
    # One cold-path CLI spawn + transaction per chunk; 50 keeps a ~190-event
    # fleet scan to ~4 spawns while staying far under any payload cap.
    _CHUNK = 50
    for i in range(0, len(events), _CHUNK):
        outcomes.extend(emit_batch(root, events[i:i + _CHUNK]))
    by_status: dict[str, int] = {}
    for o in outcomes:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    return {"scan_id": scan_id, "entities": len(entities),
            "tombstoned": tombstoned, "complete": complete,
            "foreign_host_rows": foreign_rows,
            "outcomes": by_status}
