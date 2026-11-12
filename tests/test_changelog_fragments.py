"""Changelog fragments (#1714): a PR adds its entry as changelog.d/<branch>.md, and a
weekly roll-up moves the fragments into CHANGELOG.md.

Every PR used to add its entry at the top of CHANGELOG.md, so each merge put every
other open PR that carried one in conflict. bin/changelog.py `check` is the CI gate
(.github/workflows/changelog.yml) and `assemble` makes the roll-up; the rules are in
changelog.d/README.md. The git-backed cases build a throwaway repository, so they
read no state of this checkout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from tests.conftest import constructed_env, git_isolation_env

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "changelog.py"
WORKFLOW = REPO / ".github" / "workflows" / "changelog.yml"
ROLLOUT_CALLER = REPO / ".github" / "workflows" / "rollout-check.yml"
TEMPLATE = REPO / ".github" / "pull_request_template.md"
README = REPO / "changelog.d" / "README.md"

OLD_LOG = (
    "# Changelog\n\nAll notable changes.\n\n## [Unreleased]\n\n"
    "### Fixed — an older fix (#1)\n\nOlder.\n"
)
GOOD = "### Fixed — what a reader would notice (#12)\n\nWhat changed and why.\n"
SNEAKY = OLD_LOG.replace("## [Unreleased]\n\n",
                         "## [Unreleased]\n\n### Fixed — straight in (#2)\n\nX.\n\n")
MOVE = ("This PR adds an entry to CHANGELOG.md itself. Move it into "
        "`changelog.d/fix-12-thing.md`: CHANGELOG.md takes its entries only from the roll-up.")
IDENTITY = {f"GIT_{role}_{key}": value for role in ("AUTHOR", "COMMITTER")
            for key, value in (("NAME", "test"), ("EMAIL", "test@example.invalid"))}


@pytest.fixture(scope="module")
def cl():
    """bin/changelog.py, loaded as a module."""
    if not SCRIPT.exists():
        pytest.fail(f"missing {SCRIPT.relative_to(REPO)}")
    spec = importlib.util.spec_from_file_location("changelog_fragments", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _plain_git(monkeypatch):
    """Throwaway repositories read no user or system git config, and commit as a fixed
    identity; bin/changelog.py's own git calls inherit the same."""
    for key, value in git_isolation_env(os.devnull, **IDENTITY).items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)


class Repo:
    """A throwaway repository whose main holds a CHANGELOG.md and changelog.d/README.md."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.commit("base", {"CHANGELOG.md": OLD_LOG, "changelog.d/README.md": "# Fragments\n",
                             "src.txt": "one\n"})

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, message: str, files: dict[str, str | None]) -> str:
        """Write (or, for None, delete) the files and commit them on the current branch."""
        for rel, text in files.items():
            target = self.path / rel
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def read(self, rel: str) -> str:
        return (self.path / rel).read_text(encoding="utf-8")

    def pr(self, branch: str, files: dict[str, str | None], body: str = "") -> dict:
        """Commit the files on a new branch off main, and return its pull_request event."""
        self.git("checkout", "-q", "-b", branch, "main")
        head = self.commit(f"work on {branch}", files)
        self.git("checkout", "-q", "main")
        return {"pull_request": {"head": {"ref": branch, "sha": head},
                                 "base": {"sha": self.git("rev-parse", "main")}, "body": body}}


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo")


def run_cli(*args: str, **env: str) -> subprocess.CompletedProcess:
    """bin/changelog.py in a constructed environment, which carries no GITHUB_* variable of
    a CI job, with _plain_git's git settings."""
    git = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=constructed_env(**git, **env))


# --- the check ------------------------------------------------------------------

def test_a_pr_that_adds_its_fragment_passes(cl, repo):
    event = repo.pr("fix/12-thing", {"changelog.d/fix-12-thing.md": GOOD, "src.txt": "two\n"})
    problems, passed = cl.check(repo.path, event)
    assert problems == []
    assert "`changelog.d/fix-12-thing.md` adds 1 entry" == passed


