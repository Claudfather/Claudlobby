#!/usr/bin/env python3
"""Changelog fragments: each PR adds its entry as one file, and a weekly roll-up
moves the files into CHANGELOG.md. changelog.d/README.md has the format.

    python3 bin/changelog.py check
    python3 bin/changelog.py assemble [--version X] [--date YYYY-MM-DD]

check runs in CI (.github/workflows/changelog.yml) at the root of a checkout that
holds the PR's history, and reads the pull_request event at $GITHUB_EVENT_PATH. It
passes a PR that adds an entry to changelog.d/<head branch, each / as ->.md, or
whose description has a line "Changelog: none — <why>". It refuses a fragment under
any other name, an entry heading outside CATEGORIES, and a new entry written into
CHANGELOG.md itself, unless the PR is a roll-up: one that deletes fragments and
leaves CHANGELOG.md exactly as assemble makes it from them.

assemble moves every fragment into CHANGELOG.md, directly under ## [Unreleased],
newest merge first (by the first-parent commit that added each), and deletes it.
With --version X the entries go under a new ## [X] - <date> heading instead,
together with whatever [Unreleased] already held.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CHANGELOG = "CHANGELOG.md"
FRAGMENTS = "changelog.d"
README = f"{FRAGMENTS}/README.md"
UNRELEASED = "## [Unreleased]"
CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security", "Docs")

HEADING = re.compile(r"^(#{1,6})\s")
ENTRY = re.compile(r"^### ")
VALID_ENTRY = re.compile(r"^### (?:%s)(?: — \S.*)?$" % "|".join(CATEGORIES))
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
VERSION_HEADING = re.compile(r"^## \[([^\]]+)\] - (\d{4}-\d{2}-\d{2})$")
VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
OPT_OUT = re.compile(r"^\s*(?:[-*]\s+)?\**changelog:\**\s*none\b(.*)$", re.IGNORECASE)
VACUOUS = {"", "n/a", "na", "none", "no", "nothing", "tbd", "todo", "-"}
EXAMPLE = (
    "    ### Fixed — what a reader would notice, in one line (#123)\n"
    "\n"
    "    What changed and why, in a few sentences.\n"
)


class Refusal(Exception):
    """Why a command cannot go on, worded for the person who has to fix it."""


def fragment_path(branch: str) -> str:
    """The one fragment a PR from this head branch adds."""
    return f"{FRAGMENTS}/{branch.replace('/', '-')}.md"


def _structure(text: str) -> tuple[list[tuple[int, str]], bool]:
    """(line number, line) for every line outside a fenced code block, the fence
    lines included, and whether a fence was left open."""
    lines, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match:
            mark = match.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip()[len(mark):]:
                fence = None
            lines.append((number, line))
        elif fence is None:
            lines.append((number, line))
    return lines, fence is not None


def entries(text: str) -> int:
    """How many ### entry headings the text holds outside code fences."""
    return sum(1 for _, line in _structure(text)[0] if ENTRY.match(line))


def fragment_problems(text: str) -> list[str]:
    """What is wrong with a fragment's text, one fix each."""
    lines, open_fence = _structure(text)
    content = [(number, line) for number, line in lines if line.strip()]
    if not content:
        return ["it holds no entry: start it with a heading such as `### Fixed — what changed (#123)`"]
    problems = []
    if not ENTRY.match(content[0][1]):
        problems.append(f"line {content[0][0]} comes before the first `### ` entry heading")
    for number, line in content:
        heading = HEADING.match(line)
        if not heading:
            continue
        level = len(heading.group(1))
        if level < 3:
            problems.append(
                f"line {number} is a level-{level} heading: {CHANGELOG} keeps `#` and `##` for "
                "itself, and an entry starts with `### `"
            )
        elif level == 3 and not VALID_ENTRY.match(line.rstrip()):
            problems.append(
                f"line {number}, `{line.strip()}`, must read `### <Category> — <what changed>`, "
                f"with one of these categories: {', '.join(CATEGORIES)}"
            )
    if open_fence:
        problems.append("a code fence is opened and never closed")
    return problems


def render(changelog: str, fragments: list[str], version: str | None = None,
           date: str | None = None) -> str:
    """The changelog with the fragments' text directly under ## [Unreleased], in the
    order given; with a version, under a new ## [version] - date heading there."""
    lines = changelog.split("\n")
    if UNRELEASED not in lines:
        raise Refusal(f"{CHANGELOG} has no `{UNRELEASED}` line to put the entries under")
    at = lines.index(UNRELEASED) + 1
    blocks = [text.strip("\n") for text in fragments]
    if version:
        blocks.insert(0, f"## [{version}] - {date}")
    if not blocks:
        return changelog
    before, after = "\n".join(lines[:at]), "\n".join(lines[at:]).lstrip("\n")
    return before + "\n\n" + "\n\n".join(blocks) + ("\n\n" + after if after else "\n")


