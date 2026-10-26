"""Claudron path conformance gate (boundary phase L4, #2520 item 1).

Docs, docstrings, instruction files and one printed URL here name paths inside the Claudron repo.
Nothing checked that they exist, so when Claudron#231 moved its docs onto the ``documentation/``
layout every one of them broke at once and no check noticed. This gate finds each Claudron path or
URL this repo names and fails when one does not exist in a Claudron checkout at the revision
recorded in ``contracts/claudron-docs.ref``.

**The revision.** The file holds one full commit sha, in the way ``contracts/claudna.ref`` records
the clauDNA release the export contract is checked against. It does not hold a branch name, and
reading one is an error: a branch moves with Claudron, so a gate resolved against it turns every
Claudlobby pull request red the day Claudron moves a file, with no Claudlobby change to blame.
The ref moves when someone moves a reference here to a path that exists only at a newer Claudron
revision, and to the ``[vault]`` tag once a Claudron release carries the layout
(``documentation/integrations/claudron-integration.md``, "Version pin and bump policy"). The tag
cannot be the target before that: ``[vault]`` pins a release older than Claudron#231, so it still
holds the ``docs/`` paths this repo no longer names.

**What counts as a reference.** Four spellings, all in files this repo tracks:

* a ``github.com/Claudfather/Claudron`` URL to a file or folder (``blob``, ``tree`` or ``raw``);
* a path written after the word ``Claudron`` (``Claudron repo, <path>``, ``Claudron's <path>``,
  ``Claudron:<path>``, ``Claudron/<path>``, ``Claudron owns in <path>``), on the same line or on
  the next one, comment markers and indentation between them allowed;
* a path written before ``at the Claudron repo root``;
* a ``docs/`` or ``documentation/`` ``.md`` path with no marker, whose file name only Claudron has
  (this repo has no file of that name), so a path under ``docs/`` is read without a ``Claudron``
  beside it.

A path this repo has (as written, or relative to the file that names it) is this repo's own and is
not read. ``:line`` suffixes, ``#anchors`` and ``§sections`` are not part of the path, and no
anchor or section is checked.

**What it resolves.** A path with a folder must exist in the checkout as written, as a file or a
folder. A bare file name must exist somewhere in it, since a bare name follows its renamed file.
A file that Claudron renames, rather than moves, is read only when a marker or a URL sits beside
its path: the unmarked ``docs/`` and ``documentation/`` rule reads a bare name only when Claudron
has a file of that name.
A URL naming ``main``, ``master`` or ``HEAD`` is resolved at the ref and listed in a note: it can
pass here and still send a reader to a 404 after the next Claudron move. A URL naming any other
revision than the ref is listed and not checked, since the checkout holds one revision.

**What it skips, and says so.** Files that quote a path as it was, not as it is: everything under
``documentation/plans/``, any file whose name holds an ISO date, ``CHANGELOG.md``,
``tests/fixtures/``, ``.test_durations``, and this check's own module and test. The output counts
the skipped files by rule, and the files that are not UTF-8 text. A run that finds no reference at
all fails: a gate that reads nothing has not passed.

**Fails closed.** A checkout that is absent, or at another revision than the ref, is an error
(exit 2): a gate that cannot read Claudron has not passed, and a pass always names the revision it
was resolved at. In CI the job's checkout step has no ``continue-on-error``, so a failed fetch (an
outage, a rate limit, a ref file holding a sha Claudron does not have) fails the job instead of
skipping it. A re-run costs less than a green run that checked nothing.

Exit codes: 0 every reference resolves; 1 a reference does not resolve, or none was found; 2 a
usage error, an invalid ref file, an absent checkout, or a checkout at another revision.

Stdlib only, so CI runs it without installing the package:
``python3 -m claudlobby.claudron_refs --claudron <checkout of Claudron at the ref>``.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from typing import Collection, Iterable, Mapping, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The Claudron revision every reference is resolved at (one full sha, or a release tag).
DOCS_REF_FILE = Path(__file__).resolve().parent / "contracts" / "claudron-docs.ref"

#: Names a URL can give the default branch. They move, so a URL naming one is only noted.
MOVING_REFS = ("main", "master", "HEAD")

_SHA = re.compile(r"[0-9a-f]{40}")
_TAG = re.compile(r"v\d+\.\d+\.\d+")

_DATED = re.compile(r"\d{4}-\d{2}-\d{2}")
#: This check quotes stale-looking paths as data, in its module text and its tests.
_OWN_FILES = frozenset({"claudlobby/claudron_refs.py", "tests/test_claudron_refs.py"})

#: Folders the walk skips when the tree is not a git work tree (a git tree reads its index).
_WALK_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "build",
        "dist",
        ".claudron-checkout",
        ".claudna-checkout",
        ".claudna-export-checkout",
    }
)
_WALK_SKIP_TOP = frozenset({"local"})

_PATH = r"(?:[\w.-]+/)*[\w][\w.-]*\.(?:md|json|ya?ml|toml|txt|py|sh)(?![\w-])"
_OUTSIDE = r"(?<![\w/.$~{}@-])"

_URL_REF = r"[^/\s)>\]\"'`<]+"
_URL_PATH = r"[^\s)>\]\"'`<#?]+"
_URL = re.compile(
    r"https?://(?:www\.)?github\.com/Claudfather/Claudron/(?:blob|tree|raw)/"
    rf"(?P<ref>{_URL_REF})/(?P<path>{_URL_PATH})"
    r"|https?://raw\.githubusercontent\.com/Claudfather/Claudron/"
    rf"(?P<ref2>{_URL_REF})/(?P<path2>{_URL_PATH})"
)

#: What may stand between the word Claudron and the path it introduces.
_GAP = r"(?:/|:|[:,]?[ \t]+|[:,]?[ \t]*\r?\n[ \t]*(?:(?:\#|//|\*|>)[ \t]*)?)"
_MARKER = re.compile(
    r"(?<![\w/.-])Claudron(?:['’]s)?"
    r"(?:[ \t]+repo(?:sitory)?)?"
    r"(?:[ \t]+(?:owns|ships|keeps|documents|specifies|defines|publishes))?"
    r"(?:[ \t]+(?:in|at|under))?"
    rf"{_GAP}[`'\"(\[]{{0,2}}(?P<path>{_PATH})"
)
_AFTER = re.compile(
    rf"{_OUTSIDE}(?P<path>{_PATH})[`'\")\]]{{0,2}}[ \t]+(?:at|in|from)[ \t]+(?:the[ \t]+)?"
    r"Claudron(?:['’]s)?[ \t]+repo(?:sitory)?(?!\w)"
)
_NAME = re.compile(
    rf"{_OUTSIDE}(?P<path>(?:docs|documentation)/(?:[\w.-]+/)*[\w][\w.-]*\.md)(?![\w-])"
)


class RefFileError(ValueError):
    """``claudron-docs.ref`` is missing or does not hold one full sha or release tag."""


class Reference(NamedTuple):
    file: str  # the file of this repo that names the path
    line: int  # where the path is written
    kind: str  # "url", "marker" (after the word Claudron) or "name" (a docs path only Claudron has)
    path: str  # the Claudron path, as written
    ref: str | None = None  # the revision a URL names


class Resolution(NamedTuple):
    ok: list[Reference]  # found at the ref
    stale: list[Reference]  # not found at the ref
    moving: list[Reference]  # the found URLs that name main, master or HEAD
    elsewhere: list[Reference]  # URLs that name another revision, not checked


class Scan(NamedTuple):
    paths: frozenset[str]  # every file of this repo, read or not: what "this repo has" means
    files: dict[str, str]  # the text of the files the gate reads
    skipped: dict[str, int]  # files left unread, by rule


def read_docs_ref(path: Path = DOCS_REF_FILE) -> str:
    """The revision in the ref file: one full sha or one release tag, nothing that moves."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RefFileError(f"cannot read {path.name}: {exc}") from exc
    ref = text.strip()
    if not (_SHA.fullmatch(ref) or _TAG.fullmatch(ref)):
        raise RefFileError(
            f"{path.name} must hold one full commit sha (or a release tag such as v0.11.0), "
            f"not {ref!r}: a branch name moves with Claudron, and a gate that moves turns "
            "every unrelated pull request red"
        )
    return ref


