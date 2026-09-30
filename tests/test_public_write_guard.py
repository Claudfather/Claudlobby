"""The public-write guard, through the REAL hook script (lib/public-write-guard.sh).

It refuses a GitHub-bound write that would put a term from the host's list into a
PUBLIC repository, and lets the same write through to a private one. The terms
here are INVENTED; the real list is host configuration and never appears in a
repository. Visibility is answered by a fake `gh` that logs every lookup, so a
test can also pin that a write without a hit asks nothing."""

from __future__ import annotations

import json
import os
import subprocess
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
}

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_GH_LOG"], "a").write(" ".join(sys.argv[1:]) + "\n")
vis = json.load(open(os.environ["FAKE_GH_VIS"]))
if len(sys.argv) > 2 and sys.argv[1] == "api" and sys.argv[2].startswith("repos/"):
    key = sys.argv[2][len("repos/"):]
    if key in vis:
        print(vis[key]); sys.exit(0)
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
