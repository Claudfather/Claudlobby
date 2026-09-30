"""leak-check (#2042): the script behind .github/actions/leak-check, driven as CI runs it.

Every planted value is FAKE and built from fragments at run time, so this file
never holds a string the check itself would flag. The private-list term is
INVENTED; no real term appears here or in any output this file produces."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

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
    "mail " + "someone" + "@vera.com",
    "ls " + "/Users/x/Library /Users" + "/Shared/data /home/user./x /Users" + "/YOUR_USERNAME/.claude /home/alice",
    "dns " + "8.8.8.8",
    "sa " + "reporter" + "@your-project.iam.gserviceaccount.com",
    "fixture " + "5f0c2d1e" + "-0000-4000-8000-" + "000000000001",
]


def diff_of(*lines: str, path: str = "docs/notes.md") -> str:
    body = "".join(f"+{ln}\n" for ln in lines)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,{len(lines)} @@\n{body}"


def run(tmp_path, diff: str, terms: str | None = INVENTED, allow: str = "", extra=()):
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
    rc, out, err = run(
        tmp_path, diff_of("ok line"), terms=None, extra=["--terms-optional"]
    )
    assert rc == 0 and "layer 1 only" in err


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


def test_git_mode_reads_the_change_as_data_and_its_allowlist_from_head(tmp_path):
    g = ["git", "-C", str(tmp_path)]
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    subprocess.run([*g, "init", "-q", "-b", "main"], check=True, env=env)
    (tmp_path / "a.md").write_text(
        PLANTED["email"] + "\n"
    )  # already committed: never read
    subprocess.run([*g, "add", "a.md"], check=True, env=env)
    subprocess.run([*g, "commit", "-qm", "base"], check=True, env=env)
    base = subprocess.run(
        [*g, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (tmp_path / "b.json").write_text(PLANTED["uuid"] + "\n")
    subprocess.run([*g, "add", "b.json"], check=True, env=env)
    subprocess.run([*g, "commit", "-qm", "change"], check=True, env=env)

    def check():
        e = {k: v for k, v in env.items() if k != "GITHUB_ACTIONS"}
        e["LEAK_CHECK_TERMS"] = INVENTED
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--git", base, "HEAD"],
            capture_output=True,
            text=True,
            env=e,
            cwd=tmp_path,
            timeout=60,
        )

    p = check()
    assert p.returncode == 1 and "b.json:1: uuid" in p.stdout and "a.md" not in p.stdout
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "leak-check-allow.txt").write_text(
        "*.json uuid synthetic fixture\n"
    )
    subprocess.run([*g, "add", ".github"], check=True, env=env)
    subprocess.run([*g, "commit", "-qm", "allow it"], check=True, env=env)
    p = check()
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


def _check_git(tmp_path, base, env):
    e = {k: v for k, v in env.items() if k != "GITHUB_ACTIONS"}
    e["LEAK_CHECK_TERMS"] = INVENTED
    return subprocess.run([sys.executable, str(SCRIPT), "--git", base, "HEAD"],
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
