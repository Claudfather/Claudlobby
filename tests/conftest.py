"""Shared fixtures for claudlobby tests."""

from __future__ import annotations
import json
import os
import stat
from pathlib import Path
from textwrap import dedent

import pytest


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
