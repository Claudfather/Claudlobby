"""Best-effort accidental bot-context guard for operator-only commands.

Trusted local callers remain the boundary. On an activated host, ask the
existing native ancestry owner about each selected installed unit instead of
trusting an environment variable that a child process can unset.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..command_result import CommandFailure


def require_operator_context(root: str | Path | None = None) -> None:
    if any(name in os.environ for name in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE")):
        raise CommandFailure("conflict", "operation requires an operator shell outside generated bot context")
    if root is None:
        return  # Cold setup has no selected unit ancestry to inspect.

    from ..config_units import current_git_declarations
    from ..context import resolve_paths
    from ..supervision_inventory import Adapter, _catalog, collect_enrollment, is_unit_template

    try:
        root_path = Path(root).expanduser().resolve()
        package = resolve_paths(root=root_path).package
        installation = package.installation
        if installation is None:
            from ..plane.owner_source import fresh_owner_setup
            fresh_owner_setup(root_path, allow_absent=True)
            return  # Cold authoring has no current consumers; execution still requires Git.
        from ..active_config import read_current_config
        snapshot = read_current_config(root_path, installation=installation, revision=package.source_revision)
        adapter = Adapter(package)
        manager, _, _, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
        declarations = current_git_declarations(snapshot, manager)
        inventory = collect_enrollment(root_path, declarations, adapter=adapter).require_complete()
        checked = 0
        for unit in inventory.units:
            if not unit.installed or is_unit_template(unit.target):
                continue  # a template runs nothing, so it can host no caller
            checked += 1
            verdict = adapter.call("svc_activation_assert_external", unit.installed[0].path,
                                   unit.target, str(os.getpid()))
            if verdict.returncode == 1:
                raise CommandFailure("conflict", "operation requires an operator shell outside a managed unit")
            if verdict.returncode != 0:
                raise CommandFailure("unavailable", "operator ancestry could not be verified")
        if not checked:
            raise CommandFailure("unavailable", "selected host has no native unit ancestry evidence")
    except CommandFailure:
        raise
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        raise CommandFailure("unavailable", "operator ancestry could not be verified") from exc
