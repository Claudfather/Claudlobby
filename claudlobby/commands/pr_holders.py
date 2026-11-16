"""``claudlobby pr holders``: the open PRs that change given paths (#2833).

A read of GitHub only. It needs no fleet, root or plane, changes nothing, and
answers with one schema-1 result. It exits 0 when every open PR's file list was
read in full, whether or not any PR changes a path. It exits 6 (unavailable)
when any open PR's file list could not be read in full: the error names each
such PR, and data keeps what was read. When the open-PR listing itself could not
be read in full, it exits 6 too, naming no PR: the error names the repository
and the reason, and data is empty. A bad argument exits 2.

A PATH is a file's path, and its holders are the PRs that change that file. When
PRs read change files under PATH and a slash, the answer lists them apart
(``under``), as a hint that PATH names a directory; its holders stay as they are.
"""

from __future__ import annotations

from ..command_result import CommandFailure, CommandOutput


def _valid_path(path) -> bool:
    """A file's path from the repository root, as GitHub lists it."""
    return (isinstance(path, str) and bool(path) and not path.startswith("/")
            and not path.endswith("/") and not any(char in path for char in "\x00\r\n")
            and all(part not in ("", ".", "..") for part in path.split("/")))


def _holder(entry: dict) -> str:
    moved = "".join(f", {changed['status']} to {changed['name']}" for changed in entry["files"]
                    if changed["matched_on"] == "previous_name")
    return (f"#{entry['number']} ({'draft, ' if entry['draft'] else ''}into {entry['base']}, "
            f"updated {entry['updated_at']}{moved})")


def _lines(data: dict) -> tuple[str, ...]:
    window = data["read_window"]
    lines = [f"{data['read_in_full']} of {data['open_prs']} open PRs of {data['repo']} read in "
             f"full, {data['files_read']} files ({window['started_at']} to "
             f"{window['finished_at']})"]
    for answer in data["paths"]:
        lines.append(f"{answer['path']}: " + (", ".join(_holder(entry) for entry in answer["holders"])
                                              or "no open PR changes it"))
        near = [f"#{entry['number']} "
                + (changed["name"] if changed["matched_on"] == "name" else changed["previous_name"])
                for entry in answer["same_name"] for changed in entry["files"]]
        if near:
            lines.append("  same file name at another path, not counted: " + ", ".join(near))
        inside = [f"#{entry['number']} ({len(entry['files'])} "
                  + ("file)" if len(entry["files"]) == 1 else "files)")
                  for entry in answer["under"]]
        if inside:
            lines.append(f"  files under {answer['path']}/, not counted: " + ", ".join(inside))
    return tuple(lines)


def dispatch(args) -> CommandOutput:
    from .. import pr_holders

    repo = args.repo
    if (not isinstance(repo, str) or not pr_holders.REPO_PATTERN.fullmatch(repo)
            or any(part in (".", "..") for part in repo.split("/"))):
        raise CommandFailure("invalid_argument", "repository must be OWNER/REPO")
    paths = list(dict.fromkeys(args.paths or ()))
    if not paths or not all(_valid_path(path) for path in paths):
        raise CommandFailure("invalid_argument",
                             "each PATH must be a file's path from the repository root")
    try:
        read = pr_holders.read_open_prs(repo)
    except pr_holders.ReadUnavailable as exc:
        raise CommandFailure("unavailable",
                             f"the open PRs of {repo} could not be listed in full: {exc.reason}",
                             retryable=exc.transient) from exc
    data = {"repo": repo, "open_prs": read.open_prs, "read_in_full": len(read.prs),
            "files_read": sum(len(pr.files) for pr in read.prs),
            "unreadable": [{"number": pr.number, "reason": pr.reason} for pr in read.unreadable],
            "read_window": {"started_at": read.started_at, "finished_at": read.finished_at},
            "paths": pr_holders.match_paths(paths, read)}
    if read.unreadable:
        numbers = ", ".join(f"#{pr.number}" for pr in read.unreadable)
        lists = ("the file list of open PR" if len(read.unreadable) == 1
                 else "the file lists of open PRs")
        raise CommandFailure(
            "unavailable", f"{lists} {numbers} could not be read in full, so no path's "
                           "holders are known",
            retryable=all(pr.transient for pr in read.unreadable), data=data)
    return CommandOutput(data, lines=_lines(data))
