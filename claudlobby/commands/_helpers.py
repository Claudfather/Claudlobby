"""Shared CLI helpers used by multiple command modules."""

from __future__ import annotations

import logging
import os
import sys

from .. import dotenv
from ..context import load_context, resolve_paths
from ..paths import Paths

log = logging.getLogger("claudlobby")


def _resolve_paths(args) -> Paths:
    try:
        return resolve_paths(root=getattr(args, "root", None),
                             fleet=getattr(args, "fleet", None),
                             seed=getattr(args, "seed", False))
    except (OSError, ValueError, RuntimeError) as e:
        log.error("%s", e)
        sys.exit(1)


def _load_env(paths: Paths) -> None:
    """Load .env file into os.environ (without overriding existing vars).
    Handles both `VAR=value` and `export VAR=value` formats — the latter is
    what hand-edited .env files commonly use."""
    for k, v in dotenv.read(paths.env_file).items():
        if k not in os.environ:
            os.environ[k] = v


def _load_fleet_or_exit(paths: Paths) -> tuple["FleetConfig", dict]:
    """Wrap load_fleet() with user-friendly error messages on common failures.

    Returns ``(FleetConfig, merged_defaults)`` — the merged defaults dict
    produced by ``_merge_system_into_defaults()``.
    """
    import yaml

    try:
        context = load_context(paths)
        return context.fleet, context.merged_defaults
    except FileNotFoundError as e:
        log.error("%s", e)
        sys.exit(1)
    except ValueError as e:
        log.error("%s", e)
        sys.exit(1)
    except yaml.YAMLError as e:
        log.error("invalid YAML in %s: %s", paths.fleet_yaml, e)
        sys.exit(1)


def _validation_gate(fleet: "FleetConfig", paths: Paths, *, context: str) -> bool:
    """Run validate() and report; return True when composing may proceed.

    The shared load->validate->log gate for every command that composes
    outside the private compositor (currently move-bot).
    Warnings are surfaced (not just errors) so did-you-mean hints reach
    the user on these paths too.
    """
    from ..validator import render_warnings, validate

    report = validate(fleet, paths)
    for line in render_warnings(report):
        log.warning("%s", line)
    if report.has_errors:
        for err in report.errors:
            log.error("%s", err)
        log.error("validation failed — fix the errors above, then %s", context)
        return False
    return True


def refuse_unreachable(command: str, note: str) -> int:
    """The one refusal every plane-only reader prints (F18 closure, R2b-1):
    the plane cannot answer, so NOTHING is served rather than a wrong answer —
    unreachable is not empty. rc 3, deliberately not 2 (a malformed call) nor
    1 (the thing asked failed): the question could not be answered."""
    print(f"claudlobby {command}: UNREACHABLE — {note}", file=sys.stderr)
    return 3
