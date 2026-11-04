"""Read one sealed host baseline for explicit conversion; never execute or repair it.

This is historical evidence, not a current Git configuration or native-state
observation. The common update owner separately fences and observes consumers.
"""
from dataclasses import dataclass, field
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .activation_state import ActivationRefusal, read_activation, read_selection
from .active_config import context_from_plan
from .activation_identity import read_registry_identity_bindings, validate_current_identity_bindings
from .config_install import _desired, read_config_install
from .config_plan import path_state, read_plan
from .releases import read_release
from .supervision_inventory import UnitDeclaration, _target

_PHASES = ("ingest", "bots", "producers")
_COMPLETE = ("producers_paused", "sessions_handed_off", "sessions_quiesced", "ingest_quiesced",
             "queues_classified", "backup_saved", "migration_applied", "selection_switched",
             "configuration_applied", "ingest_started", "bots_started", "verified", "producers_resumed")
_SHA = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class SealedBaseline:
    root: Path
    selection: dict
    selector_bytes: bytes
    selector_sha256: str
    selector_path: Path
    activation: object
    release: object
    plan: object
    contexts: dict
    identities: dict
    declarations: tuple[UnitDeclaration, ...]
    units: tuple[dict, ...]
    placements: tuple[dict, ...]
    manager: str
    domain: str
    install_directory: Path
    evidence: dict
    interruption: dict | None = None
    authored_plan: object | None = None
    parked: dict = field(default_factory=dict)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.parent.resolve() != path.parent:
        raise ActivationRefusal("sealed baseline path is redirected")
    return path


def _read(path, evidence):
    path = _path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ActivationRefusal("sealed baseline evidence is not an owned plain file")
    content = path.read_bytes()
    current = path.lstat()
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(current, name) != getattr(info, name) for name in fields):
        raise ActivationRefusal("sealed baseline evidence changed during read")
    evidence[str(path)] = hashlib.sha256(content).hexdigest()
    return content


def _state(plan, target):
    states = []
    for change in plan.changes:
        path, after = Path(change.target), change.after
        if target == path:
            states.append(after)
        elif after.get("kind") == "tree" and target.is_relative_to(path):
            item = after["files"].get(str(target.relative_to(path)))
            if item is not None:
                states.append({"kind": "file", **item})
    if len(states) != 1:
        raise ActivationRefusal("sealed baseline has no unique frozen output")
    return states[0]


def _complete(body):
    if (body.get("status") != "active" or body.get("pending") is not None
            or body.get("completed") != list(_COMPLETE)
            or "upgrade_abort" in body or "adoption_abort" in body):
        raise ActivationRefusal("sealed activation is incomplete or retains recovery intent")
    handoffs = body.get("handoff_effects", {})
    if not isinstance(handoffs, dict) or any(value not in ("handed_off", "server_absent") for value in handoffs.values()):
        raise ActivationRefusal("sealed activation retains an unresolved handoff")
    for name in ("start_effects", "bot_quiescence"):
        effects = body.get(name, {})
        if not isinstance(effects, dict) or any(not isinstance(row, dict) or row.get("result") is None
                                                for row in effects.values()):
            raise ActivationRefusal("sealed activation retains an unresolved native effect")


def _activation_names(root):
    directory = _path(root / "state/activations")
    if directory.resolve() != directory:
        raise ActivationRefusal("sealed activation store is redirected")
    names = []
    for path in directory.iterdir():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ActivationRefusal("sealed activation store contains unverified evidence")
        names.append(path.name)
    return tuple(sorted(names))


