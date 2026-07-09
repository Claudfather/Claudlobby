"""Shared fixtures for claudlobby tests."""

from __future__ import annotations
import json
import os
import stat
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture(autouse=True)
def _isolate_claudlobby_root(monkeypatch):
    """Bot sessions and timer jobs export CLAUDLOBBY_ROOT, and Paths.detect()
    honors it over the cwd walk-up — strip it so hint-less detection in tests
    is hermetic. Tests that need it set it explicitly via monkeypatch.setenv."""
    monkeypatch.delenv("CLAUDLOBBY_ROOT", raising=False)


# Captures chat id, the (expanded) state dir the caller resolved, and the
# message — the observation point for the emit_* fleet-signal paths.
TG_STUB = (
    "#!/bin/bash\n"
    'printf "%s|%s|%s\\n" "$TELEGRAM_GROUP_CHAT_ID" "$TELEGRAM_STATE_DIR" "$1" >> "$TG_CAPTURE"\n'
)


def read_fleet_events(root):
    """Concatenated fleet-event JSONL under <root>/state/events (or '')."""
    events_dir = Path(root) / "state" / "events"
    if not events_dir.is_dir():
        return ""
    return "".join(f.read_text() for f in sorted(events_dir.iterdir()))


def _scrubbed_env(**overrides):
    """os.environ minus the bot-session vars that would short-circuit chat
    resolution (FLEET_PULSE_ESCALATION_CHAT_ID et al) or repoint the root."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TELEGRAM", "CLAUDLOBBY", "FLEET"))
    }
    env.update(overrides)
    return env


def _write_exec(path, content):
    """Write a stub script and set its exec bits (shared shell-test harness helper)."""
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def call_lib_fn(fn: str, value: str) -> str:
    """Source lib-common.sh and call one function on a single value, returning
    stdout. The value travels as a positional arg so the shell never
    interprets it."""
    lib = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"
    r = subprocess.run(
        ["bash", "-c", f'. "{lib}"; {fn} "$1"', "_", value],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=10,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def load_lib_module(name: str):
    """Import a lib/*.py script as a module (they have no package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), Path(__file__).parent.parent / "lib" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_jsonl(path: Path, rows: list) -> None:
    import json

    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def dispatch_row(bot, dispatched_at, expected_by, task_id=None, **extra):
    """Ledger-schema fixture for dispatch-log.jsonl rows (task_id optional)."""
    row = {
        "ts": "2026-05-27T10:00:00Z",
        "manager": "lead",
        "bot": bot,
        "task": "do x",
        "dispatched_at": dispatched_at,
        "expected_by": expected_by,
    }
    if task_id is not None:
        row["task_id"] = task_id
    row.update(extra)
    return row


def report_row(bot, ts, status="completed", task_id=None, **extra):
    """Ledger-schema fixture for report-back.jsonl rows (task_id optional)."""
    row = {
        "ts": ts,
        "bot": bot,
        "status": status,
        "summary": "done",
        "pr_url": "",
        "issues": "",
        "skill": "",
    }
    if task_id is not None:
        row["task_id"] = task_id
    row.update(extra)
    return row


def load_test_fleet(fleet_dir: Path):
    """load_fleet against a fleet_dir fixture, returning just the FleetConfig."""
    from claudlobby.config import load_fleet

    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return fleet


def make_paths(fleet_dir: Path):
    """Paths rooted at a fleet_dir fixture (root == fleet_dir)."""
    from claudlobby.paths import Paths

    return Paths(root=fleet_dir, fleet_dir=fleet_dir)


def install_real_template(root: Path) -> None:
    """Overwrite a test root's stub claude.md.j2 with the repo's real template.

    The fleet_dir fixture ships a minimal stub; tests asserting real template
    sections (Projects table, Fleet You Manage, …) install the real one.
    """
    (root / "templates").mkdir(exist_ok=True)
    (root / "templates" / "claude.md.j2").write_text(
        (Path(__file__).parent.parent / "templates" / "claude.md.j2").read_text()
    )


MINIMAL_FLEET_YAML = dedent("""\
    fleet:
      name: test-fleet
      service_prefix: com.test
      telegram_group_chat_id: "-100999"

      accounts:
        default: ~/.claude

      defaults:
        model: opus
        guardrails: [no-push-main]
        protocols: [report-back]

      teams:
        eng:
          manager: lead
          workers: [worker-1]

      bots:
        lead:
          expertise: [orchestration]
          telegram:
            handle: lead_bot
            token_env: TELEGRAM_TOKEN_LEAD
            require_mention: false
        worker-1:
          expertise: [software-engineering]
          telegram:
            handle: worker1_bot
            token_env: TELEGRAM_TOKEN_WORKER1
""")


@pytest.fixture
def fleet_dir(tmp_path: Path) -> Path:
    """Create a minimal fleet layout under tmp_path and return the root."""
    root = tmp_path / "claudlobby"
    root.mkdir()

    # fleet.yaml
    (root / "fleet.yaml").write_text(MINIMAL_FLEET_YAML)

    # Minimal library
    for kind in (
        "expertise",
        "guardrails",
        "protocols",
        "integrations",
        "mcp",
        "skills",
        "resources",
        "lessons",
    ):
        (root / "library" / kind).mkdir(parents=True)

    # Expertise files the fleet references
    (root / "library" / "expertise" / "orchestration.md").write_text(
        "# Orchestrator\n\nManage the team.\n"
    )
    (root / "library" / "expertise" / "software-engineering.md").write_text(
        "# Engineer\n\nBuild things.\n"
    )

    # A guardrail and protocol the defaults reference
    (root / "library" / "guardrails" / "no-push-main.md").write_text(
        "---\ntitle: No push to main\n---\n\nNever push to main.\n"
    )
    (root / "library" / "protocols" / "report-back.md").write_text(
        "---\ntitle: Report-Back Protocol\n---\n\nReport back when done.\n"
    )

    # MCP fragment
    mcp_frag = {
        "github": {"command": "gh", "args": ["mcp"]},
        "_env_contract": {
            "GITHUB_PAT": {"description": "GitHub PAT", "tier": "fleet"},
        },
    }
    (root / "library" / "mcp" / "github.json").write_text(json.dumps(mcp_frag))

    # Integration doc with env_contract
    (root / "library" / "integrations" / "github.md").write_text(
        dedent("""\
        ---
        title: GitHub MCP
        env_contract:
          GITHUB_PAT:
            description: GitHub personal access token
            tier: fleet
        ---

        # GitHub MCP

        Use GitHub MCP for repo operations.
    """)
    )

    # Template
    (root / "templates").mkdir()
    (root / "templates" / "claude.md.j2").write_text(
        "# {{ bot.name }}\n\n{{ expertise_body }}\n"
    )

    # Voices dir
    (root / "voices").mkdir()

    # Runtime dir
    (root / "runtime" / "bots").mkdir(parents=True)

    return root
