"""Tests for library/skills/printify — the first skill to ship an executable helper.

Wrapped in Python so CI enforces it (the #723 shell-tests-not-in-CI lesson).
Three surfaces:

  - ``printify/test.sh``: the skill's own suite, run with the PRINTIFY_* env
    scrubbed so only the hermetic arm executes — the write-door ``--dry-run``
    payload/coverage assertions run off ``fixtures/``, zero creds, zero network.
  - The **execute bit**, in the git index. Skills are symlinked rather than
    rendered, so ``printify_api.sh`` runs at whatever mode git recorded. A
    helper checked in 100644 is a skill that cannot run on a fresh clone.
  - The **shared env contract**. SKILL.md and mcp-vs-api.md both promise "one
    env contract, two tools" against library/mcp/printify.json — pin it, or the
    two halves drift apart silently.

Why not ``tests/test_sh_suites.py``: that glob covers ``tests/test_*.sh`` and
passes the ambient env, on a contract that a suite never reaches a real service.
This suite lives beside the skill (it resolves ``fixtures/`` relative to itself,
and a fleet overlay ships the pair together) and deliberately *keeps* a live arm
that activates when real credentials are present. Scrubbing the credentials in
the subprocess env is what makes it CI-safe, so it needs this wrapper rather
than that glob. Do not move it without moving the scrub.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from claudlobby.loader import frontmatter_error, parse_frontmatter

REPO_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_DIR / "library" / "skills" / "printify"
SCRIPT = SKILL_DIR / "printify_api.sh"
SUITE = SKILL_DIR / "test.sh"
MCP_FRAGMENT = REPO_DIR / "library" / "mcp" / "printify.json"

# The suite's write-door assertions build and inspect JSON with jq.
needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="jq not installed — write-door dry-run assertions need it",
)


@pytest.fixture(scope="module")
def hermetic_run() -> subprocess.CompletedProcess:
    """One run of the skill's suite with every Printify credential stripped.

    Scrubbing is what keeps CI hermetic: with no token the suite's live-smoke
    arm prints ``skip`` instead of calling the API, so a developer box that
    happens to export real creds cannot turn this into a live test. Module
    scope because the suite is deterministic — the assertions below read two
    different facts off the same observation, they do not need two runs.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PRINTIFY_API_KEY", "PRINTIFY_API_TOKEN", "PRINTIFY_SHOP_ID")
    }
    return subprocess.run(
        ["bash", str(SUITE)],
        cwd=SKILL_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@needs_jq
def test_skill_suite_passes_hermetically(hermetic_run):
    assert hermetic_run.returncode == 0, (
        f"printify test.sh failed:\n{hermetic_run.stdout}\n{hermetic_run.stderr}"
    )
    assert re.search(r"\b0 failed\b", hermetic_run.stdout), (
        f"expected 0 failures:\n{hermetic_run.stdout}"
    )


@needs_jq
def test_suite_skips_live_smoke_without_creds(hermetic_run):
    """The credential-free run must reach the skip, not an authenticated call."""
    assert "skip - live smoke" in hermetic_run.stdout, (
        f"live smoke did not self-skip:\n{hermetic_run.stdout}"
    )


@needs_jq
def test_multi_fetch_regression_runs_without_creds(hermetic_run):
    """The crash-class regression must be covered in *this* CI, which has no creds.

    A door that calls api_get twice in one scope used to die with "cfg: unbound
    variable" (a RETURN trap re-firing on the caller's return). That regression
    was originally guarded only inside the live-creds block — so in an OSS repo
    it skipped and the suite went green with the bug present, which reads as
    coverage without being it. Pin that the hermetic arm actually ran: without
    this, moving the check back behind the credential gate is a silent no-op.
    """
    assert (
        "ok   - hermetic: two api_get calls in one door scope" in hermetic_run.stdout
    ), "the credential-free multi-fetch regression did not run:\n" + hermetic_run.stdout


def test_helper_is_executable_in_the_git_index():
    """Skills are symlinked, never rendered — the mode ships from the index.

    ``library/tools/`` chmods its rendered output to 0755 at generate time; a
    skill-dir helper has no such step, so 100755 in git is the only thing
    standing between a fresh clone and a non-runnable skill.
    """
    r = subprocess.run(
        ["git", "ls-files", "-s", "--", "library/skills/printify"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:  # not a git checkout (sdist) — filesystem bit still applies
        pytest.skip("not a git checkout")
    seen = 0
    for line in r.stdout.splitlines():
        mode, _, _, tracked = line.split()
        if tracked.endswith(".sh"):  # positive-only: other assets may legitimately vary
            seen += 1
            assert mode == "100755", f"{tracked} is {mode}, expected 100755"
    assert seen == 2, f"expected 2 tracked .sh helpers, found {seen}"


@pytest.mark.parametrize("script", [SCRIPT, SUITE], ids=lambda p: p.name)
def test_helper_executable_on_disk(script):
    """The sdist path, where the git-index check above skips."""
    assert os.access(script, os.X_OK), f"{script.name} is not executable"


def test_env_contract_matches_the_mcp_fragment():
    """ "One env contract, two tools" — the claim SKILL.md and mcp-vs-api.md make.

    Every var the MCP fragment declares must be one the script actually reads,
    so a bot can equip the skill, the MCP, or both off a single secret set.
    """
    declared = set(json.loads(MCP_FRAGMENT.read_text())["_env_contract"])
    body = SCRIPT.read_text()
    # Braced and bare forms both count — the script uses ${PRINTIFY_API_KEY:-}
    # and bare $PRINTIFY_FIXTURE_VARIANTS.
    read_by_script = set(re.findall(r"\$\{?(PRINTIFY_[A-Z_]+)", body))
    assert declared <= read_by_script, (
        f"MCP fragment declares {sorted(declared - read_by_script)}, "
        "which printify_api.sh never reads — the shared contract has drifted"
    )


def test_skill_frontmatter_parses_and_names_the_skill():
    """library/skills/README.md: SKILL.md uses Claude Code's native frontmatter.

    Parsed through the production reader, not a regex, so this fails on YAML
    the compositor would itself reject.
    """
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert frontmatter_error(text) is None, frontmatter_error(text)
    meta, _ = parse_frontmatter(text)
    assert meta.get("name") == SKILL_DIR.name
    assert meta.get("description", "").strip()


def test_write_doors_are_draft_first():
    """The skill's central safety promise: create/migrate never auto-publish.

    ``door_publish`` is the only door that may reach the publish endpoint, and
    the shell suite proves at runtime that it refuses without ``--yes``. This
    pins the structural half: nothing *above* that door touches publish.json —
    which also covers the shared write helpers create/migrate delegate to.
    """
    body = SCRIPT.read_text()
    before_publish_door = body[: body.index("door_publish()")]
    assert "publish.json" not in before_publish_door, (
        "a door or shared helper other than door_publish reaches publish.json"
    )


def test_fixtures_are_synthetic():
    """Fixtures are sample data, never a live-store export.

    The write-door dry-run assertions read these; a real product id or a real
    image URL slipping in would put store data in a public repo.
    """
    for fixture in sorted((SKILL_DIR / "fixtures").glob("*.json")):
        raw = fixture.read_text()
        json.loads(raw)  # must parse
        for host in ("printify.com", "cdn.shopify.com"):
            assert host not in raw, f"{fixture.name} references a live host ({host})"