def _journal_rows(plan, body, status):
    """Match the retained writer's targets, allowing only its ancestor ensures."""
    changes = {change.target: change for change in plan.changes}
    rows = body["rows"]
    if not isinstance(rows, list) or len(changes) != len(plan.changes):
        raise ActivationRefusal("sealed publication rows are ambiguous")
    seen = set()
    for row in rows:
        target = _path(row["target"])
        if (str(target) in seen or row["status"] != status
                or row["parent"] != str(target.parent.resolve())
                or status == "pending" and set(row) != {"target", "before", "after", "ensure", "parent", "status"}):
            raise ActivationRefusal("sealed publication row is incomplete or changed")
        seen.add(str(target))
        change = changes.get(str(target))
        if change is not None:
            valid = (row["before"] == change.before and row["after"] == _desired(change.after)
                     and row["ensure"] is (change.after["kind"] == "directory"))
        else:
            valid = (row["ensure"] is True and row["before"] == {"resolved": str(target), "node": {"kind": "absent"}}
                     and row["after"] == {"kind": "directory", "mode": 0o755, "entries": {}}
                     and any(target in Path(path).parents for path in changes)
                     and (status != "applied" or isinstance(row.get("identity"), list)
                         and len(row["identity"]) == 2 and all(type(value) is int and value >= 0 for value in row["identity"])))
        if not valid:
            raise ActivationRefusal("sealed publication rows differ from frozen targets")
    if not changes.keys() <= seen:
        raise ActivationRefusal("sealed publication lacks an exact target row")


def _untouched_abort(record, plan, journals, root, evidence):
    """Recognize only later parking left unstarted by a recorded early abort."""
    body, effects = record.body, plan.effects
    phase, aid = effects.get("phase"), record.activation_id
    if (record.status != "rolled_back" or body.get("pending") is not None or phase not in {"bots", "ingest"}
            or effects.get("activation_id") != aid or body.get("start_effects") or body.get("start_phases")
            or body.get("bot_quiescence") or "identity_bindings" in body):
        return False
    abort = body.get("upgrade_abort") or body.get("adoption_abort")
    if not isinstance(abort, dict) or not isinstance(abort.get("result"), dict):
        return False
    if "upgrade_abort" in body:
        previous = body.get("previous_selection")
        valid = ("adoption_abort" not in body and isinstance(previous, dict)
            and body["intent"].get("source_kind") != "legacy-unsealed"
            and body["intent"].get("source_release_id") == previous.get("release_id") == plan.release_id
            and body.get("completed") == ["producers_paused"] and set(body.get("evidence", {})) == {"producers_paused"}
            and body.get("forward") in [dict(status="activating", completed=["producers_paused"], pending=pending,
                evidence=body["evidence"]) for pending in (None, "sessions_handed_off")])
        if valid:
            journal = read_config_install(root, aid)
            raw = json.loads(_read(journal.directory / "journal.json", evidence))["journal"]
            candidate = read_plan(root, journal.plan_id)
            _read(candidate.directory / "plan.json", evidence)
            valid = journal.status == "prepared" and journal.plan_id == body["intent"]["plan_id"]
            if valid:
                _journal_rows(candidate, raw, "pending")
    else:
        valid = (body["intent"].get("source_kind") == "legacy-unsealed"
            and body["intent"].get("source_release_id") is None and body.get("previous_selection") is None
            and body.get("completed") == [] and body.get("evidence") == {} and not body.get("handoff_effects")
            and body.get("forward") == dict(status="activating", completed=[], pending="producers_paused", evidence={}))
    if not valid:
        return False
    producer_id = "units-" + hashlib.sha256(_json([aid, "producers"])).hexdigest()
    producer = journals.get(producer_id)
    if producer is None:
        return False
    journal, producer_plan, raw = producer
    shared = {key: value for key, value in effects.items() if key != "phase"}
    if (journal.status != "rolled_back" or producer_plan.effects.get("phase") != "producers"
            or {key: value for key, value in producer_plan.effects.items() if key != "phase"} != shared
            or effects.get("enrollment_digest") != body["intent"].get("enrollment_digest")
            or hashlib.sha256(_json(effects["enrollment"])).hexdigest() != effects["enrollment_digest"]):
        return False
    for parking in (plan, producer_plan):
        membership = effects["phases"]
        if set(membership) != set(_PHASES):
            return False
        phase_targets = set(membership[parking.effects["phase"]])
        parked = {unit["installed"][0]["path"] for unit in effects["enrollment"]["units"]
                  if unit["target"] in phase_targets and len(unit["installed"]) == 1}
        if ({change.target for change in parking.changes} != parked
                or any(change.after != {"kind": "absent"} for change in parking.changes)):
            return False
    _journal_rows(producer_plan, raw, "reverted")
    targets = [unit["target"] for unit in effects["enrollment"]["units"] if unit["target"] in effects["phases"]["producers"]]
    receipt = dict(phase="producers", journal_id=producer_id, plan_id=producer_plan.plan_id,
                   targets=targets, operation="aborted")
    return abort["result"].get("evidence") == hashlib.sha256(_json(receipt)).hexdigest()