def skipped(path: str) -> str | None:
    """The rule that leaves a file of this repo unread, or None when the gate reads it."""
    if path.startswith("documentation/plans/"):
        return "plan"
    if _DATED.search(path.rsplit("/", 1)[-1]):
        return "dated"
    if path == "CHANGELOG.md":
        return "changelog"
    if path.startswith("tests/fixtures/"):
        return "fixture"
    if path == ".test_durations":
        return "data"
    if path in _OWN_FILES:
        return "own"
    return None


def _tracked(root: Path) -> list[str] | None:
    """The files in git's index when ``root`` is itself the top of a work tree, else None."""
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root.resolve():
            return None
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=60, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in listing.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def _walked(root: Path) -> list[str]:
    found = []
    for folder, dirs, names in os.walk(root):
        rel = Path(folder).relative_to(root)
        skip = _WALK_SKIP_DIRS | (_WALK_SKIP_TOP if rel == Path(".") else frozenset())
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.endswith(".egg-info"))
        found.extend((rel / name).as_posix() for name in sorted(names))
    return found


def scan_repo(root: Path = REPO_ROOT) -> Scan:
    """Read the text of every file of the repo at ``root`` that the gate reads, and count the rest."""
    listed = _tracked(root)
    paths = frozenset(listed if listed is not None else _walked(root))
    files: dict[str, str] = {}
    left: dict[str, int] = {}
    for name in sorted(paths):
        rule = skipped(name)
        if rule is None and (root / name).is_file():
            try:
                files[name] = (root / name).read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                rule = "not text"
            except OSError:
                rule = "unreadable"
        if rule is not None:
            left[rule] = left.get(rule, 0) + 1
    return Scan(paths, files, left)


