"""Fixture vaults for claudron tests."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Minimal vault with _shared/knowledge/ containing one doc."""
    root = tmp_path / "vault"
    (root / "_shared" / "knowledge").mkdir(parents=True)
    (root / "_shared" / "decisions").mkdir(parents=True)
    (root / "_shared" / "runbooks").mkdir(parents=True)
    (root / "projects").mkdir()

    (root / "_shared" / "knowledge" / "auth-patterns.md").write_text(
        dedent("""\
        ---
        title: Auth Patterns Across Services
        type: knowledge
        status: active
        owner: mason
        tags: [auth, jwt, architecture]
        created: 2026-04-01
        updated: 2026-05-01
        ---

        # Auth Patterns Across Services

        All services use JWT with RS256. Tokens issued by auth-service,
        validated by middleware in each service.

        ## Token Refresh

        Refresh tokens are stored in httpOnly cookies.
    """)
    )

    (root / "_shared" / "knowledge" / "deploy-checklist.md").write_text(
        dedent("""\
        ---
        title: Deploy Checklist
        type: knowledge
        status: active
        owner: alex
        tags: [deploy, operations]
        created: 2026-03-15
        updated: 2026-04-20
        ---

        # Deploy Checklist

        Before deploying, verify migrations, env vars, and health checks.
    """)
    )

    return root


@pytest.fixture
def vault_with_projects(vault_dir: Path) -> Path:
    """Vault with project-specific knowledge."""
    proj = vault_dir / "projects" / "storydump"
    proj.mkdir(parents=True)
    (proj / "neon-pooling.md").write_text(
        dedent("""\
        ---
        title: Neon Connection Pooling Gotchas
        type: knowledge
        status: active
        owner: alex
        tags: [neon, database, storydump]
        created: 2026-04-10
        updated: 2026-05-05
        ---

        # Neon Connection Pooling Gotchas

        Neon's serverless driver pools differently than pg.
        Connection limits are per-branch, not per-project.
    """)
    )
    return vault_dir


@pytest.fixture
def vault_with_fleet(vault_dir: Path) -> Path:
    """Vault with a fleet overlay containing fleet-scoped knowledge."""
    fleet = vault_dir / "test-fleet"
    fleet.mkdir()
    (fleet / "fleet.yaml").write_text(
        dedent("""\
        fleet:
          name: test-fleet
          service_prefix: com.test
          bots: {}
    """)
    )
    (fleet / "shared" / "knowledge").mkdir(parents=True)
    (fleet / "shared" / "knowledge" / "railway-gotchas.md").write_text(
        dedent("""\
        ---
        title: Railway Deploy Gotchas
        type: knowledge
        status: active
        owner: branden
        tags: [railway, deploy]
        created: 2026-04-15
        updated: 2026-05-10
        ---

        # Railway Deploy Gotchas

        Railway needs RAILWAY_TOKEN set. Watch for cold-start timeouts.
    """)
    )
    return vault_dir


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    """Vault with _shared/ but no docs."""
    root = tmp_path / "empty-vault"
    (root / "_shared" / "knowledge").mkdir(parents=True)
    (root / "_shared" / "decisions").mkdir(parents=True)
    (root / "_shared" / "runbooks").mkdir(parents=True)
    return root
