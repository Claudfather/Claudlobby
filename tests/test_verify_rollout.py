"""The rollout check: every PR names the production check that proves it (#2111).

The checker is the Python inside .github/workflows/verify-rollout.yml, run here
exactly as the workflow runs it, against a stand-in for `gh api`
(tests/fixtures/fake-gh-rollout.py) under a minimal environment. So the code under
test is the code that runs in CI, and no test can reach GitHub.

What it must hold:
- a missing or vacuous "## Rollout check" fails, as a product fleet's version and
  its peer review found it can be faked: a heading inside a fenced block or an HTML
  comment does not count, and a label left empty fails in each form it is written;
- "N/A" passes only as docs-only or tests-only, and only when every changed path is
  under the paths the caller file on the DEFAULT branch declares, so a PR cannot
  widen its own exemption;
- a failed lookup fails the check: it is never read as an empty answer;
- a PR that changes a workflow file is flagged, because it can rename or replace
  the job that checks it (the merge guardrails refuse it without a reviewer
  verdict that names the change);
- the check's name in the rollup is the name the merge guardrails read.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
CALLER = WORKFLOWS / "rollout-check.yml"
REUSABLE = WORKFLOWS / "verify-rollout.yml"
TEMPLATE = REPO / ".github" / "pull_request_template.md"
FAKE_GH = REPO / "tests" / "fixtures" / "fake-gh-rollout.py"
GUARDRAILS = REPO / "library" / "guardrails"
CHECK_NAME = "rollout-check / Rollout check"

FILLED = """\
## Rollout check
- **Observe:** `claudlobby --json brief` shows `data.brief.work.items[0].deadline` set
- **Control:** a task with no assignment shows `deadline: null`
- **When:** right after the release that contains the merge is active
- **Who:** any bot with the fleet's credentials
"""

NARROW_CALLER = """\
name: Rollout check
on:
  pull_request:
    types: [opened, edited, reopened, synchronize, ready_for_review]
jobs:
  rollout-check:
    uses: ./.github/workflows/verify-rollout.yml
    with:
      docs-paths: "documentation/** README.md CHANGELOG.md"
      tests-paths: "tests/**"
