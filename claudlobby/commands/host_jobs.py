"""Read the effective host-job declarations through the config owner's merge."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    if args.public_command == "host.job.run":
        return _run(args)
    from ..config import load_host_jobs

    jobs = load_host_jobs()
    if args.public_command == "host.job.list":
        items = [{"name": name, "enroll": cfg.get("enroll", True)}
                 for name, cfg in sorted(jobs.items())]
        return CommandOutput({"jobs": items}, lines=tuple(
            f"{item['name']}\tenroll={'true' if item['enroll'] else 'false'}" for item in items))

    if args.name not in jobs:
        raise CommandFailure("not_found", f"host job {args.name!r} is not packaged in this release",
                             data={"name": args.name, "available": sorted(jobs)})
    # YAML parses an unquoted hold.until date into a date object. The JSON
    # representation is the same ISO spelling expected by the existing reader.
    job = json.loads(json.dumps(jobs[args.name], sort_keys=True, default=str))
    return CommandOutput({"name": args.name, "job": job},
                         lines=(json.dumps(job, sort_keys=True),))


def _run(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch
    from ..supervision_inventory import InventoryError
    from ..host_job_operations import HostJobError, run_host_job
    from .host import _operator_shell
    from .releases import _host_root

    _operator_shell()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.name):
        raise CommandFailure("invalid_argument", "supply one exact host job name")
    root = _host_root(args)
    data = {"name": args.name, "native_outcome": "unattempted", "completion": "unobserved"}
    try:
        result = run_host_job(root, args.name)
    except HostJobError as exc:
        data.update(native_outcome="unknown" if exc.effect_attempted else "unattempted",
                    release_id=exc.release_id, target=exc.target)
        raise CommandFailure("unavailable" if exc.unavailable else "conflict", str(exc),
                             data=data, release_id=exc.release_id,
                             hint="inspect the exact native job before any retry" if exc.effect_attempted else None) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "host job requires the selected executable and release",
                             data=data) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected host job release differs from this executable",
                             data=data) from exc
    except (ActivationError, PlanError) as exc:
        raise CommandFailure("conflict", "selected host job activation or configuration is incomplete",
                             data=data) from exc
    except (InventoryError, OSError, RuntimeError) as exc:
        raise CommandFailure("unavailable", "host job native state or effective configuration is unavailable",
                             data=data) from exc
    return CommandOutput(asdict(result), release_id=result.release_id,
                         lines=(f"{result.name}: native run requested; completion unobserved.",))
