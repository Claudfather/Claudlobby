"""The preview gate in library/integrations/ decides as the rule in vercel.md says.

Each case commits to a small site repo, clones it the way Vercel does
(``git clone --depth=10``), and runs the ``ignoreCommand`` of the vercel.json
snippet in the gate's own header under ``sh -c`` in the clone, with the Vercel
variables the case sets. Vercel builds on exit 1 and cancels the deployment on
exit 0.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "library" / "integrations" / "vercel_preview_gate.sh"
DOC = REPO / "library" / "integrations" / "vercel.md"

BUILD, SKIP = 1, 0

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _snippet() -> dict:
    """The vercel.json keys the gate's header tells a repo to add, parsed as JSON."""
    header = "\n".join(line[1:] for line in GATE.read_text(encoding="utf-8").splitlines()
                       if line.startswith("#"))
    blocks = [b for b in re.findall(r"^\s*(\{.*?^\s*\})", header, re.M | re.S)
              if '"ignoreCommand"' in b]
    assert len(blocks) == 1, "the gate's header should hold one vercel.json snippet"
    return json.loads(blocks[0])


SNIPPET = _snippet()
COMMAND = SNIPPET["ignoreCommand"]


def _env(home: Path) -> dict:
    """A minimal env of our own: conftest's constructed_env needs conftest, which this
    file runs without, and would hand git the host's real HOME and XDG config."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Site Author",
        "GIT_AUTHOR_EMAIL": "author@example.com",
        "GIT_COMMITTER_NAME": "Site Author",
        "GIT_COMMITTER_EMAIL": "author@example.com",
    }


class Site:
    """An origin repo laid out like a small site, with the gate applied as its header says."""

    def __init__(self, root: Path, project: str = "") -> None:
        self.home = root / "home"
        self.home.mkdir()
        self.origin = root / "origin"
        self.root = root
        self.project = project
        self._clones = 0
        self.git("init", "-q", "-b", "main", str(self.origin), cwd=root)
        files = {
            "app/page.tsx": "export default function Page() { return 'v1' }\n",
            "content/post.md": "# A post the site renders\n",
            "README.md": "# Site\n",
            "docs/guide.md": "# Guide\n",
            ".github/workflows/ci.yml": "on: push\n",
            "tests/smoke.test.ts": "test('smoke', () => {})\n",
            "package.json": '{"dependencies": {"next": "15.5.26"}}\n',
            "vercel.json": json.dumps(SNIPPET, indent=2) + "\n",
            "scripts/vercel_preview_gate.sh": GATE.read_text(encoding="utf-8"),
        }
        self.commit("site", *((str(Path(project, rel)), text) for rel, text in files.items()))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(["git", *args], cwd=cwd or self.origin, env=_env(self.home),
                              capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def commit(self, message: str, *changes: tuple[str, str]) -> None:
        for rel, text in changes:
            path = self.origin / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if changes:
            self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str) -> None:
        self.git("checkout", "-q", "-B", name, "main")

    def deploy(self, ref: str = "feature", **vercel: str) -> tuple[int, str]:
        """Clone ``ref`` as Vercel does and run the snippet's command in the Root Directory.

        With any Vercel variable set, Vercel also sets the branch, as it does."""
        self._clones += 1
        clone = self.root / f"clone{self._clones}"
        self.git("clone", "-q", "--depth=10", "--branch", ref, self.origin.as_uri(), str(clone),
                 cwd=self.root)
        if vercel:
            vercel = {"VERCEL_GIT_COMMIT_REF": ref, **vercel}
        proc = subprocess.run(["sh", "-c", COMMAND], cwd=clone / self.project,
                              env={**_env(self.home), **vercel}, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr


def preview(previous: str = "") -> dict:
    return {"VERCEL_ENV": "preview", "VERCEL_GIT_PREVIOUS_SHA": previous}


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


@pytest.fixture
def built(site: Site) -> str:
    """The commit of a feature branch's last built preview."""
    site.branch("feature")
    site.commit("a page change, built", ("app/page.tsx", "export default 'v2'\n"))
    return site.head()


