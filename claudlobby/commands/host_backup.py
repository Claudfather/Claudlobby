"""Explicit local backup/restore adapters; no credential or native service effects."""
from pathlib import Path

from ..command_result import CommandFailure, CommandOutput


def dispatch(args):
    from ..activation_state import ActivationRefusal
    from ..host_backup import create_backup, inspect_backup, restore_backup
    from .operator_context import require_operator_context
    from .releases import _host_root

    require_operator_context()
    if args.fleet is not None or args.seed:
        raise CommandFailure("invalid_argument", "host backup rejects --fleet and --seed")
    action = args.backup_command
    try:
        if action == "inspect":
            data = inspect_backup(Path(args.bundle).expanduser())
            lines = ("Backup files verified; no restore or activation performed.",)
        elif action == "create":
            root = _host_root(args)
            require_operator_context(root)
            data = create_backup(root, Path(args.destination).expanduser())
            lines = ("Backup created. Keep a private off-host copy using your own storage tools.",)
            if data["schema"] == 2:
                lines += ("Configuration files are authored_at_backup; committed configuration and update records are separate recovery evidence.",)
        else:
            if args.root is None or not Path(args.root).expanduser().is_absolute():
                raise CommandFailure("invalid_argument", "restore requires explicit absolute --root")
            root = Path(args.root).expanduser()
            data = restore_backup(Path(args.bundle).expanduser(), root,
                                  replacing_retired_host=args.replacing_retired_host)
            lines = ("Backup restored to an inactive root; no service was installed or started.",)
            if data["schema"] == 2:
                lines += ("Reinstall the recorded Git revision and its locked dependencies in a new installation.",
                          "Review authored_at_backup configuration and old absolute paths before explicit initialization and recomposition.",
                          "state/restored-backup/config.json and any saved update.json are inert evidence; do not promote or resume them.",
                          "Git initialization must explicitly admit the restored database; restore alone does not make it runnable.",
                          "Reconcile external work, re-provision credentials, and pair and authorize owner access again.")
            else:
                lines += ("Install the recorded release, review configuration and external work, re-provision credentials,",
                          "then plan and activate through the normal host commands. Pair and authorize owner access again.")
            lines += ("If host-system.yaml exists, select it with CLAUDLOBBY_HOST_SYSTEM_YAML when planning.",)
    except ActivationRefusal as exc:
        raise CommandFailure("conflict", f"backup refused: {exc}") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CommandFailure("unavailable", "backup operation did not complete; inspect the destination before retrying") from exc
    return CommandOutput(data, lines=lines)