def _settled_history(root, names, evidence, interruption=None):
    """One-time conversion gate, including effects newer than the selected release."""
    records, journals = {}, {}
    for name in names:
        directory = root / "state/activations" / name
        path = directory / "activation.json"
        if os.path.lexists(path):
            record = read_activation(root, name)
            _read(path, evidence)
            allowed = interruption is not None and name == interruption["activation_id"]
            if not allowed and (record.status not in {"active", "rolled_back"} or record.body.get("pending") is not None):
                raise ActivationRefusal("another sealed lifecycle is unfinished; reconcile its original request before conversion")
            records[name] = record
        else:
            if not re.fullmatch(r"(?:units|enrollment)-[0-9a-f]{64}", name):
                raise ActivationRefusal("sealed activation store contains unverified evidence")
            journal = read_config_install(root, name)
            raw = json.loads(_read(journal.directory / "journal.json", evidence))["journal"]
            plan = read_plan(root, journal.plan_id)
            _read(plan.directory / "plan.json", evidence)
            owner = "activation-units-v1" if name.startswith("units-") else "activation-enrollment-v1"
            aid, phase = plan.effects.get("activation_id"), plan.effects.get("phase")
            phases = _PHASES if name.startswith("units-") else (*_PHASES, "directories")
            if (plan.effects.get("owner") != owner or phase not in phases
                    or name != name.split("-", 1)[0] + "-" + hashlib.sha256(_json([aid, phase])).hexdigest()):
                raise ActivationRefusal("sealed native journal differs from its recorded parent")
            journals[name] = (journal, plan, raw)
    for name, (journal, plan, raw) in journals.items():
        if journal.status in {"applied", "rolled_back"}:
            continue
        if interruption is not None and name in interruption["journals"].values():
            continue  # Its exact membership/progress was proved by _interrupted_upgrade.
        record = records.get(plan.effects.get("activation_id"))
        if (journal.status != "prepared" or not name.startswith("units-") or record is None
                or not _untouched_abort(record, plan, journals, root, evidence)):
            raise ActivationRefusal("sealed native publication retains unfinished recovery evidence")
        _journal_rows(plan, raw, "pending")


