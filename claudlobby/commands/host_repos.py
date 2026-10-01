"""Operator-only selected-fleet repository pull results."""

from __future__ import annotations

import os
from pathlib import Path

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..context import BotNotFoundError
    from ..paths import InvalidPathSelector
    from ..repo_pull_operations import RepositoryPullError, pull_repositories
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch

    if args.seed or not args.fleet:
        raise CommandFailure("invalid_argument", "select one declared fleet with --fleet")
    value = args.root or os.environ.get("CLAUDLOBBY_ROOT")
    if not value:
        raise CommandFailure("invalid_argument", "select the host data root with --root")
    root = Path(value).expanduser().resolve()
    from .operator_context import require_operator_context
    require_operator_context(root)
    data = {"fleet": args.fleet, "bot": args.bot, "native_outcome": "unattempted",
            "repositories": []}
    try:
        result = pull_repositories(root, args.fleet, args.bot)
    except RepositoryPullError as exc:
        data["native_outcome"] = "unknown" if exc.effect_attempted else "unattempted"
        raise CommandFailure("unavailable" if exc.effect_attempted else "conflict", str(exc),
                             data=data) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid fleet or root selector", data=data) from exc
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", "selected fleet declaration is missing", data=data) from exc
    except BotNotFoundError as exc:
        raise CommandFailure("not_found", "bot is not declared in the selected fleet", data=data) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "repository pull requires the selected executable",
                             data=data) from exc
    except (ActivationError, ReleaseError, ValueError) as exc:
        raise CommandFailure("conflict", "selected repository scope cannot be verified",
                             data=data) from exc
    rows = [{"repository": name, "status": status} for name, status in result.repositories]
    data.update(release_id=result.release_id, repositories=rows,
                native_outcome="partial" if any(row["status"] in
                    ("failed", "skipped_dirty", "skipped_blocked", "skipped_redirected") for row in rows)
                else "completed" if rows else "no_repositories")
    lines = tuple(f"{name}: {status}" for name, status in result.repositories)
    if data["native_outcome"] == "partial":
        raise CommandFailure("conflict", "some repositories were not pulled", data=data,
                             release_id=result.release_id, hint="\n".join(lines))
    return CommandOutput(data, result.release_id, lines or ("no repositories found",))