def _first_below_unreleased(changelog: str) -> str | None:
    lines = changelog.split("\n")
    if UNRELEASED not in lines:
        return None
    return next((line for line in lines[lines.index(UNRELEASED) + 1:] if line.strip()), None)


def version_cut(old: str, new: str) -> tuple[str | None, str | None]:
    """The (version, date) of a ## [X] - date heading the new changelog adds directly
    below ## [Unreleased], else (None, None)."""
    line = _first_below_unreleased(new)
    match = VERSION_HEADING.match(line or "")
    if match and line != _first_below_unreleased(old):
        return match.group(1), match.group(2)
    return None, None


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, encoding="utf-8")
    if done.returncode != 0:
        raise Refusal(f"`git {' '.join(args)}` failed: {done.stderr.strip()}")
    return done.stdout


def show(repo: Path, rev: str, path: str) -> str | None:
    """The file at rev, or None where rev has no such file."""
    done = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=repo, capture_output=True,
                          encoding="utf-8")
    return done.stdout if done.returncode == 0 else None


def merge_order(repo: Path, rev: str, paths: list[str]) -> list[str]:
    """The paths, newest first by the commit on rev's first-parent line that added each."""
    position = {sha: i for i, sha in enumerate(git(repo, "rev-list", "--first-parent", rev).split())}
    keyed = []
    for path in paths:
        added = git(repo, "log", "--first-parent", "--diff-filter=A", "--format=%H", rev, "--",
                    path).split()
        if not added:
            raise Refusal(f"{path} is not in the history of {rev}: commit it before the roll-up")
        keyed.append((position[added[0]], path))
    return [path for _, path in sorted(keyed)]


def _visible_lines(body: str) -> list[str]:
    """The description's lines a reader sees: no code fences, no HTML comments."""
    text = "\n".join(line for _, line in _structure(body)[0] if not FENCE.match(line))
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).splitlines()


def opt_out(body: str) -> str | None:
    """The reason a description gives for needing no entry: None when it has no
    "Changelog: none" line, "" when that line gives no reason."""
    for line in _visible_lines(body):
        match = OPT_OUT.match(line)
        if match:
            reason = match.group(1).strip().lstrip("—–-:").strip().strip("*").strip()
            vacuous = reason.lower().rstrip(".") in VACUOUS or re.fullmatch(r"<[^>]*>", reason)
            return "" if vacuous else reason
    return None


def missing(own: str) -> str:
    """What a PR with no entry is told: the file to add and its format."""
    return (
        f"This PR adds no changelog entry. Add the file {own}, holding the entry as it "
        f"should read in {CHANGELOG}:\n\n{EXAMPLE}\n"
        f"The heading names one category: {', '.join(CATEGORIES)}. A file can hold more "
        "than one entry, each under its own ### heading. Do not edit "
        f"{CHANGELOG} itself: the weekly roll-up moves every fragment into it.\n"
        "If this PR needs no entry, add this line to its description instead: "
        "Changelog: none — <why>"
    )


def check(repo: Path, event: dict) -> tuple[list[str], str]:
    """(problems, what passed) for the pull request of a GitHub pull_request event."""
    pr = event["pull_request"]
    branch, head, base = pr["head"]["ref"], pr["head"]["sha"], pr["base"]["sha"]
    since = git(repo, "merge-base", base, head).strip()
    fields = git(repo, "diff", "--name-status", "--no-renames", "-z", since, head).split("\0")
    status = {path: letter[0] for letter, path in zip(fields[0::2], fields[1::2]) if letter}
    own = fragment_path(branch)
    fragments = {p: s for p, s in status.items() if p.startswith(FRAGMENTS + "/") and p != README}
    problems = [
        f"`{path}` is not this PR's fragment. Name it `{own}`: the head branch, "
        f"`{branch}`, with each `/` as `-`."
        for path, letter in sorted(fragments.items()) if letter == "A" and path != own
    ]
    added = 0
    if status.get(own) in ("A", "M"):
        text = show(repo, head, own) or ""
        problems += [f"`{own}`: {problem}." for problem in fragment_problems(text)]
        added = entries(text) - (entries(show(repo, since, own) or "") if status[own] == "M" else 0)
    old_log, new_log = show(repo, since, CHANGELOG) or "", show(repo, head, CHANGELOG) or ""
    deleted = sorted(p for p, s in fragments.items() if s == "D")
    if deleted:
        # A roll-up deletes fragments and needs no fragment of its own. Any other PR that
        # deletes one, such as a revert, must leave CHANGELOG.md alone and say why.
        version, date = version_cut(old_log, new_log)
        texts = [show(repo, since, path) or "" for path in merge_order(repo, since, deleted)]
        if new_log == render(old_log, texts, version, date):
            return problems, f"a roll-up of {len(deleted)} fragment(s), exactly as assemble makes it"
        if new_log != old_log or not opt_out(pr.get("body") or ""):
            problems.append(
                f"This PR deletes {len(deleted)} fragment(s), but {CHANGELOG} is not exactly what "
                "`python3 bin/changelog.py assemble` makes of them. A roll-up commits that "
                "result unchanged, from a branch off fresh main. Any other PR that deletes a "
                "fragment, such as a revert, leaves CHANGELOG.md alone and says why in its "
                "description: Changelog: none — <why>."
            )
            return problems, ""
    if entries(new_log) > entries(old_log):
        problems.append(
            f"This PR adds an entry to {CHANGELOG} itself. Move it into `{own}`: {CHANGELOG} "
            "takes its entries only from the weekly roll-up."
        )
    if added > 0:
        return problems, f"`{own}` adds {added} {'entry' if added == 1 else 'entries'}"
    reason = opt_out(pr.get("body") or "")
    if reason is None:
        problems.insert(0, missing(own))
    elif not reason:
        problems.append(
            'The description says "Changelog: none" but not why. Put the reason after it, on '
            "the same line: Changelog: none — <why this PR needs no entry>."
        )
    deletes = f", and it deletes {', '.join(deleted)}" if deleted else ""
    return problems, f'its description says why it needs no entry: "{reason}"{deletes}'


