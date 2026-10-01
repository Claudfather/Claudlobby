"""Operator activation and read-only recorded-state orientation.

The activation owner holds the lock and owns every effect. This adapter binds
an explicit resume ID to that owner; it never retries an operation automatically.
Imports stay stdlib-only until the selected command needs its backend.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import sys
from uuid import uuid4

from ..command_result import CommandFailure, CommandOutput
from .releases import _executing_release, _host_releases, _host_root


def _hint(root):
    return (f"inspect claudlobby --root {shlex.quote(str(root))} host status; "
            "use host activate PLAN_ID --resume ID --install-directory PATH for a supported recorded step; "
            "running-session handoff and starts without durable receipts need their missing witness repaired first")


def _status(args, root):
    problem = None
    try:
        diagnosis = _host_releases(args, root)
        evidence, executing = diagnosis.data, diagnosis.release_id
    except CommandFailure as exc:
        if not exc.data:
            raise
        evidence, executing, problem = exc.data, exc.release_id, exc.error
    selected = evidence["selected_activation"]
    state = ("incomplete" if evidence["unfinished_activations"] else
             "indeterminate" if problem else "active" if selected else "unselected")
    from ..activation import resumable_running_step
    from ..activation_state import read_activation
    recovery = []
    if not evidence["activation_errors"]:
        for item in evidence["unfinished_activations"]:
            try:
                record = read_activation(root, item["activation_id"])
                step = resumable_running_step(record)
            except Exception:
                step = None
            recovery.append({"activation_id": item["activation_id"], "supported_step": step})
    data = {"root": str(root), "recorded_status": state, "selection": evidence["selection"],
            "selected_activation": selected, "unfinished_activations": evidence["unfinished_activations"],
            "activation_errors": evidence["activation_errors"], "selection_error": evidence["selection_error"],
            "releases": evidence["items"], "runtime_observation": "unknown",
            "bootstrap_eligibility": "not_checked", "upgrade_supported": state == "active",
            "recovery_supported": len(recovery) == 1 and recovery[0]["supported_step"] is not None,
            "recovery": recovery}
    if problem:
        raise CommandFailure(problem.code, f"{problem.code}: recorded host state is {state}",
                             data=data, release_id=executing, hint=_hint(root))
    return CommandOutput(data, executing,
                         (f"Host recorded state: {state}; running processes are unobserved.",))


def _operator_shell(root=None):
    from .operator_context import require_operator_context
    require_operator_context(root)


def _recorded(root, activation_id):
    from ..activation_state import ActivationError, read_activation
    path = root / "state/activations" / activation_id / "activation.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        record = read_activation(root, activation_id)
        return {"activation_id": activation_id, "status": record.status,
                "pending_step": record.body["pending"], "completed_steps": record.body["completed"],
                "plan_id": record.body["intent"]["plan_id"], "release_id": record.body["intent"]["release_id"],
                "verification": "verified"}
    except (ActivationError, KeyError, TypeError):
        return {"activation_id": activation_id, "status": None, "pending_step": None,
                "completed_steps": None, "plan_id": None, "release_id": None, "verification": "failed"}


def _activate(args, root):
    from ..config_plan import PlanError, read_plan

    if not re.fullmatch(r"p-[0-9a-f]{64}", args.plan_id):
        raise CommandFailure("invalid_argument", "invalid argument: invalid configuration plan ID")
    if getattr(args, "resume", None) and args.adopt_existing:
        raise CommandFailure("invalid_argument", "invalid argument: --resume and --adopt-existing are exclusive")
    if getattr(args, "resume", None) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", args.resume):
        raise CommandFailure("invalid_argument", "invalid argument: invalid activation resume ID")
    directory = Path(args.install_directory).expanduser()
    if not directory.is_absolute():
        raise CommandFailure("invalid_argument", "invalid argument: --install-directory must be absolute")
    try:
        plan = read_plan(root, args.plan_id)
    except PlanError as exc:
        if not (root / "state/config-plans" / args.plan_id).exists():
            raise CommandFailure("not_found", f"configuration plan not found: {args.plan_id}") from exc
        raise CommandFailure("conflict", "conflict: configuration plan is incomplete or changed", hint=_hint(root)) from exc
    executing = _executing_release(root)
    data = {"activation_id": args.activation_id, "plan_id": plan.plan_id, "release_id": plan.release_id,
            "recorded_activation": None, "recording": "unchanged"}
    if executing != plan.release_id:
        raise CommandFailure("release_mismatch",
            f"release mismatch: expected {plan.release_id}, found {executing or 'unsealed executable'}; no mutation performed",
            data=data, release_id=executing,
            hint=f"use this plan's sealed candidate CLI; inspect claudlobby --root {shlex.quote(str(root))} host releases")
    if getattr(args, "resume", None):
        from ..activation import resumable_running_step
        from ..activation_state import ActivationError, read_activation
        try:
            prior = read_activation(root, args.resume)
        except ActivationError as exc:
            raise CommandFailure("not_found", "activation resume record is unavailable or invalid",
                                 data=data, release_id=executing, hint=_hint(root)) from exc
        if prior.body["intent"]["plan_id"] != plan.plan_id:
            raise CommandFailure("conflict", "resume ID belongs to a different frozen plan",
                                 data={**data, "recorded_activation": _recorded(root, args.resume)},
                                 release_id=executing, hint=_hint(root))
        if resumable_running_step(prior) is None:
            raise CommandFailure("conflict", "recorded activation step cannot safely resume; "
                                 "required handoff or start evidence is missing",
                                 data={**data, "recorded_activation": _recorded(root, args.resume)},
                                 release_id=executing,
                                 hint="inspect the pending step and repair its native/journal witness before retrying")
    try:
        from ..activation import adopt_existing_activation, bootstrap_activation, upgrade_activation, resume_activation
        from ..activation_state import read_selection
        activate = (resume_activation if getattr(args, "resume", None) else
                    adopt_existing_activation if args.adopt_existing else
                    upgrade_activation if read_selection(root) is not None else bootstrap_activation)
        record = activate(root, args.activation_id, plan.plan_id, directory)
    except Exception as exc:
        from subprocess import TimeoutExpired
        from ..activation_state import ActivationError, ActivationRefusal, CandidateDisabledOverride
        recorded = _recorded(root, args.activation_id)
        # The owner prepares the record before any pause or effect; no record
        # for this ID means the refusal happened while the host was unchanged.
        data.update(recorded_activation=recorded, recording="unknown" if recorded else "unchanged")
        hint = _hint(root) if recorded else (
            "no activation record was created; correct the refusal and rerun the same command")
        pending = "inspect its pending step" if recorded else "no activation record was created"
        if isinstance(exc, CandidateDisabledOverride):
            code = "conflict"
            message = ("conflict: candidate launchd units have persistent disabled overrides: "
                       + ", ".join(exc.targets) + "; no activation was started")
            hint = ("review these units and explicitly enable their launchd overrides, "
                    "or unenroll them in authored config before retrying")
            data["recording"] = "unchanged"
        elif isinstance(exc, TimeoutExpired):
            code, message = "unavailable", f"unavailable: native user manager did not answer in time; {pending}"
        elif isinstance(exc, (ImportError, OSError)):
            code, message = "unavailable", "unavailable: cold-host activation dependency or native access"
        elif isinstance(exc, ActivationRefusal):
            code, message = "conflict", f"conflict: activation refused: {exc}; {pending}"
        elif isinstance(exc, ActivationError):
            # Native stderr can contain arbitrary text. Disclose only the
            # operation and rc from the product-owned refusal envelope.
            refusal = re.match(r"\A(svc_activation_[a-z_]+) refused \(([0-9]{1,3})\):", str(exc))
            # The owner's own drift refusal names one frozen enrollment target.
            changed = re.fullmatch(r"native enrollment changed: ([A-Za-z0-9_.@:/-]{1,200})", str(exc))
            code, message = "conflict", (
                f"conflict: {refusal[1]} refused ({refusal[2]})" if refusal else
                f"conflict: native enrollment changed since inventory: {changed[1]}; {pending}" if changed else
                f"conflict: activation did not complete; {pending}")
            if getattr(args, "resume", None) and data["recorded_activation"]:
                from ..activation import resumable_running_step
                from ..activation_state import read_activation
                try:
                    supported = resumable_running_step(read_activation(root, args.activation_id)) is not None
                except ActivationError:
                    supported = False
                if not supported:
                    message = ("conflict: recorded activation step cannot safely resume; "
                               "required handoff or start evidence is missing")
                    hint = ("inspect the exact pending step and repair its native/journal witness "
                            "before retrying the same activation ID")
            if data["recorded_activation"] is None and str(exc) == "another host activation holds the lock":
                message = "conflict: host activation lock is held; no activation record was created"
                hint = "inspect running host operations and activation.lock holders before retrying"
        elif isinstance(exc, (ValueError, RuntimeError)):
            code, message = "conflict", f"conflict: activation did not complete; {pending}"
        else:
            diagnostic = str(uuid4())
            print(f"diagnostic {diagnostic}: {type(exc).__name__}", file=sys.stderr)
            code, message = "internal_error", f"internal error; see {diagnostic}"
        raise CommandFailure(code, message, data=data, release_id=executing, hint=hint) from exc
    saved = _recorded(root, args.activation_id)
    if (record.status != "active" or record.activation_id != args.activation_id or saved is None
            or saved["verification"] != "verified" or saved["status"] != "active"
            or saved["plan_id"] != plan.plan_id or saved["release_id"] != plan.release_id):
        raise CommandFailure("conflict", "conflict: activation owner did not confirm active state",
                             data={**data, "recorded_activation": _recorded(root, args.activation_id), "recording": "unknown"},
                             release_id=executing, hint=_hint(root))
    data.update(recorded_activation=saved, recording="committed",
                runtime_observation="readiness_checked_during_activation",
                user_manager_startup="host_prerequisite", upgrade_supported=True, recovery_supported=False)
    return CommandOutput(data, executing,
        (f"Activation {args.activation_id}: recorded active; release {executing}; plan {plan.plan_id}.",))


def _abort(args, root):
    """Operator-only early abort of an unsealed first adoption stopped pausing producers.

    Restores only the original producer pause. It is not general rollback: it
    never starts bots or ingest, changes selection, or restores SQL.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", args.activation_id):
        raise CommandFailure("invalid_argument", "invalid argument: invalid activation ID")
    reason = args.reason.strip()
    if not reason or len(reason) > 500 or not reason.isprintable():
        raise CommandFailure("invalid_argument", "invalid argument: --reason must be one printable line")
    if args.expected_sql_version < 0:
        raise CommandFailure("invalid_argument", "invalid argument: --expected-sql-version must be non-negative")
    executing = _executing_release(root)
    data = {"activation_id": args.activation_id, "release_id": executing,
            "recorded_activation": _recorded(root, args.activation_id), "recording": "unchanged"}
    if data["recorded_activation"] is None:
        raise CommandFailure("not_found", "activation record not found; no mutation performed", data=data)
    if executing is None:
        raise CommandFailure("release_mismatch",
            "release mismatch: early abort requires a sealed release CLI; no mutation performed",
            data=data, hint=f"inspect claudlobby --root {shlex.quote(str(root))} host releases")
    try:
        from ..activation_state import locked_activation
        from ..activation_units import abort_early_adoption
        from ..plane.db import connect_ro, db_file
        from ..releases import read_release
        release = read_release(root, executing)
        with locked_activation(root) as store:
            conn = connect_ro(db_file(root))
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            if version != args.expected_sql_version:
                raise CommandFailure("conflict", "conflict: database user_version differs from the "
                                     "operator's preflight expectation; no mutation performed",
                                     data=data, release_id=executing)
            evidence = abort_early_adoption(store, args.activation_id, reason=reason, release=release,
                                            sql_user_version=version)
    except CommandFailure:
        raise
    except Exception as exc:
        from subprocess import TimeoutExpired
        from ..activation_state import ActivationError, ActivationRefusal
        data.update(recorded_activation=_recorded(root, args.activation_id), recording="unknown")
        code = "conflict"
        if isinstance(exc, TimeoutExpired):
            code, message = "unavailable", "unavailable: native user manager did not answer in time"
        elif isinstance(exc, (ImportError, OSError)):
            code, message = "unavailable", "unavailable: abort dependency, database or native access"
        elif isinstance(exc, ActivationRefusal):
            message = f"conflict: early abort refused: {exc}"
        elif isinstance(exc, ActivationError):
            # Native stderr can contain arbitrary text; disclose operation and rc only.
            refusal = re.match(r"\A(svc_activation_[a-z_]+) refused \(([0-9]{1,3})\):", str(exc))
            message = (f"conflict: {refusal[1]} refused ({refusal[2]})" if refusal
                       else "conflict: early abort did not complete")
        else:
            diagnostic = str(uuid4())
            print(f"diagnostic {diagnostic}: {type(exc).__name__}", file=sys.stderr)
            code, message = "internal_error", f"internal error; see {diagnostic}"
        from ..activation_state import read_activation
        try:
            marked = "adoption_abort" in read_activation(root, args.activation_id).body
        except ActivationError:
            marked = False
        hint = ("the recorded abort marker keeps forward activation refused; repair the named "
                "condition, then rerun this same abort command explicitly" if marked else
                f"no abort marker is recorded; inspect claudlobby --root {shlex.quote(str(root))} "
                "host status and the recorded activation before rerunning")
        raise CommandFailure(code, message, data=data, release_id=executing, hint=hint) from exc
    saved = _recorded(root, args.activation_id)
    if saved is None or saved["verification"] != "verified" or saved["status"] != "rolled_back":
        raise CommandFailure("conflict", "conflict: abort owner did not confirm rolled_back state",
                             data={**data, "recorded_activation": saved, "recording": "unknown"},
                             release_id=executing)
    data.update(recorded_activation=saved, recording="committed", restored_targets=list(evidence.targets),
                operation=evidence.operation)
    done = ("terminal early abort rechecked; hidden runtime masks cleared" if evidence.operation == "rechecked"
            else "early adoption aborted")
    return CommandOutput(data, executing, (
        f"Activation {args.activation_id}: {done}; original producer files and states "
        "verified. Bots, ingest, selection and SQL were not touched.",))


def dispatch(args):
    activating = args.public_command == "host.activate"
    aborting = args.public_command == "host.abort-adoption"
    try:
        if activating or aborting:
            _operator_shell()  # activation itself already checks native ancestry
        if args.root is None:
            raise CommandFailure("invalid_argument", "invalid argument: an explicit --root is required",
                                 hint=f"supply claudlobby --root PATH {args.public_command.replace('.', ' ')}")
        root = _host_root(args)
        if aborting:
            return _abort(args, root)
        return _activate(args, root) if activating else _status(args, root)
    except CommandFailure as exc:
        if activating or aborting:
            exc.data.setdefault("activation_id", args.activation_id)
        raise
