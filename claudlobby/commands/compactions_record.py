"""Record the selected fleet's Claude Code compactions on the Plane (#2206).

The pulse runs this each tick; a manager may run it after ``bot compact`` to
see the compaction at once. Each compact_boundary row a bot's transcripts
appended since the last pass becomes one ``compaction`` event, once
(``claudlobby.compactions``). A bot whose transcript cannot be read is named
with its reason and records nothing; only a Plane that refused the events
fails the command, and then no cursor moves.
"""

from __future__ import annotations

import sqlite3

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..active_config import ActivationError
    from ..compactions import record_compactions
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import OperationContextError, OperationContextUnavailableError
    from ..paths import InvalidPathSelector
    from ..plane import emit_api
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch
    from .checkin import _scope, _runtime_hint

    try:
        destination, origin, selected, _bindings = _scope(args)
        root = destination.paths.root
        from ..runtime_admission import mutation_admission, RuntimeIdentity, runtime_route_matches
        from .message_write import _bound_release
        bound = _bound_release(destination.paths.package, generated=origin is not None)
        with mutation_admission(root, identity=RuntimeIdentity.current(), expected_release=bound) as current:
            if not runtime_route_matches(current, selected, package=destination.paths.package):
                raise CommandFailure("release_mismatch", "current compaction runtime changed",
                                     data=selected.provenance)
            selected.check(root, package=destination.paths.package)
            def emit(raws):
                selected.check(root, package=destination.paths.package)
                return emit_api.emit_batch(root, raws)
            data = record_compactions(destination.paths, destination.fleet, emit=emit)
        lines = (f"{data['fleet']}: {data['recorded']} compaction(s) recorded"
                 + (f"; not recorded: {data['error']}" if data["error"] else "") + ".",
                 *(f"{row['bot']}: {row['rows']} row(s), {row['recorded']} new, "
                   f"{row['duplicates']} already recorded; read {row['bytes_read']} bytes"
                   + (f", skipped {row['skipped_bytes']}" if row["skipped_bytes"] else "")
                   + (f"; unread: {row['reason']}" if row["reason"] else "")
                   for row in data["bots"]))
        selected.check(root, package=destination.paths.package,
                       message="active compaction selection changed during the pass")
        data.update(selected.provenance)
        if data["error"]:
            raise CommandFailure("unavailable", f"compactions not recorded: {data['error']}",
                                 data=data, release_id=selected.release_id)
        return CommandOutput(data, release_id=selected.release_id, lines=lines)
    except CommandFailure:
        raise
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected runtime differs from compaction caller",
                             hint=_runtime_hint(exc.hint)) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid compaction root or fleet selector") from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "compaction identity registry is unavailable") from exc
    except (ActivationError, PlanError, OperationContextError, BotNotFoundError) as exc:
        raise CommandFailure("conflict", "active compaction scope is incomplete") from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected compaction release is unavailable") from exc
    except (OSError, sqlite3.Error, PendingMigrationError, DowngradeError) as exc:
        raise CommandFailure("unavailable", "compaction cursor or transcript source is unavailable") from exc
