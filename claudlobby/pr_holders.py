"""The open PRs that change given paths, read from GitHub in one pass (#2833).

Before a manager gives a worker a file, it needs to know whether an open PR
already changes it. This module reads every open PR of a repository with its
files and matches the paths. It fails closed: a listing is taken as complete only
when it agrees with GitHub's own counts, and an open PR whose whole file list
cannot be read leaves the answer unreadable, never a pass.

The read:

- GraphQL lists the open PRs, ``PAGE_PRS`` at a time, oldest first: each PR's
  number, draft state, base branch, last update and changed-file count
  (``changedFiles``), with its first ``FILES_INLINE`` files (path and change
  type). Each page is one request.
- GraphQL names a renamed or copied file by its new path only. So a PR with
  such a file, or with more files than came inline, is read again through REST
  (``pulls/N/files``, every page), whose ``previous_filename`` is the old name.
- A PR's file list is read in full when the files read number exactly its
  ``changedFiles``, each once. REST lists at most 3000 files, so a larger PR is
  unreadable, and so is any other shortfall. The listing is read in full when it
  ends with as many PRs as its ``totalCount`` said on every page.
- A renamed or copied file that REST lists with no previous name leaves its PR
  unreadable too: its old path is unknown, so no path could match it.

Each gh call's exit status is read directly, never through a pipe, and its whole
body is parsed, because gh prints an error body on stdout too (#1066). A reason
recorded here is fixed wording with gh's exit status and HTTP status, never
gh's own text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import subprocess


#: Open PRs per GraphQL listing page, and the files that come inline with each.
PAGE_PRS = 50
FILES_INLINE = 100
#: A repository with more open PRs than this is refused, never read in part.
MAX_OPEN_PRS = 1000
#: Seconds one gh call may take. A REST call reads every page of one PR's files.
GH_TIMEOUT_S = 60
#: The most files REST lists for one PR (GitHub's "List pull requests files").
REST_FILES_CAP = 3000

REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

LISTING_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: %d, after: $after,
                 orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number isDraft baseRefName updatedAt changedFiles
        files(first: %d) { pageInfo { hasNextPage } nodes { path changeType } }
      }
    }
  }
}
""" % (PAGE_PRS, FILES_INLINE)

#: GraphQL's change types in REST's words, so both reads report one vocabulary.
_STATUS = {"ADDED": "added", "CHANGED": "changed", "COPIED": "copied",
           "DELETED": "removed", "MODIFIED": "modified", "RENAMED": "renamed"}
#: A file GraphQL names only by its new path: its old name is read through REST.
#: So is a PR with a change type this list does not know.
_NEEDS_OLD_NAME = frozenset({"renamed", "copied"})
_REST_FILES_JQ = ".[] | [.filename, .previous_filename, .status]"


