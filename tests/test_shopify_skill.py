"""Tests for library/skills/shopify — a domain actuator whose value is its traps.

Wrapped in Python so CI enforces it (the #723 shell-tests-not-in-CI lesson), and
modelled on ``tests/test_printify_skill.py``, the precedent for a skill shipping
an executable helper.

Four surfaces:

  - ``shopify/test.sh``: the skill's own suite, run with the SHOPIFY_* env
    scrubbed so only the hermetic arm executes — the trap-logic assertions run
    off ``fixtures/``, zero creds, zero network.
  - The **execute bit**, in the git index. Skills are symlinked rather than
    rendered, so the helper runs at whatever mode git recorded. A helper checked
    in 100644 is a skill that cannot run on a fresh clone.
  - **Read-only by construction.** The helper has no write door on purpose: the
    obvious one to add is a status flip, which silently 404s every redirect
    pointing at the product and unpublishes it from all channels irreversibly
    (traps.md 7). Pin the absence, or a future convenience commit erases the
    reasoning.
  - **Public-repo hygiene.** This repo is public and the skill was authored from
    a real store's data. GitGuardian catches credentials; it does not catch a
    store domain or a product id, so those get their own assertion.

Why not ``tests/test_sh_suites.py``: that glob covers ``tests/test_*.sh`` and
passes the ambient env, on a contract that a suite never reaches a real service.
This suite lives beside the skill (it resolves ``fixtures/`` relative to itself)
and deliberately *keeps* a live arm that activates when real credentials are
present. Scrubbing the credentials in the subprocess env is what makes it
CI-safe, so it needs this wrapper rather than that glob. Do not move it without
moving the scrub.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from claudlobby.loader import parse_frontmatter

REPO_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_DIR / "library" / "skills" / "shopify"
SCRIPT = SKILL_DIR / "shopify_api.sh"
TRAPS = SKILL_DIR / "traps.md"

SHOPIFY_ENV = (
    "SHOPIFY_STORE_DOMAIN",
    "SHOPIFY_SHOP_DOMAIN",
    "SHOPIFY_ACCESS_TOKEN",
    "SHOPIFY_ADMIN_ACCESS_TOKEN",
    "SHOPIFY_API_VERSION",
)


def _scrubbed_env() -> dict[str, str]:
    """Ambient env minus every Shopify credential, so the live arm stays dormant."""
    import os

    return {k: v for k, v in os.environ.items() if k not in SHOPIFY_ENV}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq required by the helper")
def test_skill_suite_passes_hermetically() -> None:
    proc = subprocess.run(
        ["bash", str(SKILL_DIR / "test.sh")],
        capture_output=True,
        text=True,
        env=_scrubbed_env(),
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"shopify/test.sh failed:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "0 failed" in proc.stdout


def test_helper_is_executable_in_the_git_index() -> None:
    out = subprocess.run(
        ["git", "ls-files", "-s", str(SCRIPT.relative_to(REPO_DIR))],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    ).stdout
    assert out.startswith("100755"), (
        f"{SCRIPT.name} must be 0755 in git or it cannot run on a fresh clone "
        f"(skills are symlinked, not rendered). Got: {out.strip()!r}. "
        f"Fix: git update-index --chmod=+x {SCRIPT.relative_to(REPO_DIR)}"
    )


def test_actuator_has_no_write_door() -> None:
    """Read-only by construction — see the module docstring and traps.md 7."""
    body = SCRIPT.read_text()
    assert not re.search(r"-X\s*(PUT|DELETE|PATCH)", body), (
        "shopify_api.sh must stay read-only. A status flip breaks every redirect "
        "pointing at the product and unpublishes it from all channels."
    )
    # The single POST reaches the GraphQL endpoint, which is a read.
    assert body.count("api POST") == 1


def test_frontmatter_is_valid_and_routes() -> None:
    meta, _ = parse_frontmatter((SKILL_DIR / "SKILL.md").read_text())
    assert meta.get("name") == "shopify"
    assert meta.get("description")
    assert meta.get("argument-hint"), "the router needs an argument-hint"
    for door in ("health-check", "catalog", "discounts", "collections", "orders"):
        assert door in meta["argument-hint"], f"{door} missing from argument-hint"


def test_every_door_is_documented() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text()
    for door in (
        "health-check",
        "orders",
        "catalog",
        "discounts",
        "collections",
        "raw",
    ):
        assert door in skill, f"SKILL.md does not document the {door!r} door"


def test_traps_are_all_present_and_cross_referenced() -> None:
    """The traps file IS the deliverable; the API calls are incidental."""
    traps = TRAPS.read_text()
    for n in range(1, 9):
        assert re.search(rf"^## {n}\. ", traps, re.M), f"traps.md is missing entry {n}"
    # The helper must point at the reasoning rather than restate it.
    assert "traps.md" in SCRIPT.read_text()


def test_no_store_identifiers_committed() -> None:
    """This repo is public. GitGuardian does not catch business identifiers."""
    allowed_hosts = {"myshop.myshopify.com", "example.myshopify.com"}
    for path in SKILL_DIR.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")

        for host in re.findall(r"[A-Za-z0-9-]+\.myshopify\.com", text):
            assert host in allowed_hosts, (
                f"{path.name}: real-looking store domain {host!r}"
            )

        for token in re.findall(r"\b(?:shpat|shpca|shppa)_[A-Za-z0-9]+", text):
            raise AssertionError(f"{path.name}: Shopify access token {token[:8]}…")

        # A long hex run is how a real product/variant/token id would look.
        for blob in re.findall(r"\b[0-9a-f]{16,}\b", text):
            raise AssertionError(f"{path.name}: long hex identifier {blob[:12]}…")