def _interrupted_upgrade(root, names, selected, selected_plan, rows, placements, evidence):
    """Read one parked Linux upgrade before configuration/ingest publication.

    No historical effect is completed or retried. Its before-images only explain
    why exact native paths are absent; current native disposition is observed by
    the common inventory and lifecycle owners.
    """
    unfinished = [read_activation(root, name) for name in names
                  if (root / "state/activations" / name / "activation.json").exists()
                  and read_activation(root, name).status not in {"active", "rolled_back"}]
    if not unfinished:
        return None, None, {}
    if len(unfinished) != 1:
        raise ActivationRefusal("more than one sealed lifecycle is unfinished")
    record = unfinished[0]
    body, intent, aid = record.body, record.body["intent"], record.activation_id
    if (body.get("previous_selection") != selected or body.get("status") != "activating"
            or body.get("completed") != ["producers_paused"] or body.get("pending") != "sessions_handed_off"
            or set(body.get("evidence", {})) != {"producers_paused"}
            or intent.get("source_release_id") != selected["release_id"]
            or intent.get("source_kind") == "legacy-unsealed" or intent.get("quiesce_mode") != "per-bot-v1"
            or any(body.get(key) for key in ("start_effects", "start_phases"))
            or any(key in body for key in ("identity_bindings", "forward", "upgrade_abort", "adoption_abort"))
            or os.path.lexists(root / "state/activations" / aid / "migration.json")):
        raise ActivationRefusal("unfinished sealed lifecycle is outside the pre-publication parking boundary")
    _read(root / "state/activations" / aid / "activation.json", evidence)
    candidate = read_plan(root, intent["plan_id"])
    _read(candidate.directory / "plan.json", evidence)
    release = read_release(root, intent["release_id"], verify_files=False)
    _read(release.directory / "release.json", evidence)
    if candidate.release_id != release.release_id or candidate.release_seal != release.seal_sha256:
        raise ActivationRefusal("unfinished upgrade candidate identity differs")
    config = read_config_install(root, aid)
    raw = json.loads(_read(config.directory / "journal.json", evidence))["journal"]
    if config.status != "prepared" or config.plan_id != candidate.plan_id:
        raise ActivationRefusal("unfinished upgrade configuration publication has begun")
    _journal_rows(candidate, raw, "pending")
    for entry in raw["rows"]:
        if path_state(Path(entry["target"])) != entry["before"]:
            raise ActivationRefusal("unfinished upgrade generated configuration changed before publication")
    selected_rows = {row["source"]: row for row in rows if row["platform"] == "Linux"}
    installed = {entry["source"]: entry for entry in placements}
    if intent.get("install_directory") not in {str(Path(entry["installed"]).parent) for entry in placements}:
        raise ActivationRefusal("unfinished upgrade changes native install directory")
    for phase in (*_PHASES, "directories"):
        identifier = "enrollment-" + hashlib.sha256(_json([aid, phase])).hexdigest()
        if os.path.lexists(root / "state/activations" / identifier):
            raise ActivationRefusal("unfinished upgrade candidate native publication has begun")
    journals, parked, shared = {}, {}, None
    for phase in _PHASES:
        identifier = "units-" + hashlib.sha256(_json([aid, phase])).hexdigest()
        journal = read_config_install(root, identifier)
        raw = json.loads(_read(journal.directory / "journal.json", evidence))["journal"]
        plan = read_plan(root, journal.plan_id)
        _read(plan.directory / "plan.json", evidence)
        effects = plan.effects
        common = {key: value for key, value in effects.items() if key != "phase"}
        if (effects.get("owner") != "activation-units-v1" or effects.get("activation_id") != aid
                or effects.get("phase") != phase or effects.get("enrollment_digest") != intent.get("enrollment_digest")
                or shared is not None and shared != common
                or plan.release_id != selected_plan.release_id or plan.release_seal != selected_plan.release_seal):
            raise ActivationRefusal("unfinished upgrade parking identity differs")
        shared = common
        enrollment, phases = effects["enrollment"], effects["phases"]
        frozen = enrollment["units"]
        targets = [unit["target"] for unit in frozen]
        if (enrollment.get("manager") != "Linux" or enrollment.get("data_root") != str(root)
                or enrollment.get("legacy_source") or enrollment.get("bootstrap_empty")
                or hashlib.sha256(_json(enrollment)).hexdigest() != intent["enrollment_digest"]
                or set(phases) != set(_PHASES) or len(targets) != len(set(targets))
                or len(frozen) != len(selected_rows)
                or sorted(unit["target"] for unit in frozen if unit["installed"] or dict(unit["properties"]).get("LoadState") == "loaded")
                   != sorted(target for members in phases.values() for target in members)
                or {unit["declaration"]["source"] for unit in frozen} != set(selected_rows)):
            raise ActivationRefusal("unfinished upgrade parking roster differs")
        for unit in frozen:
            declaration = unit["declaration"]
            original = selected_rows[declaration["source"]]
            if (any(declaration.get(key) != original.get(key) for key in ("scope", "fleet", "bot", "service", "release_id", "working_directory"))
                    or dict(declaration["environment"]) != original["environment"]
                    or unit["generated"]["sha256"] != original["sha256"]
                    or unit["target"] != Path(original["source"]).name
                    or not original["enroll"] and (unit["installed"] or dict(unit["properties"]).get("LoadState") != "not-found")):
                raise ActivationRefusal("unfinished upgrade frozen declaration differs from selected roster")
        membership = {unit["installed"][0]["path"]: unit for unit in frozen
                      if unit["target"] in phases[phase] and len(unit["installed"]) == 1}
        if (len(membership) != len(phases[phase]) or {change.target for change in plan.changes} != set(membership)
                or any(change.after != {"kind": "absent"} for change in plan.changes)):
            raise ActivationRefusal("unfinished upgrade parking target coverage differs")
        if phase == "producers":
            receipt = dict(phase=phase, journal_id=identifier, plan_id=plan.plan_id,
                targets=[unit["target"] for unit in frozen if unit["target"] in phases[phase]], operation="paused")
            if body["evidence"]["producers_paused"] != hashlib.sha256(_json(receipt)).hexdigest():
                raise ActivationRefusal("unfinished upgrade lacks its exact completed producer pause receipt")
            scheduled = {unit["declaration"]["service"] for unit in frozen
                         if unit["target"] in phases[phase] and unit["target"].endswith(".timer")}
            observations = effects.get("native_observations")
            if not isinstance(observations, dict):
                raise ActivationRefusal("unfinished upgrade lacks original producer native observations")
            for unit in membership.values():
                target = unit["target"]
                if "@." in target:
                    continue
                properties = dict(unit["properties"])
                previous = [properties[key] for key in ("UnitFileState", "LoadState", "ActiveState")]
                observed = observations.get(target, "").split()
                matching = observed == previous
                if target in scheduled:
                    states = {"active", "activating", "inactive", "failed"}
                    matching = (len(observed) == 3 and observed[:2] == previous[:2]
                                and observed[2] in states and previous[2] in states)
                if not matching:
                    raise ActivationRefusal("unfinished upgrade producer native observation differs from original intent")
        status = "pending" if phase == "ingest" else "applied"
        if journal.status != ("prepared" if phase == "ingest" else "applied"):
            raise ActivationRefusal("unfinished upgrade parking progress is unsupported")
        _journal_rows(plan, raw, status)
        for entry in raw["rows"]:
            unit = membership[entry["target"]]
            snapshot, declaration = unit["installed"][0], unit["declaration"]
            original = selected_rows[declaration["source"]]
            placement = installed[declaration["source"]]
            node = entry["before"]["node"]
            if (node.get("kind") != "file" or snapshot["link"] is not None
                    or snapshot["path"] != placement["installed"] or snapshot["resolved"] != snapshot["path"]
                    or node != {"kind": "file", "mode": snapshot["mode"], "sha256": snapshot["sha256"]}
                    or snapshot["sha256"] != original["sha256"] or unit["target"] != placement["target"]
                    or any(declaration.get(key) != original.get(key) for key in ("scope", "fleet", "bot", "service", "release_id"))
                    or declaration["working_directory"] != original["working_directory"]
                    or dict(declaration["environment"]) != original["environment"]
                    or unit["generated"]["sha256"] != original["sha256"]
                    or unit["target"] not in phases[original["phase"]]):
                raise ActivationRefusal("unfinished upgrade before-image differs from selected native ownership")
            content = _read(journal.directory / "blobs" / snapshot["sha256"], evidence)
            if (hashlib.sha256(content).hexdigest() != snapshot["sha256"]
                    or base64.b64decode(snapshot["content"]["base64"], validate=True) != content):
                raise ActivationRefusal("unfinished upgrade native before-image is unavailable")
            if phase == "ingest":
                if path_state(Path(snapshot["path"])) != entry["before"]:
                    raise ActivationRefusal("unfinished upgrade ingest state changed")
            else:
                if os.path.lexists(snapshot["path"]):
                    raise ActivationRefusal("unfinished upgrade parked native path reappeared")
                parked[snapshot["path"]] = {"snapshot": snapshot, "properties": unit["properties"],
                    "target": unit["target"], "phase": phase, "journal": str(journal.directory / "journal.json")}
                if phase == "producers" and "@." not in unit["target"]:
                    parked[snapshot["path"]]["paused_native"] = observations[unit["target"]]
        journals[phase] = identifier
    bot_targets = set(shared["phases"]["bots"])
    if (not isinstance(body.get("handoff_effects"), dict) or set(body["handoff_effects"]) - bot_targets
            or not isinstance(body.get("bot_quiescence"), dict) or set(body["bot_quiescence"]) - bot_targets):
        raise ActivationRefusal("unfinished upgrade has unknown effect targets")
    return {"activation_id": aid, "plan_id": candidate.plan_id, "journals": journals}, candidate, parked


