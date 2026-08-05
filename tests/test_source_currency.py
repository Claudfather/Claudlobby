"""Tests for the source-currency predicates in lib-common.sh (#1009).

`lib/validate-bot-change.sh` drives these end to end against real repos and is
the mandatory pre-merge gate — but CI runs pytest and does NOT run that harness,
so without this file every predicate the #1009 fix rests on is CI-invisible.
This covers the pure logic (URL parsing, tag selection, track resolution, the
pull guards, the org filter); the harness keeps the behavioural half.

The guards are the reason this is worth its length. `repo_pull_blocker` is what
stands between an unattended weekly job and somebody's uncommitted work, and it
fails silently in the dangerous direction: a guard that wrongly returns "no
blocker" reads exactly like a guard that is working.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"

# A private HOME for every git subprocess this module starts, and a CLOSED env.
#
# Both halves are load-bearing, and both were bugs (found root-causing a flake
# on PR #1013 — 6 of these 7 blocker tests failed in one full-suite run, passed
# on rerun and in isolation):
#
#   * The fixture helpers used to inherit the ambient environment. Any GIT_*
#     variable in the pytest process then reshapes fixture creation: exporting
#     GIT_DIR or GIT_WORK_TREE reproduces that exact 6-of-7 signature on demand,
#     and the one survivor is test_non_git_dir_blocks — which passes for the
#     WRONG REASON, because every repo now looks like "not a git checkout".
#     Env leaks across files are real here: `_load_env` (commands/_helpers.py)
#     injects a fleet .env into os.environ permanently, and test_main.py leaves
#     MY_TEST_VAR/MY_OTHER_VAR behind — measured reaching this module mid-suite.
#   * HOME pointed at /tmp, which is shared with every process on the host. Any
#     /tmp/.gitconfig any of them writes becomes this module's GLOBAL git config.
#
# So: no inheritance, a per-module HOME nothing else knows the path of, and both
# git config tiers routed to /dev/null. These tests now depend on nothing outside
# themselves — which is why the exact trigger for that one run no longer has to
# be identified to be sure it cannot recur here.
_GIT_HOME = Path(tempfile.mkdtemp(prefix="claudlobby-test-githome-"))
atexit.register(shutil.rmtree, _GIT_HOME, True)


def _clean_env(extra: dict | None = None) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(_GIT_HOME),
        # Ambient git config cannot reach these tests from any tier.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        # Deterministic identity, so a host with no git identity still commits.
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        **(extra or {}),
    }


def _sh(script: str, env: dict | None = None) -> str:
    """Run a snippet with lib-common.sh sourced; return stdout stripped."""
    proc = subprocess.run(
        ["bash", "-c", f'. "$1"; shift; {script}', "_", str(LIB_COMMON)],
        capture_output=True,
        text=True,
        env=_clean_env(env),
        timeout=60,
    )
    return proc.stdout.strip()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )


def _repo(tmp_path: Path, name: str, origin: str | None = None) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("v1")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "c1")
    _git(repo, "branch", "-M", "main")
    if origin:
        _git(repo, "remote", "add", "origin", origin)
    return repo


class TestRepoRemoteOrg:
    """The predicate that separates framework from product repos."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/Claudfather/Claudlobby.git", "claudfather"),
            ("git@github.com:Claudfather/Claudron.git", "claudfather"),
            ("ssh://git@github.com/Claudfather/clauDNA.git", "claudfather"),
            ("https://github.com/Claudfather/Claudlobby", "claudfather"),
            ("https://gitlab.com/SomeOrg/thing.git", "someorg"),
            ("https://github.com/Artemis-xyz/huntress.git", "artemis-xyz"),
        ],
    )
    def test_parses_both_url_spellings_and_lowercases(self, tmp_path, url, expected):
        repo = _repo(tmp_path, "r", origin=url)
        assert _sh(f'repo_remote_org "{repo}"') == expected

    def test_no_remote_yields_empty(self, tmp_path):
        """Empty means 'no org to match siblings against' — watch root alone."""
        repo = _repo(tmp_path, "r")
        assert _sh(f'repo_remote_org "{repo}"') == ""

    def test_reads_raw_config_not_insteadof_rewrite(self, tmp_path):
        """A corporate `insteadOf` mirror must not redefine the repo's owner.

        `git remote get-url` applies url.<base>.insteadOf, so on a host with a
        mirror rewrite every sibling would be judged against the MIRROR's org
        and silently unwatched.
        """
        repo = _repo(tmp_path, "r", origin="https://github.com/Claudfather/X.git")
        _git(
            repo,
            "config",
            "url./srv/mirror/X.git.insteadOf",
            "https://github.com/Claudfather/X.git",
        )
        assert _sh(f'repo_remote_org "{repo}"') == "claudfather"


