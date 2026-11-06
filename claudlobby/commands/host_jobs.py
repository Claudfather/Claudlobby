"""Read the effective host-job declarations through the config owner's merge."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from ..command_result import CommandFailure, CommandOutput


def _enrollment_hint(name: str) -> str:
    return (f"host.jobs.{name}.enroll=false in the effective host configuration; "
            f"inspect claudlobby host job show {name} and claudlobby host doctor --switches. "
            "To change enrollment, review this host's authored override, then use "
            "the operator host update apply workflow. A run still requires committed configuration and installed native placement.")


def dispatch(args) -> CommandOutput:
    if args.public_command == "host.job.run":
        return _run(args)
    from ..config import load_host_jobs

    jobs = load_host_jobs()
    if args.public_command == "host.job.list":
        items = [{"name": name, "enroll": cfg.get("enroll", True)}
                 for name, cfg in sorted(jobs.items())]
        return CommandOutput({"jobs": items, "source": "effective_host_configuration",
                              "native_state": "not_checked"}, lines=tuple(
            f"{item['name']}\tenroll={'true' if item['enroll'] else 'false'}"
            + (f" — host.jobs.{item['name']}.enroll=false; inspect host job show {item['name']} "
               "and host doctor --switches" if item['enroll'] is False else "")
            for item in items) + ("Effective declarations only; installed/running state was not checked.",))

    if args.name not in jobs:
        raise CommandFailure("not_found", f"host job {args.name!r} is not packaged in this release",
                             data={"name": args.name, "available": sorted(jobs)})
    # YAML parses an unquoted hold.until date into a date object. The JSON
    # representation is the same ISO spelling expected by the existing reader.
    job = json.loads(json.dumps(jobs[args.name], sort_keys=True, default=str))
    hint = _enrollment_hint(args.name) if job.get("enroll", True) is False else None
    return CommandOutput({"name": args.name, "job": job, "source": "effective_host_configuration",
                          "native_state": "not_checked", "enrollment_hint": hint},
                         lines=(json.dumps(job, sort_keys=True),
                                "Effective declaration only; installed/running state was not checked.",
                                *((hint,) if hint else ())))


def _run(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..releases import ReleaseError
    from ..supervision_inventory import InventoryError
    from ..host_job_operations import HostJobError, run_host_job
    from .host import _operator_shell
    from .releases import _host_root

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.name):
        raise CommandFailure("invalid_argument", "supply one exact host job name")
    root = _host_root(args)
    _operator_shell(root)
    data = {"name": args.name, "native_outcome": "unattempted", "completion": "unobserved"}
    try:
        result = run_host_job(root, args.name)
    except HostJobError as exc:
        data.update(native_outcome="unknown" if exc.effect_attempted else "unattempted",
                    release_id=exc.release_id, target=exc.target)
        hint = "inspect the exact native job before any retry"
        if not exc.effect_attempted:
            hint = (_enrollment_hint(args.name) if str(exc) == "host job is disabled by this host's effective configuration" else
                    f"inspect claudlobby host job show {args.name} and claudlobby host doctor --switches; "
                    "committed configuration and installed native placement are still required")
        raise CommandFailure("unavailable" if exc.unavailable else "conflict", str(exc),
                             data=data, release_id=exc.release_id, hint=hint) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "host job installation differs from this executable",
                             data=data) from exc
    except (ActivationError, PlanError) as exc:
        raise CommandFailure("conflict", "committed host job configuration is incomplete",
                             data=data) from exc
    except (InventoryError, OSError, RuntimeError) as exc:
        raise CommandFailure("unavailable", "host job native state or effective configuration is unavailable",
                             data=data) from exc
    return CommandOutput(asdict(result), release_id=result.release_id,
                         lines=(f"{result.name}: native run requested; completion unobserved.",))
