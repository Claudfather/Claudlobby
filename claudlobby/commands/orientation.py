"""Read declarations and composition evidence without claiming live authority.

Environment values, MCP fragments, hook commands and arbitrary grant arguments
are never rendered. Registry snapshots describe recorded declarations, not a
running process or observed permission enforcement. SQLite read-only queries may
create their own empty WAL/SHM sidecars; no SQL, configuration or enrollment is
written, and missing storage is never provisioned.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def _context(args):
    from ..context import BotNotFoundError, generated_selectors, resolve_context
    from ..paths import InvalidPathSelector
    import yaml

    try:
        fleet, bot = generated_selectors(fleet=args.fleet, bot=getattr(args, "bot_id", None),
                                        include_bot=args.public_command == "context.show", seed=args.seed)
        return resolve_context(root=args.root, fleet=fleet, bot=bot, seed=args.seed)
    except BotNotFoundError as exc:
        raise CommandFailure("not_found", "bot is not declared in the selected fleet",
                             hint="inspect bot list in the selected fleet") from exc
    except InvalidPathSelector as exc:
        explicit = {"root": args.root is not None, "fleet": args.fleet is not None,
                    "seed": args.seed}[exc.selector]
        raise CommandFailure("invalid_argument" if explicit else "conflict",
                             "context selector is invalid",
                             hint="check --root DATA_ROOT, --fleet FLEET and mutually exclusive --seed") from exc
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", "context source was not found",
                             hint="select --root DATA_ROOT and --fleet FLEET explicitly") from exc
    except (ValueError, yaml.YAMLError) as exc:
        # Config errors may quote secret scalar values. Do not forward them.
        raise CommandFailure("conflict", "context selectors or fleet configuration are invalid",
                             hint="check the explicit root, fleet and local bot declaration") from exc


def _release(context):
    from ..activation_state import ActivationError, read_activation, read_selection
    from .releases import _executing_release

    executing = _executing_release(context.paths.root)
    expected = os.environ.get("CLAUDLOBBY_RELEASE_ID")
    valid_expected = bool(expected and re.fullmatch(r"r-[0-9a-f]{64}", expected))
    result = {"executing_release_id": executing,
              "executing_artifact_id": context.paths.package.artifact_id,
              "context_release_id": expected if valid_expected else None,
              "context_release_state": "valid" if valid_expected else "invalid" if expected is not None else "absent",
              "selected_release_id": None, "activation_status": None,
              "selection_state": "absent", "context_matches_selection": None,
              "executing_matches_selection": None, "running_release": "unknown",
              "operational_admission": "not_checked", "diagnosis_command": "host releases"}
    try:
        selected = read_selection(context.paths.root)
        if selected:
            record = read_activation(context.paths.root, selected["activation_id"])
            result.update(selected_release_id=selected["release_id"],
                          activation_status=record.status, selection_state="recorded",
                          executing_matches_selection=(executing == selected["release_id"]
                                                        if executing else None))
            if result["context_release_id"]:
                result["context_matches_selection"] = result["context_release_id"] == selected["release_id"]
            if any(record.body["intent"][key] != selected[key] for key in ("release_id", "plan_id")):
                result["selection_state"] = "conflict"
    except (ActivationError, KeyError, TypeError):
        result["selection_state"] = "unavailable"
    return result


def _composition(context):
    from ..composer import changed_manifest_inputs, read_manifest_provenance

    path = context.paths.runtime / "composed.json"
    result = {"source": str(path), "state": "absent", "composed_at": None,
              "manifest_inputs_match": None, "changed_inputs": None,
              "equipment_freshness": "not_checked"}
    provenance = read_manifest_provenance(context.paths)
    if not isinstance(provenance, dict):
        result["state"] = "unreadable" if path.exists() else "absent"
        return result
    try:
        if provenance.get("fleet") != context.fleet.name or not isinstance(provenance.get("files"), dict):
            raise ValueError("different composition context")
        changed = changed_manifest_inputs(context.fleet, context.paths, provenance)
        result.update(state="recorded", composed_at=provenance.get("composed_at"),
                      manifest_inputs_match=not changed, changed_inputs=changed)
    except (ValueError, TypeError, AttributeError):
        result["state"] = "unreadable"
    return result


def _registry(context):
    from ..plane.db import open_ro
    from ..plane.registry_read import current_entities, last_scan

    result = {"state": "unavailable", "source": "plane.registry_snapshots",
              "last_scan_at": None, "last_scan_scope": None, "last_scan_complete": None,
              "items": [], "meaning": "recorded declarations, not live execution"}
    conn, _ = open_ro(context.paths.root)
    if conn is None:
        return result
    try:
        with conn:
            rows = current_entities(conn, fleet=context.fleet.name)
            scan = last_scan(conn, fleet=context.fleet.name)
            result.update(state="read", last_scan_at=scan.get("occurred_at") if scan else None,
                          last_scan_scope=scan.get("scope") if scan else None,
                          last_scan_complete=scan.get("complete") if scan else None,
                          items=[{key: row[key] for key in (
                              "entity_type", "entity_alias", "entity_uid", "host_uid",
                              "occurred_at", "payload_hash")} for row in rows])
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        result["state"] = "unreadable"
    finally:
        conn.close()
    return result


def _base(context):
    paths, fleet = context.paths, context.fleet
    return {"host": {"data_root": str(paths.root)}, "fleet": fleet.name,
            "bot": context.bot_id, "manager": fleet.manager,
            "source": {"fleet_yaml": str(paths.fleet_yaml), "source_dir": str(paths.source_dir),
                       "projects_yaml": str(paths.projects_yaml), "seed": paths.seed},
            "release": _release(context), "composition": _composition(context),
            "registry": _registry(context)}


def _file(path):
    try:
        if not path.is_file():
            return {"path": str(path), "state": "unreadable" if path.exists() else "absent", "sha256": None}
        content = path.read_bytes()
        return {"path": str(path), "state": "present", "sha256": hashlib.sha256(content).hexdigest()}
    except FileNotFoundError:
        return {"path": str(path), "state": "absent", "sha256": None}
    except OSError:
        return {"path": str(path), "state": "unreadable", "sha256": None}


def _rule(grant):
    # Even Bash arguments can contain credentials. Rule digests preserve exact
    # comparison; tool names and provenance remain useful without dumping them.
    name = grant.split("(", 1)[0]
    return {"tool": name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.*-]*", name) else "unknown",
            "sha256": hashlib.sha256(grant.encode()).hexdigest()}


def _permissions(context, bot, settings_path):
    from ..freshbox import BASE_TOOLS, _sourced_grants, classify_grants

    declared = {"allow": [_rule(value) for value in bot.tool_permissions.allow],
                "deny": [_rule(value) for value in bot.tool_permissions.deny]}
    result = {"declared_overrides": declared, "composed": {"state": "absent"},
              "enforcement": "unknown", "arguments": "withheld; exact rules identified by SHA-256"}
    try:
        if not settings_path.is_file():
            result["composed"]["state"] = "unreadable" if settings_path.exists() else "absent"
            return result
        settings = json.loads(settings_path.read_text())
        permissions = settings.get("permissions", {})
        allow, deny = permissions.get("allow", []), permissions.get("deny", [])
        if any(not isinstance(values, list) or not all(isinstance(v, str) for v in values)
               for values in (allow, deny)):
            raise ValueError("invalid permission list")
    except FileNotFoundError:
        return result
    except (OSError, ValueError, AttributeError):
        result["composed"] = {"state": "unreadable"}
        return result
    sourced = _sourced_grants(bot, context.fleet, context.paths)
    result["composed"] = {"state": "present", "source": str(settings_path),
                          "allow": [_rule(value) for value in allow],
                          "deny": [_rule(value) for value in deny],
                          "provenance_findings": [
                              {"kind": kind, "severity": severity, "rule": _rule(grant)}
                              for kind, severity, grant in classify_grants(
                                  allow, deny, sourced, set(BASE_TOOLS), set(bot.tool_permissions.allow))]}
    return result


def _bot(context, bot_id, *, capabilities=False):
    from ..composer import _channel_plugins
    from ..plane.registry_emit import bot_payload

    bot, paths = context.fleet.bots[bot_id], context.paths
    projection = bot_payload(paths, context.fleet, bot, None)
    # The registry's plugins slot is not implemented; do not present its empty
    # placeholder as evidence that no plugins are configured or installed.
    equipment = {key: value for key, value in projection["equipment"].items() if key != "plugins"}
    directory = paths.bot_runtime(bot_id)
    result = {"bot_id": bot_id, "name": bot.name, "alias": projection["alias"],
              "role": "manager" if bot_id == context.fleet.manager else "worker",
              "manager": context.fleet.manager,
              "declared": {"mission": bot.mission, "reports_to": bot.reports_to,
                           "manages": list(bot.manages or []), "expertise": list(bot.expertise),
                           "skills": list(bot.skills), "mcp": [item.name for item in bot.mcp],
                           "integrations": list(bot.integrations), "protocols": list(bot.protocols),
                           "tools": [item.name for item in bot.tools],
                           "fleet_plugins": list(context.fleet.plugins.required),
                           "channel_plugins": _channel_plugins(bot.channels)},
              "effective_configuration": {"equipment": equipment,
                                          "source": "config loader and compositor resolvers"},
              "composed": {"directory": str(directory), "files": {
                  name: _file(directory / name) for name in (
                      "CLAUDE.md", "bot.conf", ".mcp.json", ".claude/settings.local.json")}},
              "runtime_observed": {"session": "not_probed", "release": "unknown",
                                   "permission_enforcement": "unknown"}}
    if capabilities:
        result["permissions"] = _permissions(context, bot, directory / ".claude/settings.local.json")
    return result


def _project(context, project):
    source = "derived_from_bot_scope" if context.fleet.projects_derived else "projects_yaml_or_loader_default"
    return {"key": project.key, "title": project.title, "repos": sorted(project.repos),
            "mission_file": project.mission_file,
            "mission_base": str(context.paths.fleet_config_dir),
            "validation": {"tier": project.validation.tier, "source": source,
                           "source_path": str(context.paths.fleet_yaml if context.fleet.projects_derived
                                              else context.paths.projects_yaml),
                           "checks_observed": "unknown", "executed_by_this_command": False}}


def dispatch(args) -> CommandOutput:
    context = _context(args)
    command = args.public_command
    data = {"context": _base(context)}
    fleet = context.fleet
    lines = [f"Fleet {fleet.name}; manager {fleet.manager}; data {context.paths.root}."]
    release = data["context"]["release"]
    lines.append(f"Executing release: {release['executing_release_id'] or 'unverified'}; "
                 f"selected: {release['selected_release_id'] or release['selection_state']}; running: unknown.")
    if command == "context.show":
        lines.append(f"Bot: {context.bot_id or 'not selected'}. Running release: unknown.")
    elif command == "fleet.show":
        data["fleet"] = {"name": fleet.name, "manager": fleet.manager,
                         "workers": sorted(set(fleet.bots) - {fleet.manager}),
                         "mission": fleet.mission, "mission_file": fleet.mission_file,
                         "teams": [{"name": team.name, "manager": team.manager,
                                    "workers": list(team.workers)} for team in fleet.teams.values()]}
        lines.append(f"Declared bots: {', '.join(sorted(fleet.bots))}.")
        if fleet.mission:
            lines.append(f"Declared mission: {fleet.mission}")
        if fleet.mission_file:
            lines.append(f"Mission pointer: {fleet.mission_file}")
    elif command == "bot.list":
        data.update(items=[_bot(context, name) for name in sorted(fleet.bots)], next_cursor=None)
        lines.extend(f"{item['bot_id']}  {item['role']}  session not probed" for item in data["items"])
    elif command in {"bot.show", "bot.capabilities"}:
        data["bot"] = _bot(context, context.bot_id, capabilities=command == "bot.capabilities")
        lines.append(f"{context.bot_id}: {data['bot']['role']}; runtime permission enforcement unknown.")
        equipment = data["bot"]["effective_configuration"]["equipment"]
        lines.extend(f"Effective {kind}: {', '.join(equipment[kind]) or 'none'}"
                     for kind in ("expertise", "skills", "tools", "mcp"))
        if command == "bot.capabilities":
            composed = data["bot"]["permissions"]["composed"]
            lines.append(f"Composed permission settings: {composed['state']}; "
                         f"{len(composed.get('allow', []))} allow rules, {len(composed.get('deny', []))} deny rules.")
            if composed.get("deny"):
                lines.append("Composed deny tools: " + ", ".join(sorted({rule["tool"] for rule in composed["deny"]})))
    else:
        projects = fleet.projects
        if command == "project.show":
            if args.project_id not in projects:
                raise CommandFailure("not_found", "project is not declared in the selected fleet")
            data["project"] = _project(context, projects[args.project_id])
            shown = [data["project"]]
        else:
            shown = [_project(context, projects[key]) for key in sorted(projects)]
            data.update(items=shown, next_cursor=None)
        lines.extend(f"{item['key']}: {item['validation']['tier']} "
                     f"({item['validation']['source']}); checks not observed." for item in shown)
        lines.extend(f"Mission pointer: {item['mission_file']} (relative to {item['mission_base']})"
                     for item in shown if item["mission_file"])
    return CommandOutput(data, data["context"]["release"]["executing_release_id"], tuple(lines))
