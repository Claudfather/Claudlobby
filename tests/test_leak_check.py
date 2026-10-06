"""leak-check (#2042): the script behind .github/actions/leak-check, driven as CI runs it.

Every planted value is FAKE and built from fragments at run time, so this file
never holds a string the check itself would flag. The private-list term is
INVENTED; no real term appears here or in any output this file produces."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "actions" / "leak-check" / "leak_check.py"
spec = importlib.util.spec_from_file_location("leak_check", SCRIPT)
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)

INVENTED = "zqxv[ ._-]?wombat"  # an invented private-list pattern
INVENTED_TEXT = "notes for Zqxv " + "Wombat, the account"  # holds it

# One planted positive per layer-1 class, each FAKE, each assembled here.
PLANTED = {
    "telegram-chat-id": "GROUP_CHAT_ID=" + "-100" + "4827" + "561093",
    "telegram-bot-token": "token "
    + "58"
    + "3920174"
    + ":"
    + "AAF"
    + "k3Rq9" * 6
    + "zX",
    "github-token": "pat " + "gh" + "p_" + "Rq3k9ZxT" * 5,
    "api-token": "key " + "sk-" + "ant-" + "api03" + "Tq7Lm2" * 5,
    "email": "mail " + "jordan.m" + "@" + "fastmail" + ".com",
    "home-path": "cd " + "/home" + "/jordan" + "/code",
    "ip-address": "host " + "100" + ".101." + "54.7",
    "uuid": "id " + "3f2a9c1e" + "-7b4d-4e8f-" + "9a1c-" + "5d6e7f8091ab",
}

# Must pass: the placeholders CLAUDE.md prescribes, and ordinary text.
PLACEHOLDERS = [
    "chat_id: " + "-100" + "1234567890",
    "TELEGRAM_TOKEN=" + "8888888:" + "A" * 20,
    "GITHUB_PAT=" + "gh" + "p_" + "x" * 20,
    "NOTION_TOKEN=" + "ntn_" + "X" * 20,
    "contact " + "someone" + "@" + "example.com" + " or t@example.invalid",
    "git clone " + "git" + "@" + "github.com:org/repo.git",
    "cd " + "/home" + "/user/project && ls /Users/you",
    "bind " + "127.0.0.1" + " and 0.0.0.0, see 192.0.2.10 and 203.0.113.5",
    "id " + "00000000-0000-0000-0000-000000000000",
    "run " + "12345678-1234-5678-1234-567812345678",
    "version 2.1.284 and pytest -m 'not quarantine'",
    "NOTION_TOKEN=" + "ntn_your_integration_token and SLACK=" + "xoxp-your-token",
    "a string holding " + "\\n@pytest.mark.skipif(True)",
    "Co-Authored-By: Claude <" + "noreply" + "@anthropic.com>",
    "mail " + "someone" + "@mailhost.com",
    "ls " + "/Users/x/Library /Users" + "/Shared/data /home/user./x /Users" + "/YOUR_USERNAME/.claude /home/alice",
    "dns " + "8.8.8.8",
    "sa " + "reporter" + "@your-project.iam.gserviceaccount.com",
    "fixture " + "5f0c2d1e" + "-0000-4000-8000-" + "000000000001",
    "REQUEST = " + "00112233" + "-4455-6677-8899-" + "aabbccddeeff",
    "example " + "550e8400" + "-e29b-41d4-a716-" + "446655440000" + " and urn:uuid:" + "f81d4fae" + "-7dec-11d0-a765-" + "00a0c91e6bf6",
    "unit " + "wireplumber" + "@" + "main.service" + " and getty" + "@" + "tty1.service",
]


def diff_of(*lines: str, path: str = "docs/notes.md") -> str:
    body = "".join(f"+{ln}\n" for ln in lines)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,{len(lines)} @@\n{body}"


def run(tmp_path, diff: str, terms: str | None = INVENTED, allow: str = "", extra=(), summary=None):
    d = tmp_path / "change.diff"
    d.write_text(diff)
    a = tmp_path / "allow.txt"
    a.write_text(allow)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("LEAK_CHECK_TERMS", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY")
    }
    if terms is not None:
        env["LEAK_CHECK_TERMS"] = terms
    if summary is not None:
        env["GITHUB_STEP_SUMMARY"] = str(summary)
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--diff", str(d), "--allow", str(a), *extra],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return p.returncode, p.stdout, p.stderr


@pytest.mark.parametrize("cls", list(PLANTED))
def test_each_layer1_class_fires_on_its_planted_positive(tmp_path, cls):
    rc, out, err = run(tmp_path, diff_of("ok line", PLANTED[cls]))
    assert rc == 1, (out, err)
    assert f"docs/notes.md:2: {cls}" in out


def test_every_class_is_planted():
    assert set(PLANTED) == set(lc.CLASSES)


def test_the_private_list_fires_on_an_invented_term(tmp_path):
    rc, out, err = run(tmp_path, diff_of("ok line", INVENTED_TEXT))
    assert rc == 1 and "docs/notes.md:2: private term #1" in out


def test_a_failing_check_says_what_to_do_and_a_passing_one_says_nothing(tmp_path):
    rc, out, err = run(tmp_path, diff_of("ok line", INVENTED_TEXT, PLANTED["email"]))
    assert rc == 1
    advice = [ln for ln in out.splitlines() if ln.startswith("leak-check: replace each value")]
    assert len(advice) == 1, out
    assert "allow.txt as `<path glob> <class> <reason>`" in advice[0]
    assert "A private term cannot be allowed" in advice[0]
    assert "ask a maintainer, who can see the list" in advice[0]
    rc, out, err = run(tmp_path, diff_of(*PLACEHOLDERS))
    assert rc == 0 and "replace each value" not in out


def test_placeholders_and_ordinary_text_pass(tmp_path):
    rc, out, err = run(tmp_path, diff_of(*PLACEHOLDERS))
    assert rc == 0, out
    assert "0 hit(s)" in out


def test_no_matched_text_and_no_pattern_ever_reaches_the_output(tmp_path):
    """Actions logs on a public repository are world-readable: a hit is a
    location and a label, never what matched."""
    rc, out, err = run(
        tmp_path,
        diff_of(INVENTED_TEXT, *PLANTED.values()),
        terms="# a comment\n" + INVENTED + "\nquillon[ ._-]?media\n",
    )
    assert rc == 1
    both = out + err
    for secret in (
        "wombat",
        "zqxv",
        "quillon",
        *(v.split(" ", 1)[-1].split("=", 1)[-1] for v in PLANTED.values()),
    ):
        assert secret.lower() not in both.lower(), secret
    assert "private term #1" in out


def test_no_list_is_loud_and_fails_closed(tmp_path):
    for terms in (None, "", "# only a comment\n\n"):
        rc, out, err = run(tmp_path, diff_of("ok line"), terms=terms)
        assert rc == 2 and "NOT ARMED" in err and "LEAK_CHECK_TERMS" in err, (
            terms,
            err,
        )
        # Its reader is a contributor, who cannot set a secret and did not cause this.
        assert "A maintainer must set" in err and "nothing in this change caused this" in err
        assert "--terms-optional" not in err
    rc, out, err = run(
        tmp_path, diff_of("ok line"), terms=None, extra=["--terms-optional"]
    )
    assert rc == 0 and "layer 1 only" in err


def test_unarmed_still_reports_layer1_hits_before_it_fails(tmp_path):
    """No list: layer 1 runs and its hits are printed above the NOT ARMED
    failure, so a contributor still sees the leaks they can fix."""
    d = tmp_path / "change.diff"
    d.write_text(diff_of("ok line", PLANTED["email"]))
    env = {k: v for k, v in os.environ.items()
           if k not in ("LEAK_CHECK_TERMS", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY")}
    p = subprocess.run([sys.executable, str(SCRIPT), "--diff", str(d), "--allow", str(tmp_path / "none")],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, timeout=60)
    assert p.returncode == 2, p.stdout
    lines = p.stdout.splitlines()
    hit = lines.index("docs/notes.md:2: email")
    failure = next(i for i, ln in enumerate(lines) if "NOT ARMED" in ln)
    assert hit < failure, p.stdout
    assert any(ln.startswith("leak-check: replace each value") for ln in lines[:failure])


def test_an_address_at_a_target_domain_is_still_an_address(tmp_path):
    # `.target` is a systemd unit type, and also a live top-level domain.
    rc, out, err = run(tmp_path, diff_of("mail " + "ops" + "@" + "shop" + ".target"))
    assert rc == 1 and "docs/notes.md:1: email" in out, (out, err)


@pytest.mark.parametrize("line", ["broken[", "(?P=quillon)", "a|", ".*", "\\b"])
def test_a_broken_list_line_fails_closed_naming_its_index_only(tmp_path, line):
    rc, out, err = run(tmp_path, diff_of("ok line"), terms=INVENTED + "\n" + line)
    assert rc == 2 and "pattern #2" in err
    assert "quillon" not in err and "broken[" not in err


def test_the_allowlist_is_the_visible_escape(tmp_path):
    diff = diff_of(PLANTED["uuid"], path="tests/fixtures/ids.json")
    assert run(tmp_path, diff)[0] == 1
    assert run(tmp_path, diff, allow="tests/fixtures/* uuid synthetic ids\n")[0] == 0
    # the entry is scoped to its class and its paths
    assert (
        run(
            tmp_path,
            diff_of(PLANTED["email"], path="tests/fixtures/ids.json"),
            allow="tests/fixtures/* uuid synthetic ids\n",
        )[0]
        == 1
    )
    assert (
        run(
            tmp_path,
            diff_of(PLANTED["uuid"], path="docs/ids.md"),
            allow="tests/fixtures/* uuid synthetic ids\n",
        )[0]
        == 1
    )


def test_an_allowlist_entry_must_name_a_class_and_a_reason(tmp_path):
    for bad in ("tests/* uuid\n", "tests/* everything because\n"):
        rc, out, err = run(tmp_path, diff_of("ok line"), allow=bad)
        assert rc == 2 and "allowlist line 1" in err


def test_the_private_list_has_no_allowlist(tmp_path):
    rc, out, err = run(
        tmp_path,
        diff_of(INVENTED_TEXT, path="tests/x.md"),
        allow="tests/* uuid synthetic ids\n",
    )
    assert rc == 1 and "private term #1" in out


def test_a_path_is_content_too(tmp_path):
    rc, out, err = run(tmp_path, diff_of("ok line", path="notes/zqxv-wombat.md"))
    assert rc == 1 and "notes/zqxv-wombat.md (the path): private term #1" in out


def test_only_added_lines_count(tmp_path):
    diff = (
        "diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n@@ -3,1 +3,1 @@\n"
        f"-{PLANTED['email']}\n+a cleaned line\n"
    )
    rc, out, err = run(tmp_path, diff)
    assert rc == 0, out


def test_line_numbers_follow_the_hunks(tmp_path):
    diff = (
        "diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n@@ -10,0 +11,2 @@\n+ok\n"
        f"+{PLANTED['home-path']}\n@@ -40,0 +42,1 @@\n+{PLANTED['uuid']}\n"
    )
    rc, out, err = run(tmp_path, diff)
    assert "a.md:12: home-path" in out and "a.md:42: uuid" in out


def test_in_actions_each_hit_is_an_annotation_without_its_text(tmp_path):
    d = tmp_path / "change.diff"
    d.write_text(diff_of(INVENTED_TEXT, PLANTED["email"]))
    summary = tmp_path / "summary.md"
    env = dict(
        os.environ,
        LEAK_CHECK_TERMS=INVENTED,
        GITHUB_ACTIONS="true",
        GITHUB_STEP_SUMMARY=str(summary),
    )
    p = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--diff",
            str(d),
            "--allow",
            str(tmp_path / "none"),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p.returncode == 1
    assert "::error file=docs/notes.md,line=1::leak-check: private term #1" in p.stdout
    assert "::error file=docs/notes.md,line=2::leak-check: email" in p.stdout
    text = p.stdout + p.stderr + summary.read_text()
    assert "wombat" not in text.lower() and "fastmail" not in text


def test_git_mode_reads_the_change_as_data_and_its_allowlist_from_base(tmp_path):
    """The allowlist is read from BASE: an entry that arrives with the change
    exempts nothing until it is merged, and one already at the base does."""
    g, env = _scratch_repo(tmp_path)
    (tmp_path / "a.md").write_text(PLANTED["email"] + "\n")  # already committed: never read
    base = _commit(g, env, "base")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "leak-check-allow.txt").write_text("*.json uuid synthetic fixture\n")
    merged_first = _commit(g, env, "the entry, merged first")
    (tmp_path / "b.json").write_text(PLANTED["uuid"] + "\n")
    _commit(g, env, "change")
    p = _check_git(tmp_path, base, env)  # the entry arrives with the change
    assert p.returncode == 1 and "b.json:1: uuid" in p.stdout and "a.md" not in p.stdout, p.stdout + p.stderr
    assert "an entry takes effect only once merged" in p.stdout
    p = _check_git(tmp_path, merged_first, env)  # the entry is at the base
    assert p.returncode == 0, p.stdout + p.stderr


def test_an_added_line_shaped_like_a_file_header_does_not_steer_the_parser(tmp_path):
    """An added `++ /dev/null` shows in the diff as `+++ /dev/null`. Read by
    its hunk's counts it is content, and the lines after it are still read."""
    diff = diff_of("ok", "++ /dev/null", PLANTED["email"], INVENTED_TEXT)
    rc, out, err = run(tmp_path, diff)
    assert rc == 1, out
    assert "docs/notes.md:3: email" in out and "docs/notes.md:4: private term #1" in out