class ReadUnavailable(Exception):
    """A read that could not be completed. ``reason`` is fixed wording, and
    ``transient`` says whether the same read could succeed when run again."""

    def __init__(self, reason: str, *, transient: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.transient = transient


@dataclass(frozen=True)
class ChangedFile:
    name: str
    previous_name: str | None
    status: str


@dataclass(frozen=True)
class OpenPr:
    number: int
    draft: bool
    base: str
    updated_at: str
    changed_files: int
    files: tuple[ChangedFile, ...]


@dataclass(frozen=True)
class Unreadable:
    number: int
    reason: str
    transient: bool


@dataclass(frozen=True)
class OpenPrRead:
    repo: str
    open_prs: int
    prs: tuple[OpenPr, ...]
    unreadable: tuple[Unreadable, ...]
    started_at: str
    finished_at: str


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gh(args: list[str]) -> str:
    """gh's stdout, or ReadUnavailable naming its exit and HTTP status."""
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                              timeout=GH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ReadUnavailable(f"gh timed out after {GH_TIMEOUT_S} s") from exc
    except OSError as exc:
        raise ReadUnavailable(f"gh could not run ({type(exc).__name__})",
                              transient=False) from exc
    if proc.returncode:
        status = re.search(r"\(HTTP ([0-9]{3})\)", proc.stderr or "")
        raise ReadUnavailable(
            f"gh exit {proc.returncode}" + (f", HTTP {status.group(1)}" if status else ""),
            transient=not (status and status.group(1) == "401"))
    return proc.stdout


def _values(text: str, what: str) -> list:
    """Every JSON value gh printed, back to back (one per line under --jq)."""
    decoder, values, at = json.JSONDecoder(), [], 0
    try:
        while True:
            while at < len(text) and text[at].isspace():
                at += 1
            if at == len(text):
                return values
            value, at = decoder.raw_decode(text, at)
            values.append(value)
    except ValueError as exc:
        raise ReadUnavailable(f"{what} returned an unreadable body") from exc


def _is_int(value, low: int) -> bool:
    return type(value) is int and value >= low


def _listing_page(owner: str, name: str, after: str | None) -> dict:
    args = ["api", "graphql", "-f", f"query={LISTING_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}"]
    if after is not None:
        args += ["-f", f"after={after}"]
    bodies = _values(_gh(args), "the open PR listing")
    if len(bodies) != 1 or not isinstance(bodies[0], dict):
        raise ReadUnavailable("the open PR listing returned an unreadable body")
    if bodies[0].get("errors"):
        raise ReadUnavailable("the open PR listing answered with an error")
    try:
        page = bodies[0]["data"]["repository"]["pullRequests"]
        shaped = (_is_int(page["totalCount"], 0)
                  and type(page["pageInfo"]["hasNextPage"]) is bool
                  and isinstance(page["nodes"], list))
    except (KeyError, TypeError):
        shaped = False
    if not shaped:
        raise ReadUnavailable("the open PR listing has an unexpected shape")
    for node in page["nodes"]:
        try:
            files = node["files"]
            shaped = (_is_int(node["number"], 1) and type(node["isDraft"]) is bool
                      and isinstance(node["baseRefName"], str)
                      and isinstance(node["updatedAt"], str)
                      and _is_int(node["changedFiles"], 0)
                      and (files is None
                           or (type(files["pageInfo"]["hasNextPage"]) is bool
                               and isinstance(files["nodes"], list)
                               and all(isinstance(f, dict) and isinstance(f.get("path"), str)
                                       and f["path"] and isinstance(f.get("changeType"), str)
                                       for f in files["nodes"]))))
        except (KeyError, TypeError):
            shaped = False
        if not shaped:
            raise ReadUnavailable("the open PR listing has an unexpected shape")
    return page


def _listing(repo: str) -> tuple[int, list[dict]]:
    """Every open PR's listing node, checked against the listing's totalCount."""
    owner, name = repo.split("/", 1)
    after, total, nodes = None, None, []
    for _ in range(MAX_OPEN_PRS // PAGE_PRS + 1):
        page = _listing_page(owner, name, after)
        if total is None:
            total = page["totalCount"]
            if total > MAX_OPEN_PRS:
                raise ReadUnavailable(f"{total} open PRs, more than the {MAX_OPEN_PRS} "
                                      "this read covers", transient=False)
        elif page["totalCount"] != total:
            raise ReadUnavailable("the open PR listing changed while it was read")
        nodes.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"].get("endCursor")
        if not isinstance(after, str) or not after:
            raise ReadUnavailable("the open PR listing has an unexpected shape")
    else:
        raise ReadUnavailable("the open PR listing did not end", transient=False)
    numbers = [node["number"] for node in nodes]
    if len(nodes) != total or len(set(numbers)) != len(numbers):
        raise ReadUnavailable("the open PR listing changed while it was read")
    return total, nodes


def _rest_files(repo: str, number: int) -> list[ChangedFile]:
    """One PR's files, every page, each with its previous name, through REST."""
    rows = _values(_gh(["api", "--paginate",
                        f"repos/{repo}/pulls/{number}/files?per_page=100",
                        "--jq", _REST_FILES_JQ]), "the REST file list")
    files = []
    for row in rows:
        if (not isinstance(row, list) or len(row) != 3
                or not isinstance(row[0], str) or not row[0]
                or not (row[1] is None or (isinstance(row[1], str) and row[1]))
                or not isinstance(row[2], str)):
            raise ReadUnavailable("the REST file list has an unexpected shape")
        if row[2] in _NEEDS_OLD_NAME and row[1] is None:
            raise ReadUnavailable(f"the REST file list has a {row[2]} file with no "
                                  "previous name")
        files.append(ChangedFile(row[0], row[1], row[2]))
    return files


def _shortfall(files: list[ChangedFile], changed: int) -> Unreadable | None:
    """Why ``files`` is not the PR's whole list, or None when it is."""
    names = [changed_file.name for changed_file in files]
    if len(set(names)) != len(names):
        return Unreadable(0, "the file list changed while it was read", True)
    if len(files) != changed:
        capped = changed > REST_FILES_CAP
        return Unreadable(0, f"{len(files)} files read of the {changed} GitHub counts"
                             + (f" (REST lists at most {REST_FILES_CAP})" if capped else ""),
                          not capped)
    return None


def read_open_prs(repo: str) -> OpenPrRead:
    """Every open PR of ``repo`` with its files. A PR whose whole file list could
    not be read is in ``unreadable``, never in ``prs``. Raises ReadUnavailable
    when the listing itself cannot be read in full."""
    started = _now()
    total, nodes = _listing(repo)
    prs, unreadable = [], []
    for node in nodes:
        number, changed, files = node["number"], node["changedFiles"], node["files"]
        listed = [] if files is None else files["nodes"]
        inline = [ChangedFile(f["path"], None, _STATUS.get(f["changeType"], "unknown"))
                  for f in listed]
        if (files is None or files["pageInfo"]["hasNextPage"] or len(inline) != changed
                or any(f["changeType"] not in _STATUS for f in listed)
                or any(f.status in _NEEDS_OLD_NAME for f in inline)):
            try:
                inline = _rest_files(repo, number)
            except ReadUnavailable as exc:
                unreadable.append(Unreadable(number, exc.reason, exc.transient))
                continue
        short = _shortfall(inline, changed)
        if short is not None:
            unreadable.append(Unreadable(number, short.reason, short.transient))
            continue
        prs.append(OpenPr(number, node["isDraft"], node["baseRefName"], node["updatedAt"],
                          changed, tuple(inline)))
    return OpenPrRead(repo, total, tuple(prs), tuple(unreadable), started, _now())


def _file_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _pr(pr: OpenPr, files: list[dict]) -> dict:
    return {"number": pr.number, "draft": pr.draft, "base": pr.base,
            "updated_at": pr.updated_at, "files": files}


def _entry(changed_file: ChangedFile, matched_on: str) -> dict:
    return {"name": changed_file.name, "previous_name": changed_file.previous_name,
            "status": changed_file.status, "matched_on": matched_on}


def match_paths(paths: list[str], read: OpenPrRead) -> list[dict]:
    """For each path: the PRs that change it, matched on each file's name and its
    previous name, and apart from those, the PRs that change a file with the same
    file name at another path (a PR written before a directory move changes the
    file at its old path). The second list holds candidates, not holders.

    A path is a file's path and a holder changes that file, so a directory has no
    holders. ``under`` lists the PRs that change files under the path and a slash,
    as a hint that it names a directory; it is not counted either. A PR that holds
    the path is listed as a holder only."""
    answers = []
    for path in paths:
        holders, same_name, under = [], [], []
        directory = path + "/"
        for pr in sorted(read.prs, key=lambda pr: pr.number):
            hits = [_entry(f, "name" if f.name == path else "previous_name")
                    for f in pr.files if path in (f.name, f.previous_name)]
            if hits:
                holders.append(_pr(pr, hits))
                continue
            near, inside = [], []
            for changed_file in pr.files:
                names = (("name", changed_file.name),
                         ("previous_name", changed_file.previous_name))
                for matched_on, name in names:
                    if name and _file_name(name) == _file_name(path):
                        near.append(_entry(changed_file, matched_on))
                        break
                for matched_on, name in names:
                    if name and name.startswith(directory):
                        inside.append(_entry(changed_file, matched_on))
                        break
            if near:
                same_name.append(_pr(pr, near))
            if inside:
                under.append(_pr(pr, inside))
        answers.append({"path": path, "holders": holders, "same_name": same_name,
                        "under": under})
    return answers
