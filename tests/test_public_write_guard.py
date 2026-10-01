"""The public-write guard, through the REAL hook script (lib/public-write-guard.sh).

It refuses a GitHub-bound write that would put a term from the host's list into a
PUBLIC repository, and lets the same write through to a private one. The terms
here are INVENTED; the real list is host configuration and never appears in a
repository. Visibility is answered by a fake `gh` that logs every lookup, so a
test can also pin that a write without a hit asks nothing."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "lib" / "public-write-guard.sh"
TERMS = "# invented terms for the tests\nzephyr[ ._-]?widgets\nquillon[ ._-]?media\n"
HIT = "notes about Zephyr-Widgets"
VIS = {
    "pub-org/pub-repo": "public",
    "priv-org/priv-repo": "private",
    "pub-org/zephyr-widgets": "public",
    "int-org/int-repo": "internal",
    "caps-org/caps-repo": "PRIVATE",
}

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_GH_LOG"], "a").write(" ".join(sys.argv[1:]) + "\n")
vis = json.load(open(os.environ["FAKE_GH_VIS"]))
if len(sys.argv) > 2 and sys.argv[1] == "api" and sys.argv[2].startswith("repos/"):
    if os.environ.get("FAKE_GH_REST_DOWN"):
        print("HTTP 403: API rate limit exceeded", file=sys.stderr); sys.exit(1)
    key = sys.argv[2][len("repos/"):]
    if key in vis:
        print(vis[key]); sys.exit(0)
if sys.argv[1:3] == ["repo", "view"] and not os.environ.get("FAKE_GH_GRAPHQL_DOWN"):
    if sys.argv[3] in vis:
        print(vis[sys.argv[3]].upper()); sys.exit(0)
print("HTTP 404", file=sys.stderr); sys.exit(1)
"""


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gh").write_text(FAKE_GH)
    (fake / "gh").chmod(0o755)
    (tmp_path / "vis.json").write_text(json.dumps(VIS))
    terms = tmp_path / "terms"
    terms.write_text(TERMS)
    return {
        "HOME": str(tmp_path),
        "PATH": f"{fake}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "CLAUDLOBBY_ROOT": str(tmp_path / "root"),
        "PUBLIC_WRITE_GUARD_TERMS": str(terms),
        "PUBLIC_WRITE_GUARD_CACHE": str(tmp_path / "cache.json"),
        "PUBLIC_WRITE_GUARD_EVENTS_FILE": str(tmp_path / "events.jsonl"),
        "FAKE_GH_VIS": str(tmp_path / "vis.json"),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PLANE_EMIT_DISABLED": "1",
    }