def test_the_header_turns_off_dependabot_branches_and_the_doc_names_the_gate():
    assert SNIPPET["git"] == {"deploymentEnabled": {"dependabot/**": False}}
    assert COMMAND.startswith("bash scripts/vercel_preview_gate.sh ")
    assert f"integrations/{GATE.name}" in DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("branch, vercel, reason", [
    pytest.param("main", {"VERCEL_ENV": "production"},
                 "not a preview deployment (VERCEL_ENV=production)", id="production"),
    pytest.param("feature", {}, "VERCEL_ENV=unset", id="no-system-variables"),
    pytest.param("staging", {"VERCEL_TARGET_ENV": "staging", **preview()},
                 "the staging environment", id="custom-environment"),
    pytest.param("feature", preview(), "first deployment", id="first-deployment"),
    pytest.param("feature", preview("0123456789abcdef0123456789abcdef01234567"),
                 "cannot compare", id="unknown-base"),
])
def test_a_docs_only_push_builds_when_the_gate_may_not_judge_it(site: Site, branch, vercel,
                                                                 reason):
    site.branch(branch)
    site.commit("docs only", ("README.md", "# Site, reworded\n"))
    rc, out = site.deploy(branch, **vercel)
    assert rc == BUILD, out
    assert reason in out


@pytest.mark.parametrize("changes, expected", [
    pytest.param([("README.md", "# Site, reworded\n"), ("docs/guide.md", "# Guide, longer\n")],
                 SKIP, id="docs"),
    pytest.param([("tests/smoke.test.ts", "test('more', () => {})\n"),
                  ("app/page.test.tsx", "test('page', () => {})\n"),
                  ("vitest.config.ts", "export default {}\n")], SKIP, id="tests"),
    pytest.param([(".github/workflows/ci.yml", "on: [push, pull_request]\n"),
                  (".github/dependabot.yml", "version: 2\n")], SKIP, id="ci"),
    pytest.param([("app/page.tsx", "export default 'v3'\n")], BUILD, id="page"),
    pytest.param([("README.md", "# Site, reworded\n"), ("app/page.tsx", "export default 'v3'\n")],
                 BUILD, id="page-and-docs"),
    pytest.param([("content/post.md", "# A post, rewritten\n")], BUILD,
                 id="nested-markdown-renders"),
    pytest.param([("package.json", '{"dependencies": {"next": "15.5.27"}}\n')], BUILD,
                 id="dependency"),
    pytest.param([("vercel.json", json.dumps(SNIPPET) + "\n")], BUILD, id="vercel-json"),
    pytest.param([("api/openapi.spec.json", "{}\n")], BUILD, id="spec-that-is-not-a-test"),
])
def test_a_later_push_builds_only_when_it_can_change_what_renders(site: Site, built: str,
                                                                  changes, expected):
    site.commit("the change", *changes)
    rc, out = site.deploy(**preview(built))
    assert rc == expected, out
    if expected == SKIP:
        assert f"nothing changed since {built[:12]} reaches the build" in out
    else:
        assert "can change what the site renders" in out


def test_a_dependabot_branch_without_the_key_is_judged_by_its_files(site: Site):
    site.branch("dependabot/npm_and_yarn/next-15.5.27")
    site.commit("a docs change, built", ("docs/guide.md", "# Guide, longer\n"))
    built = site.head()
    site.commit("bump next", ("package.json", '{"dependencies": {"next": "15.5.27"}}\n'))
    rc, out = site.deploy("dependabot/npm_and_yarn/next-15.5.27", **preview(built))
    assert rc == BUILD, out
    assert "package.json can change" in out


def test_a_page_moved_into_docs_builds(site: Site, built: str):
    site.git("mv", "app/page.tsx", "docs/page.tsx")
    site.commit("move the page")
    rc, out = site.deploy(**preview(built))
    assert rc == BUILD, out
    assert "app/page.tsx can change" in out


def test_a_base_older_than_the_shallow_clone_builds(site: Site, built: str):
    for n in range(10):
        site.commit(f"filler {n}")
    rc, out = site.deploy(**preview(built))
    assert rc == BUILD, out
    assert "cannot compare" in out


def test_an_empty_commit_asks_for_a_build(site: Site, built: str):
    site.commit("docs", ("README.md", "# Site, reworded\n"))
    site.commit("Build a preview")
    rc, out = site.deploy(**preview(built))
    assert rc == BUILD, out
    assert "the head commit changes no file" in out


def test_a_redeploy_of_the_built_commit_builds(site: Site, built: str):
    rc, out = site.deploy(**preview(built))
    assert rc == BUILD, out
    assert "redeploy" in out


def test_in_a_root_directory_the_globs_still_count_from_the_repo_top(tmp_path: Path):
    site = Site(tmp_path, project="site")
    site.branch("feature")
    site.commit("a page change, built", ("site/app/page.tsx", "export default 'v2'\n"))
    built = site.head()
    site.commit("top-level docs", ("README.md", "# Monorepo\n"), ("CHANGELOG.md", "# Changes\n"))
    rc, out = site.deploy(**preview(built))
    assert rc == SKIP, out
    site.commit("a page", ("site/app/page.tsx", "export default 'v3'\n"))
    rc, out = site.deploy(**preview(built))
    assert rc == BUILD, out
    assert "site/app/page.tsx can change" in out