def tree_paths(checkout: Path) -> frozenset[str]:
    """Every file of a Claudron checkout, as a path from its root."""
    found = set()
    for folder, dirs, names in os.walk(checkout):
        dirs[:] = [d for d in dirs if d != ".git"]
        rel = Path(folder).relative_to(checkout)
        found.update((rel / name).as_posix() for name in names)
    return frozenset(found)


class _Site:
    """What tells this repo's own files from Claudron's, for one set of files."""

    def __init__(self, local: Collection[str], claudron: Collection[str]):
        self.local = set(local)
        self.local_bases = {p.rsplit("/", 1)[-1] for p in self.local}
        self.claudron_bases = {p.rsplit("/", 1)[-1] for p in claudron}
        md = {b for b in self.claudron_bases if b.endswith(".md")}
        self.only_claudron = md - self.local_bases

    def is_local(self, file: str, path: str) -> bool:
        """Whether a path is a file of this repo, read as written or relative to the file naming it."""
        if path.startswith(("/", "../")):
            return True
        path = path.removeprefix("./")
        if (
            path in self.local
            or posixpath.normpath(posixpath.join(posixpath.dirname(file), path)) in self.local
        ):
            return True
        return "/" not in path and path in self.local_bases and path not in self.claudron_bases


def _references_in(file: str, text: str, site: _Site) -> list[Reference]:
    found: list[Reference] = []
    claimed: list[tuple[int, int]] = []

    def free(start: int) -> bool:
        return not any(a <= start < b for a, b in claimed)

    def add(kind: str, path: str, start: int, ref: str | None = None) -> None:
        found.append(Reference(file, text.count("\n", 0, start) + 1, kind, path, ref))

    for m in _URL.finditer(text):
        group = "path" if m.group("path") else "path2"
        claimed.append(m.span())
        add(
            "url",
            m.group(group).rstrip(".,;:!/"),
            m.start(group),
            m.group("ref") or m.group("ref2"),
        )
    for rule in (_MARKER, _AFTER):
        for m in rule.finditer(text):
            path = m.group("path")
            if free(m.start()) and not site.is_local(file, path):
                claimed.append(m.span("path"))
                add("marker", path, m.start("path"))
    for m in _NAME.finditer(text):
        path = m.group("path")
        if (
            free(m.start("path"))
            and path.rsplit("/", 1)[-1] in site.only_claudron
            and not site.is_local(file, path)
        ):
            add("name", path, m.start("path"))
    return found