def run(env, tool, tool_input, cwd):
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)})
    p = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
    )
    assert p.returncode == 0, p.stderr
    if (
        '"permissionDecision": "deny"' in p.stdout
        or '"permissionDecision":"deny"' in p.stdout
    ):
        return "deny", json.loads(p.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
    return "allow", p.stdout


def bash(env, command, cwd):
    return run(env, "Bash", {"command": command}, cwd)


def lookups(env):
    log = Path(env["FAKE_GH_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def events(env):
    f = Path(env["PUBLIC_WRITE_GUARD_EVENTS_FILE"])
    return (
        [json.loads(ln)["type"] for ln in f.read_text().splitlines()]
        if f.exists()
        else []
    )


def repo(tmp_path, env, slug):
    d = tmp_path / slug.replace("/", "_")
    d.mkdir()
    g = ["git", "-C", str(d)]
    subprocess.run([*g, "init", "-q", "-b", "main"], check=True, env=env)
    subprocess.run(
        [*g, "remote", "add", "origin", f"https://github.com/{slug}.git"],
        check=True,
        env=env,
    )
    (d / "a.txt").write_text("base\n")
    subprocess.run([*g, "add", "a.txt"], check=True, env=env)
    subprocess.run([*g, "commit", "-qm", "base"], check=True, env=env)
    subprocess.run(
        [*g, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True, env=env
    )
    return d


def commit(d, env, name, text, message):
    (d / name).write_text(text)
    subprocess.run(["git", "-C", str(d), "add", name], check=True, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", message], check=True, env=env)


# --- MCP writers ------------------------------------------------------------------


def test_mcp_writer_to_a_public_repo_is_refused(env, tmp_path):
    verdict, why = run(
        env,
        "mcp__github__create_issue",
        {"owner": "pub-org", "repo": "pub-repo", "title": "t", "body": HIT},
        tmp_path,
    )
    assert verdict == "deny" and "pub-org/pub-repo, which is public" in why
    assert "Zephyr" not in why  # the refusal never echoes the listed text


def test_mcp_writer_to_a_private_repo_passes(env, tmp_path):
    assert (
        run(
            env,
            "mcp__github__add_issue_comment",
            {"owner": "priv-org", "repo": "priv-repo", "issue_number": 1, "body": HIT},
            tmp_path,
        )[0]
        == "allow"
    )


def test_mcp_push_files_reads_every_file(env, tmp_path):
    files = [{"path": "a.md", "content": "fine"}, {"path": "b.md", "content": HIT}]
    assert (
        run(
            env,
            "mcp__github__push_files",
            {
                "owner": "pub-org",
                "repo": "pub-repo",
                "branch": "main",
                "files": files,
                "message": "m",
            },
            tmp_path,
        )[0]
        == "deny"
    )


def test_mcp_reader_is_not_a_write(env, tmp_path):
    assert run(env, "mcp__github__search_issues", {"q": HIT}, tmp_path)[0] == "allow"
    assert lookups(env) == []


def test_mcp_new_public_repository_is_refused(env, tmp_path):
    assert (
        run(
            env,
            "mcp__github__create_repository",
            {"name": "x", "description": HIT, "private": False},
            tmp_path,
        )[0]
        == "deny"
    )
    assert (
        run(
            env,
            "mcp__github__create_repository",
            {"name": "x", "description": HIT, "private": True},
            tmp_path,
        )[0]
        == "allow"
    )


def test_a_write_without_a_hit_asks_nothing(env, tmp_path):
    assert (
        run(
            env,
            "mcp__github__create_issue",
            {"owner": "pub-org", "repo": "pub-repo", "title": "t", "body": "plain"},
            tmp_path,
        )[0]
        == "allow"
    )
    assert lookups(env) == []


# --- Bash gh writers ------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        f'gh issue create -R pub-org/pub-repo --title t --body "{HIT}"',
        'gh pr comment 3 --repo pub-org/pub-repo -b "QUILLON_MEDIA"',
        f"gh pr create -R pub-org/pub-repo --title t --body \"$(cat <<'EOF'\n{HIT}\nEOF\n)\"",
        f'gh release create v1 -R pub-org/pub-repo --notes "{HIT}"',
        f'gh api repos/pub-org/pub-repo/issues -f title=t -f body="{HIT}"',
        f'cd /tmp && gh issue edit 4 -R pub-org/pub-repo --body "{HIT}"',
    ],
)
def test_gh_writer_to_a_public_repo_is_refused(env, tmp_path, command):
    assert bash(env, command, tmp_path)[0] == "deny", command


def test_gh_writer_to_a_private_repo_passes(env, tmp_path):
    assert (
        bash(
            env,
            f'gh issue create -R priv-org/priv-repo --title t --body "{HIT}"',
            tmp_path,
        )[0]
        == "allow"
    )


def test_a_body_file_is_read(env, tmp_path):
    (tmp_path / "body.md").write_text(HIT)
    assert (
        bash(
            env,
            "gh pr create -R pub-org/pub-repo --title t --body-file body.md",
            tmp_path,
        )[0]
        == "deny"
    )


def test_the_current_repository_is_the_target_without_R(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    priv = repo(tmp_path, env, "priv-org/priv-repo")
    assert bash(env, f'gh issue create --title t --body "{HIT}"', pub)[0] == "deny"
    assert bash(env, f'gh issue create --title t --body "{HIT}"', priv)[0] == "allow"


def test_gh_reads_are_not_writes(env, tmp_path):
    assert (
        bash(env, f'gh issue list -R pub-org/pub-repo --search "{HIT}"', tmp_path)[0]
        == "allow"
    )
    assert (
        bash(
            env,
            f'gh api repos/pub-org/pub-repo/issues --jq ".[] | select(.title == \\"{HIT}\\")"',
            tmp_path,
        )[0]
        == "allow"
    )


# --- git commit and git push ----------------------------------------------------------


def test_commit_message_to_a_public_repo_is_refused(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    assert bash(env, f'git commit -m "{HIT}"', pub)[0] == "deny"
    assert bash(env, f'git -C {pub} commit -m "{HIT}"', tmp_path)[0] == "deny"


def test_a_staged_added_line_is_read(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    (pub / "a.txt").write_text(f"base\n{HIT}\n")
    subprocess.run(["git", "-C", str(pub), "add", "a.txt"], check=True, env=env)
    verdict, why = bash(env, 'git commit -m "ordinary"', pub)
    assert verdict == "deny" and "in the staged changes" in why  # names the part, not the text
    assert "Zephyr" not in why


def test_a_removed_line_is_not_a_hit(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    (pub / "a.txt").write_text(f"{HIT}\n")
    subprocess.run(
        ["git", "-C", str(pub), "commit", "-qam", "seed"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(pub), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
        env=env,
    )
    (pub / "a.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(pub), "add", "a.txt"], check=True, env=env)
    assert bash(env, 'git commit -m "remove it"', pub)[0] == "allow"


def test_commit_to_a_private_repo_passes(env, tmp_path):
    priv = repo(tmp_path, env, "priv-org/priv-repo")
    assert bash(env, f'git commit -m "{HIT}"', priv)[0] == "allow"


def test_push_reads_the_commits_it_would_send(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    (pub / "b.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(pub), "add", "b.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(pub), "commit", "-qm", HIT], check=True, env=env)
    assert bash(env, "git push origin main", pub)[0] == "deny"
    assert bash(env, "git push", pub)[0] == "deny"
    subprocess.run(
        ["git", "-C", str(pub), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
        env=env,
    )
    assert bash(env, "git push origin main", pub)[0] == "allow"  # nothing left to send


def test_a_repository_with_no_github_remote_is_out_of_scope(env, tmp_path):
    d = tmp_path / "local"
    d.mkdir()
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True, env=env)
    assert bash(env, f'git commit --allow-empty -m "{HIT}"', d)[0] == "allow"


# --- failure directions ---------------------------------------------------------------


def test_no_list_lets_the_write_through_and_says_so(env, tmp_path):
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).unlink()
    assert (
        run(
            env,
            "mcp__github__create_issue",
            {"owner": "pub-org", "repo": "pub-repo", "body": HIT},
            tmp_path,
        )[0]
        == "allow"
    )
    assert events(env) == ["public_write_guard_unarmed"]


def test_a_list_that_does_not_compile_refuses_every_guarded_write(env, tmp_path):
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).write_text("broken[\n")
    verdict, why = run(
        env,
        "mcp__github__create_issue",
        {"owner": "priv-org", "repo": "priv-repo", "body": "plain"},
        tmp_path,
    )
    assert verdict == "deny" and "does not compile" in why


def test_unknown_visibility_on_a_hit_refuses(env, tmp_path):
    verdict, why = run(
        env,
        "mcp__github__create_issue",
        {"owner": "gone-org", "repo": "gone", "body": HIT},
        tmp_path,
    )
    assert verdict == "deny" and "whose visibility could not be read" in why


def test_an_unparseable_payload_passes(env, tmp_path):
    p = subprocess.run(
        ["bash", str(HOOK)],
        input="not json mcp__github__",
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=30,
    )
    assert p.returncode == 0 and p.stdout == ""


def test_visibility_is_cached_between_writes(env, tmp_path):
    for _ in range(3):
        run(
            env,
            "mcp__github__create_issue",
            {"owner": "priv-org", "repo": "priv-repo", "body": HIT},
            tmp_path,
        )
    assert len(lookups(env)) == 1


def test_the_off_switch(env, tmp_path):
    off = Path(env["CLAUDLOBBY_ROOT"]) / "state" / "public-write-guard"
    off.mkdir(parents=True)
    (off / "disabled").write_text("")
    assert (
        run(
            env,
            "mcp__github__create_issue",
            {"owner": "pub-org", "repo": "pub-repo", "body": HIT},
            tmp_path,
        )[0]
        == "allow"
    )


def test_the_prefilter_starts_python_only_for_a_github_shaped_call(env, tmp_path):
    """The zero-fork front door: an unrelated call never starts the decider, and a
    GitHub-shaped one does (the positive control)."""
    shim = tmp_path / "shim"
    shim.mkdir()
    os.symlink("/bin/cat", shim / "cat")
    marker = tmp_path / "python-started"
    (shim / "python3").write_text(f"#!/bin/sh\n: > {marker}\ncat >/dev/null\n")
    (shim / "python3").chmod(0o755)
    env = dict(env, PATH=str(shim))

    def send(tool, tool_input):
        payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
        p = subprocess.run(["/bin/bash", str(HOOK)], input=payload, capture_output=True,
                           text=True, env=env, timeout=30)
        assert p.returncode == 0 and p.stdout == "", p.stderr

    send("Read", {"file_path": "/x"})
    send("Bash", {"command": "ls -la"})
    assert not marker.exists()
    send("mcp__github__create_issue", {"owner": "o", "repo": "r", "body": "b"})
    assert marker.exists()


# --- what counts as a write, and what counts as its content ---------------------------


def test_an_mcp_tool_that_is_not_a_read_is_a_write(env, tmp_path):
    """Writers are every tool but get_/list_/search_, so a tool a newer server
    adds is covered before anyone lists it."""
    tool = "mcp__github__submit_pending_pull_request_review"
    assert run(env, tool, {"owner": "pub-org", "repo": "pub-repo", "body": HIT}, tmp_path)[0] == "deny"


def test_the_target_repository_name_is_not_content(env, tmp_path):
    assert (
        run(
            env,
            "mcp__github__create_issue",
            {"owner": "pub-org", "repo": "zephyr-widgets", "title": "t", "body": "plain"},
            tmp_path,
        )[0]
        == "allow"
    )


@pytest.mark.parametrize(
    "command",
    [
        f'gh issue close 5 -R pub-org/pub-repo --comment "{HIT}"',
        f'gh pr merge 5 -R pub-org/pub-repo --squash --body "{HIT}"',
        f'gh issue -R pub-org/pub-repo create --title t --body "{HIT}"',
        f'gh label create "{HIT}" -R pub-org/pub-repo',
        f"cat <<'EOF' | gh issue create -R pub-org/pub-repo --title t --body-file -\n{HIT}\nEOF",
        f'bash -c "gh issue create -R pub-org/pub-repo --title t --body \\"{HIT}\\""',
        f'gh api graphql -f query="mutation {{ addComment(input: {{body: \\"{HIT}\\"}}) }}"',
    ],
)
def test_other_gh_write_shapes_are_read(env, tmp_path, command):
    assert bash(env, command, tmp_path)[0] == "deny", command


@pytest.mark.parametrize(
    "command",
    [
        f'gh api graphql -f query="query {{ search(query: \\"{HIT}\\") }}"',
        f'gh api -X GET search/issues -f q="{HIT}"',
    ],
)
def test_gh_api_reads_are_not_writes(env, tmp_path, command):
    assert bash(env, command, tmp_path)[0] == "allow", command


def test_a_new_repository_takes_its_visibility_from_its_flag(env, tmp_path):
    assert bash(env, f'gh repo create x --public --description "{HIT}"', tmp_path)[0] == "deny"
    assert bash(env, f'gh repo create x --private --description "{HIT}"', tmp_path)[0] == "allow"


def test_a_path_holding_a_listed_term_is_not_content(env, tmp_path):
    """A directory named by cd, and a body file's path, are not what the write
    puts in the repository: a clean write from such a path passes."""
    d = tmp_path / "zephyr-widgets-work"
    d.mkdir()
    (d / "clean.md").write_text("plain\n")
    assert (
        bash(
            env,
            f"cd {d} && gh issue create -R pub-org/pub-repo --title t --body-file {d}/clean.md",
            tmp_path,
        )[0]
        == "allow"
    )


def test_a_body_file_the_command_writes_is_read_from_the_command(env, tmp_path):
    body = tmp_path / "body.md"
    write = f"cat > {body} <<'EOF'\n{{}}\nEOF\ngh pr create -R pub-org/pub-repo --title t --body-file {body}"
    assert bash(env, write.format(HIT), tmp_path)[0] == "deny"
    assert not body.exists()
    assert bash(env, write.format("plain"), tmp_path)[0] == "allow"


def test_a_body_file_a_program_writes_cannot_be_read_and_refuses(env, tmp_path):
    verdict, why = bash(
        env,
        f'python3 -c "print(1)" > {tmp_path}/b.md && gh pr create -R pub-org/pub-repo --title t --body-file {tmp_path}/b.md',
        tmp_path,
    )
    assert verdict == "deny" and "could not read" in why


# --- following the shell to the repository ------------------------------------------------


def test_a_variable_directory_is_followed(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    priv = repo(tmp_path, env, "priv-org/priv-repo")
    assert bash(env, f'W={pub}; cd "$W" && git commit -m "{HIT}"', tmp_path)[0] == "deny"
    assert bash(env, f'W={priv}; cd "$W" && git commit -m "{HIT}"', tmp_path)[0] == "allow"


def test_a_backslash_continuation_is_followed(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    assert bash(env, f'cd {pub} && \\\ngit commit -m "{HIT}"', tmp_path)[0] == "deny"


def test_a_directory_this_guard_cannot_name_refuses_a_git_write(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    verdict, why = bash(env, f'cd "$(echo {pub})" && git push', tmp_path)
    assert verdict == "deny" and "literal path" in why


def test_a_git_add_in_the_same_command_is_read(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    (pub / "a.txt").write_text(f"base\n{HIT}\n")
    assert bash(env, 'git add a.txt && git commit -m "ordinary"', pub)[0] == "deny"
    assert bash(env, 'git commit -m "ordinary" a.txt', pub)[0] == "deny"


def test_a_push_of_a_clean_up_commit_passes(env, tmp_path):
    """Removing a listed term is how a leak gets fixed: the push must pass."""
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    commit(pub, env, "a.txt", f"{HIT}\n", "seed")
    subprocess.run(
        ["git", "-C", str(pub), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
        env=env,
    )
    commit(pub, env, "a.txt", "base\n", "remove it")
    assert bash(env, "git push origin main", pub)[0] == "allow"


def test_the_next_line_is_not_read_as_refs_to_push(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    commit(pub, env, "b.txt", "plain\n", "plain")
    command = (
        "git push origin main  # ship it\n"
        f'gh pr create -R priv-org/priv-repo --title t --body "{HIT}"'
    )
    assert bash(env, command, pub)[0] == "allow"


def test_a_bare_push_goes_where_git_sends_it(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    g = ["git", "-C", str(pub)]
    subprocess.run([*g, "remote", "add", "priv", "https://github.com/priv-org/priv-repo.git"], check=True, env=env)
    subprocess.run([*g, "update-ref", "refs/remotes/priv/main", "HEAD"], check=True, env=env)
    subprocess.run([*g, "config", "branch.main.remote", "priv"], check=True, env=env)
    commit(pub, env, "b.txt", "x\n", HIT)
    assert bash(env, "git push", pub)[0] == "allow"  # branch.main.remote: private
    assert bash(env, "git push origin main", pub)[0] == "deny"


def test_a_gist_is_public_whatever_its_flag(env, tmp_path):
    (tmp_path / "notes.md").write_text(HIT)
    (tmp_path / "plain.md").write_text("plain\n")
    assert bash(env, f"gh gist create {tmp_path}/notes.md", tmp_path)[0] == "deny"
    assert bash(env, f"gh gist create --public {tmp_path}/plain.md", tmp_path)[0] == "allow"
    assert lookups(env) == []  # a gist's visibility is known without asking


def test_a_redirect_is_not_a_ref_to_push(env, tmp_path):
    """`2>&1` tokenizes as `2`, `>&`, `1`: the descriptor is not a refspec."""
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    commit(pub, env, "b.txt", "plain\n", "plain")
    assert bash(env, "git push origin main 2>&1", pub)[0] == "allow"
    assert bash(env, "git push origin main >/dev/null 2>&1 || echo failed", pub)[0] == "allow"


def test_check_reports_the_list_without_printing_a_term(env, tmp_path):
    def check():
        p = subprocess.run(["python3", str(REPO / "lib" / "public-write-guard.py"), "--check"],
                           capture_output=True, text=True, env=env, timeout=30)
        return p.returncode, p.stdout

    rc, out = check()
    assert rc == 0 and "ok, 2 pattern(s)" in out and "zephyr" not in out.lower()
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).write_text("broken[\n")
    rc, out = check()
    assert rc == 1 and "broken" in out
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).unlink()
    rc, out = check()
    assert rc == 1 and "absent" in out


# --- the shapes the first cut read less of than the command says (vera, #2032) ---------


def issue(owner, repo, body=HIT):
    return {"owner": owner, "repo": repo, "title": "t", "body": body}


def check_list(env):
    p = subprocess.run(["python3", str(REPO / "lib" / "public-write-guard.py"), "--check"],
                       capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout


@pytest.mark.parametrize(
    "shape",
    [
        'gh pr create -R {r} \\\n  --title t \\\n  --body "{t}"',
        'gh issue comment 5 -R {r} \\\n\t--body "{t}"',
        'gh issue comment 5 \\\n  -R {r} --body "{t}"',
        'gh api repos/{r}/issues \\\n  -f title=t \\\n  -f body="{t}"',
    ],
    ids=["pr-create", "tab-indent", "target-on-line-2", "api"],
)
def test_an_indented_continuation_is_one_command(env, tmp_path, shape):
    """A backslash-newline joins two lines. The indent after it used to split the
    command, so a body on a later line was never read."""
    assert bash(env, shape.format(r="pub-org/pub-repo", t=HIT), tmp_path)[0] == "deny"
    assert bash(env, shape.format(r="priv-org/priv-repo", t=HIT), tmp_path)[0] == "allow"
    assert bash(env, shape.format(r="pub-org/pub-repo", t="plain"), tmp_path)[0] == "allow"


def test_a_commit_message_on_a_continued_line_is_read(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    assert bash(env, f'git commit --allow-empty \\\n  -m "{HIT}"', pub)[0] == "deny"


def test_a_backslash_newline_inside_single_quotes_is_text(env, tmp_path):
    """Single quotes keep a backslash-newline, so the body there is not the
    listed term; double quotes join the lines, and then it is."""
    single = "gh issue comment 5 -R pub-org/pub-repo --body 'zephyr\\\nwidgets'"
    assert bash(env, single, tmp_path)[0] == "allow"
    assert bash(env, single.replace("'", '"'), tmp_path)[0] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        'git add new.txt && git commit -m "widgets"',
        'git add -A && git commit -m "widgets"',
        'git add . && git commit -m "widgets" && git push',
        'git add new.txt && git commit -m "widgets" && git push origin main',
    ],
    ids=["add-commit", "add-all", "add-dot-push", "push-origin"],
)
def test_a_new_file_added_and_committed_in_one_command_is_read(env, tmp_path, command):
    """git diff never lists an untracked file, and the push runs before the
    commit exists: the new file is read from the add's own pathspecs."""
    for slug, want in (("pub-org/pub-repo", "deny"), ("priv-org/priv-repo", "allow")):
        d = repo(tmp_path, env, slug)
        (d / "new.txt").write_text(f"{HIT}\n")
        assert bash(env, command, d)[0] == want, slug


def test_an_add_counts_only_the_new_files_it_takes_in(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    (pub / "new.txt").write_text("plain\n")
    (pub / "other.txt").write_text(f"{HIT}\n")
    assert bash(env, 'git add new.txt && git commit -m "widgets"', pub)[0] == "allow"
    assert bash(env, 'git add -u && git commit -m "widgets"', pub)[0] == "allow"
    assert bash(env, 'git add . && git commit -m "widgets"', pub)[0] == "deny"
    (pub / "other.txt").unlink()
    (pub / ".gitignore").write_text("ignored.txt\n")
    (pub / "ignored.txt").write_text(f"{HIT}\n")
    assert bash(env, 'git add . && git commit -m "widgets"', pub)[0] == "allow"
    assert bash(env, 'git add -f ignored.txt && git commit -m "widgets"', pub)[0] == "deny"


def test_a_file_the_command_writes_then_adds_is_read_from_the_command(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    command = "cat > notes.md <<'EOF'\n{}\nEOF\ngit add notes.md && git commit -m widgets"
    assert bash(env, command.format(HIT), pub)[0] == "deny"
    assert bash(env, command.format("plain"), pub)[0] == "allow"
    assert not (pub / "notes.md").exists()  # the hook reads the command, never runs it


def test_a_push_reads_a_commit_made_earlier_in_the_same_command(env, tmp_path):
    """The commit does not exist yet when the guard runs. Its own remote is
    private here; the push in the same command names a public one."""
    d = repo(tmp_path, env, "priv-org/priv-repo")
    g = ["git", "-C", str(d)]
    subprocess.run([*g, "remote", "add", "pub", "https://github.com/pub-org/pub-repo.git"],
                   check=True, env=env)
    subprocess.run([*g, "update-ref", "refs/remotes/pub/main", "HEAD"], check=True, env=env)
    assert bash(env, f'git commit --allow-empty -m "{HIT}" && git push pub main', d)[0] == "deny"
    assert bash(env, 'git commit --allow-empty -m "plain" && git push pub main', d)[0] == "allow"


@pytest.mark.parametrize(
    "command",
    [
        'gh issue comment 5 -R {r} --body "$(python3 gen.py)"',
        'gh issue comment 5 -R {r} --body "notes: `python3 gen.py`"',
        "gh issue comment 5 -R {r} --body $(python3 gen.py)",
        'B="$(python3 gen.py)"; gh issue comment 5 -R {r} --body "$B"',
        'gh issue comment 5 -R {r} --body "$(cat notes.md | head -5)"',
        'echo "$(date)" > b.md && gh issue comment 5 -R {r} --body-file b.md',
    ],
    ids=["dollar-paren", "backticks", "unquoted", "via-variable", "cat-in-a-pipe", "written-file"],
)
def test_a_programs_output_substituted_into_a_write_cannot_be_read(env, tmp_path, command):
    """The failure table: a program's output used as a body counts as a hit."""
    verdict, why = bash(env, command.format(r="pub-org/pub-repo"), tmp_path)
    assert verdict == "deny" and "could not read" in why and "@@" not in why
    assert bash(env, command.format(r="priv-org/priv-repo"), tmp_path)[0] == "allow"


def test_a_substitution_in_a_flag_that_is_not_content_is_left_as_written(env, tmp_path):
    """A sha read from GitHub is not content. This is the auto-merge recipe: the
    replay of this host's recorded history found 91 merges of this shape, each
    of which a rule on every word would have refused on a public repository."""
    merge = ('PH=$(gh api repos/pub-org/pub-repo/pulls/5 --jq .head.sha); '
             'gh pr merge 5 -R pub-org/pub-repo --squash --admin --match-head-commit "$PH"')
    assert bash(env, merge, tmp_path)[0] == "allow"
    api = ('gh api -X PUT repos/pub-org/pub-repo/pulls/5/merge '
           '-f sha="$(git rev-parse HEAD)" -f merge_method=squash')
    assert bash(env, api, tmp_path)[0] == "allow"
    # the controls: the same writes with a program's output where content goes
    body = 'gh pr merge 5 -R pub-org/pub-repo --squash --body "$(python3 gen.py)"'
    assert bash(env, body, tmp_path)[0] == "deny"
    field = 'gh api repos/pub-org/pub-repo/issues -f title=t -f body="$(python3 gen.py)"'
    assert bash(env, field, tmp_path)[0] == "deny"


def test_cat_of_a_file_or_a_heredoc_is_read_where_it_is_substituted(env, tmp_path):
    (tmp_path / "body.md").write_text("plain\n")
    for form in ('"$(cat body.md)"', '"$(< body.md)"', "\"$(cat <<'EOF'\nplain\nEOF\n)\""):
        command = f"gh issue comment 5 -R pub-org/pub-repo --body {form}"
        assert bash(env, command, tmp_path)[0] == "allow", form
    (tmp_path / "body.md").write_text(f"{HIT}\n")
    command = 'gh issue comment 5 -R pub-org/pub-repo --body "$(cat body.md)"'
    assert bash(env, command, tmp_path)[0] == "deny"


def test_a_substitution_inside_single_quotes_is_text(env, tmp_path):
    command = "gh issue comment 5 -R pub-org/pub-repo --body 'run $(pwd) and `ls`'"
    assert bash(env, command, tmp_path)[0] == "allow"


def test_the_commands_inside_a_substitution_are_read(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    commit(pub, env, "b.txt", "x\n", HIT)
    assert bash(env, 'out="$(git push origin main 2>&1)"; echo "$out"', pub)[0] == "deny"


def test_a_url_names_the_repository_gh_writes_to(env, tmp_path):
    priv = repo(tmp_path, env, "priv-org/priv-repo")
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    to_pub = f'gh issue comment https://github.com/pub-org/pub-repo/issues/5 --body "{HIT}"'
    to_priv = f'gh pr comment https://github.com/priv-org/priv-repo/pull/5 --body "{HIT}"'
    assert bash(env, to_pub, priv)[0] == "deny"  # a public target from a private checkout
    assert bash(env, to_pub, tmp_path)[0] == "deny"  # and from no checkout
    assert bash(env, to_priv, pub)[0] == "allow"  # a private target from a public checkout
    assert bash(env, to_pub.replace("github.com", "ghe.example.com"), pub)[0] == "allow"


def test_a_body_file_named_by_an_unset_variable_is_unreadable_not_a_crash(env, tmp_path):
    command = 'gh issue comment 5 -R pub-org/pub-repo --body-file "$NOPE_UNSET"'
    verdict, why = bash(env, command, tmp_path)
    assert verdict == "deny" and "could not read" in why
    both = ('gh issue comment 5 -R priv-org/priv-repo --body-file "$NOPE_UNSET" && '
            f'gh issue comment 6 -R pub-org/pub-repo --body "{HIT}"')
    assert bash(env, both, tmp_path)[0] == "deny"  # a crash failed the whole line open


def test_env_names_the_repository_too(env, tmp_path):
    command = 'env GH_REPO={r} gh issue create --title t --body "' + HIT + '"'
    assert bash(env, command.format(r="pub-org/pub-repo"), tmp_path)[0] == "deny"
    assert bash(env, command.format(r="priv-org/priv-repo"), tmp_path)[0] == "allow"


@pytest.mark.parametrize("line", ["widgets|", ".*", "\\b", "x?"])
def test_a_line_that_can_match_an_empty_string_breaks_the_list(env, tmp_path, line):
    """Every write would be a hit: that is a broken list, named by its line."""
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).write_text(f"zephyr\n{line}\n")
    verdict, why = run(env, "mcp__github__create_issue", issue("priv-org", "priv-repo", "plain"),
                       tmp_path)
    assert verdict == "deny" and "line 2 can match an empty string" in why
    rc, out = check_list(env)
    assert rc == 1 and "line 2" in out


def test_a_broken_line_is_named_by_position_never_by_its_text(env, tmp_path):
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).write_text("# invented\nzephyr\n(?P=quillon)\n")
    verdict, why = run(env, "mcp__github__create_issue", issue("priv-org", "priv-repo", "plain"),
                       tmp_path)
    assert verdict == "deny" and "line 3 does not compile" in why
    assert "quillon" not in why.lower()
    rc, out = check_list(env)
    assert rc == 1 and "quillon" not in out.lower()


def test_check_names_the_off_switch(env, tmp_path):
    off = Path(env["CLAUDLOBBY_ROOT"]) / "state" / "public-write-guard"
    off.mkdir(parents=True)
    (off / "disabled").write_text("")
    rc, out = check_list(env)
    assert rc == 1 and "ok, 2 pattern(s)" in out and "disabled: set" in out


def test_a_cache_stamp_from_the_future_is_not_trusted(env, tmp_path):
    """The Pi has no RTC and steps its clock at boot."""
    Path(env["PUBLIC_WRITE_GUARD_CACHE"]).write_text(
        json.dumps({"pub-org/pub-repo": ["private", time.time() + 3600]}))
    assert run(env, "mcp__github__create_issue", issue("pub-org", "pub-repo"), tmp_path)[0] == "deny"
    assert len(lookups(env)) == 1


def test_a_cached_answer_past_its_lifetime_is_asked_again(env, tmp_path):
    Path(env["PUBLIC_WRITE_GUARD_CACHE"]).write_text(
        json.dumps({"pub-org/pub-repo": ["private", time.time() - 601]}))
    assert run(env, "mcp__github__create_issue", issue("pub-org", "pub-repo"), tmp_path)[0] == "deny"
    assert len(lookups(env)) == 1


def test_an_unknown_answer_is_asked_again_every_time(env, tmp_path):
    for _ in range(2):
        assert run(env, "mcp__github__create_issue", issue("gone-org", "gone"), tmp_path)[0] == "deny"
    assert sum(1 for ln in lookups(env) if ln.startswith("api ")) == 2


def test_an_internal_repository_is_not_public(env, tmp_path):
    assert run(env, "mcp__github__create_issue", issue("int-org", "int-repo"), tmp_path)[0] == "allow"


def test_the_visibility_answer_and_the_cache_key_ignore_case(env, tmp_path):
    for owner, name in (("caps-org", "caps-repo"), ("priv-org", "priv-repo"), ("Priv-Org", "PRIV-REPO")):
        assert run(env, "mcp__github__create_issue", issue(owner, name), tmp_path)[0] == "allow"
    assert lookups(env) == ["api repos/caps-org/caps-repo --jq .visibility",
                            "api repos/priv-org/priv-repo --jq .visibility"]


def test_a_rest_throttle_is_asked_again_over_graphql(env, tmp_path):
    env = dict(env, FAKE_GH_REST_DOWN="1")
    assert run(env, "mcp__github__create_issue", issue("priv-org", "priv-repo"), tmp_path)[0] == "allow"
    verdict, why = run(env, "mcp__github__create_issue", issue("pub-org", "pub-repo"), tmp_path)
    assert verdict == "deny" and "which is public" in why


def test_a_cached_answer_stands_in_when_github_cannot_be_read(env, tmp_path):
    env = dict(env, FAKE_GH_REST_DOWN="1", FAKE_GH_GRAPHQL_DOWN="1")
    now = time.time()
    Path(env["PUBLIC_WRITE_GUARD_CACHE"]).write_text(json.dumps({
        "priv-org/priv-repo": ["private", now - 3600],
        "pub-org/pub-repo": ["public", now - 3600],
        "old-org/old-repo": ["private", now - 2 * 86400],
    }))
    assert run(env, "mcp__github__create_issue", issue("priv-org", "priv-repo"), tmp_path)[0] == "allow"
    verdict, why = run(env, "mcp__github__create_issue", issue("pub-org", "pub-repo"), tmp_path)
    assert verdict == "deny" and "which is public" in why
    verdict, why = run(env, "mcp__github__create_issue", issue("old-org", "old-repo"), tmp_path)
    assert verdict == "deny" and "could not be read" in why


def test_deleting_a_remote_branch_carries_no_content(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    commit(pub, env, "b.txt", "x\n", HIT)
    assert bash(env, "git push origin --delete old-branch", pub)[0] == "allow"
    assert bash(env, "git push origin :old-branch", pub)[0] == "allow"
    assert bash(env, "git push origin main", pub)[0] == "deny"  # the control


def test_a_new_files_path_is_content(env, tmp_path):
    pub = repo(tmp_path, env, "pub-org/pub-repo")
    (pub / "zephyr-widgets.md").write_text("plain\n")
    subprocess.run(["git", "-C", str(pub), "add", "zephyr-widgets.md"], check=True, env=env)
    verdict, why = bash(env, 'git commit -m "plain"', pub)
    assert verdict == "deny" and "staged changes" in why


# Each arm of the hook's prefilter with a payload that ONLY it admits: a target
# such as pub-org/pub-repo contains `repo`, which the `*gh*repo*` arm admits,
# so an arm dropped from the hook would pass every other test.
PREFILTER_ARMS = {
    "*mcp__*github*": ("mcp__github__create_issue", {"owner": "o", "repo": "r", "body": "b"}),
    "*gh*issue*": ("Bash", {"command": "gh issue create -R o/r"}),
    "*gh*pr*": ("Bash", {"command": "gh pr create -R o/r"}),
    "*gh*release*": ("Bash", {"command": "gh release create v1 -R o/r"}),
    "*gh*gist*": ("Bash", {"command": "gh gist create f"}),
    "*gh*repo*": ("Bash", {"command": "gh repo create x"}),
    "*gh*label*": ("Bash", {"command": "gh label create x -R o/r"}),
    "*gh*api*": ("Bash", {"command": "gh api x -f a=b"}),
    "*git*commit*": ("Bash", {"command": "git commit -m m"}),
    "*git*push*": ("Bash", {"command": "git push"}),
}


@pytest.mark.parametrize("arm", sorted(PREFILTER_ARMS))
def test_every_prefilter_arm_starts_the_decider_on_its_own(env, tmp_path, arm):
    tool, tool_input = PREFILTER_ARMS[arm]
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": "/w"})
    assert [a for a in PREFILTER_ARMS if fnmatch.fnmatchcase(payload, a)] == [arm]
    assert arm in HOOK.read_text()  # the arm this case holds is the hook's own
    shim = tmp_path / "shim"
    shim.mkdir()
    os.symlink("/bin/cat", shim / "cat")
    marker = tmp_path / "python-started"
    (shim / "python3").write_text(f"#!/bin/sh\n: > {marker}\ncat >/dev/null\n")
    (shim / "python3").chmod(0o755)
    p = subprocess.run(["/bin/bash", str(HOOK)], input=payload, capture_output=True, text=True,
                       env=dict(env, PATH=str(shim)), timeout=30)
    assert p.returncode == 0 and marker.exists(), p.stderr


def test_the_guards_event_types_are_registered():
    """The no-list alarm is critical: that is what puts it under the bot's
    ALERTS and in `events --critical`."""
    from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY

    assert SYSTEM_EVENT_SEVERITY["public_write_guard_unarmed"] == "critical"
    assert SYSTEM_EVENT_SEVERITY["public_write_refused"] == "notice"


def test_the_guards_events_land_on_the_plane_anchored_on_the_bot(env, tmp_path):
    """Through the REAL emission path, not the test seam: a refusal, the no-list
    alarm and the fail-open breadcrumb each land anchored on the bot, so the
    rollout's `claudlobby events --bot <bot>` finds them."""
    from tests.plane_fixtures import _scene
    from tests.test_plane_events_door import _door_env, _events_cmd, _rows

    root, paths, _, _ = _scene(tmp_path)
    bot_dir = paths.runtime_bots / "w1"
    (bot_dir / "data").mkdir(parents=True)
    real = shutil.which("python3", path="/usr/bin:/bin")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").write_text(  # fails the decider on demand, and nothing else
        '#!/bin/bash\ncase "$1" in */public-write-guard.py) [ -n "${PWG_FAIL:-}" ] && exit 1 ;; esac\n'
        f'exec {real} "$@"\n'
    )
    (shim / "python3").chmod(0o755)
    e = {k: v for k, v in env.items()
         if k not in ("PLANE_EMIT_DISABLED", "PUBLIC_WRITE_GUARD_EVENTS_FILE")}
    e.update(_door_env(root))
    e.update(PATH=f"{shim}:{tmp_path / 'bin'}:/usr/bin:/bin", BOT_DIR=str(bot_dir), BOT_ID="w1",
             FLEET_EVENT_EMIT_TIMEOUT_S="120")

    def landed(*args):
        deadline = time.monotonic() + 180
        while True:
            rows = _rows(_events_cmd(root, "--json", "--bot", "w1", *args))
            if rows or time.monotonic() > deadline:
                return rows
            time.sleep(1)

    assert run(e, "mcp__github__create_issue", issue("pub-org", "pub-repo"), tmp_path)[0] == "deny"
    assert [r["type"] for r in landed("--type", "public_write_refused")] == ["public_write_refused"]
    no_dir = {k: v for k, v in e.items() if k != "BOT_DIR"}  # BOT_ID alone still names the bot
    assert run(no_dir, "mcp__github__create_issue", issue("pub-org", "pub-repo"), tmp_path)[0] == "deny"
    deadline = time.monotonic() + 180
    while len(landed("--type", "public_write_refused")) < 2 and time.monotonic() < deadline:
        time.sleep(1)
    assert len(landed("--type", "public_write_refused")) == 2
    Path(e["PUBLIC_WRITE_GUARD_TERMS"]).unlink()
    assert run(e, "mcp__github__create_issue", issue("pub-org", "pub-repo"), tmp_path)[0] == "allow"
    assert [r["type"] for r in landed("--critical")] == ["public_write_guard_unarmed"]
    failing = dict(e, PWG_FAIL="1")
    assert run(failing, "mcp__github__create_issue", issue("pub-org", "pub-repo"), tmp_path)[0] == "allow"
    crumbs = landed("--type", "script_error")
    assert len(crumbs) == 1 and "INACTIVE" in crumbs[0]["data"]["message"]


# --- a URL a write's text mentions is text; only a URL given as the target is one ---------

PRIV_URL = "https://github.com/priv-org/priv-repo/issues/3"
PUB_URL = "https://github.com/pub-org/pub-repo/pull/9"

URL_ROWS = [
    # The target is the checkout's repository whatever a text flag's value mentions.
    (f'gh issue comment 5 --body "{HIT} see {PRIV_URL}"', "pub", "deny"),
    (f'gh pr comment 5 -b "{HIT} see {PRIV_URL}"', "pub", "deny"),
    (f'gh issue create --title "{PRIV_URL} {HIT}" --body "plain"', "pub", "deny"),
    (f'gh issue create --title "{HIT}" --body "{PRIV_URL}"', "pub", "deny"),
    # -c, --comment and -d are switches in these commands, so the next word is a flag.
    (f'gh pr review 5 --comment --body "{HIT} see {PRIV_URL}"', "pub", "deny"),
    (f'gh pr review 5 -c -b "{HIT} see {PRIV_URL}"', "pub", "deny"),
    (f'gh pr create -d --title "{HIT}" --body "{PRIV_URL}"', "pub", "deny"),
    (f'gh pr merge 5 -d --body "{HIT} see {PRIV_URL}"', "pub", "deny"),
    # In these the same flags take a value: the comment is text, even a bare URL.
    (f'gh issue close 5 --comment "{HIT} see {PRIV_URL}"', "pub", "deny"),
    (f'gh pr close 5 -d -c "{HIT} see {PRIV_URL}"', "pub", "deny"),
    (f'gh issue reopen 5 -c "{PRIV_URL}" --comment "{HIT}"', "pub", "deny"),
    # The mirror: a private checkout's write that mentions a public URL.
    (f'gh issue comment 5 --body "{HIT} see {PUB_URL}"', "priv", "allow"),
    (f'gh pr review 5 --comment --body "{HIT} see {PUB_URL}"', "priv", "allow"),
    # No listed term: never refused, whatever the text mentions.
    (f'gh issue comment 5 --body "plain see {PRIV_URL}"', "pub", "allow"),
    (f'gh pr review 5 --comment --body "plain see {PRIV_URL}"', "pub", "allow"),
    # A URL given AS the target still decides where the write goes, after any other word.
    (f'gh issue comment {PRIV_URL} --body "{HIT}"', "pub", "allow"),
    (f'gh issue close --reason completed {PRIV_URL} --comment "{HIT}"', "pub", "allow"),
    # A switch never swallows the URL after it: read as a value flag, it would leave the
    # private checkout as the target of a write that goes to the public pull request.
    (f'gh pr merge -d {PUB_URL} --body "{HIT}"', "priv", "deny"),
    (f'gh pr review -c {PUB_URL} --body "{HIT}"', "priv", "deny"),
    (f'gh pr close -d {PUB_URL} -c "{HIT}"', "priv", "deny"),
    # A word is the target only as a whole URL, even in the value of a flag this guard
    # does not list.
    (f'gh issue edit 5 --milestone "Q4 {PRIV_URL}" --body "{HIT}"', "pub", "deny"),
]


@pytest.mark.parametrize("command,where,want", URL_ROWS, ids=[str(i) for i in range(len(URL_ROWS))])
def test_a_url_in_a_write_s_text_is_never_its_target(env, tmp_path, command, where, want):
    """A body that cites another repository's issue is everyday text. Read as the target, it
    sent a write with a listed term from a public checkout to a private repository's verdict."""
    checkout = repo(tmp_path, env, {"pub": "pub-org/pub-repo", "priv": "priv-org/priv-repo"}[where])
    assert bash(env, command, checkout)[0] == want


def test_a_clean_write_by_url_to_a_repository_named_like_a_term_passes(env, tmp_path):
    """The repository's name says where a write goes, not what it publishes: by -R, by the
    checkout and by URL alike."""
    issue_url = "https://github.com/pub-org/zephyr-widgets/issues/5"
    pull_url = "https://github.com/pub-org/zephyr-widgets/pull/5"
    for command in (
        f'gh issue comment {issue_url} --body "plain words"',
        f"gh issue close {issue_url}",
        f"gh pr ready {pull_url}",
        f"gh pr merge {pull_url} --squash --admin --match-head-commit " + "0" * 40,
    ):
        assert bash(env, command, tmp_path)[0] == "allow", command
    assert lookups(env) == []  # no hit, so no visibility call
    assert bash(env, f'gh issue comment {issue_url} --body "{HIT}"', tmp_path)[0] == "deny"


MERGE_LADDER = (
    "REPO=pub-org/pub-repo; N=5\n"
    'BR=$(gh pr view "$N" --repo "$REPO" --json headRefName --jq .headRefName)\n'
    'PH=$(gh api "repos/$REPO/pulls/$N" --jq .head.sha)\n'
    'RH=$(gh api "repos/$REPO/git/ref/heads/$BR" --jq .object.sha)\n'
    '[ "$PH" = "$RH" ] || { echo "REFUSE: head mismatch"; exit 1; }\n'
    "DELETE=--delete-branch\n"
    '[ -n "$BR" ] && STACKED=$(gh pr list --repo "$REPO" --base "$BR" --state open --json number '
    "--jq '.[].number') && [ -z \"$STACKED\" ] || { DELETE=\"\"; echo \"KEEPING $BR\"; }\n"
    '[ -n "$REPO" ] && [ -n "$N" ] && [ -n "$PH" ] || { echo "REFUSE: not all set"; exit 1; }\n'
    'gh pr merge "$N" --repo "$REPO" --squash --admin $DELETE --match-head-commit "$PH"'
)


def test_the_merge_gate_refuses_only_a_merge_that_carries_a_term(env, tmp_path):
    """Every merge runs `gh pr merge`, so a clean one must never be refused: plain, in the
    one-call ladder with its substitutions, or with no answer from GitHub at all."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    merge = f"gh pr merge 5 -R pub-org/pub-repo --squash --admin --delete-branch --match-head-commit {sha}"
    (tmp_path / "clean.md").write_text("plain words\n")
    for command in (
        merge,
        merge.replace("-R ", "--repo "),
        merge.replace(sha, '"$(gh pr view 5 --json headRefOid --jq .headRefOid)"'),
        f'SHA=$(gh api repos/pub-org/pub-repo/pulls/5 --jq .head.sha); {merge.split(" --match")[0]} --match-head-commit "$SHA"',
        merge + " --body-file " + str(tmp_path / "clean.md"),
        MERGE_LADDER,
    ):
        assert bash(env, command, tmp_path)[0] == "allow", command
    assert lookups(env) == []
    for flag in ("--subject", "--body"):
        assert bash(env, f'{merge} {flag} "{HIT}"', tmp_path)[0] == "deny", flag
    with_term = MERGE_LADDER.replace("--match-head-commit", f'--subject "{HIT}" --match-head-commit')
    assert bash(env, with_term, tmp_path)[0] == "deny"
