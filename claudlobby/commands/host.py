"""Passive host status and historical evidence; no sealed deployment dispatch."""

from __future__ import annotations

import shlex

from ..command_result import CommandFailure, CommandOutput
from .releases import _host_releases, _host_root

def _status_data(root, state="unselected", evidence=None, recovery=()):
    if evidence is None:
        evidence = {"selection": None, "selected_activation": None, "unfinished_activations": [],
                    "activation_errors": [], "selection_error": None, "items": []}
    return {"root": str(root), "recorded_status": state, "selection": evidence["selection"],
            "selected_activation": evidence["selected_activation"], "unfinished_activations": evidence["unfinished_activations"],
            "activation_errors": evidence["activation_errors"], "selection_error": evidence["selection_error"],
            "releases": evidence["items"], "runtime_observation": "unknown",
            "bootstrap_eligibility": "not_checked", "upgrade_supported": False,
            "recovery_supported": False,
            "recovery": list(recovery)}


def _status_recovery_hint(root, recovery):
    if not recovery:
        return ("Recovery support could not be verified. Inspect claudlobby --root "
                f"{shlex.quote(str(root))} host releases and the recorded activation journals; "
                "do not retry an activation without its missing evidence.")
    lines = []
    for row in recovery:
        if row["abort_in_progress"]:
            lines.append(f"Activation {row['activation_id']}: a recorded abort is in progress; "
                         "preserve the original abort evidence for reviewed migration.")
        elif row["supported_step"] is None:
            lines.append(f"Activation {row['activation_id']}: no supported recovery step is recorded; "
                         "preserve its journal and review the pending step with the operator; no resume command is offered.")
        else:
            lines.append(f"Activation {row['activation_id']}: supported recorded step {row['supported_step']}; "
                         "preserve this historical journal for an explicit reviewed migration; sealed execution is retired.")
    return "\n".join(lines)


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
    from ..activation_state import resumable_running_step
    from ..activation_state import read_activation
    recovery = []
    if not evidence["activation_errors"]:
        for item in evidence["unfinished_activations"]:
            try:
                record = read_activation(root, item["activation_id"])
                step = resumable_running_step(record)
                aborting = "adoption_abort" in record.body or "upgrade_abort" in record.body
            except Exception:
                step, aborting = None, False
            # An abort in progress continues only through host abort-adoption.
            recovery.append({"activation_id": item["activation_id"], "supported_step": step,
                             "abort_in_progress": aborting})
    data = _status_data(root, state, evidence, recovery)
    from ..activation_state import ActivationRefusal
    from ..host_backup import restore_status
    try:
        restored = restore_status(root)
    except (ActivationRefusal, OSError, ValueError, TypeError, KeyError) as exc:
        data["restored_backup"] = {"state": "unavailable"}
        raise CommandFailure("unavailable", "restored backup provenance is invalid or unreadable",
                             data=data, release_id=executing,
                             hint="inspect state/restored-backup.json and the host identity; do not activate until resolved") from exc
    if restored is not None:
        data["restored_backup"] = restored
    if problem:
        message = problem.message if state == "incomplete" else f"{problem.code}: recorded host state is {state}"
        raise CommandFailure(problem.code, message,
                             data=data, release_id=executing,
                             hint=_status_recovery_hint(root, recovery))
    lines = (f"Host recorded state: {state}; running processes are unobserved.",)
    if restored is not None and state == "unselected":
        lines += ("Restored backup is inactive. Review external work and credentials before planning activation; "
                  "owner access must be paired and authorized again.",)
    return CommandOutput(data, executing, lines)


def _passive_status(args):
    from ..setup_observations import ObservationError, absent_status_root, observe, text_lines

    try:
        root = _host_root(args)
        missing = False
    except CommandFailure as existing:
        if existing.error.code != "not_found":
            raise
        try:
            root = absent_status_root(args.root)
        except ObservationError as exc:
            code = "invalid_argument" if exc.area == "argument" else "unavailable"
            raise CommandFailure(code, f"{code}: host status requires a safely inspectable data root") from exc
        if root is None:
            raise existing  # Preserve the recorded owner's existing-root refusal.
        missing = True
    observation = observe(root)
    if not missing:
        try:
            output = _status(args, root)
        except CommandFailure as exc:
            if exc.data:
                exc.data["preactivation"] = observation
            raise
        output.data["preactivation"] = observation
        return CommandOutput(output.data, output.release_id, output.lines + text_lines(observation))
    data = {**_status_data(root), "preactivation": observation}
    return CommandOutput(data, lines=("Host data root is absent; no setup or activation was performed.",
                                      *text_lines(observation)))


def _operator_shell(root=None):
    from .operator_context import require_operator_context
    require_operator_context(root)


def dispatch(args):
    if args.public_command != "host.status":
        raise CommandFailure("invalid_argument", "unsupported host command")
    if args.root is None:
        raise CommandFailure("invalid_argument", "invalid argument: an explicit --root is required")
    return _passive_status(args)