"""


def _load(path: Path) -> dict:
    if not path.exists():
        pytest.fail(f"missing {path.relative_to(REPO)}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _checker_source() -> str:
    doc = _load(REUSABLE)
    runs = [step.get("run", "") for step in doc["jobs"]["check"]["steps"]]
    found = [m.group(1) for run in runs
             for m in [re.search(r"python3 - <<'PY'\n(.*?)\nPY\s*$", run, re.S)] if m]
    assert len(found) == 1, "verify-rollout.yml must run exactly one inline checker"
    return found[0]


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, calls: list[list[str]], summary: str):
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        self.calls = calls
        self.summary = summary

    @property
    def errors(self) -> list[str]:
        return [l for l in self.out.splitlines() if l.startswith("::error")]

    @property
    def warnings(self) -> list[str]:
        return [l for l in self.out.splitlines() if l.startswith("::warning")]


def run_check(tmp_path: Path, body, files=("claudlobby/brief.py",), *, base_caller=NARROW_CALLER,
              fail=None, docs_run="", tests_run="", pr="7", event_body=None) -> Run:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "checker.py"
    script.write_text(_checker_source(), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    # A /bin/sh shim, so the stand-in runs under this interpreter on every runner
    # rather than whichever python3 the PATH below happens to reach.
    (fake_bin / "gh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n',
                                 encoding="utf-8")
    (fake_bin / "gh").chmod(0o755)
    entries = [f if isinstance(f, dict) else {"filename": f} for f in files]
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"body": body, "files": entries, "base_caller": base_caller,
                                 "fail": fail or {}}), encoding="utf-8")
    log = tmp_path / "gh-calls.log"
    log.write_text("", encoding="utf-8")
    summary = tmp_path / "summary.md"
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONUTF8": "1",
           "REPO": "example-org/example-repo", "PR_NUMBER": pr, "DEFAULT_BRANCH": "main",
           "DOCS_PATHS_RUN": docs_run, "TESTS_PATHS_RUN": tests_run,
           "FAKE_GH_STATE": str(state), "FAKE_GH_LOG": str(log),
           "GITHUB_STEP_SUMMARY": str(summary)}
    if event_body is not None:
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": int(pr), "body": event_body}}),
                         encoding="utf-8")
        env["GITHUB_EVENT_PATH"] = str(event)
    proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True,
                          text=True, timeout=60)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return Run(proc, calls, summary.read_text(encoding="utf-8") if summary.exists() else "")


def section(**fields: str) -> str:
    lines = ["## Rollout check"]
    for name in ("Observe", "Control", "When", "Who"):
        if name in fields and fields[name] is not None:
            lines.append(f"- **{name}:** {fields[name]}".rstrip())
    return "\n".join(lines) + "\n"


FULL = dict(Observe="the brief shows the deadline", Control="an unassigned task shows none",
            When="right after activation", Who="any bot")


# --- present and filled in -----------------------------------------------------

def test_a_complete_section_passes(tmp_path):
    run = run_check(tmp_path, "Intro.\n\n" + FILLED + "\n## Notes\nMore.\n")
    assert run.rc == 0, run.out + run.err
    assert run.errors == []


def test_a_body_with_no_section_fails(tmp_path):
    run = run_check(tmp_path, "## Summary\nA fix.\n")
    assert run.rc == 1
    assert any("no \"## Rollout check\" section" in e for e in run.errors), run.out


@pytest.mark.parametrize("body", ["", None], ids=["empty", "null"])
def test_an_empty_body_fails(tmp_path, body):
    run = run_check(tmp_path, body)
    assert run.rc == 1
    assert any("no \"## Rollout check\" section" in e for e in run.errors), run.out


def test_the_template_as_shipped_fails_and_filled_in_passes(tmp_path):
    if not TEMPLATE.exists():
        pytest.fail(f"missing {TEMPLATE.relative_to(REPO)}")
    template = TEMPLATE.read_text(encoding="utf-8")
    run = run_check(tmp_path / "as-shipped", template)
    assert run.rc == 1
    for name in ("Observe", "Control", "When", "Who"):
        assert any(f"**{name}:**" in e for e in run.errors), (name, run.out)
    filled = re.sub(r"(\*\*(?:Observe|Control|When|Who):\*\*) <!--.*?-->", r"\1 a real answer",
                    template)
    assert filled != template, "the template's four fields should each carry a comment prompt"
    run = run_check(tmp_path / "filled", filled)
    assert run.rc == 0, run.out


@pytest.mark.parametrize("missing", ["Observe", "Control", "When", "Who"])
def test_each_field_must_be_present(tmp_path, missing):
    run = run_check(tmp_path, section(**{k: v for k, v in FULL.items() if k != missing}))
    assert run.rc == 1
    assert any(f"no **{missing}:** line" in e for e in run.errors), run.out


@pytest.mark.parametrize("token", ["", "N/A", "n/a", "NA", "none", "None.", "TBD", "todo", "-", "?",
                                   "**N/A**"])
def test_a_vacuous_field_fails(tmp_path, token):
    run = run_check(tmp_path, section(**{**FULL, "Control": token}))
    assert run.rc == 1
    assert any("**Control:**" in e and "empty" in e for e in run.errors), run.out


@pytest.mark.parametrize("label", ["1. **Observe:**", "2) **Observe:**", "**Observe**:",
                                   "- **Observe**:", "Observe:"])
def test_a_label_left_empty_fails_however_it_is_written(tmp_path, label):
    body = section(**{k: v for k, v in FULL.items() if k != "Observe"}) + label + "\n"
    run = run_check(tmp_path, body)
    assert run.rc == 1
    assert any("**Observe:**" in e for e in run.errors), run.out


def test_a_numbered_label_filled_in_passes(tmp_path):
    body = ("## Rollout check\n1. **Observe:** the page serves 200\n2. **Control:** it served 404\n"
            "3. **When:** after the deploy\n4. **Who:** any bot\n")
    assert run_check(tmp_path, body).rc == 0


def test_the_section_ends_at_the_next_level_two_heading(tmp_path):
    body = "## Rollout check\n- **Observe:** x\n\n## Notes\n- **Control:** y\n- **When:** z\n- **Who:** w\n"
    run = run_check(tmp_path / "split", body)
    assert run.rc == 1
    assert any("no **Control:** line" in e for e in run.errors), run.out
    deeper = section(**FULL) + "### Detail\nA level-three heading is part of the section.\n"
    assert run_check(tmp_path / "h3", deeper).rc == 0


# --- fenced blocks and comments do not count (dara, from a peer review) ----------

@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_a_heading_only_inside_a_fence_does_not_count(tmp_path, fence):
    body = f"Here is the rule:\n\n{fence}markdown\n{section(**FULL)}{fence}\n"
    run = run_check(tmp_path, body)
    assert run.rc == 1
    assert any("no \"## Rollout check\" section" in e for e in run.errors), run.out


def test_a_fenced_example_then_a_real_section_passes(tmp_path):
    body = f"```\n## Rollout check\n- **Observe:** N/A\n```\n\n{section(**FULL)}"
    assert run_check(tmp_path, body).rc == 0


def test_a_field_given_only_as_a_fenced_command_passes(tmp_path):
    body = ("## Rollout check\n- **Observe:**\n  ```bash\n  claudlobby --json brief\n  ```\n"
            "- **Control:** an unassigned task shows none\n- **When:** right after activation\n"
            "- **Who:** any bot\n")
    run = run_check(tmp_path, body)
    assert run.rc == 0, run.out


@pytest.mark.parametrize("fence", ["```\n```\n", "```\n\n```\n", "```\n"],
                         ids=["closed-empty", "closed-blank", "unclosed-empty"])
def test_a_field_holding_only_an_empty_fence_fails(tmp_path, fence):
    body = section(**{k: v for k, v in FULL.items() if k != "Who"}) + "- **Who:**\n" + fence
    run = run_check(tmp_path, body)
    assert run.rc == 1
    assert any("**Who:**" in e for e in run.errors), run.out


def test_an_unclosed_fence_hides_the_heading_after_it(tmp_path):
    run = run_check(tmp_path, "```\nsome code\n\n" + section(**FULL))
    assert run.rc == 1


@pytest.mark.parametrize("opener,closer", [("````", "```"), ("~~~", "```"), ("```", "~~~")],
                         ids=["shorter", "tilde-then-backtick", "backtick-then-tilde"])
def test_a_fence_closes_only_on_its_own_character_and_length(tmp_path, opener, closer):
    body = f"{opener}\nquoted\n{closer}\n{section(**FULL)}"
    run = run_check(tmp_path, body)
    assert run.rc == 1, "the heading is still inside the open fence"


def test_a_backtick_info_string_with_a_backtick_is_not_a_fence(tmp_path):
    body = "```a`b\n" + section(**FULL)
    assert run_check(tmp_path, body).rc == 0


@pytest.mark.parametrize("body", [
    "<!--\n" + section(**FULL) + "-->\n",
    "<!-- unclosed\n" + section(**FULL),
    "<!-- one line --> text\n<!--\n" + section(**FULL),
], ids=["inside", "after-unclosed", "after-closed-then-unclosed"])
def test_a_heading_in_or_after_an_open_comment_does_not_count(tmp_path, body):
    assert run_check(tmp_path, body).rc == 1


@pytest.mark.parametrize("before", [
    "Inline code like `<!--` is text.\n",
    "A lone <!-- in a sentence, never closed, is text.\n",
    "A double-backtick span ``<!-- `x` `` is text too.\n",
], ids=["code-span", "unclosed-inline", "double-backtick-span"])
def test_a_comment_marker_github_shows_as_text_hides_nothing(tmp_path, before):
    """Found on #2116's own body (dara): `<!--` in inline code turned a comment
    on, and every later line, the section included, was dropped."""
    run = run_check(tmp_path, before + "\n" + section(**FULL))
    assert run.rc == 0, run.out


def test_a_complete_inline_comment_is_dropped(tmp_path):
    run = run_check(tmp_path, section(**{**FULL, "Who": "<!-- any bot -->"}))
    assert run.rc == 1
    assert any("**Who:**" in e for e in run.errors), run.out


@pytest.mark.parametrize("heading,passes", [
    ("## Rollout check", True), ("## rollout check:", True), ("   ## Rollout check", True),
    ("## Rollout check ##", True), ("    ## Rollout check", False), ("### Rollout check", False),
], ids=["plain", "lower-colon", "indented-3", "closing-hashes", "indented-4-is-code", "level-3"])
def test_the_heading_forms_github_renders(tmp_path, heading, passes):
    body = section(**FULL).replace("## Rollout check", heading, 1)
    assert (run_check(tmp_path, body).rc == 0) is passes


def test_a_comment_marker_inside_a_fence_is_text(tmp_path):
    body = "```html\n<!-- not a comment here\n```\n" + section(**FULL)
    assert run_check(tmp_path, body).rc == 0


# --- exemptions ------------------------------------------------------------------

def test_docs_only_passes_when_every_path_is_a_docs_path(tmp_path):
    run = run_check(tmp_path, "## Rollout check\nN/A: docs-only\n",
                    files=["documentation/guide.md", "CHANGELOG.md", "README.md"])
    assert run.rc == 0, run.out


def test_docs_only_fails_on_a_path_outside_docs_paths(tmp_path):
    run = run_check(tmp_path, "## Rollout check\nN/A: docs-only\n",
                    files=["documentation/guide.md", "library/guardrails/verify-rollout.md"])
    assert run.rc == 1
    assert any("library/guardrails/verify-rollout.md" in e for e in run.errors), run.out


def test_tests_only_is_checked_against_tests_paths(tmp_path):
    body = "## Rollout check\n**N/A:** tests-only\n"
    assert run_check(tmp_path / "ok", body, files=["tests/test_a.py", "tests/fixtures/x.txt"]).rc == 0
    run = run_check(tmp_path / "no", body, files=["tests/test_a.py", "claudlobby/brief.py"])
    assert run.rc == 1
    assert any("claudlobby/brief.py" in e for e in run.errors), run.out


def test_a_renamed_file_counts_by_its_old_path_too(tmp_path):
    files = [{"filename": "documentation/moved.md", "previous_filename": "library/protocols/moved.md"}]
    run = run_check(tmp_path, "## Rollout check\nN/A: docs-only\n", files=files)
    assert run.rc == 1
    assert any("library/protocols/moved.md" in e for e in run.errors), run.out


@pytest.mark.parametrize("line", ["N/A", "N/A: trivial", "n/a - small change", "NA: refactor"])
def test_n_a_without_a_known_reason_fails(tmp_path, line):
    run = run_check(tmp_path, f"## Rollout check\n{line}\n", files=["documentation/a.md"])
    assert run.rc == 1
    assert any("docs-only" in e and "tests-only" in e for e in run.errors), run.out


def test_the_exemption_reads_the_default_branch_caller_not_this_run(tmp_path):
    """dara (#2111): under on: pull_request the PR's own caller file runs, so its
    `with:` values are the PR's. A PR that widens docs-paths must not pass by it."""
    run = run_check(tmp_path, "## Rollout check\nN/A: docs-only\n",
                    files=["library/guardrails/no-push-main.md"],
                    docs_run="documentation/** library/**")
    assert run.rc == 1
    assert any("library/guardrails/no-push-main.md" in e for e in run.errors), run.out
    contents = [c for c in run.calls if any("/contents/" in a for a in c)]
    assert len(contents) == 1, run.calls
    assert any(a.endswith("/contents/.github/workflows/rollout-check.yml?ref=main") for a in contents[0])
    assert any("docs-paths" in w and "default branch" in w for w in run.warnings), run.out


def test_an_exemption_fails_when_the_default_branch_declares_no_paths(tmp_path):
    run = run_check(tmp_path, "## Rollout check\nN/A: docs-only\n", files=["documentation/a.md"],
                    base_caller=None)
    assert run.rc == 1
    assert any("no .github/workflows/rollout-check.yml" in e for e in run.errors), run.out


# --- a failed lookup fails the check (dara) ------------------------------------------

@pytest.mark.parametrize("what,body", [
    ("body", FILLED),
    ("files", FILLED),
    ("contents", "## Rollout check\nN/A: docs-only\n"),
])
def test_a_failed_lookup_fails_the_check(tmp_path, what, body):
    run = run_check(tmp_path, body, files=["documentation/a.md"], fail={what: "502"})
    assert run.rc == 1
    assert any("could not read" in e for e in run.errors), run.out


def test_the_body_is_read_live_not_from_the_event_payload(tmp_path):
    """A re-run replays the original event, so a payload body can be stale."""
    run = run_check(tmp_path, "## Summary\nThe section was deleted after the event.\n",
                    event_body=FILLED)
    assert run.rc == 1
    assert any(any(re.fullmatch(r"repos/[^/]+/[^/]+/pulls/7", a) for a in call) for call in run.calls)


# --- a PR that changes workflow files is flagged ---------------------------------------

@pytest.mark.parametrize("path", [".github/workflows/rollout-check.yml",
                                  ".github/workflows/verify-rollout.yml",
                                  ".github/workflows/test.yml"],
                         ids=["caller", "reusable", "another-workflow"])
def test_a_pr_that_changes_a_workflow_file_is_flagged(tmp_path, path):
    run = run_check(tmp_path, FILLED, files=["claudlobby/brief.py", path])
    assert run.rc == 0, "flagged, not failed: a PR that edits workflows can replace this check anyway"
    assert any(path in w and "reviewer" in w for w in run.warnings), run.out
    assert path in run.summary


def test_the_flag_is_raised_on_a_failing_body_too(tmp_path):
    run = run_check(tmp_path, "no section", files=[".github/workflows/rollout-check.yml"])
    assert run.rc == 1
    assert any(".github/workflows/rollout-check.yml" in w for w in run.warnings), run.out


# --- wiring -------------------------------------------------------------------------------

def test_a_run_with_no_pull_request_checks_nothing(tmp_path):
    run = run_check(tmp_path, FILLED, pr="")
    assert run.rc == 0
    assert run.calls == []


def test_the_rollup_name_is_the_name_the_merge_guardrails_read():
    caller, reusable = _load(CALLER), _load(REUSABLE)
    (job_id, job), = caller["jobs"].items()
    assert job["uses"] == "./.github/workflows/verify-rollout.yml"
    (_, check), = reusable["jobs"].items()
    assert f"{job.get('name', job_id)} / {check['name']}" == CHECK_NAME
    for name in ("merge-policy-auto-admin.md", "merge-policy-auto-after-review.md",
                 "verify-rollout.md"):
        assert f"`{CHECK_NAME}`" in (GUARDRAILS / name).read_text(encoding="utf-8"), name


def test_the_caller_reruns_on_a_body_edit_and_grants_read_only():
    caller = _load(CALLER)
    on = caller.get("on", caller.get(True))
    assert set(on) == {"pull_request"}
    assert {"opened", "edited", "reopened", "synchronize"} <= set(on["pull_request"]["types"])
    assert caller["permissions"] == {"contents": "read", "pull-requests": "read"}
    check = _load(REUSABLE)["jobs"]["check"]
    assert check["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_the_checker_takes_every_value_from_the_environment():
    """An expression spliced into a run: script is a script injection."""
    for step in _load(REUSABLE)["jobs"]["check"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_the_docs_paths_here_do_not_cover_composed_library_text():
    """library/ is composed into every bot, so it is production code in this repo."""
    run = _load(CALLER)["jobs"]["rollout-check"]["with"]
    globs = run["docs-paths"].split() + run["tests-paths"].split()
    for path in ("library/guardrails/no-push-main.md", "claudlobby/brief.py",
                 "claudlobby/_runtime_scripts/keepalive.sh", ".github/workflows/test.yml"):
        assert not any(fnmatch.fnmatchcase(path, g) for g in globs), path
