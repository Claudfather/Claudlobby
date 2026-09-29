"""Operator activation and read-only recorded-state orientation.

The activation owner holds the lock and owns every effect. This adapter neither
retries nor recovers an interrupted activation.
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
            "this route does not recover an interrupted activation; inspect its recorded pending step")


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
    data = {"root": str(root), "recorded_status": state, "selection": evidence["selection"],
            "selected_activation": selected, "unfinished_activations": evidence["unfinished_activations"],
            "activation_errors": evidence["activation_errors"], "selection_error": evidence["selection_error"],
            "releases": evidence["items"], "runtime_observation": "unknown",
            "bootstrap_eligibility": "not_checked", "upgrade_supported": state == "active", "recovery_supported": False}
    if problem:
        raise CommandFailure(problem.code, f"{problem.code}: recorded host state is {state}",
                             data=data, release_id=executing, hint=_hint(root))
    return CommandOutput(data, executing,
                         (f"Host recorded state: {state}; running processes are unobserved.",))


def _operator_shell():
    # Generated context is a trusted-local restriction, not authentication.
    # The backend additionally asks the OS adapter about actual caller ancestry.
    if any(name in os.environ for name in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE")):
        raise CommandFailure("conflict", "conflict: host activation requires an operator shell outside generated bot context",
                             hint="run from an operator shell outside the managed bot/job process trees")


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
    try:
        from ..activation import adopt_existing_activation, bootstrap_activation, upgrade_activation
        from ..activation_state import read_selection
        activate = (adopt_existing_activation if args.adopt_existing else
                    upgrade_activation if read_selection(root) is not None else bootstrap_activation)
        record = activate(root, args.activation_id, plan.plan_id, directory)
    except Exception as exc:
        from ..activation_state import ActivationError
        data.update(recorded_activation=_recorded(root, args.activation_id), recording="unknown")
        if isinstance(exc, (ImportError, OSError)):
            code, message = "unavailable", "unavailable: cold-host activation dependency or native access"
        elif isinstance(exc, ActivationError):
            # Native stderr can contain arbitrary text. Disclose only the
            # operation and rc from the product-owned refusal envelope.
            refusal = re.match(r"\A(svc_activation_[a-z_]+) refused \(([0-9]{1,3})\):", str(exc))
            code, message = "conflict", (f"conflict: {refusal[1]} refused ({refusal[2]})"
                                         if refusal else "conflict: activation did not complete; inspect its pending step")
        elif isinstance(exc, (ValueError, RuntimeError)):
            code, message = "conflict", "conflict: activation did not complete; inspect its pending step"
        else:
            diagnostic = str(uuid4())
            print(f"diagnostic {diagnostic}: {type(exc).__name__}", file=sys.stderr)
            code, message = "internal_error", f"internal error; see {diagnostic}"
        raise CommandFailure(code, message, data=data, release_id=executing, hint=_hint(root)) from exc
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


def dispatch(args):
    activating = args.public_command == "host.activate"
    try:
        if activating:
            _operator_shell()
        if args.root is None:
            raise CommandFailure("invalid_argument", "invalid argument: an explicit --root is required",
                                 hint=f"supply claudlobby --root PATH {args.public_command.replace('.', ' ')}")
        root = _host_root(args)
        return _activate(args, root) if activating else _status(args, root)
    except CommandFailure as exc:
        if activating:
            exc.data.setdefault("activation_id", args.activation_id)
        raise