def find_references(
    files: Mapping[str, str], local_paths: Collection[str], claudron_paths: Collection[str]
) -> list[Reference]:
    """Every Claudron path or URL the given files name, once per file, line and path."""
    site = _Site(local_paths, claudron_paths)
    once: dict[tuple[str, int, str], Reference] = {}
    for file in sorted(files):
        for r in _references_in(file, files[file], site):
            once.setdefault((r.file, r.line, r.path), r)
    return sorted(once.values(), key=lambda r: (r.file, r.line, r.path))


def _names_ref(url_ref: str, ref: str) -> bool:
    """Whether a URL's revision is the ref (a short sha counts when it starts the ref's sha)."""
    if url_ref == ref:
        return True
    return (
        bool(_SHA.fullmatch(ref))
        and len(url_ref) >= 7
        and re.fullmatch(r"[0-9a-f]+", url_ref) is not None
        and ref.startswith(url_ref)
    )


def resolve(refs: Iterable[Reference], claudron_paths: Collection[str], ref: str) -> Resolution:
    """Sort references into those found at the ref, those not found, and URLs naming another revision."""
    paths = set(claudron_paths)
    folders = {p.rsplit("/", i)[0] for p in paths for i in range(1, p.count("/") + 1)}
    bases = {p.rsplit("/", 1)[-1] for p in paths}
    ok: list[Reference] = []
    stale: list[Reference] = []
    moving: list[Reference] = []
    elsewhere: list[Reference] = []
    for r in refs:
        if r.kind == "url" and r.ref not in MOVING_REFS and not _names_ref(r.ref or "", ref):
            elsewhere.append(r)
            continue
        if r.kind == "url" or "/" in r.path:
            hit = r.path in paths or r.path in folders
        else:
            hit = r.path in bases
        (ok if hit else stale).append(r)
        if hit and r.kind == "url" and r.ref in MOVING_REFS:
            moving.append(r)
    return Resolution(ok, stale, moving, elsewhere)


