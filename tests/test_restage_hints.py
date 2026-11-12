"""A refused operational read points to the executable, read-only Git status door."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys

import pytest

from claudlobby.active_config import load_active_context
from claudlobby.command_result import CommandFailure
from claudlobby.installation import install_updater
from tests.fixtures.current_runtime import current_runtime, tmp_path  # noqa: F401


@pytest.mark.parametrize("door", ["brief", "check-in"])
@pytest.mark.parametrize("cause", ["missing-current-config", "scope-identities-differ"])
def test_refusal_hint_runs_current_status_without_mutating_or_staging(door, cause, current_runtime, monkeypatch):
    from claudlobby.commands import brief_read, checkin

    root, installation, _, host = current_runtime
    scope = load_active_context(root, "example", package=host.package)
    monkeypatch.setattr("claudlobby.operation_context.resolve_operation_scope", lambda **kwargs: (scope, None))
    if cause == "missing-current-config":
        (root / "state/config.json").unlink()
    else:
        scope.fleet.bots.pop("worker")
    install_updater(installation, interpreter=sys.executable)
    before = {str(path.relative_to(root)): path.read_bytes()
              for path in root.rglob("*") if path.is_file()}
    args = argparse.Namespace(boot=False, json=False, usage_since=None, seed=False,
                              root=str(root), fleet="example", bot=None,
                              public_command="checkin.list")
    with pytest.raises(CommandFailure) as refused:
        (brief_read.dispatch if door == "brief" else checkin.dispatch)(args)
    hint = refused.value.error.hint
    assert "config plan" not in hint and "host activate" not in hint
    command = re.search(r"claudlobby(?:\s+--root\s+(?:'[^']*'|\S+))?\s+host\s+update\s+status", hint)
    assert command, hint
    run = subprocess.run([str(installation.cli), *shlex.split(command.group())[1:]],
                         text=True, capture_output=True, timeout=30)
    assert run.returncode == 0, (hint, run.stdout, run.stderr)
    status = json.loads(run.stdout)
    assert status["read_only"] is True and status["update"]["phase"] == "done"
    assert {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()} == before
