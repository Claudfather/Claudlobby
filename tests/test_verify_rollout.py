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
- a draft is not checked, and marking it ready for review runs the check, so a body
  finished while the PR is a draft leaves no failed run (#2467); a failing run says so.
- the body also names what ran for the PR, under "## Harness" (#2792): what CI ran,
  each instrument or live test run with its result, or one line saying why none
  applies. Any one of them filled in passes; a missing section, or one that names
  nothing, fails.
- two jobs run the one checker (#2797). When a PR is offered (opened, reopened or
  marked ready) with a body that fails, the gate, which has the rollup name, re-reads
  the body for up to 180 s, so an edit in that time passes it; the cue judges once and
  fails at once, as the check always did, and cannot fail the run. On any other event
  both judge once, and a failed read during the wait fails at once.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
import time
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
SUCCESS_LINE = "Rollout check: the PR body names what ran and its rollout check."
# The gate's wait, shortened for these tests: test_the_shipped_wait_is_180_s_read_every_15_s
# pins the real values. A body that passes ends the wait at once, and each read of the fake
# gh is a new process, so the wait is long enough for a slow host to reach the edit.
WAIT_TEST_S = 5
POLL_TEST_S = 0.05

FILLED = """\
## Rollout check
- **Observe:** `claudlobby --json brief` shows `data.brief.work.items[0].deadline` set
- **Control:** a task with no assignment shows `deadline: null`
- **When:** right after the release that contains the merge is active
- **Who:** any bot with the fleet's credentials
"""

#: Put in front of each body unless a test passes its own, so the tests of the Rollout
#: check read only that section's findings.
HARNESS = "## Harness\n- **CI:** the harness job's scenarios, which run this checker\n\n"

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


def _checker_with_wait(wait_s: float, poll_s: float) -> str:
    source = _checker_source()
    for name, value in (("WAIT_S", wait_s), ("POLL_S", poll_s)):
        source, count = re.subn(rf"^{name} = \d+$", f"{name} = {value}", source, flags=re.M)
        assert count == 1, f"the checker must set {name} once, as a whole number of seconds"
    return source


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

    @property
    def body_reads(self) -> int:
        return sum(1 for call in self.calls
                   if any(re.fullmatch(r"repos/[^/]+/[^/]+/pulls/\d+", a) for a in call))


def _prepare(tmp_path: Path, body, files=("claudlobby/brief.py",), *, base_caller=NARROW_CALLER,
             fail=None, docs_run="", tests_run="", pr="7", event_body=None, role="gate",
             action="edited", wait_s=WAIT_TEST_S, harness=HARNESS):
    """Write the checker and the stand-in gh for one job's run, and return the script, its
    environment, and the call log and summary paths. A list for body serves one body per
    read, the last one repeating. The default event is an edit, which both jobs judge once.
    harness goes in front of each body served that is not empty; None leaves them as given."""
    def with_harness(text):
        return harness + text if harness and isinstance(text, str) and text else text
    body = [with_harness(b) for b in body] if isinstance(body, list) else with_harness(body)
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "checker.py"
    script.write_text(_checker_with_wait(wait_s, POLL_TEST_S), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    # A /bin/sh shim, so the stand-in runs under this interpreter on every runner
    # rather than whichever python3 the PATH below happens to reach.
    (fake_bin / "gh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n',
                                 encoding="utf-8")
    (fake_bin / "gh").chmod(0o755)
    entries = [f if isinstance(f, dict) else {"filename": f} for f in files]
    state = tmp_path / "state.json"
    served = {"bodies": body} if isinstance(body, list) else {"body": body}
    state.write_text(json.dumps({**served, "files": entries, "base_caller": base_caller,
                                 "fail": fail or {}}), encoding="utf-8")
    log = tmp_path / "gh-calls.log"
    log.write_text("", encoding="utf-8")
    summary = tmp_path / "summary.md"
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONUTF8": "1",
           "REPO": "example-org/example-repo", "PR_NUMBER": pr, "DEFAULT_BRANCH": "main",
           "DOCS_PATHS_RUN": docs_run, "TESTS_PATHS_RUN": tests_run,
           "ROLE": role, "EVENT_ACTION": action,
           "FAKE_GH_STATE": str(state), "FAKE_GH_LOG": str(log),
           "GITHUB_STEP_SUMMARY": str(summary)}
    if event_body is not None:
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": int(pr), "body": event_body}}),
                         encoding="utf-8")
        env["GITHUB_EVENT_PATH"] = str(event)
    return script, env, log, summary


def run_check(tmp_path: Path, body, files=("claudlobby/brief.py",), **opts) -> Run:
    """Run the checker as one job of the workflow does (_prepare takes the options)."""
    script, env, log, summary = _prepare(tmp_path, body, files, **opts)
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


def test_a_failing_run_says_to_keep_the_pr_a_draft_until_the_body_is_done(tmp_path):
    """A body finished after opening failed its first run, and that run emailed the
    operator (#2467). A draft is not checked, so the failure names the way round it."""
    run = run_check(tmp_path, "## Summary\nA fix.\n")
    assert run.rc == 1
    told = [line for line in run.out.splitlines() if not line.startswith("::")]
    assert any("draft" in line and "ready for review" in line for line in told), run.out


@pytest.mark.parametrize("body", ["", None], ids=["empty", "null"])
def test_an_empty_body_fails(tmp_path, body):
    run = run_check(tmp_path, body)
    assert run.rc == 1
    assert any("no \"## Rollout check\" section" in e for e in run.errors), run.out


def test_the_template_as_shipped_fails_and_filled_in_passes(tmp_path):
    if not TEMPLATE.exists():
        pytest.fail(f"missing {TEMPLATE.relative_to(REPO)}")
    template = TEMPLATE.read_text(encoding="utf-8")
    run = run_check(tmp_path / "as-shipped", template, harness=None)
    assert run.rc == 1
    assert any('"## Harness" names nothing' in e for e in run.errors), run.out
    for name in ("Observe", "Control", "When", "Who"):
        assert any(f"**{name}:**" in e for e in run.errors), (name, run.out)
    prompt = r"(\*\*(?:CI|Live|Observe|Control|When|Who):\*\*) <!--.*?-->"
    assert len(re.findall(prompt, template)) == 6, "each of the six fields carries a comment prompt"
    filled = re.sub(prompt, r"\1 a real answer", template)
    run = run_check(tmp_path / "filled", filled, harness=None)
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
    check = reusable["jobs"]["check"]
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


def test_a_draft_is_skipped_and_marking_it_ready_runs_the_check():
    """A draft cannot merge, so its events are not checked (#2467). The skip is the
    reusable job's own if:, so an adopter's pinned caller gets it and the skipped run
    keeps the rollup name. Marking the PR ready for review must stay a trigger: without
    it, a draft marked ready keeps only a skipped run, which a required check passes."""
    caller = _load(CALLER)
    on = caller.get("on", caller.get(True))
    assert "ready_for_review" in on["pull_request"]["types"]
    assert "if" not in caller["jobs"]["rollout-check"], "skip drafts in the reusable job"
    check = _load(REUSABLE)["jobs"]["check"]
    assert check.get("if") == "github.event.pull_request.draft != true"


def test_a_gate_and_a_cue_run_the_one_checker():
    """The gate has the rollup name; the cue's failure shows but cannot fail the run. The
    names are plain text: a skipped matrix job, as a draft's is, shows its name unexpanded."""
    jobs = _load(REUSABLE)["jobs"]
    assert set(jobs) == {"check", "cue"}
    gate, cue = jobs["check"], jobs["cue"]
    assert (gate["name"], cue["name"]) == ("Rollout check", "Rollout check (no wait)")
    assert (gate["env"], cue["env"]) == ({"ROLE": "gate"}, {"ROLE": "cue"})
    assert cue["continue-on-error"] is True and "continue-on-error" not in gate
    assert cue["steps"] is gate["steps"], "the cue's steps must be the gate's, by YAML alias"
    assert cue["if"] == gate["if"]
    assert cue["permissions"] == gate["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert (cue["runs-on"], cue["timeout-minutes"]) == (gate["runs-on"], gate["timeout-minutes"])
    assert "strategy" not in gate and "strategy" not in cue
    assert gate["concurrency"]["group"] != cue["concurrency"]["group"], \
        "a new event's run must replace a waiting gate, not the cue"
    assert gate["concurrency"]["cancel-in-progress"] is True
    (step,) = gate["steps"]
    assert step["env"]["EVENT_ACTION"] == "${{ github.event.action }}"


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


# --- a field ends where GitHub ends it (#2116 review) ---------------------------------

FOOTER = "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)"


@pytest.mark.parametrize("after", [FOOTER, "[who]: https://example.com/any-bot", "Thanks for reviewing."],
                         ids=["footer", "link-reference", "closing-sentence"])
def test_text_under_a_last_section_is_not_the_last_fields_answer(tmp_path, after):
    """GitHub renders **Who:** empty here: after the blank line, the text is outside its list
    item. Most bodies end with the footer, and the template puts this section last."""
    body = section(**{**FULL, "Who": None}) + "- **Who:**\n\n" + after + "\n"
    run = run_check(tmp_path, body)
    assert run.rc == 1
    assert any("**Who:**" in e for e in run.errors), run.out


@pytest.mark.parametrize("answer", ["  ```bash\n  claudlobby --json brief\n  ```", "  any bot with the fleet's credentials"],
                         ids=["indented-fence", "indented-paragraph"])
def test_an_indented_answer_after_a_blank_line_still_counts(tmp_path, answer):
    body = section(**{**FULL, "Who": None}) + "- **Who:**\n\n" + answer + "\n"
    assert run_check(tmp_path, body).rc == 0


@pytest.mark.parametrize("value", ["<!-- any bot with the fleet's credentials,\n  or operator only, and why -->",
                                   "<!-- any bot\n  -->"],
                         ids=["wrapped-prompt", "closed-on-the-next-line"])
def test_a_comment_closed_later_in_its_paragraph_hides_the_text(tmp_path, value):
    """GitHub hides an inline comment up to its -->, across the lines of one paragraph."""
    run = run_check(tmp_path, section(**{**FULL, "Who": value}))
    assert run.rc == 1
    assert any("**Who:**" in e for e in run.errors), run.out


@pytest.mark.parametrize("body", [
    section(**{**FULL, "Who": "any bot, see `<!--`\n  in the log -->"}),
    section(**{**FULL, "Who": "<!-- any bot, never closed"}),
    section(**{**FULL, "Who": "any bot <!-- not closed here"}) + "\nA later paragraph -->\n",
    section(**{**FULL, "Who": "`<!-- shown as code -->`"}),
], ids=["code-span-then-arrow", "only-an-unclosed-comment", "closed-only-after-a-blank-line",
        "code-span-holding-a-comment"])
def test_an_open_comment_github_shows_as_text_still_counts(tmp_path, body):
    assert run_check(tmp_path, body).rc == 0, "GitHub shows this <!-- as text"


def test_the_guardrails_skeleton_left_unfilled_fails(tmp_path):
    """Bots write the section from the guardrail's skeleton: gh pr create --body skips the
    template. Left unfilled, it must fail like the template does."""
    text = (GUARDRAILS / "verify-rollout.md").read_text(encoding="utf-8")
    skeleton = re.search(r"```markdown\n(## Harness\n.*?)```", text, re.S).group(1)
    assert "\n## Rollout check\n" in skeleton, "one skeleton carries both sections"
    run = run_check(tmp_path, skeleton, harness=None)
    assert run.rc == 1
    assert any('"## Harness" names nothing' in e for e in run.errors), run.out
    for name in ("Observe", "Control", "When", "Who"):
        assert any(f"**{name}:**" in e for e in run.errors), (name, run.out)


# --- what ran for the PR: "## Harness" (#2792) ------------------------------------------

def test_a_body_with_no_harness_section_fails(tmp_path):
    run = run_check(tmp_path, FILLED, harness=None)
    assert run.rc == 1
    assert run.errors == ['::error title=Rollout check::the PR body has no "## Harness" section'], run.out


@pytest.mark.parametrize("answer", [
    "- **CI:** the harness job's validate-bot-change scenarios, which source the changed script",
    "- **Live:** `harness/rehearse-held-push.sh` in a scratch root: PASS, 3 of 3 checks",
    "N/A: a typo in a comment, which no scenario, instrument or live test reads",
], ids=["a-what-ci-ran", "b-a-live-run-and-its-result", "c-why-none-applies"])
def test_each_harness_answer_alone_passes(tmp_path, answer):
    run = run_check(tmp_path, FILLED, harness=f"## Harness\n{answer}\n\n")
    assert run.rc == 0, run.out
    assert run.errors == []


@pytest.mark.parametrize("lines", [
    "",
    "- **CI:**\n- **Live:**\n",
    "- **CI:** <!-- what CI ran -->\n- **Live:** <!-- each live test -->\n",
    "- **CI:** N/A\n- **Live:** TBD\n",
    "N/A\n",
    "**N/A:** none\n",
    "The harness job ran.\n",
], ids=["empty", "labels-left-empty", "prompts-left-in", "vacuous-answers", "n-a-without-why",
        "n-a-with-a-vacuous-why", "prose-without-a-label"])
def test_a_harness_section_that_names_nothing_fails(tmp_path, lines):
    run = run_check(tmp_path, FILLED, harness="## Harness\n" + lines + "\n")
    assert run.rc == 1
    assert any('"## Harness" names nothing' in e for e in run.errors), run.out


@pytest.mark.parametrize("wrapped", ["```\n## Harness\n- **CI:** x\n```\n",
                                     "<!--\n## Harness\n- **CI:** x\n-->\n"],
                         ids=["fenced", "commented"])
def test_a_harness_heading_github_does_not_show_does_not_count(tmp_path, wrapped):
    run = run_check(tmp_path, wrapped + "\n" + FILLED, harness=None)
    assert run.rc == 1
    assert any('no "## Harness" section' in e for e in run.errors), run.out


def test_the_harness_section_ends_at_the_next_heading_and_may_follow_the_rollout_check(tmp_path):
    split = "## Harness\n\n## Notes\n- **CI:** the harness job\n\n" + FILLED
    run = run_check(tmp_path / "split", split, harness=None)
    assert run.rc == 1
    assert any('"## Harness" names nothing' in e for e in run.errors), run.out
    after = FILLED + "\n## Harness\n- **Live:** the held-push rehearsal: PASS\n"
    assert run_check(tmp_path / "after", after, harness=None).rc == 0


def test_a_live_run_given_as_a_fenced_command_passes(tmp_path):
    harness = "## Harness\n- **Live:**\n  ```bash\n  harness/rehearse-held-push.sh\n  ```\n\n"
    assert run_check(tmp_path, FILLED, harness=harness).rc == 0


def test_both_sections_are_judged_in_one_run(tmp_path):
    """One run names every missing section, so one edit can fix the body."""
    run = run_check(tmp_path, "## Summary\nA fix.\n", harness=None)
    assert run.rc == 1
    assert any('no "## Harness" section' in e for e in run.errors), run.out
    assert any('no "## Rollout check" section' in e for e in run.errors), run.out
    told = " ".join(line for line in run.out.splitlines() if not line.startswith("::"))
    assert "## Harness" in told and "## Rollout check" in told, run.out


def test_a_failed_lookup_keeps_the_harness_finding(tmp_path):
    run = run_check(tmp_path, "## Rollout check\nN/A: docs-only\n", files=["documentation/a.md"],
                    fail={"contents": "502"}, harness=None)
    assert run.rc == 1
    assert any('no "## Harness" section' in e for e in run.errors), run.out
    assert any("could not read" in e for e in run.errors), run.out


# --- the gate waits for an edit when a PR is offered (#2797) ------------------------------

NO_SECTION = "## Summary\nA fix.\n"
WAITED = re.compile(r"^Rollout check: the body was incomplete when the PR was offered and "
                    r"passed after \d+ s\.$", re.M)


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review"])
def test_the_gate_passes_an_edit_made_while_it_waits(tmp_path, action):
    """Authors write the section once the cue has failed; an edit inside the wait passes."""
    run = run_check(tmp_path, [NO_SECTION, NO_SECTION, FILLED], action=action)
    assert run.rc == 0, run.out + run.err
    assert run.body_reads == 3
    assert WAITED.search(run.out), run.out
    assert run.out.splitlines()[-1] == SUCCESS_LINE
    assert run.errors == [], "a body that passed in the wait leaves no error annotation"


def test_the_gate_fails_at_its_deadline_when_no_edit_comes(tmp_path):
    run = run_check(tmp_path, [NO_SECTION], action="opened", wait_s=0.5)
    assert run.rc == 1
    assert run.body_reads >= 2, "the gate re-reads the body before it fails"
    assert any('no "## Rollout check" section' in e for e in run.errors), run.out
    assert not WAITED.search(run.out)


def test_the_gate_waits_for_the_harness_section_too(tmp_path):
    """The wait re-judges the whole body, so an edit that adds "## Harness" passes the gate,
    and a body still without it fails the gate at its deadline."""
    run = run_check(tmp_path / "edited", [FILLED, FILLED, HARNESS + FILLED], action="opened",
                    harness=None)
    assert run.rc == 0, run.out + run.err
    assert run.body_reads == 3
    assert WAITED.search(run.out), run.out
    late = run_check(tmp_path / "late", [FILLED], action="opened", wait_s=0.5, harness=None)
    assert late.rc == 1
    assert late.body_reads >= 2, "the gate re-reads the body before it fails"
    assert any('no "## Harness" section' in e for e in late.errors), late.out


@pytest.mark.parametrize("action", ["edited", "synchronize"])
def test_an_edit_or_a_push_is_judged_once(tmp_path, action):
    """Each later edit or push has a run of its own."""
    run = run_check(tmp_path, [NO_SECTION, FILLED], action=action)
    assert run.rc == 1
    assert run.body_reads == 1


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review", "edited"])
def test_the_cue_judges_once_and_fails_at_once(tmp_path, action):
    """Its failure is what prompts the edit, so it never waits."""
    run = run_check(tmp_path, [NO_SECTION, FILLED], role="cue", action=action)
    assert run.rc == 1
    assert run.body_reads == 1


def test_the_cue_fails_with_the_text_the_check_always_gave(tmp_path):
    cue = run_check(tmp_path / "cue", NO_SECTION, role="cue", action="opened")
    once = run_check(tmp_path / "once", NO_SECTION, role="gate", action="edited")
    assert cue.rc == once.rc == 1
    assert cue.out == once.out


def test_the_gate_says_at_once_why_it_waits(tmp_path):
    """Its stdout is a pipe, so the line is flushed: a pending gate's log says why it waits."""
    script, env, _, _ = _prepare(tmp_path, [NO_SECTION], action="opened", wait_s=8)
    start = time.monotonic()
    proc = subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    try:
        first = proc.stdout.readline()
        elapsed = time.monotonic() - start
    finally:
        proc.kill()
        proc.communicate(timeout=60)
    assert first.startswith("The body fails as offered."), first
    assert elapsed < 4, f"the wait line came {elapsed:.1f} s in, when the gate ended"


def test_a_body_that_passes_as_offered_is_not_waited_on(tmp_path):
    run = run_check(tmp_path, [FILLED, NO_SECTION], action="opened")
    assert run.rc == 0
    assert run.body_reads == 1
    assert not WAITED.search(run.out)
    assert SUCCESS_LINE in run.out.splitlines()


def test_a_failed_read_during_the_wait_fails_at_once(tmp_path):
    run = run_check(tmp_path, [NO_SECTION, {"fail": "502"}, FILLED], action="opened")
    assert run.rc == 1
    assert run.body_reads == 2
    assert any("could not read the PR body" in e for e in run.errors), run.out


def test_the_shipped_wait_is_180_s_read_every_15_s():
    """Constants, not inputs: nothing turns the wait off. The success line is the one that
    rollout checks search job logs for, so it stays as it was."""
    source = _checker_source()
    assert re.search(r"^WAIT_S = 180$", source, re.M)
    assert re.search(r"^POLL_S = 15$", source, re.M)
    assert re.search(r'^OFFERED = \{"opened", "reopened", "ready_for_review"\}$', source, re.M)
    assert f'print("{SUCCESS_LINE}")' in source
    guide = (GUARDRAILS / "verify-rollout.md").read_text(encoding="utf-8")
    assert "180 s" in guide and "`rollout-check / Rollout check (no wait)`" in guide
