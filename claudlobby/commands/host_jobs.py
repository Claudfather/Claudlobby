"""Read the effective host-job declarations through the config owner's merge."""

from __future__ import annotations

import json

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..config import load_host_jobs

    jobs = load_host_jobs()
    if args.public_command == "host.job.list":
        items = [{"name": name, "enroll": cfg.get("enroll", True)}
                 for name, cfg in sorted(jobs.items())]
        return CommandOutput({"jobs": items}, lines=tuple(
            f"{item['name']}\t{'enrolled' if item['enroll'] else 'dormant'}" for item in items))

    if args.name not in jobs:
        raise CommandFailure("not_found", f"host job {args.name!r} is not packaged in this release",
                             data={"name": args.name, "available": sorted(jobs)})
    # YAML parses an unquoted hold.until date into a date object. The JSON
    # representation is the same ISO spelling expected by the existing reader.
    job = json.loads(json.dumps(jobs[args.name], sort_keys=True, default=str))
    return CommandOutput({"name": args.name, "job": job},
                         lines=(json.dumps(job, sort_keys=True),))