def _git_commit(checkout: Path, rev: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def verify_revision(checkout: Path, ref: str) -> tuple[str | None, str | None]:
    """(problem, note): a checkout that is not at the ref is a problem, one that cannot say is a note."""
    if not (checkout / ".git").exists():
        return None, "NOTE: the checkout has no .git, so its revision is not verified."
    head = _git_commit(checkout, "HEAD")
    if head is None:
        return f"cannot read the revision of the Claudron checkout {checkout}", None
    if head != _git_commit(checkout, ref):
        return f"the Claudron checkout is at {head[:12]}, not at the ref {ref[:12]}", None
    return None, None


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _lines(refs: Iterable[Reference]) -> list[str]:
    return [f"  {r.file}:{r.line}: {r.path} ({r.kind})" for r in refs]


def _url_note(urls: list[Reference], one: str, many: str, why: str) -> list[str]:
    if not urls:
        return []
    verb = one if len(urls) == 1 else many
    return [f"NOTE: {_count(len(urls), 'URL')} {verb} ({why}):", *_lines(urls)]


def check(root: Path, claudron: Path, ref: str, *, listing: bool = False) -> tuple[int, str]:
    """Resolve the repo's Claudron references in a checkout at ``ref`` -> (exit code, report)."""
    if not claudron.is_dir():
        return 2, (
            f"FAIL claudron-refs: the Claudron checkout {claudron} is absent, so there is nothing to "
            "resolve against. A gate that cannot read Claudron has not passed: check Claudron out at "
            "the ref (`python3 -m claudlobby.claudron_refs --print-ref`) and pass that folder."
        )
    problem, note = verify_revision(claudron, ref)
    if problem:
        return 2, f"FAIL claudron-refs: {problem}"
    scan = scan_repo(root)
    tree = tree_paths(claudron)
    refs = find_references(scan.files, scan.paths, tree)
    short = ref[:12]
    tail = []
    if scan.skipped:
        tail.append(
            "skipped files: " + ", ".join(f"{rule} {n}" for rule, n in sorted(scan.skipped.items()))
        )
    if note:
        tail.append(note)
    if not refs:
        read = _count(len(scan.files), "file")
        return 1, "\n".join(
            [
                f"FAIL claudron-refs: found no Claudron references in {read}, so there is nothing "
                "to check. A gate that reads nothing has not passed: either the finder is blind "
                "or this repo no longer names Claudron paths.",
                *tail,
            ]
        )
    res = resolve(refs, tree, ref)
    notes = [
        *_url_note(
            res.moving,
            "names Claudron main, which moves",
            "name Claudron main, which move",
            f"resolved at {short} only; a reader can still be sent to a 404 after the next move",
        ),
        *_url_note(
            res.elsewhere,
            "names another revision and was not checked",
            "name another revision and were not checked",
            f"the checkout holds {short} only",
        ),
    ]
    head = _lines(refs) if listing else []
    total = _count(len(refs), "reference")
    if res.stale:
        return 1, "\n".join(
            [
                *head,
                f"STALE claudron-refs: {len(res.stale)} of {total} not found at Claudron {short}:",
                *_lines(res.stale),
                "Fix: write the path where the file is at that revision, or move "
                "claudlobby/contracts/claudron-docs.ref to a Claudron revision that holds it.",
                *notes,
                *tail,
            ]
        )
    places = _count(len({r.file for r in refs}), "file")
    return 0, "\n".join(
        [
            *head,
            f"OK claudron-refs: {total} in {places}, all found at Claudron {short}.",
            *notes,
            *tail,
        ]
    )


_USAGE = (
    "usage: python3 -m claudlobby.claudron_refs --claudron <Claudron checkout at the ref> "
    "[--root <repo>] [--ref-file <file>] [--list]\n"
    "       python3 -m claudlobby.claudron_refs --print-ref [--ref-file <file>]"
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    opts: dict[str, str] = {}
    flags: set[str] = set()
    while args:
        arg = args.pop(0)
        if arg in ("--print-ref", "--list"):
            flags.add(arg)
        elif arg in ("--claudron", "--root", "--ref-file") and args:
            opts[arg] = args.pop(0)
        else:
            print(_USAGE, file=sys.stderr)
            return 2
    if "--print-ref" not in flags and "--claudron" not in opts:
        print(_USAGE, file=sys.stderr)
        return 2
    try:
        ref = read_docs_ref(Path(opts["--ref-file"]) if "--ref-file" in opts else DOCS_REF_FILE)
    except RefFileError as exc:
        print(f"claudron-refs: {exc}", file=sys.stderr)
        return 2
    if "--print-ref" in flags:
        print(ref)
        return 0
    root = Path(opts["--root"]) if "--root" in opts else REPO_ROOT
    code, report = check(
        root, Path(opts["--claudron"]).expanduser(), ref, listing="--list" in flags
    )
    print(report)
    return code


if __name__ == "__main__":  # pragma: no cover - module CLI
    raise SystemExit(main())