def assemble(repo: Path, version: str | None, date: str) -> str:
    """Do the roll-up in the checkout at repo, and say what it did."""
    dirty = git(repo, "status", "--porcelain", "--", CHANGELOG, FRAGMENTS)
    if dirty.strip():
        raise Refusal(f"{CHANGELOG} or {FRAGMENTS}/ has uncommitted changes. Commit or discard "
                      f"them first:\n{dirty.rstrip()}")
    folder = repo / FRAGMENTS
    paths = sorted(f"{FRAGMENTS}/{p.name}" for p in folder.iterdir()
                   if p.is_file() and f"{FRAGMENTS}/{p.name}" != README) if folder.is_dir() else []
    if not paths and not version:
        return f"{FRAGMENTS}/ holds no fragments: nothing to roll up."
    problems = [f"  {path}: a fragment is a .md file" for path in paths if not path.endswith(".md")]
    problems += [f"  {path}: {problem}" for path in paths if path.endswith(".md")
                 for problem in fragment_problems((repo / path).read_text(encoding="utf-8"))]
    if problems:
        raise Refusal("Fix these fragments before the roll-up:\n" + "\n".join(problems))
    ordered = merge_order(repo, "HEAD", paths)
    log = repo / CHANGELOG
    log.write_text(render(log.read_text(encoding="utf-8"),
                          [(repo / path).read_text(encoding="utf-8") for path in ordered],
                          version, date), encoding="utf-8")
    for path in paths:
        (repo / path).unlink()
    where = f"a new `## [{version}] - {date}` heading" if version else f"`{UNRELEASED}`"
    return (f"Moved {len(paths)} fragment(s) into {CHANGELOG} under {where}, newest merge "
            f"first, and deleted them. Commit {CHANGELOG} and the deletions as they are, and open "
            "the roll-up PR with `N/A: docs-only` under its `## Rollout check`.")


def _annotate(problems: list[str]) -> None:
    """Repeat each problem as a GitHub error annotation, shown on the PR's check."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for problem in problems:
        text = problem.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Changelog fragment::{text}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="changelog.py", description="Changelog fragments; changelog.d/README.md has the rules.")
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser(
        "check", help="CI: does this PR add its fragment, or say why it needs none?")
    check_parser.add_argument("--repo", type=Path, default=Path.cwd())
    check_parser.add_argument("--event", type=Path, default=os.environ.get("GITHUB_EVENT_PATH") or None)
    assemble_parser = commands.add_parser(
        "assemble", help="the weekly roll-up: move every fragment into CHANGELOG.md")
    assemble_parser.add_argument("--repo", type=Path, default=Path.cwd())
    assemble_parser.add_argument("--version", help="put the entries under ## [VERSION] - DATE")
    assemble_parser.add_argument("--date", help="the version's date (default: today, UTC)")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            if not args.event:
                raise Refusal("no pull_request event: set GITHUB_EVENT_PATH or pass --event")
            try:
                event = json.loads(args.event.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise Refusal(f"cannot read the event at {args.event}: {error}") from error
            if not isinstance(event, dict) or "pull_request" not in event:
                raise Refusal(f"{args.event} is not a pull_request event")
            problems, passed = check(args.repo, event)
            if problems:
                print("\n\n".join(problems))
                _annotate(problems)
                return 1
            print(f"Changelog fragment: passes, because {passed}.")
            return 0
        if args.version and not VERSION.match(args.version):
            raise Refusal(f"--version {args.version!r} is not a version such as 0.2.0")
        if args.date and not args.version:
            raise Refusal("--date only dates a --version heading")
        date = args.date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise Refusal(f"--date {date!r} is not YYYY-MM-DD")
        print(assemble(args.repo, args.version, date))
        return 0
    except Refusal as refusal:
        print(f"changelog.py {args.command}: {refusal}", file=sys.stderr)
        return 2 if args.command == "check" else 1


if __name__ == "__main__":
    sys.exit(main())