def test_an_added_line_cannot_re_aim_the_allowlist(tmp_path):
    allow = "library/skills/network/SKILL.md email a sender address\n"
    diff = diff_of("++ b/library/skills/network/SKILL.md", PLANTED["email"], path="docs/other.md")
    rc, out, err = run(tmp_path, diff, allow=allow)
    assert rc == 1 and "docs/other.md:2: email" in out, out


def test_a_path_with_a_space_is_reported_and_allowlisted_as_itself(tmp_path):
    path = "docs/x y.md"
    diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\t\n+++ b/{path}\t\n@@ -0,0 +1,1 @@\n"
            f"+{PLANTED['uuid']}\n")
    rc, out, err = run(tmp_path, diff)
    assert rc == 1 and "docs/x y.md:1: uuid" in out and "\t" not in out
    assert run(tmp_path, diff, allow="docs/x?y.md uuid synthetic\n")[0] == 0


def _scratch_repo(tmp_path):
    g = ["git", "-C", str(tmp_path)]
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run([*g, "init", "-q", "-b", "main"], check=True, env=env)
    return g, env


def _commit(g, env, msg):
    subprocess.run([*g, "add", "-A"], check=True, env=env)
    subprocess.run([*g, "commit", "-qm", msg], check=True, env=env)
    return subprocess.run([*g, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def _check_git(tmp_path, base, env, extra=()):
    e = {k: v for k, v in env.items() if k != "GITHUB_ACTIONS"}
    e["LEAK_CHECK_TERMS"] = INVENTED
    return subprocess.run([sys.executable, str(SCRIPT), "--git", base, "HEAD", *extra],
                          capture_output=True, text=True, env=e, cwd=tmp_path, timeout=60)


def test_a_pure_rename_and_a_binary_file_have_their_paths_read(tmp_path):
    """Neither produces a `+++` header, so only the diff's own name list sees
    their new names."""
    g, env = _scratch_repo(tmp_path)
    (tmp_path / "notes.md").write_text("plain text that stays the same\n" * 5)
    base = _commit(g, env, "base")
    (tmp_path / "notes.md").rename(tmp_path / "zqxv-wombat.md")  # a pure rename
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "zqxv_wombat.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02binary")
    _commit(g, env, "rename and binary")
    p = _check_git(tmp_path, base, env)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "zqxv-wombat.md (the path): private term #1" in p.stdout
    assert "img/zqxv_wombat.png (the path): private term #1" in p.stdout


def test_a_change_that_edits_the_allowlist_says_so(tmp_path):
    g, env = _scratch_repo(tmp_path)
    (tmp_path / "a.md").write_text("plain\n")
    base = _commit(g, env, "base")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "leak-check-allow.txt").write_text("*.json uuid synthetic\n")
    _commit(g, env, "allow")
    p = _check_git(tmp_path, base, env)
    assert p.returncode == 0 and "edits the allowlist" in p.stdout
    assert "an entry takes effect only once merged" in p.stdout