def _units(plan, release, contexts):
    rows = plan.effects.get("units")
    if not isinstance(rows, list) or not rows:
        raise ActivationRefusal("sealed baseline has no native declarations")
    seen = set()
    for row in rows:
        source = _path(row["source"])
        env = row["environment"]
        if (str(source) in seen or row["release_id"] != release.release_id
                or row["platform"] not in {"Linux", "Darwin"} or row["phase"] not in _PHASES
                or type(row["enroll"]) is not bool or row["scope"] not in {"host", "fleet", "bot"}
                or not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
                or any(env.get(key) != value for key, value in {
                    "CLAUDLOBBY_ROOT": str(plan.data_root), "CLAUDLOBBY_RELEASE_ID": release.release_id,
                    "CLAUDLOBBY_NATIVE_DIR": str(release.native_path), "CLAUDLOBBY_CLI": str(release.cli_path),
                    "CLAUDLOBBY_ARTIFACT_ID": release.inputs.artifact_id}.items())
                or "CLAUDLOBBY_INSTALLATION" in env):
            raise ActivationRefusal("sealed native declaration identity differs")
        seen.add(str(source))
        if row["scope"] == "host":
            if row["fleet"] is not None or row["bot"] is not None:
                raise ActivationRefusal("sealed host declaration has a fleet or bot identity")
            directory, working = plan.data_root / "runtime/_host/timers", plan.data_root
        else:
            context = contexts.get(row["fleet"])
            if (context is None or (row["scope"] == "bot" and (row["bot"] not in context.fleet.bots
                    or row["phase"] != "bots")) or row["scope"] == "fleet" and row["bot"] is not None):
                raise ActivationRefusal("sealed native declaration differs from frozen roster")
            directory = (context.paths.bot_runtime(row["bot"]) if row["scope"] == "bot"
                         else context.paths.runtime_fleet / "timers")
            working = directory if row["scope"] == "bot" else plan.data_root
            if env.get("FLEET_ROOT") != str(context.paths.fleet_config_dir):
                raise ActivationRefusal("sealed native declaration has another fleet root")
        if (source.parent != directory or row["working_directory"] != str(working)
                or _state(plan, source) != {"kind": "file", "mode": row["mode"], "sha256": row["sha256"]}
                or hashlib.sha256(plan.blob(row["sha256"])).hexdigest() != row["sha256"]):
            raise ActivationRefusal("sealed native declaration differs from frozen generated bytes")
    return rows