class TestRepoNewestTag:
    def test_no_tags_is_empty(self, tmp_path):
        """Empty is meaningful: the repo has no release track."""
        assert _sh(f'repo_newest_tag "{_repo(tmp_path, "r")}"') == ""

    def test_no_tags_does_not_abort_an_errexit_caller(self, tmp_path):
        """Regression: the tag filter ends in grep, which exits 1 on no match.

        Unguarded, `tag=$(repo_newest_tag ...)` propagated that and killed the
        whole run for every untagged repo — i.e. for claudlobby, always.
        """
        repo = _repo(tmp_path, "r")
        out = _sh(f'set -e; t=$(repo_newest_tag "{repo}"); echo "survived[$t]"')
        assert out == "survived[]"

    def test_version_sorted_not_chronological(self, tmp_path):
        repo = _repo(tmp_path, "r")
        _git(repo, "tag", "v0.10.0")
        _git(repo, "tag", "v0.9.0")  # tagged later, lower version
        assert _sh(f'repo_newest_tag "{repo}"') == "v0.10.0"

    def test_prerelease_never_outranks_a_release(self, tmp_path):
        """This feeds an unattended fast-forward — an rc is not a release."""
        repo = _repo(tmp_path, "r")
        _git(repo, "tag", "v1.0.0")
        _git(repo, "tag", "v1.1.0-rc1")
        assert _sh(f'repo_newest_tag "{repo}"') == "v1.0.0"

    def test_non_release_tags_are_ignored(self, tmp_path):
        repo = _repo(tmp_path, "r")
        _git(repo, "tag", "v1.0.0")
        _git(repo, "tag", "nightly")
        _git(repo, "tag", "backup-2026-08")
        assert _sh(f'repo_newest_tag "{repo}"') == "v1.0.0"


class TestRepoCurrencyTarget:
    """One track rule, shared — the reporter and applier must not disagree."""

    def test_tagged_repo_tracks_its_newest_release(self, tmp_path):
        repo = _repo(tmp_path, "r")
        _git(repo, "tag", "v2.0.0")
        assert _sh(f'repo_currency_target "{repo}"') == "v2.0.0"

    def test_untagged_repo_tracks_its_default_branch(self, tmp_path):
        """A repo that ships by merging has no release to be behind."""
        repo = _repo(tmp_path, "r")
        assert _sh(f'repo_currency_target "{repo}"') == "origin/main"