def test_annotation_values_are_escaped(tmp_path):
    d = tmp_path / "change.diff"
    d.write_text(diff_of(PLANTED["uuid"], path="docs/a,b:c%d.md"))
    env = dict(os.environ, LEAK_CHECK_TERMS=INVENTED, GITHUB_ACTIONS="true")
    env.pop("GITHUB_STEP_SUMMARY", None)
    p = subprocess.run([sys.executable, str(SCRIPT), "--diff", str(d), "--allow", str(tmp_path / "none")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert "::error file=docs/a%2Cb%3Ac%25d.md,line=1::leak-check: uuid" in p.stdout, p.stdout


def test_the_workflow_keeps_its_own_safety_properties():
    """What this check's safety rests on, pinned: the base is checked out
    without credentials, the token reads only, and no event value is
    expanded into a shell line."""
    import yaml

    wf = yaml.safe_load((REPO / ".github" / "workflows" / "leak-check.yml").read_text())
    on = wf.get("on", wf.get(True))
    assert set(on) == {"pull_request_target"}
    assert wf["permissions"] == {"contents": "read"}
    steps = wf["jobs"]["leak-check"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkout["with"]["persist-credentials"] is False
    action = yaml.safe_load((SCRIPT.parent / "action.yml").read_text())
    for step in steps + action["runs"]["steps"]:
        assert "${{" not in str(step.get("run", "")), step


def test_nothing_from_the_pull_request_is_checked_out_or_run():
    """pull_request_target hands the job the base repository's secrets, so the
    job runs nothing of the pull request's: one checkout, of the base, the check
    taken from that checkout, and the head fetched as git objects to be diffed."""
    import yaml

    wf = yaml.safe_load((REPO / ".github" / "workflows" / "leak-check.yml").read_text())
    job = wf["jobs"]["leak-check"]
    assert "permissions" not in job  # the workflow's `contents: read` is the job's
    steps = job["steps"]
    uses = [str(s.get("uses", "")) for s in steps]
    checkouts = [u for u in uses if u.startswith("actions/checkout")]
    assert len(checkouts) == 1 and re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkouts[0]), checkouts
    assert uses.index("./.github/actions/leak-check") > uses.index(checkouts[0])
    assert not [u for u in uses if u.startswith(("actions/cache", "actions/upload-artifact"))]
    action = yaml.safe_load((SCRIPT.parent / "action.yml").read_text())
    runs = [str(s.get("run", "")) for s in steps + action["runs"]["steps"]]
    verbs = r"\bgit\s+(?:checkout|switch|worktree|reset|merge|pull|apply|am|cherry-pick|submodule)\b"
    assert not [r for r in runs if re.search(verbs, r)], runs
    assert [r for r in runs if "refs/pull/${PR}/head:refs/leak-check/head" in r]
    assert [r for r in runs if r.startswith('python3 "$GITHUB_ACTION_PATH/leak_check.py"')]


def test_no_diff_driver_runs_even_when_git_configuration_names_one(tmp_path):
    """The change is read with --no-ext-diff and --no-textconv: an external diff
    or a textconv driver, selected by the change's own .gitattributes and
    defined in the environment's git configuration, never runs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    g, env = _scratch_repo(repo)
    (repo / "a.md").write_text("base\n")
    base = _commit(g, env, "base")
    marker = tmp_path / "driver-ran"
    driver = tmp_path / "driver.sh"
    driver.write_text(f"#!/bin/sh\ntouch {marker}\ncat \"$1\" 2>/dev/null\n")
    driver.chmod(0o755)
    (repo / ".gitattributes").write_text("* diff=planted\n")
    (repo / "b.md").write_text("ok\n" + INVENTED_TEXT + "\n")
    _commit(g, env, "change")
    env = dict(env, GIT_EXTERNAL_DIFF=str(driver), GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="diff.planted.textconv", GIT_CONFIG_VALUE_0=str(driver))
    p = _check_git(repo, base, env)
    assert not marker.exists(), "a diff driver ran while the change was read"
    assert p.returncode == 1 and "b.md:2: private term #1" in p.stdout, (p.stdout, p.stderr)


def test_the_action_never_traces_or_echoes_the_list():
    """The composite action hands the list to python through the environment
    only: no `set -x`, and the secret is never expanded into a shell line."""
    action = (SCRIPT.parent / "action.yml").read_text()
    assert "set -x" not in action and "xtrace" not in action
    assert "LEAK_CHECK_TERMS: ${{ inputs.private-terms }}" in action
    run_lines = [
        ln
        for ln in action.splitlines()
        if "private-terms" in ln and "LEAK_CHECK_TERMS" not in ln
    ]
    assert all("run:" not in ln and "echo" not in ln for ln in run_lines)


def _git_env(tmp_path):
    e = {k: v for k, v in os.environ.items() if k not in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY")}
    e.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return e


@pytest.mark.parametrize("mode", ["diff", "git"])
def test_a_carriage_return_inside_an_added_line_does_not_split_it(tmp_path, mode):
    """Read in text mode, a lone CR ends a line, so an added line holding
    `\\r+++ /dev/null` would read as a file header and hide the line after it."""
    crafted = "ok\r" + "+++ /dev/null"
    if mode == "diff":
        rc, out, err = run(tmp_path, diff_of(crafted, PLANTED["email"]))
        assert rc == 1 and "docs/notes.md:2: email" in out, (out, err)
        return
    g, env = _scratch_repo(tmp_path)
    (tmp_path / "a.md").write_text("plain\n")
    base = _commit(g, env, "base")
    (tmp_path / "x.md").write_bytes((crafted + "\n" + PLANTED["email"] + "\n").encode())
    _commit(g, env, "change")
    p = _check_git(tmp_path, base, env)
    assert p.returncode == 1 and "x.md:2: email" in p.stdout, p.stdout + p.stderr


def test_an_added_line_outside_any_hunk_fails_closed(tmp_path):
    diff = diff_of("ok") + f"+{PLANTED['email']}\n"
    rc, out, err = run(tmp_path, diff)
    assert rc == 2 and "outside any hunk" in err, (out, err)


def test_a_diff_that_ends_inside_a_hunk_fails_closed(tmp_path):
    diff = "diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n@@ -0,0 +1,3 @@\n+ok\n"
    rc, out, err = run(tmp_path, diff)
    assert rc == 2 and "inside a hunk" in err, (out, err)


def test_a_binary_file_is_checked_for_private_terms_and_says_how(tmp_path):
    """git shows a file with a NUL byte as binary and no line of it, so its
    bytes are read from the head commit and checked against the private list."""
    g, env = _scratch_repo(tmp_path)
    (tmp_path / "a.md").write_text("plain\n")
    base = _commit(g, env, "base")
    (tmp_path / "doc.bin").write_bytes(b"\x00\x01" + INVENTED_TEXT.encode() + b"\n")
    (tmp_path / "clean.bin").write_bytes(b"\x00\x01 nothing to see\n")
    _commit(g, env, "binaries")
    p = _check_git(tmp_path, base, env)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "doc.bin (binary content): private term #1" in p.stdout
    assert "clean.bin" not in p.stdout
    assert "2 binary file(s)" in p.stderr and "private list only" in p.stderr
    rc, out, err = run(tmp_path, "diff --git a/d.bin b/d.bin\nnew file mode 100644\n"
                       "index 0000000..1111111\nBinary files /dev/null and b/d.bin differ\n")
    assert rc == 0 and "1 binary file(s)" in err and "not checked" in err, (out, err)


def test_a_deleted_file_s_name_is_not_a_hit_and_an_empty_new_file_s_name_is(tmp_path):
    deleted = ("diff --git a/notes/zqxv-wombat.md b/notes/zqxv-wombat.md\n"
               "deleted file mode 100644\nindex 1111111..0000000\n"
               "--- a/notes/zqxv-wombat.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n")
    rc, out, err = run(tmp_path, deleted)
    assert rc == 0, (out, err)
    empty = ("diff --git a/notes/zqxv-wombat.md b/notes/zqxv-wombat.md\n"
             "new file mode 100644\nindex 0000000..e69de29\n")
    rc, out, err = run(tmp_path, empty)
    assert rc == 1 and "notes/zqxv-wombat.md (the path): private term #1" in out, (out, err)
    (tmp_path / "repo").mkdir()
    g, env = _scratch_repo(tmp_path / "repo")
    (tmp_path / "repo" / "zqxv-wombat.md").write_text("old\n")
    base = _commit(g, env, "base")
    (tmp_path / "repo" / "zqxv-wombat.md").unlink()
    _commit(g, env, "delete it")
    p = _check_git(tmp_path / "repo", base, env)
    assert p.returncode == 0, p.stdout + p.stderr


def test_a_path_cannot_start_a_workflow_command(tmp_path):
    for path in ("::error::x.md", " ##[error]y.md"):
        rc, out, err = run(tmp_path, diff_of(PLANTED["uuid"], path=path))
        assert rc == 1, (out, err)
        starts = [ln for ln in out.splitlines() if ln.lstrip().startswith(("::", "##["))]
        assert not starts, starts


def test_all_mode_reads_text_and_binary_files(tmp_path):
    g, env = _scratch_repo(tmp_path)
    (tmp_path / "t.md").write_bytes(b"caf\xe9 " + INVENTED_TEXT.encode() + b"\n")  # not UTF-8
    (tmp_path / "b.bin").write_bytes(b"\x00" + INVENTED_TEXT.encode())
    _commit(g, env, "tip")
    e = _git_env(tmp_path)
    e["LEAK_CHECK_TERMS"] = INVENTED
    p = subprocess.run([sys.executable, str(SCRIPT), "--all", "--allow", "none"],
                       capture_output=True, text=True, env=e, cwd=tmp_path, timeout=60)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "t.md:1: private term #1" in p.stdout
    assert "b.bin (binary content): private term #1" in p.stdout


# --- who gets the private list, and the files' own wiring (#2048 review) ----------

OFF = ["--private-layer", "off"]
SAME_REPO = ("github.event.pull_request.head.repo.id == github.event.pull_request.base.repo.id"
             " && github.actor != 'dependabot[bot]'")


def test_a_write_access_run_arms_layer_2_and_fails_closed_without_its_list(tmp_path):
    on = ["--private-layer", "on"]
    rc, out, err = run(tmp_path, diff_of("ok line", INVENTED_TEXT), extra=on)
    assert rc == 1 and "docs/notes.md:2: private term #1" in out, (out, err)
    assert lc.PRIVATE_OFF not in out
    rc, out, err = run(tmp_path, diff_of("ok line"), terms=None, extra=on)
    assert rc == 2 and "NOT ARMED" in err, (out, err)


@pytest.mark.parametrize("terms", [None, "", INVENTED, "broken["])
def test_an_outside_run_reads_no_list_and_never_fails_unarmed(tmp_path, terms):
    """The list absent, empty, handed in by a wiring slip, or broken: an outside
    run never reads it, so a private term passes and a broken line is never
    compiled, while a generic hit still fails. One fixed line says layer 2 did
    not run, in the log and in the step summary."""
    summary = tmp_path / "summary.md"
    rc, out, err = run(tmp_path, diff_of("ok line", INVENTED_TEXT), terms=terms, extra=OFF, summary=summary)
    assert rc == 0, (out, err)
    assert "private term" not in out and "NOT ARMED" not in err and "pattern #" not in err
    assert out.splitlines().count("leak-check: " + lc.PRIVATE_OFF) == 1, out
    assert summary.read_text().count(lc.PRIVATE_OFF) == 1
    rc, out, err = run(tmp_path, diff_of("ok line", PLANTED["email"]), terms=terms, extra=OFF)
    assert rc == 1 and "docs/notes.md:2: email" in out, (out, err)


def test_a_warning_from_compiling_the_list_is_never_printed(tmp_path):
    """A warning can quote a pattern (Python 3.10 and older do for a flag inside
    one), so none is printed. This pattern draws a FutureWarning here."""
    re.purge()
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        re.compile("(?:[[]quillon)", re.IGNORECASE)
    assert seen, "this Python does not warn on the pattern: pick one that does"
    rc, out, err = run(tmp_path, diff_of("ok line"), terms=INVENTED + "\n[[]quillon\n")
    assert rc == 0 and "Warning" not in err and "quillon" not in out + err, (out, err)


def test_a_crafted_binary_file_name_runs_nothing(tmp_path):
    """A binary file's bytes are read by naming the file to git, so the name
    must reach git as an argument, never through a shell."""
    g, env = _scratch_repo(tmp_path)
    (tmp_path / "a.md").write_text("plain\n")
    base = _commit(g, env, "base")
    for name in ("x$(touch ran1).bin", "x`touch ran2`.bin", "x;touch ran3;.bin"):
        (tmp_path / name).write_bytes(b"\x00\x01 binary\n")
    _commit(g, env, "crafted names")
    p = _check_git(tmp_path, base, env)
    assert p.returncode == 0 and "3 binary file(s)" in p.stderr, p.stdout + p.stderr
    assert not [f for f in ("ran1", "ran2", "ran3") if (tmp_path / f).exists()]


def _without_descriptions(node):
    if isinstance(node, dict):
        return {k: _without_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_without_descriptions(v) for v in node]
    return node


def test_the_workflow_and_the_action_keep_exactly_their_reviewed_shape():
    """Every key and value of both files but their prose. Any change to either
    file fails here until this test changes with it, in the same review. The
    two copies of the private-layer test are one string."""
    import yaml

    wf = yaml.safe_load((REPO / ".github" / "workflows" / "leak-check.yml").read_text())
    assert _without_descriptions(wf) == {
        "name": "leak-check",
        True: {"pull_request_target": {"types": ["opened", "synchronize", "reopened"]}},  # YAML 1.1: `on` is true
        "permissions": {"contents": "read"},
        "concurrency": {"group": "leak-check-${{ github.event.pull_request.number }}", "cancel-in-progress": True},
        "jobs": {"leak-check": {
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 10,
            "steps": [
                {"uses": "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
                 "with": {"ref": "${{ github.event.pull_request.base.sha }}", "fetch-depth": 0,
                          "persist-credentials": False}},
                {"uses": "./.github/actions/leak-check",
                 "with": {"private-terms": "${{ " + SAME_REPO + " && secrets.LEAK_CHECK_TERMS || '' }}"}},
            ],
        }},
    }
    action = yaml.safe_load((SCRIPT.parent / "action.yml").read_text())
    assert _without_descriptions(action) == {
        "name": "leak-check",
        "inputs": {
            "private-terms": {"required": False, "default": ""},
            "allowlist": {"required": False, "default": ".github/leak-check-allow.txt"},
        },
        "runs": {"using": "composite", "steps": [
            {"name": "Fetch the pull request as data", "shell": "bash",
             "env": {"PR": "${{ github.event.pull_request.number }}"},
             "run": 'if [ -z "$PR" ]; then\n'
                    '  echo "::error::leak-check runs on pull_request_target: no pull request in this event"\n'
                    "  exit 2\n"
                    "fi\n"
                    'git fetch --no-tags --quiet origin "+refs/pull/${PR}/head:refs/leak-check/head"\n'},
            {"name": "Check the added lines", "shell": "bash",
             "env": {"LEAK_CHECK_TERMS": "${{ inputs.private-terms }}",
                     "PRIVATE_LAYER": "${{ " + SAME_REPO + " && 'on' || 'off' }}",
                     "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                     "ALLOW": "${{ inputs.allowlist }}"},
             "run": 'python3 "$GITHUB_ACTION_PATH/leak_check.py" --git "$BASE_SHA" refs/leak-check/head'
                    ' --allow "$ALLOW" --private-layer "$PRIVATE_LAYER"'},
        ]},
    }


_EXPR_TOKEN = re.compile(r"\s*(?:(?P<s>'[^']*')|(?P<op>==|!=|&&|\|\|)|(?P<p>[A-Za-z_][\w.-]*))\s*")


def _gh_value(value, ctx):
    """A workflow or action value as the runner evaluates it, for the part of
    GitHub's expression language these files use: a whole-value ``${{ }}`` of
    context paths, quoted strings, ==, !=, && and ||, where && and || return an
    operand, as GitHub's do. A stand-in, not GitHub's engine: it compares strings
    case-sensitively, and a missing path reads as null."""
    if not isinstance(value, str) or "${{" not in value:
        return value
    m = re.fullmatch(r"\$\{\{(.*)\}\}", value, re.S)
    assert m, f"not a whole-value expression: {value!r}"
    body, pos, src, vals = m.group(1), 0, [], []
    for t in _EXPR_TOKEN.finditer(body):
        assert t.start() == pos, f"cannot read {body[pos:]!r}"
        pos = t.end()
        if t["s"]:
            src.append(repr(t["s"][1:-1]))
        elif t["op"]:
            src.append({"&&": " and ", "||": " or "}.get(t["op"], f" {t['op']} "))
        else:
            node = ctx
            for part in t["p"].split("."):
                node = node.get(part) if isinstance(node, dict) else None
            vals.append(node)
            src.append(f"_v[{len(vals) - 1}]")
    assert pos == len(body), f"cannot read {body[pos:]!r}"
    return eval("".join(src), {"__builtins__": {}}, {"_v": vals})


def _as_env(v):
    return "" if v is None else "true" if v is True else "false" if v is False else str(v)


class ActionRun(NamedTuple):
    rc: int  # the first failing step's exit code, or the last step's
    out: str
    err: str
    summary: str
    inputs: dict  # what the action received from the workflow's `with:`


def _run_the_action(tmp_path, head_files, head_repo, actor="maintainer", secret=None) -> ActionRun:
    """One pull_request_target event, run as the runner runs this repository's
    workflow: a scratch origin publishes the head as refs/pull/7/head, the
    checkout step's ref is checked out, and the action's own run texts run in
    order with their env evaluated against the event. The base already holds a
    hit, which a correctly wired check never reads."""
    import yaml

    tmp_path.mkdir(parents=True, exist_ok=True)
    origin, src, ws = tmp_path / "origin.git", tmp_path / "src", tmp_path / "ws"
    env = _git_env(tmp_path)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=env)
    src.mkdir()
    g, _ = _scratch_repo(src)
    (src / "a.md").write_text(PLANTED["email"] + "\n")
    base = _commit(g, env, "base")
    for name, text in head_files.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text(text)
    head = _commit(g, env, "the pull request")
    subprocess.run([*g, "push", "-q", str(origin), f"{base}:refs/heads/main", f"{head}:refs/pull/7/head"],
                   check=True, env=env)
    ctx = {
        "github": {"actor": actor, "event": {"pull_request": {
            "number": 7,
            "base": {"sha": base, "repo": {"id": 1001}},
            "head": {"sha": head, "repo": None if head_repo is None else {"id": head_repo}},
        }}},
        "secrets": {} if secret is None else {"LEAK_CHECK_TERMS": secret},
    }
    checkout, call = yaml.safe_load((REPO / ".github" / "workflows" / "leak-check.yml").read_text())[
        "jobs"]["leak-check"]["steps"]
    subprocess.run(["git", "clone", "-q", str(origin), str(ws)], check=True, env=env)
    subprocess.run(["git", "-C", str(ws), "checkout", "-q", "--detach", _gh_value(checkout["with"]["ref"], ctx)],
                   check=True, env=env)
    action = yaml.safe_load((SCRIPT.parent / "action.yml").read_text())
    inputs = {k: v.get("default", "") for k, v in action["inputs"].items()}
    inputs.update({k: _as_env(_gh_value(v, ctx)) for k, v in call["with"].items()})
    summary = tmp_path / "summary.md"
    summary.write_text("")
    out = err = ""
    for i, step in enumerate(action["runs"]["steps"]):
        script = tmp_path / f"step{i}.sh"
        script.write_text(step["run"])
        step_env = {k: _as_env(_gh_value(v, {**ctx, "inputs": inputs})) for k, v in step.get("env", {}).items()}
        p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                           capture_output=True, text=True, cwd=ws, timeout=60,
                           env={**env, "GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary),
                                "GITHUB_ACTION_PATH": str(SCRIPT.parent), **step_env})
        out, err = out + p.stdout, err + p.stderr
        if p.returncode:
            break
    return ActionRun(p.returncode, out, err, summary.read_text(), inputs)


def test_end_to_end_a_write_access_run_checks_both_layers(tmp_path):
    """A branch in this repository: the list arrives and layer 2 runs on the
    head's added lines only, and without the list the check fails closed."""
    r = _run_the_action(tmp_path / "1", {"b.md": "ok\n" + INVENTED_TEXT + "\n"}, head_repo=1001, secret=INVENTED)
    assert r.rc == 1 and "b.md:2: private term #1" in r.out, (r.out, r.err)
    assert r.inputs["private-terms"] == INVENTED and "a.md" not in r.out and lc.PRIVATE_OFF not in r.out
    r = _run_the_action(tmp_path / "2", {"b.md": "ok\n"}, head_repo=1001)
    assert r.rc == 2 and "NOT ARMED" in r.err, (r.out, r.err)


@pytest.mark.parametrize("head_repo, actor", [(2002, "outsider"), (None, "outsider"), (1001, "dependabot[bot]")],
                         ids=["fork", "deleted fork", "dependabot"])
def test_end_to_end_an_outside_run_never_holds_the_list(tmp_path, head_repo, actor):
    """A fork, a deleted fork, and Dependabot, which pushes branches here but
    whose runs receive no Actions secrets. Each is handed the list anyway (for
    Dependabot, as if a Dependabot secret had the same name): it never reaches
    the action, layer 2 does not run and says so, and nothing fails for want
    of it."""
    r = _run_the_action(tmp_path, {"b.md": "ok\n" + INVENTED_TEXT + "\n"}, head_repo=head_repo, actor=actor,
                        secret=INVENTED)
    assert r.rc == 0, (r.out, r.err)
    assert r.inputs["private-terms"] == "" and "private term" not in r.out and "NOT ARMED" not in r.err
    assert "leak-check: " + lc.PRIVATE_OFF in r.out and lc.PRIVATE_OFF in r.summary


def test_end_to_end_a_fork_s_own_allowlist_cannot_silence_its_hit(tmp_path):
    head = {"b.json": PLANTED["uuid"] + "\n", ".github/leak-check-allow.txt": "*.json uuid synthetic fixture\n"}
    r = _run_the_action(tmp_path, head, head_repo=2002, actor="outsider", secret=INVENTED)
    assert r.rc == 1 and "b.json:1: uuid" in r.out, (r.out, r.err)
    assert "an entry takes effect only once merged" in r.out