def _placements(root, selected, record, candidate, rows, evidence):
    all_entries = None
    digest = record.body["intent"]["enrollment_digest"]
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise ActivationRefusal("sealed enrollment digest is unavailable")
    for phase in _PHASES:
        identifier = "enrollment-" + hashlib.sha256(_json([selected["activation_id"], phase])).hexdigest()
        journal = read_config_install(root, identifier)
        raw = json.loads(_read(journal.directory / "journal.json", evidence))["journal"]
        publication = read_plan(root, journal.plan_id)
        _read(publication.directory / "plan.json", evidence)
        effects = publication.effects
        entries = effects.get("entries")
        if (journal.status != "applied"
                or publication.release_id != candidate.release_id or publication.release_seal != candidate.release_seal
                or effects.get("owner") != "activation-enrollment-v1" or effects.get("phase") != phase
                or effects.get("activation_id") != selected["activation_id"]
                or effects.get("candidate_plan") != candidate.plan_id or effects.get("enrollment_digest") != digest
                or effects.get("install_directory") != record.body["intent"].get("install_directory")
                or not isinstance(entries, list) or not entries or all_entries is not None and entries != all_entries):
            raise ActivationRefusal("sealed native publication differs from active ownership")
        _journal_rows(publication, raw, "applied")
        for entry in entries:
            if entry["phase"] != phase:
                continue
            if entry.get("publish", True):
                if _state(publication, Path(entry["installed"])) != entry["after"]:
                    raise ActivationRefusal("sealed native publication differs from its frozen bytes")
            elif any(change.target == entry["installed"] for change in publication.changes):
                raise ActivationRefusal("stopped sealed bot was unexpectedly published")
        all_entries = entries
    sources = {entry["source"] for entry in all_entries}
    if len(sources) != len(all_entries):
        raise ActivationRefusal("sealed native placements are ambiguous")
    matching = [row for row in rows if row["source"] in sources]
    platforms = {row["platform"] for row in matching}
    if len(platforms) != 1:
        raise ActivationRefusal("sealed native platform is ambiguous")
    manager = platforms.pop()
    wanted = {row["source"] for row in rows if row["platform"] == manager and row["enroll"]}
    if sources != wanted:
        raise ActivationRefusal("sealed publication does not cover the enrolled frozen roster")
    directory = _path(record.body["intent"]["install_directory"])
    domains, declarations = set(), []
    states = record.body["intent"].get("bot_states", {})
    for row in matching:
        entry = next(value for value in all_entries if value["source"] == row["source"])
        source, installed = Path(row["source"]), _path(entry["installed"])
        target = entry["target"]
        domain = target.rsplit("/", 1)[0] if manager == "Darwin" else ""
        if manager == "Darwin" and domain not in {f"gui/{os.getuid()}", f"user/{os.getuid()}"}:
            raise ActivationRefusal("sealed native placement belongs to another user")
        domains.add(domain)
        disposition = states.get(target)
        publish = disposition is None or disposition.get("disposition") == "restart"
        if (entry["phase"] != row["phase"] or entry["working_directory"] != row["working_directory"]
                or entry["environment"] != row["environment"] or entry.get("service") != row.get("service")
                or entry["after"] != {"kind": "file", "sha256": row["sha256"], "mode": row["mode"]}
                or installed.parent != directory or installed.name != source.name or installed == source
                or target != _target(manager, domain, source) or entry.get("publish", True) is not publish):
            raise ActivationRefusal("sealed native placement differs from its declared owner")
        declarations.append(UnitDeclaration(source, row["scope"], Path(row["working_directory"]),
            row["release_id"], tuple(sorted(row["environment"].items())), row["fleet"], row["bot"], row.get("service")))
    for row in rows:
        if row["platform"] == manager and row["source"] not in sources:
            declarations.append(UnitDeclaration(Path(row["source"]), row["scope"], Path(row["working_directory"]),
                row["release_id"], tuple(sorted(row["environment"].items())), row["fleet"], row["bot"], row.get("service")))
    if len(domains) != 1:
        raise ActivationRefusal("sealed native domains disagree")
    return manager, domains.pop(), directory, tuple(declarations), tuple(rows), tuple(all_entries)


