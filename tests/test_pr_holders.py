"""``claudlobby pr holders``: the open PRs that change given paths (#2833).

A fake gh stands in for GitHub. It answers the GraphQL listing and each REST
file read from one list of PRs, and it records every call, so a test can say
which reads ran. Nothing here reaches the network.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from claudlobby import pr_holders
from claudlobby.__main__ import main
from tests.test_cli_loading import PARSE, _run

REPO = "octo/repo"
_TYPES = {"added": "ADDED", "removed": "DELETED", "modified": "MODIFIED",
          "renamed": "RENAMED", "copied": "COPIED", "changed": "CHANGED"}


def _pr(number, files, *, draft=False, base="main", updated="2026-11-16T08:00:00Z",
        changed=None, types=None):
    """``files`` are (name, previous name, REST status) rows, in GitHub's order."""
    return {"number": number, "draft": draft, "base": base, "updated": updated,
            "files": files, "changed": len(files) if changed is None else changed,
            "types": types or {}}


def _modified(*names):
    return [(name, None, "modified") for name in names]


class FakeGh:
    """GitHub as gh shows it: GraphQL pages of ``page`` PRs, ``inline`` files
    each, and REST file lists as ``--jq`` prints them, one row a line.
    ``rest`` maps a PR number to a row cap (an int) or a callable that answers
    the call itself; ``totals`` overrides each page's totalCount in turn."""

    def __init__(self, prs, *, page=50, inline=100, rest=None, totals=None, listing=None):
        self.prs, self.page, self.inline = prs, page, inline
        self.by_number = {pr["number"]: pr for pr in prs}
        self.rest, self.totals, self.listing = rest or {}, list(totals or ()), listing
        self.calls = []

    def __call__(self, cmd, **kwargs):
        assert cmd[0] == "gh" and kwargs.get("timeout") == pr_holders.GH_TIMEOUT_S
        self.calls.append(cmd)
        if cmd[1:3] == ["api", "graphql"]:
            return self.listing(cmd) if self.listing else self._graphql(cmd)
        if cmd[1:3] == ["api", "--paginate"]:
            return self._rest(cmd)
        raise AssertionError(f"unexpected gh call: {cmd}")

    def graphql_calls(self):
        return [call for call in self.calls if call[1:3] == ["api", "graphql"]]

    def rest_numbers(self):
        return [int(re.search(r"/pulls/(\d+)/files", call[3]).group(1))
                for call in self.calls if call[1:3] == ["api", "--paginate"]]

    def _graphql(self, cmd):
        fields = _fields(cmd)
        assert (fields["owner"], fields["name"]) == tuple(REPO.split("/"))
        assert "pullRequests(states: OPEN" in fields["query"]
        start = int(fields.get("after", "0"))
        chunk = self.prs[start:start + self.page]
        end = start + len(chunk)
        total = self.totals.pop(0) if self.totals else len(self.prs)
        body = {"data": {"repository": {"pullRequests": {
            "totalCount": total,
            "pageInfo": {"hasNextPage": end < len(self.prs), "endCursor": str(end)},
            "nodes": [self._node(pr) for pr in chunk]}}}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")

    def _node(self, pr):
        files = pr["files"]
        return {"number": pr["number"], "isDraft": pr["draft"], "baseRefName": pr["base"],
                "updatedAt": pr["updated"], "changedFiles": pr["changed"],
                "files": {"pageInfo": {"hasNextPage": len(files) > self.inline},
                          "nodes": [{"path": name,
                                     "changeType": pr["types"].get(name, _TYPES[status])}
                                    for name, _previous, status in files[:self.inline]]}}

    def _rest(self, cmd):
        found = re.fullmatch(rf"repos/{REPO}/pulls/(\d+)/files\?per_page=100", cmd[3])
        assert found and cmd[4:] == ["--jq", pr_holders._REST_FILES_JQ], cmd
        number = int(found.group(1))
        answer = self.rest.get(number)
        if callable(answer):
            return answer(cmd)
        files = self.by_number[number]["files"]
        if answer is not None:
            files = files[:answer]
        return subprocess.CompletedProcess(cmd, 0, _rows(files), "")


def _raise(exc):
    raise exc


def _rows(files):
    return "".join(json.dumps([name, previous, status]) + "\n"
                   for name, previous, status in files)


def _fields(cmd):
    fields, tokens = {}, iter(cmd)
    for token in tokens:
        if token == "-f":
            key, _, value = next(tokens).partition("=")
            fields[key] = value
    return fields


def _holders(monkeypatch, capsys, fake, *paths, text=False):
    monkeypatch.setattr(pr_holders.subprocess, "run", fake)
    argv = ([] if text else ["--json"]) + ["pr", "holders", *paths, "--repo", REPO]
    code = main(argv)
    out = capsys.readouterr()
    return code, (out.out if text else json.loads(out.out)), out.err


def _answer(result, path):
    [answer] = [answer for answer in result["data"]["paths"] if answer["path"] == path]
    return answer


def test_an_open_pr_that_changes_a_path_is_named_with_its_draft_state_base_and_update(
        monkeypatch, capsys):
    fake = FakeGh([_pr(11, _modified("README.md")),
                   _pr(12, _modified("a/x.py", "a/y.py"), draft=True, base="feature",
                       updated="2026-11-15T10:00:00Z"),
                   _pr(13, [("a/z.py", None, "added")])])
    code, result, _ = _holders(monkeypatch, capsys, fake, "a/x.py", "a/q.py")
    assert code == 0 and result["ok"] and result["command"] == "pr.holders"
    data = result["data"]
    assert (data["repo"], data["open_prs"], data["read_in_full"], data["files_read"]) == (
        REPO, 3, 3, 4)
    assert data["unreadable"] == []
    assert _answer(result, "a/x.py") == {"path": "a/x.py", "same_name": [], "holders": [{
        "number": 12, "draft": True, "base": "feature", "updated_at": "2026-11-15T10:00:00Z",
        "files": [{"name": "a/x.py", "previous_name": None, "status": "modified",
                   "matched_on": "name"}]}]}
    assert _answer(result, "a/q.py") == {"path": "a/q.py", "holders": [], "same_name": []}
    # One GraphQL page answered it all: no PR renamed a file or had more than 100.
    assert len(fake.graphql_calls()) == 1 and fake.rest_numbers() == []


def test_a_renamed_file_matches_on_its_previous_name_which_only_rest_gives(
        monkeypatch, capsys):
    fake = FakeGh([_pr(21, _modified("docs/a.md")),
                   _pr(22, [("new/x.py", "old/x.py", "renamed"), ("new/y.py", None, "added")])])
    code, result, _ = _holders(monkeypatch, capsys, fake, "old/x.py", "new/x.py")
    assert code == 0
    assert _answer(result, "old/x.py")["holders"][0]["files"] == [{
        "name": "new/x.py", "previous_name": "old/x.py", "status": "renamed",
        "matched_on": "previous_name"}]
    assert _answer(result, "new/x.py")["holders"][0]["files"][0]["matched_on"] == "name"
    # GraphQL names only the new path, so the PR with the rename alone is read again.
    assert fake.rest_numbers() == [22]


def test_a_change_type_this_read_does_not_know_is_read_again_through_rest(monkeypatch, capsys):
    fake = FakeGh([_pr(25, [("a/x.py", None, "changed")], types={"a/x.py": "SOMETHING_NEW"})])
    code, result, _ = _holders(monkeypatch, capsys, fake, "a/x.py")
    assert code == 0 and fake.rest_numbers() == [25]
    assert _answer(result, "a/x.py")["holders"][0]["files"][0]["status"] == "changed"


def test_a_pr_with_more_files_than_come_inline_is_read_through_every_rest_page(
        monkeypatch, capsys):
    many = _modified(*(f"src/f{index:03}.py" for index in range(150)))
    fake = FakeGh([_pr(31, many), _pr(32, _modified("src/f140.py"))])
    code, result, _ = _holders(monkeypatch, capsys, fake, "src/f130.py", "src/f140.py")
    assert code == 0 and result["data"]["files_read"] == 151
    assert [h["number"] for h in _answer(result, "src/f130.py")["holders"]] == [31]
    assert [h["number"] for h in _answer(result, "src/f140.py")["holders"]] == [31, 32]
    assert fake.rest_numbers() == [31]


def test_a_file_list_shorter_than_githubs_count_fails_closed_and_names_the_pr(
        monkeypatch, capsys):
    """REST lists at most 3000 files, so a larger PR is unreadable, never a pass;
    what the other PRs answered stays in data."""
    huge = _modified(*(f"gen/f{index:04}.txt" for index in range(3500)))
    fake = FakeGh([_pr(41, _modified("a/x.py")), _pr(42, huge)], rest={42: 3000})
    code, result, err = _holders(monkeypatch, capsys, fake, "a/x.py")
    assert code == 6 and not result["ok"] and result["error"]["code"] == "unavailable"
    assert "#42" in result["error"]["message"] and "#41" not in result["error"]["message"]
    assert result["error"]["retryable"] is False
    data = result["data"]
    assert data["unreadable"] == [{"number": 42, "reason": "3000 files read of the 3500 "
                                                            "GitHub counts (REST lists at most 3000)"}]
    assert (data["open_prs"], data["read_in_full"]) == (2, 1)
    assert [h["number"] for h in _answer(result, "a/x.py")["holders"]] == [41]


@pytest.mark.parametrize(("answer", "reason", "retryable"), [
    # gh printed a whole first page, then failed on the next: only the exit says so.
    (lambda cmd: subprocess.CompletedProcess(
        cmd, 1, _rows(_modified(*(f"s/f{i:03}.py" for i in range(100)))),
        "gh: Server Error (HTTP 502)\n"), "gh exit 1, HTTP 502", True),
    (lambda cmd: subprocess.CompletedProcess(cmd, 1, '{"message":"Bad credentials"}',
                                             "gh: Bad credentials (HTTP 401)\n"),
     "gh exit 1, HTTP 401", False),
    (lambda cmd: _raise(subprocess.TimeoutExpired(cmd, 60)), "gh timed out after 60 s", True),
    (lambda cmd: subprocess.CompletedProcess(cmd, 0, "<html>unavailable</html>", ""),
     "the REST file list returned an unreadable body", True),
    (lambda cmd: subprocess.CompletedProcess(cmd, 0, '{"message":"Not Found"}\n', ""),
     "the REST file list has an unexpected shape", True),
    (lambda cmd: subprocess.CompletedProcess(
        cmd, 0, _rows(_modified(*(f"s/f{i:03}.py" for i in range(100)))
                      + _modified(*(f"s/f{i:03}.py" for i in range(50)))), ""),
     "the file list changed while it was read", True),
], ids=["http-502-after-a-first-page", "http-401", "timeout", "html-body", "error-object",
        "a-file-listed-twice"])
def test_a_rest_read_that_does_not_end_complete_leaves_its_pr_unreadable(
        monkeypatch, capsys, answer, reason, retryable):
    many = _modified(*(f"s/f{index:03}.py" for index in range(150)))
    fake = FakeGh([_pr(51, many)], rest={51: answer})
    code, result, _ = _holders(monkeypatch, capsys, fake, "s/f001.py")
    assert code == 6 and result["error"]["code"] == "unavailable"
    assert result["error"]["message"] == ("the file list of open PR #51 could not be read "
                                          "in full, so no path's holders are known")
    assert result["data"]["unreadable"] == [{"number": 51, "reason": reason}]
    assert result["error"]["retryable"] is retryable
    assert _answer(result, "s/f001.py")["holders"] == []


def test_the_listing_is_read_page_by_page_to_its_total_count(monkeypatch, capsys):
    prs = [_pr(60 + index, _modified(f"p/{index}.py")) for index in range(5)]
    fake = FakeGh(prs, page=2)
    code, result, _ = _holders(monkeypatch, capsys, fake, "p/4.py")
    assert code == 0 and result["data"]["read_in_full"] == 5
    assert [h["number"] for h in _answer(result, "p/4.py")["holders"]] == [64]
    assert [_fields(call).get("after") for call in fake.graphql_calls()] == [None, "2", "4"]


@pytest.mark.parametrize(("fake_kwargs", "reason"), [
    ({"page": 2, "totals": [5, 4, 4]}, "the open PR listing changed while it was read"),
    ({"page": 5, "totals": [6]}, "the open PR listing changed while it was read"),
    ({"totals": [pr_holders.MAX_OPEN_PRS + 1]},
     f"{pr_holders.MAX_OPEN_PRS + 1} open PRs, more than the {pr_holders.MAX_OPEN_PRS} "
     "this read covers"),
], ids=["total-drops-between-pages", "fewer-prs-than-the-total", "more-open-prs-than-the-cap"])
def test_a_listing_that_does_not_agree_with_its_total_count_fails_closed(
        monkeypatch, capsys, fake_kwargs, reason):
    prs = [_pr(70 + index, _modified(f"p/{index}.py")) for index in range(5)]
    code, result, _ = _holders(monkeypatch, capsys, FakeGh(prs, **fake_kwargs), "p/0.py")
    assert code == 6 and result["data"] == {}
    assert result["error"]["message"] == f"the open PRs of {REPO} could not be listed in full: {reason}"


@pytest.mark.parametrize(("listing", "reason"), [
    (lambda cmd: subprocess.CompletedProcess(
        cmd, 1, '{"message":"API rate limit exceeded"}',
        "gh: API rate limit exceeded (HTTP 403)\n"), "gh exit 1, HTTP 403"),
    # gh exits 1 on a GraphQL error and prints the error body on stdout.
    (lambda cmd: subprocess.CompletedProcess(
        cmd, 1, '{"errors":[{"message":"Could not resolve to a Repository"}]}',
        "gh: Could not resolve to a Repository\n"), "gh exit 1"),
    (lambda cmd: subprocess.CompletedProcess(
        cmd, 0, '{"data":null,"errors":[{"message":"Something went wrong"}]}', ""),
     "the open PR listing answered with an error"),
    (lambda cmd: subprocess.CompletedProcess(cmd, 0, '{"data":{"repository":null}}', ""),
     "the open PR listing has an unexpected shape"),
    (lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
     "the open PR listing returned an unreadable body"),
    (lambda cmd: _raise(FileNotFoundError("gh")), "gh could not run (FileNotFoundError)"),
], ids=["http-403", "graphql-error-exit-1", "graphql-error-exit-0", "no-repository",
        "empty-body", "gh-missing"])
def test_a_listing_that_cannot_be_read_is_unavailable_never_an_empty_answer(
        monkeypatch, capsys, listing, reason):
    code, result, _ = _holders(monkeypatch, capsys, FakeGh([], listing=listing), "a/x.py")
    assert code == 6 and not result["ok"] and result["data"] == {}
    assert result["error"]["message"] == f"the open PRs of {REPO} could not be listed in full: {reason}"


def test_a_file_with_the_same_name_at_another_path_is_listed_apart_not_as_a_holder(
        monkeypatch, capsys):
    """An open PR written before a directory move changes the file at its old path."""
    fake = FakeGh([_pr(81, _modified("lib/keepalive.sh", "lib/other.sh")),
                   _pr(82, [("tools/keepalive.sh", "bin/keepalive.sh", "renamed")]),
                   _pr(83, _modified("runtime/keepalive.sh"))])
    path = "runtime/keepalive.sh"
    code, result, _ = _holders(monkeypatch, capsys, fake, path)
    assert code == 0
    answer = _answer(result, path)
    assert [h["number"] for h in answer["holders"]] == [83]
    assert [(entry["number"], [(f["name"], f["matched_on"]) for f in entry["files"]])
            for entry in answer["same_name"]] == [
        (81, [("lib/keepalive.sh", "name")]), (82, [("tools/keepalive.sh", "name")])]


def test_the_text_answer_names_the_read_and_each_path(monkeypatch, capsys):
    fake = FakeGh([_pr(91, [("new/x.py", "old/x.py", "renamed")], draft=True,
                       updated="2026-11-15T10:00:00Z"),
                   _pr(92, _modified("lib/x.py"))])
    code, out, _ = _holders(monkeypatch, capsys, fake, "old/x.py", "other/y.py", text=True)
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("2 of 2 open PRs of octo/repo read in full, 2 files (")
    assert lines[1:] == [
        "old/x.py: #91 (draft, into main, updated 2026-11-15T10:00:00Z, renamed to new/x.py)",
        "  same file name at another path, not counted: #92 lib/x.py",
        "other/y.py: no open PR changes it"]


@pytest.mark.parametrize("path", [
    "/etc/passwd", "../x.py", "a//b.py", "a/", "./a.py", "a/./b.py", "", "b\n.py"])
def test_a_path_that_is_not_one_from_the_repository_root_is_refused_before_any_read(
        monkeypatch, capsys, path):
    fake = FakeGh([])
    monkeypatch.setattr(pr_holders.subprocess, "run", fake)
    code = main(["--json", "pr", "holders", "a/x.py", path, "--repo", REPO])
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["error"]["code"] == "invalid_argument"
    assert fake.calls == []


@pytest.mark.parametrize("repo", ["octo", "octo/repo/extra", "octo/..", "../repo", "oc to/repo"])
def test_a_repository_that_is_not_owner_slash_name_is_refused_before_any_read(
        monkeypatch, capsys, repo):
    fake = FakeGh([])
    monkeypatch.setattr(pr_holders.subprocess, "run", fake)
    code = main(["--json", "pr", "holders", "a/x.py", "--repo", repo])
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["error"]["message"] == "repository must be OWNER/REPO"
    assert fake.calls == []


def test_pr_holders_help_needs_only_stdlib(tmp_path):
    result = _run(PARSE, "pr", "holders", "--help", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "OWNER/REPO" in result.stdout and "PATH" in result.stdout


def test_a_syntax_error_is_argparses_refusal_until_the_public_table_names_the_route(tmp_path):
    """The table that turns a syntax error into a schema-1 result is
    ``_PUBLIC`` in claudlobby/command_result.py, which this change leaves alone.
    Until ``pr holders`` is in it, a syntax error is argparse's: exit 2 and
    nothing on stdout, even with --json. Every other answer is a result."""
    result = _run(PARSE, "--json", "pr", "holders", "--repo", REPO, tmp_path=tmp_path)
    assert result.returncode == 2 and result.stdout == ""
    assert "the following arguments are required: PATH" in result.stderr