@pytest.mark.parametrize("branch, path", [
    ("fix/12-thing", "changelog.d/fix-12-thing.md"),
    ("a/b/c", "changelog.d/a-b-c.md"),
    ("plain", "changelog.d/plain.md"),
])
def test_a_head_branch_names_one_fragment(cl, branch, path):
    assert cl.fragment_path(branch) == path


def test_a_pr_without_an_entry_is_told_the_file_to_add_and_its_format(cl, repo):
    """The failure is the only part of this a stranger sees: it names the file, shows the
    format, lists the categories and the way out."""
    event = repo.pr("fix/12-thing", {"src.txt": "two\n"})
    problems, _ = cl.check(repo.path, event)
    assert len(problems) == 1
    message = problems[0]
    assert "Add the file changelog.d/fix-12-thing.md" in message
    for category in cl.CATEGORIES:
        assert category in message
    assert "Do not edit CHANGELOG.md itself" in message
    assert "Changelog: none — <why>" in message
    assert cl.EXAMPLE in message
    assert cl.fragment_problems(textwrap.dedent(cl.EXAMPLE)) == []


def test_the_cli_fails_the_job_and_annotates_the_pr(cl, repo, tmp_path):
    event = repo.pr("fix/12-thing", {"src.txt": "two\n"})
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    run = run_cli("check", "--repo", str(repo.path), "--event", str(path), GITHUB_ACTIONS="true")
    assert run.returncode == 1, run
    assert "Add the file changelog.d/fix-12-thing.md" in run.stdout
    errors = [line for line in run.stdout.splitlines()
              if line.startswith("::error title=Changelog fragment::")]
    assert len(errors) == 1 and "changelog.d/fix-12-thing.md" in errors[0] and "%0A" in errors[0]

    event = repo.pr("fix/13-other", {"changelog.d/fix-13-other.md": GOOD})
    path.write_text(json.dumps(event), encoding="utf-8")
    run = run_cli("check", "--repo", str(repo.path), "--event", str(path))
    assert run.returncode == 0, run
    assert run.stdout.startswith("Changelog fragment: passes, because")


def test_the_cli_refuses_to_judge_without_an_event(tmp_path):
    run = run_cli("check", "--repo", str(tmp_path))
    assert run.returncode == 2
    assert "no pull_request event" in run.stderr


def test_the_description_read_live_is_the_one_judged(cl, repo, tmp_path):
    """CI passes the description as read through the API: a re-run replays the original
    event, whose copy of the description can be stale."""
    event = repo.pr("docs/x", {"src.txt": "two\n"}, body="Nothing about the changelog.")
    path, live = tmp_path / "event.json", tmp_path / "body.md"
    path.write_text(json.dumps(event), encoding="utf-8")
    live.write_text("Changelog: none — the description was edited after the event\n", encoding="utf-8")
    run = run_cli("check", "--repo", str(repo.path), "--event", str(path), "--body", str(live))
    assert run.returncode == 0, run
    event["pull_request"]["body"] = "Changelog: none — only the stale copy says so"
    path.write_text(json.dumps(event), encoding="utf-8")
    live.write_text("", encoding="utf-8")
    run = run_cli("check", "--repo", str(repo.path), "--event", str(path), "--body", str(live))
    assert run.returncode == 1 and "This PR adds no changelog entry." in run.stdout, run


def test_a_fragment_named_for_another_branch_is_refused_with_the_name_to_use(cl, repo):
    event = repo.pr("fix/12-thing", {"changelog.d/thing.md": GOOD})
    problems, _ = cl.check(repo.path, event)
    assert problems == ["`changelog.d/thing.md` is not this PR's fragment. Name it "
                        "`changelog.d/fix-12-thing.md`: the head branch, `fix/12-thing`, with "
                        "each `/` as `-`."]