class TestRepoPullBlocker:
    """What stands between the weekly job and somebody's work in progress."""

    def _with_upstream(self, tmp_path, name="r") -> Path:
        bare = tmp_path / f"{name}.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        repo = _repo(tmp_path, name, origin=str(bare))
        _git(repo, "push", "-q", "origin", "main")
        _git(repo, "branch", "--set-upstream-to=origin/main", "main")
        return repo

    def test_clean_synced_checkout_has_no_blocker(self, tmp_path):
        repo = self._with_upstream(tmp_path)
        assert _sh(f'repo_pull_blocker "{repo}"') == ""

    def test_dirty_working_tree_blocks(self, tmp_path):
        repo = self._with_upstream(tmp_path)
        (repo / "f.txt").write_text("edited but not committed")
        assert "dirty" in _sh(f'repo_pull_blocker "{repo}"')

    def test_untracked_file_blocks(self, tmp_path):
        """`git status --porcelain` counts untracked, and it should: an
        unattended pull landing on top of somebody's new file is the same
        surprise as landing on top of an edit."""
        repo = self._with_upstream(tmp_path)
        (repo / "scratch.txt").write_text("new")
        assert "dirty" in _sh(f'repo_pull_blocker "{repo}"')

    def test_unpushed_local_commits_block(self, tmp_path):
        repo = self._with_upstream(tmp_path)
        (repo / "g.txt").write_text("local work")
        _git(repo, "add", "g.txt")
        _git(repo, "commit", "-q", "-m", "local")
        assert "local commits not pushed" in _sh(f'repo_pull_blocker "{repo}"')

    def test_detached_head_blocks(self, tmp_path):
        """A pinned-version or bisect checkout is a deliberate position."""
        repo = self._with_upstream(tmp_path)
        _git(repo, "checkout", "-q", "--detach", "HEAD")
        assert "detached HEAD" in _sh(f'repo_pull_blocker "{repo}"')

    def test_no_upstream_blocks(self, tmp_path):
        repo = _repo(tmp_path, "r")
        assert "no upstream" in _sh(f'repo_pull_blocker "{repo}"')

    def test_non_git_dir_blocks(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        assert "not a git checkout" in _sh(f'repo_pull_blocker "{d}"')


class TestDiscoverFrameworkCheckouts:
    """The set. A list of paths was the bug; this is what replaced it."""

    def _discover(self, root: Path, locations: list[Path]) -> list[str]:
        locs = "\n".join(str(p) for p in locations)
        out = _sh(
            "_editable_project_locations() { printf '%s\\n' \"$_LOCS\"; }; "
            "discover_framework_checkouts",
            env={"CLAUDLOBBY_ROOT": str(root), "_LOCS": locs},
        )
        return [line for line in out.splitlines() if line]

    def test_same_org_sibling_is_watched_without_configuration(self, tmp_path):
        root = _repo(tmp_path, "root", origin="https://github.com/Org/Root.git")
        sib = _repo(tmp_path, "sib", origin="https://github.com/Org/Sib.git")
        assert str(sib) in self._discover(root, [sib])

    def test_other_org_repo_is_excluded(self, tmp_path):
        """Bots install the products they work on as editable packages too."""
        root = _repo(tmp_path, "root", origin="https://github.com/Org/Root.git")
        prod = _repo(tmp_path, "prod", origin="https://github.com/Other/Prod.git")
        assert str(prod) not in self._discover(root, [prod])

    def test_root_is_always_first_and_appears_once(self, tmp_path):
        """claudlobby is itself an editable install — without dedupe it would
        be fetched, reported and pulled twice."""
        root = _repo(tmp_path, "root", origin="https://github.com/Org/Root.git")
        found = self._discover(root, [root, root])
        assert found[0] == str(root)
        assert found.count(str(root)) == 1

    def test_subdirectory_of_a_sibling_resolves_to_its_top_level(self, tmp_path):
        root = _repo(tmp_path, "root", origin="https://github.com/Org/Root.git")
        sib = _repo(tmp_path, "sib", origin="https://github.com/Org/Sib.git")
        sub = sib / "pkg"
        sub.mkdir()
        assert self._discover(root, [sub]).count(str(sib)) == 1

    def test_rootless_org_watches_root_alone(self, tmp_path):
        """No remote on the compositor → no org to match siblings against."""
        root = _repo(tmp_path, "root")
        sib = _repo(tmp_path, "sib", origin="https://github.com/Org/Sib.git")
        assert self._discover(root, [sib]) == [str(root)]

    def test_non_git_location_is_skipped(self, tmp_path):
        root = _repo(tmp_path, "root", origin="https://github.com/Org/Root.git")
        plain = tmp_path / "plain"
        plain.mkdir()
        assert self._discover(root, [plain]) == [str(root)]


class TestEditableEnumeration:
    """The enumerator that feeds discovery — pip-free by design."""

    def test_does_not_require_pip(self):
        """A `venv --without-pip` (and `uv venv` by default) has no pip. The
        earlier pip-based version printed nothing and exited 0 there, silently
        collapsing discovery to claudlobby-only — #1009 re-armed, invisibly.
        """
        src = LIB_COMMON.read_text()
        assert "importlib.metadata" in src
        assert (
            "pip" not in src.split("_editable_project_locations()")[1].split("\n}")[0]
        )


class TestHermeticity:
    """The isolation itself, exercised — not just asserted in a comment.

    Root-causing the #1013 flake: exporting GIT_DIR (or GIT_WORK_TREE) made 6 of
    the 7 TestRepoPullBlocker tests fail on demand, with the seventh passing for
    the wrong reason. That is the signature that was reported as host load. These
    run the real fixture path under those exact variables, so a future edit that
    drops `env=_clean_env()` from a helper fails HERE rather than surfacing as an
    unexplained flake in somebody else's full-suite run.
    """

    @pytest.mark.parametrize(
        "var,value",
        [
            ("GIT_DIR", "/nonexistent/poison.git"),
            ("GIT_WORK_TREE", "/tmp"),
            ("GIT_INDEX_FILE", "/nonexistent/poison-index"),
            ("GIT_CONFIG_GLOBAL", "/nonexistent/poison.gitconfig"),
            ("HOME", "/nonexistent/poison-home"),
        ],
    )
    def test_ambient_git_env_cannot_reach_the_fixtures(self, tmp_path, monkeypatch, var, value):
        monkeypatch.setenv(var, value)
        repo = _repo(tmp_path, "r", origin="https://github.com/Org/R.git")
        (repo / "f.txt").write_text("edited")
        assert "dirty" in _sh(f'repo_pull_blocker "{repo}"')
        assert _sh(f'repo_remote_org "{repo}"') == "org"

    def test_clean_env_declares_no_repo_pointing_git_vars(self):
        """A closed allowlist, not a denylist: anything not named here is gone."""
        env = _clean_env()
        for leaky in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
            assert leaky not in env
        assert env["HOME"] != "/tmp", "HOME must not be the shared /tmp"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
