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
  - **Read-only, proven by execution.** The helper has no write door on purpose:
    the obvious one to add is a status flip, which silently 404s every redirect
    pointing at the product and unpublishes it from all channels irreversibly
    (traps.md 7). This used to be pinned with a regex over the source for
    ``-X (PUT|DELETE|PATCH)``, which could never match — the script passes
    ``-X "$method"`` and never spells a verb literally, so a wired write door
    walked past it. The checks now source the script and call the guards, and a
    reserved exit code separates a refusal from any other failure. A guard you
    cannot run is a guard nobody can trust.
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


def _guard(snippet: str) -> subprocess.CompletedProcess[str]:
    """Source the actuator and run `snippet` against it, with no credentials.

    Sourcing works because the script only dispatches when executed. That is the
    whole point: a guard has to be *run* to be believed.
    """
    return subprocess.run(
        ["bash", "-c", f"source {SCRIPT}; {snippet}"],
        capture_output=True,
        text=True,
        env=_scrubbed_env(),
        timeout=60,
    )


# Exit 7 is reserved by the script for a guard refusal, so it distinguishes
# "the guard stopped this" from "it failed for some other reason".
GUARD_REFUSED = 7
NO_CREDENTIALS = 2


@pytest.mark.parametrize(
    "snippet",
    [
        "api DELETE https://x.example/y",
        "api PUT https://x.example/y",
        "api PATCH https://x.example/y",
        # The REST write door: a POST at a REST path, which the method
        # allowlist alone would happily let through.
        "api POST https://x.example/admin/api/2026-04/products.json '{}'",
        # The load-bearing case. A mutation is a POST with a query-shaped
        # envelope, so NO method-level check can see it.
        "gql 'mutation { productUpdate(input:{}) { product { id } } }'",
        "gql 'subscription { x }'",
        # A document may carry several operations; checking only the first
        # would miss this one.
        "gql 'query { a } mutation { b }'",
    ],
)
def test_write_attempts_are_refused_when_executed(snippet: str) -> None:
    """Read-only, proven by running it — see traps.md 7 and the script header.

    This replaces a regex over the source for ``-X (PUT|DELETE|PATCH)``. That
    pattern could never match: the script passes ``-X "$method"`` and never
    spells a verb literally, so a wired write door walked past this guard and
    the bash one. Both asserted the appearance of the property.
    """
    proc = _guard(snippet)
    assert proc.returncode == GUARD_REFUSED, (
        f"{snippet!r} was not refused by a guard "
        f"(rc={proc.returncode}, expected {GUARD_REFUSED}).\n{proc.stderr}"
    )


@pytest.mark.parametrize("query", ["query { shop { name } }", "{ shop { name } }"])
def test_a_real_read_still_passes_the_guard(query: str) -> None:
    """The guard must not reject the reads the skill exists to perform.

    Without this, a guard that refused everything would pass every assertion
    above. A valid query gets through and stops at the absent credentials.
    """
    proc = _guard(f"gql '{query}'")
    assert proc.returncode == NO_CREDENTIALS, (
        f"a valid read was blocked (rc={proc.returncode}, expected "
        f"{NO_CREDENTIALS}).\n{proc.stderr}"
    )


def test_curl_is_invoked_only_inside_api() -> None:
    """Secondary and structural: no door may issue its own request.

    Deliberately not the load-bearing check — that is what the executed
    refusals above are for. Counts invocations rather than the word, because
    the header prose contains "anyone can curl Shopify" and a check that counts
    prose repeats the mistake this file just removed.
    """
    invocations = re.findall(r"^[^#\n]*\bcurl\s+-", SCRIPT.read_text(), re.M)
    assert len(invocations) == 2, (
        f"expected curl only in api() (with body / without); found "
        f"{len(invocations)} invocation(s) — a door may be bypassing the guards"
    )


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