@pytest.mark.parametrize("text", [
    "### Fix — a misspelt category\n",
    "### Fixed - a hyphen where the dash goes\n",
    "### fixed — lower case\n",
    "### Fixed —\n",
    "## Fixed — a level-2 heading\n",
    "# Fixed\n",
    "Prose before any heading.\n\n### Fixed — x\n",
    "### Fixed — x\n\n```bash\nan unclosed fence\n",
    "\n\n",
    "### Security\n\nA bare category heading: the entry has no title.\n",
])
def test_a_fragment_outside_the_format_is_refused(cl, text):
    assert cl.fragment_problems(text)


@pytest.mark.parametrize("text", [
    GOOD,
    "### Docs — a guide\n\n#### A sub-heading inside the entry\n\nText.\n",
    "### Added — one\n\nA.\n\n### Fixed — two\n\n```bash\n# a comment, not a heading\n```\n",
])
def test_a_fragment_in_the_format_passes(cl, text):
    assert cl.fragment_problems(text) == []


def test_a_bad_fragment_is_refused_by_line(cl, repo):
    event = repo.pr("fix/12-thing", {"changelog.d/fix-12-thing.md": "### Fix — typo\n"})
    problems, _ = cl.check(repo.path, event)
    assert problems == ["`changelog.d/fix-12-thing.md`: line 1, `### Fix — typo`, must read "
                        "`### <Category> — <what changed>`, with one of these categories: "
                        "Added, Changed, Deprecated, Removed, Fixed, Security, Docs."]


@pytest.mark.parametrize("other", [
    {"changelog.d/fix-12-thing.md": GOOD},
    {"src.txt": "two\n"},
], ids=["beside-its-fragment", "instead-of-one"])
def test_an_entry_written_straight_into_changelog_md_is_told_to_move(cl, repo, other):
    """The PR written the old way is told where its entry goes, and never that it has none."""
    event = repo.pr("fix/12-thing", {"CHANGELOG.md": SNEAKY, **other})
    problems, _ = cl.check(repo.path, event)
    assert problems == [MOVE]


def test_a_correction_inside_an_existing_entry_is_allowed(cl, repo):
    event = repo.pr("docs/fix-entry", {"CHANGELOG.md": OLD_LOG.replace("Older.", "Older, fixed.")},
                    body="Changelog: none — corrects the wording of an existing entry")
    problems, passed = cl.check(repo.path, event)
    assert problems == []
    assert "corrects the wording of an existing entry" in passed


@pytest.mark.parametrize("body, reason", [
    ("Changelog: none — a run log, not a change", "a run log, not a change"),
    ("- **Changelog:** none — tests only", "tests only"),
    ("Some text.\n\nchangelog: NONE: docs only", "docs only"),
    ("+ Changelog: none — a list item", "a list item"),
    ("1. __Changelog__: none — numbered and underlined", "numbered and underlined"),
    ("**Changelog**: none — the colon outside the bold", "the colon outside the bold"),
    ("Changelog: none", ""),
    ("Changelog: none — n/a", ""),
    ("Changelog: none — <why>", ""),
    ("Changelog: none — ?", ""),
    ("Changelog: none — **TBD.**", ""),
    ("Nothing about the changelog.", None),
])
def test_the_opt_out_needs_a_reason(cl, body, reason):
    """The description is read with the rollout check's spellings and vacuous words."""
    assert cl.opt_out(body) == reason


def test_an_opt_out_without_a_reason_is_refused(cl, repo):
    event = repo.pr("docs/x", {"src.txt": "two\n"}, body="Changelog: none")
    problems, _ = cl.check(repo.path, event)
    assert problems == ['The description says "Changelog: none" but not why. Put the reason after '
                        "it, on the same line: Changelog: none — <why this PR needs no entry>."]


def test_neither_the_templates_prompt_nor_a_fenced_line_opts_out(cl):
    assert cl.opt_out(TEMPLATE.read_text(encoding="utf-8")) is None
    assert cl.opt_out("```\nChangelog: none — inside a fence\n```\n") is None
    assert cl.opt_out("<!--\nChangelog: none — inside a comment\n-->\n") is None