def read_sealed_baseline(root, *, package, expected_selection=None, archived_selector=None):
    """Read exact historical authority; no current configuration or native state is synthesized.

    archived_selector is an explicit {path, sha256} from the original update
    record. It never licenses a search/fallback or an archive/retirement write.
    Selector bytes are bound separately, keeping evidence keys stable after retirement.
    """
    root = _path(root)
    evidence = {}
    try:
        if root.resolve() != root or package.installation is None or package.installation.data_root != root:
            raise ActivationRefusal("sealed conversion requires this data root's managed installation")
        selector = root / "state/selected-release.json"
        if archived_selector is not None:
            if (expected_selection is None or not isinstance(archived_selector, dict)
                    or set(archived_selector) != {"path", "sha256"}
                    or not isinstance(archived_selector["sha256"], str) or not _SHA.fullmatch(archived_selector["sha256"])):
                raise ActivationRefusal("archived selector requires its exact original selection and hash")
            selector = _path(archived_selector["path"])
            if not selector.is_relative_to(root / "state/update-work") or os.path.lexists(root / "state/selected-release.json"):
                raise ActivationRefusal("archived selector is outside recorded work or current selection still exists")
        original = _read(selector, {})
        selected = json.loads(original)
        if (not isinstance(selected, dict) or set(selected) != {"schema", "activation_id", "release_id", "plan_id"}
                or type(selected["schema"]) is not int or selected["schema"] != 1
                or expected_selection is not None and selected != expected_selection
                or archived_selector is not None and hashlib.sha256(original).hexdigest() != archived_selector["sha256"]
                or archived_selector is None and read_selection(root) != selected):
            raise ActivationRefusal("sealed selector differs from original selection")
        record = read_activation(root, selected["activation_id"])
        _read(root / "state/activations" / record.activation_id / "activation.json", evidence)
        _complete(record.body)
        history = _activation_names(root)
        if any(record.body["intent"].get(key) != selected[key] for key in ("release_id", "plan_id")):
            raise ActivationRefusal("sealed activation differs from selection")
        release = read_release(root, selected["release_id"], verify_files=False)
        _read(release.directory / "release.json", evidence)
        plan = read_plan(root, selected["plan_id"])
        _read(plan.directory / "plan.json", evidence)
        if plan.release_id != release.release_id or plan.release_seal != release.seal_sha256:
            raise ActivationRefusal("sealed configuration differs from selected release")
        contexts = {fleet: context_from_plan(plan, fleet, package=package) for fleet in plan.fleets}
        old_ids = record.body["identity_bindings"]
        if (set(old_ids) != {"schema", "root", "plan_id", "release_id", "release_seal", "host_uid", "fleets"}
                or old_ids["root"] != str(root) or old_ids["plan_id"] != plan.plan_id
                or old_ids["release_id"] != release.release_id or old_ids["release_seal"] != release.seal_sha256):
            raise ActivationRefusal("sealed identity provenance differs")
        identities = {key: old_ids[key] for key in ("schema", "host_uid", "fleets")}
        validate_current_identity_bindings(root, identities, contexts=contexts)
        if read_registry_identity_bindings(plan, package=package) != identities:
            raise ActivationRefusal("sealed identities differ from existing canonical registry")
        rows = _units(plan, release, contexts)
        manager, domain, directory, declarations, units, placements = _placements(root, selected, record, plan, rows, evidence)
        interruption, authored_plan, parked = _interrupted_upgrade(root, history, selected, plan, rows, placements, evidence)
        if interruption is not None and manager != "Linux":
            raise ActivationRefusal("interrupted parked conversion currently requires Linux")
        _settled_history(root, history, evidence, interruption)
        authored = authored_plan or plan
        if authored.fleets != plan.fleets:
            raise ActivationRefusal("unfinished upgrade changes the selected fleet roster")
        from .composer import account_dir
        for fleet in plan.fleets:
            old = contexts[fleet]
            new = context_from_plan(authored, fleet, package=package)
            if (old.paths.fleet_yaml != new.paths.fleet_yaml
                    or old.fleet.service_prefix != new.fleet.service_prefix
                    or set(old.fleet.bots) != set(new.fleet.bots)
                    or any((old.fleet.bots[name].agent_cli, account_dir(old.fleet.bots[name], old.fleet))
                           != (new.fleet.bots[name].agent_cli, account_dir(new.fleet.bots[name], new.fleet))
                           for name in old.fleet.bots)):
                raise ActivationRefusal("unfinished upgrade changes native session identity/account bindings")
        for sources in authored.effects["fleet_sources"].values():
            for descriptor in sources.values():
                path, _ = authored.frozen_input(descriptor)
                if path_state(path, source=True) != authored.inputs[str(path)]["state"]:
                    raise ActivationRefusal("authored fleet or projects differs from frozen sealed configuration")
        if _activation_names(root) != history or _read(selector, {}) != original or any(hashlib.sha256(_read(Path(path), {})).hexdigest() != digest
                                                for path, digest in evidence.items()):
            raise ActivationRefusal("sealed baseline changed during read")
        return SealedBaseline(root, selected, original, hashlib.sha256(original).hexdigest(), selector,
            record, release, plan, contexts, identities, declarations, units, placements,
            manager, domain, directory, evidence, interruption, authored_plan, parked)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        if isinstance(exc, ActivationRefusal):
            raise
        raise ActivationRefusal("sealed baseline is unavailable, changed or unsupported") from exc
