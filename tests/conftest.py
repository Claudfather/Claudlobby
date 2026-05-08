"""Shared fixtures for claudlobby tests."""
from __future__ import annotations
import json
from pathlib import Path
from textwrap import dedent

import pytest


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
    for kind in ("expertise", "guardrails", "protocols", "integrations",
                 "mcp", "skills", "resources", "lessons"):
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
    (root / "library" / "integrations" / "github.md").write_text(dedent("""\
        ---
        title: GitHub MCP
        env_contract:
          GITHUB_PAT:
            description: GitHub personal access token
            tier: fleet
        ---

        # GitHub MCP

        Use GitHub MCP for repo operations.
    """))

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