def test_a_reused_branch_name_adds_its_entry_to_the_waiting_fragment(cl, repo):
    repo.commit("an earlier PR from fix/12-thing lands", {"changelog.d/fix-12-thing.md": GOOD})
    second = GOOD + "\n### Added — the second PR's entry (#13)\n\nMore.\n"
    problems, passed = cl.check(repo.path, repo.pr("fix/12-thing", {"changelog.d/fix-12-thing.md": second}))
    assert problems == [] and "adds 1 entry" in passed
    repo.git("branch", "-D", "fix/12-thing")
    reworded = GOOD.replace("What changed", "What really changed")
    problems, _ = cl.check(repo.path, repo.pr("fix/12-thing", {"changelog.d/fix-12-thing.md": reworded}))
    assert problems[0].startswith("This PR adds no changelog entry."), problems


DELETED = ("This PR deletes {n} fragment(s), but CHANGELOG.md is not exactly what "
           "`python3 bin/changelog.py assemble` makes of them. A roll-up commits that result "
           "unchanged, from a branch off fresh main. Any other PR that deletes a fragment, such "
           "as a revert, leaves CHANGELOG.md alone and says why in its description: "
           "Changelog: none — <why>.")


def test_a_fragment_deleted_without_a_reason_is_refused(cl, repo):
    repo.commit("a PR lands", {"changelog.d/fix-1-a.md": GOOD})
    event = repo.pr("fix/12-thing", {"changelog.d/fix-1-a.md": None,
                                     "changelog.d/fix-12-thing.md": GOOD})
    problems, _ = cl.check(repo.path, event)
    assert problems == [DELETED.format(n=1)]


def test_a_revert_may_delete_a_waiting_fragment_when_it_says_why(cl, repo):
    repo.commit("a PR lands", {"changelog.d/fix-1-a.md": GOOD, "src.txt": "two\n"})
    event = repo.pr("revert/fix-1-a", {"changelog.d/fix-1-a.md": None, "src.txt": "one\n"},
                    body="Changelog: none — reverts fix-1-a before its entry was rolled up")
    problems, passed = cl.check(repo.path, event)
    assert problems == []
    assert passed.endswith(", and it deletes changelog.d/fix-1-a.md")


# --- the roll-up ----------------------------------------------------------------

def _land(repo: Repo, name: str, heading: str) -> None:
    repo.commit(f"land {name}", {f"changelog.d/{name}.md": f"### Fixed — {heading}\n\n{heading}.\n"})


def _rollup(cl, repo: Repo, version: str | None = None) -> dict:
    """Run assemble on a branch off main and return the roll-up PR's event."""
    repo.git("checkout", "-q", "-b", "chore/changelog-roll-up", "main")
    cl.assemble(repo.path, version, "2026-10-14")
    head = repo.commit("roll-up", {})
    repo.git("checkout", "-q", "main")
    return {"pull_request": {"head": {"ref": "chore/changelog-roll-up", "sha": head},
                             "base": {"sha": repo.git("rev-parse", "main")}, "body": ""}}


def test_assemble_puts_the_newest_merge_first_and_deletes_the_fragments(cl, repo):
    _land(repo, "zz-first", "first")
    _land(repo, "aa-second", "second")
    repo.git("checkout", "-q", "-b", "side", "main")
    _land(repo, "mm-third", "third")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    said = cl.assemble(repo.path, None, "2026-10-14")
    assert said.startswith("Moved 3 fragment(s) into CHANGELOG.md under `## [Unreleased]`")
    log = repo.read("CHANGELOG.md")
    order = [line for line in log.splitlines() if line.startswith("### ")]
    assert order == ["### Fixed — third", "### Fixed — second", "### Fixed — first",
                     "### Fixed — an older fix (#1)"]
    assert log.startswith(OLD_LOG.split("### Fixed — an older fix")[0] + "### Fixed — third\n\nthird.\n\n")
    assert sorted(p.name for p in (repo.path / "changelog.d").iterdir()) == ["README.md"]


