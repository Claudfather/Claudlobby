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

    from ..activation_state import read_selection
    from ..config_plan import read_plan
    from ..config_units import current_declarations
    from ..context import resolve_paths
    from ..supervision_inventory import Adapter, _catalog, collect_enrollment

    try:
        root_path = Path(root).expanduser().resolve()
        selected = read_selection(root_path)
        if selected is None:
            return
        plan = read_plan(root_path, selected["plan_id"])
        adapter = Adapter(resolve_paths(root=root_path).package)
        manager, _, _, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
        declarations = current_declarations(plan, manager)
        inventory = collect_enrollment(root_path, declarations, adapter=adapter).require_complete()
        checked = 0
        for unit in inventory.units:
            if not unit.installed:
                continue
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
