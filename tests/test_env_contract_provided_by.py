"""_env_contract "provided_by": "composer" (plan P2a, fork F2, #511).

A fragment may mark an env var as composer-provided (e.g. CLAUDRON_VAULT_PATH,
emitted into bot.conf from fleet.yaml) — such vars are self-documentation for
the fragment, not operator obligations: collect_env_contracts skips them, so
neither .env scaffolding nor doctor's env presence check ever surfaces them.
Ordinary contract vars are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from claudlobby.composer import collect_env_contracts, scaffold_env_files
from claudlobby.config import load_fleet
from claudlobby.paths import Paths

CLAUDRON_FRAGMENT = {
    "_env_contract": {
        "CLAUDRON_VAULT_PATH": {
            "description": "Claudron vault path (composed into bot.conf)",
            "tier": "fleet",
            "provided_by": "composer",
        },
    },
    "_permissions_contract": {
        "tools": ["claudron_lookup", "claudron_read", "claudron_write"],
    },
    "claudron": {"command": "claudron-mcp", "args": []},
}


def _setup(tmp_path: Path) -> tuple[Path, Paths]:
    root = tmp_path / "claudlobby"
    root.mkdir()
    (root / "fleet.yaml").write_text(
        dedent("""\
        fleet:
          name: test-fleet
          service_prefix: com.test
          bots:
            worker:
              expertise: [eng]
              mcp: [claudron, github]
        """)
    )
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (root / "library" / "mcp").mkdir(parents=True)
    (root / "library" / "mcp" / "claudron.json").write_text(
        json.dumps(CLAUDRON_FRAGMENT)
    )
    (root / "library" / "mcp" / "github.json").write_text(
        json.dumps(
            {
                "github": {"command": "gh", "args": ["mcp"]},
                "_env_contract": {
                    "GITHUB_PAT": {"description": "GitHub PAT", "tier": "fleet"},
                },
            }
        )
    )
    (root / "runtime" / "bots" / "worker").mkdir(parents=True)
    return root, Paths(root=root, fleet_dir=root)


def test_composer_provided_var_not_collected(tmp_path):
    root, paths = _setup(tmp_path)
    fleet, _md = load_fleet(root / "fleet.yaml")
    names = {v.name for v in collect_env_contracts(fleet, paths)}
    assert "CLAUDRON_VAULT_PATH" not in names
    # Sibling ordinary vars are unaffected.
    assert "GITHUB_PAT" in names


def test_composer_provided_var_not_scaffolded(tmp_path):
    root, paths = _setup(tmp_path)
    fleet, _md = load_fleet(root / "fleet.yaml")
    scaffold_env_files(fleet, paths, log=lambda m: None)
    env_text = (root / ".env").read_text()
    assert "CLAUDRON_VAULT_PATH" not in env_text
    assert "GITHUB_PAT" in env_text


def test_provided_by_other_values_still_collected(tmp_path):
    # Only the literal "composer" opts out — unknown values behave as absent
    # (the contract stays operator-facing unless explicitly composer-provided).
    root, paths = _setup(tmp_path)
    frag = dict(CLAUDRON_FRAGMENT)
    frag["_env_contract"] = {
        "CLAUDRON_VAULT_PATH": {
            "description": "x",
            "provided_by": "operator",
        }
    }
    (root / "library" / "mcp" / "claudron.json").write_text(json.dumps(frag))
    fleet, _md = load_fleet(root / "fleet.yaml")
    names = {v.name for v in collect_env_contracts(fleet, paths)}
    assert "CLAUDRON_VAULT_PATH" in names