def test_assemble_with_a_version_moves_the_unreleased_entries_under_it(cl, repo):
    _land(repo, "fix-1-a", "a")
    event = _rollup(cl, repo, version="0.2.0")
    head = event["pull_request"]["head"]["sha"]
    log = repo.git("show", f"{head}:CHANGELOG.md") + "\n"
    assert "## [Unreleased]\n\n## [0.2.0] - 2026-10-14\n\n### Fixed — a\n\na.\n\n### Fixed — an older fix (#1)\n" in log
    problems, passed = cl.check(repo.path, event)
    assert problems == [] and passed.startswith("a roll-up of 1 fragment(s)")


@pytest.mark.parametrize("old, new", [
    ("### Fixed — b\n\nb.", "### Fixed — b\n\nb, edited."),
    ("### Fixed — b\n\nb.", "### Fixed — b\n\nb.\n\n### Fixed — c\n\nAn entry no fragment held."),
], ids=["an-entry-edited", "an-entry-added"])
def test_a_rollup_made_by_assemble_passes_and_any_change_to_it_fails(cl, repo, old, new):
    _land(repo, "fix-1-a", "a")
    _land(repo, "fix-2-b", "b")
    event = _rollup(cl, repo)
    problems, passed = cl.check(repo.path, event)
    assert problems == []
    assert passed == "a roll-up of 2 fragment(s), exactly as assemble makes it"

    repo.git("checkout", "-q", "chore/changelog-roll-up")
    edited = repo.read("CHANGELOG.md").replace(old, new)
    event["pull_request"]["head"]["sha"] = repo.commit("edit the roll-up", {"CHANGELOG.md": edited})
    repo.git("checkout", "-q", "main")
    problems, _ = cl.check(repo.path, event)
    assert problems == [DELETED.format(n=2)]


def test_a_rollup_that_drops_an_entry_fails(cl, repo):
    _land(repo, "fix-1-a", "a")
    _land(repo, "fix-2-b", "b")
    event = repo.pr("chore/changelog-roll-up", {
        "changelog.d/fix-1-a.md": None, "changelog.d/fix-2-b.md": None,
        "CHANGELOG.md": OLD_LOG.replace("## [Unreleased]\n\n",
                                        "## [Unreleased]\n\n### Fixed — b\n\nb.\n\n")})
    problems, _ = cl.check(repo.path, event)
    assert len(problems) == 1 and problems[0].startswith("This PR deletes 2 fragment(s)"), problems


def test_a_rollup_that_changes_anything_else_is_refused(cl, repo):
    """A PR cannot skip its own entry, or write into CHANGELOG.md, by carrying a roll-up."""
    _land(repo, "fix-1-a", "a")
    repo.git("checkout", "-q", "-b", "fix/12-thing", "main")
    cl.assemble(repo.path, None, "2026-10-14")
    head = repo.commit("a code change that carries a roll-up", {"src.txt": "two\n"})
    repo.git("checkout", "-q", "main")
    event = {"pull_request": {"head": {"ref": "fix/12-thing", "sha": head},
                              "base": {"sha": repo.git("rev-parse", "main")}, "body": ""}}
    problems, _ = cl.check(repo.path, event)
    assert problems == ["This PR rolls 1 fragment(s) into CHANGELOG.md but also changes src.txt. "
                        "A roll-up changes nothing else: open it on a branch of its own."]


def test_assemble_with_no_fragments_changes_nothing(cl, repo):
    assert cl.assemble(repo.path, None, "2026-10-14") == "changelog.d/ holds no fragments: nothing to roll up."
    assert repo.read("CHANGELOG.md") == OLD_LOG


def test_assemble_refuses_a_bad_fragment_or_uncommitted_changes(cl, repo):
    repo.commit("a bad fragment lands", {"changelog.d/fix-1-a.md": "### Fix — typo\n"})
    with pytest.raises(cl.Refusal, match="fix-1-a.md: line 1"):
        cl.assemble(repo.path, None, "2026-10-14")
    repo.commit("fixed", {"changelog.d/fix-1-a.md": GOOD})
    (repo.path / "CHANGELOG.md").write_text(OLD_LOG + "local edit\n", encoding="utf-8")
    with pytest.raises(cl.Refusal, match="uncommitted changes"):
        cl.assemble(repo.path, None, "2026-10-14")
    assert (repo.path / "changelog.d" / "fix-1-a.md").exists()


def test_assemble_refuses_a_version_that_is_not_one(tmp_path):
    run = run_cli("assemble", "--repo", str(tmp_path), "--version", "1.0]\n## x")
    assert run.returncode == 1 and "is not a version" in run.stderr


# --- this repository --------------------------------------------------------------

def test_every_heading_in_changelog_md_names_a_category(cl):
    """The oldest entries sit under bare category headings, so this reads the category
    alone; a fragment's heading also needs its title."""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = [line for _, line in cl._structure(text)[0] if line.startswith("### ")]
    assert headings
    category = re.compile(r"^### (?:%s)(?: — \S.*)?$" % "|".join(cl.CATEGORIES))
    assert [h for h in headings if not category.match(h)] == []


def test_every_fragment_waiting_in_changelog_d_is_in_the_format(cl):
    for path in sorted((REPO / "changelog.d").iterdir()):
        if path.name != "README.md":
            assert path.suffix == ".md", path.name
            assert cl.fragment_problems(path.read_text(encoding="utf-8")) == [], path.name


def test_the_readme_shows_a_fragment_the_check_accepts(cl):
    text = README.read_text(encoding="utf-8")
    example = re.search(r"```markdown\n(.*?)```", text, re.DOTALL)
    assert example, "changelog.d/README.md should show an example fragment"
    assert cl.fragment_problems(textwrap.dedent(example.group(1))) == []
    assert "Changelog: none — <why>" in text
    assert "python3 bin/changelog.py assemble" in text
    assert f"**Categories:** {', '.join(cl.CATEGORIES)}." in text


def test_the_workflow_runs_mains_checker_read_only_with_a_timeout():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = workflow.get("on", workflow.get(True))
    assert set(on) == {"pull_request"}
    assert {"opened", "edited", "reopened", "synchronize"} <= set(on["pull_request"]["types"])
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    (_, job), = workflow["jobs"].items()
    assert job["name"] == "Changelog fragment"
    assert 0 < job["timeout-minutes"] <= 10
    assert job["concurrency"]["cancel-in-progress"] is True
    checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert 'git show "origin/$DEFAULT_BRANCH:bin/changelog.py" > "$RUNNER_TEMP/changelog.py"' in script
    # The description is read live: a re-run replays the original event.
    assert 'gh api "repos/$REPO/pulls/$PR" --jq \'.body // ""\' > "$RUNNER_TEMP/body.md"' in script
    assert 'python3 "$RUNNER_TEMP/changelog.py" check --body "$RUNNER_TEMP/body.md"' in script
    # The PR's own copy runs only where the default branch lacks this workflow too.
    assert 'elif ! git cat-file -e "origin/$DEFAULT_BRANCH:.github/workflows/changelog.yml"' in script
    assert "exit 1" in script
    assert "${{" not in script


def test_both_rollout_path_lists_let_a_pr_carry_its_fragment():
    paths = yaml.safe_load(ROLLOUT_CALLER.read_text(encoding="utf-8"))["jobs"]["rollout-check"]["with"]
    for key in ("docs-paths", "tests-paths"):
        assert "changelog.d/**" in paths[key].split(), key


def test_the_instructions_bots_read_name_the_fragment_rule():
    """clauDNA's /ship reads the project's CLAUDE.md for its changelog convention, and the
    N/A lines there name the paths the rollout check lets such a PR change."""
    root = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert ("Entries go in `changelog.d/<head branch, each / as ->.md`, never in `CHANGELOG.md`."
            in root)
    assert "`CHANGELOG.md` or `changelog.d/**` may put the one line `N/A: docs-only`" in root
    assert "`tests/**` or `changelog.d/**` the line `N/A: tests-only`" in root
