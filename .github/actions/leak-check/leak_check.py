#!/usr/bin/env python3
"""leak-check: refuse a change that adds use-case-specific data (#2042).

Two layers, one verdict:

1. Generic patterns, kept here: Telegram chat ids and bot tokens, GitHub and
   vendor API tokens, email addresses outside the reserved example domains,
   home paths, IPv4 addresses outside localhost and the documentation ranges,
   and UUIDs. The spec is CLAUDE.md's "No PII in committed assets".
2. An operator's private terms, which never go in a repository: one
   case-insensitive regular expression per line (``#`` starts a comment line),
   read from the environment variable ``LEAK_CHECK_TERMS`` (an Actions secret).

It reads ADDED lines only, and the paths of files the change touches, so what
is already committed does not fail every change. ``--all`` reads every tracked
file instead, to measure a tip.

OUTPUT NEVER CARRIES WHAT MATCHED. A hit prints ``path:line: <class>`` for
layer 1 and ``path:line: private term #<n>`` for layer 2, where n is the
pattern's index in the list: never the line, the matched text or a pattern.
Actions logs on a public repository are world-readable, and log masking hides
only a secret's verbatim value, never text a regular expression matched.

A layer-1 false positive has a visible, reviewable escape: the repository's
allowlist (``<path glob> <class> <reason>``), read from the change itself, so an
entry is part of what is reviewed. Layer 2 has none.

Fails closed: an unset or empty list, a list line that does not compile or
matches an empty string, or a diff it cannot read exits 2 and says which.

usage:
  leak_check.py --git BASE HEAD [--allow PATH] [--terms-optional]
  leak_check.py --diff FILE [--allow FILE] [--terms-optional]
  leak_check.py --all [--allow FILE] [--terms-optional]
exit: 0 clean, 1 hits, 2 could not check.
"""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

CLASSES = (
    "telegram-chat-id",
    "telegram-bot-token",
    "github-token",
    "api-token",
    "email",
    "home-path",
    "ip-address",
    "uuid",
)

# --- layer 1 ------------------------------------------------------------------

_SEQ = "0123456789012345678901234567890"
_RSEQ = _SEQ[::-1]


def _placeholder_digits(d: str) -> bool:
    """A number anyone would read as a stand-in: one repeated digit, or a run
    such as 1234567890 (the docs' ``-1001234567890``)."""
    return len(set(d)) <= 1 or d in _SEQ or d in _RSEQ


def _placeholder_token(body: str) -> bool:
    """A token body that is a stand-in: at most three distinct characters
    (``ghp_xxxx…``, ``AAAA…``), or a word that says so."""
    low = body.lower()
    return len(set(body)) <= 3 or any(
        w in low for w in ("example", "placeholder", "fake", "dummy", "redacted", "your")
    )


def _telegram_chat_id(m: re.Match) -> bool:
    return not _placeholder_digits(m.group(1))


def _context_id(m: re.Match) -> bool:
    v = m.group(1)
    v = v[4:] if v.startswith("-100") else v.lstrip("-")  # a supergroup's -100 prefix
    return not _placeholder_digits(v)


def _bot_token(m: re.Match) -> bool:
    return not (_placeholder_digits(m.group(1)) or _placeholder_token(m.group(2)))


def _token(m: re.Match) -> bool:
    return not _placeholder_token(m.group("body"))


_RESERVED_DOMAIN = re.compile(
    r"(?:^|\.)(?:example\.(?:com|org|net)|example|invalid|test|localhost)$", re.I
)


_PLACEHOLDER_LOCALS = {"someone", "user", "username", "you", "me", "name", "foo", "bar", "test"}
_UNIT_SUFFIX = re.compile(
    r"\.(?:service|socket|timer|target|mount|automount|path|scope|slice|swap|device)$"
)


def _email(m: re.Match) -> bool:
    local, domain = m.group(1).lower(), m.group(2).lower()
    if _RESERVED_DOMAIN.search(domain) or local in _PLACEHOLDER_LOCALS or "your" in domain:
        return False  # a reserved domain, or a stand-in (you@yourdomain.com)
    if "noreply" in local or "no-reply" in local:
        return False  # a service's sender, not a person
    if _UNIT_SUFFIX.search(domain):
        return False  # a systemd unit instance (name@instance.service), not an address
    return not (local == "git" and domain in ("github.com", "gitlab.com"))


_HOME_PLACEHOLDERS = {"me", "you", "runner", "someone", "foo", "bar", "alice", "bob", "operator", "shared", "root"}


def _home(m: re.Match) -> bool:
    """A real account name, not a stand-in: one or two letters, a word such as
    user or your_username, or macOS's own Shared folder are stand-ins."""
    name = m.group(2).rstrip(".").lower()
    if len(name) <= 2 or name in _HOME_PLACEHOLDERS:
        return False
    return not any(w in name for w in ("user", "your", "example", "name"))


_DOC_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "0.0.0.0/32",
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
    )
]


_PUBLIC_RESOLVERS = {"8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9"}


def _ip(m: re.Match) -> bool:
    try:
        ip = ipaddress.ip_address(m.group(0))
    except ValueError:
        return False  # 999.1.1.1: not an address
    if m.group(0) in _PUBLIC_RESOLVERS:
        return False  # the whole internet's, not anyone's
    return not any(ip in n for n in _DOC_NETS)


#: The documentation examples everyone copies: Wikipedia's and RFC 4122's own.
_TEXTBOOK_UUIDS = {"550e8400e29b41d4a716446655440000", "f81d4fae7dec11d0a76500a0c91e6bf6"}


def _uuid(m: re.Match) -> bool:
    """A random UUID uses about 14 of the 16 hex digits, has no long run of
    zeros and never runs in order; a stand-in (all zeros, ``aaaaaaaa-bbbb-…``,
    ``12345678-1234-5678-…``, ``5f0c2d1e-0000-4000-8000-000000000001``,
    ``00112233-4455-6677-8899-aabbccddeeff``) has one of those, or is a textbook
    example."""
    hexes = m.group(0).replace("-", "").lower()
    digits = [int(c, 16) for c in hexes]
    ordered = digits == sorted(digits) or digits == sorted(digits, reverse=True)
    return (len(set(hexes)) > 8 and "0000000" not in hexes and not ordered
            and hexes not in _TEXTBOOK_UUIDS)


LAYER1: list[tuple[str, re.Pattern, object]] = [
    (
        "telegram-bot-token",
        re.compile(r"(?<![\w:])(\d{8,10}):([A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])"),
        _bot_token,
    ),
    (
        "telegram-chat-id",
        re.compile(r"(?<![\w-])-100(\d{9,13})(?!\d)"),
        _telegram_chat_id,
    ),
    (
        "telegram-chat-id",
        re.compile(
            r"(?i)\b(?:chat|user|from|group)[_-]?ids?\b[\"']?\s*[:=]\s*[\"']?(-?\d{6,15})(?!\d)"
        ),
        _context_id,
    ),
    (
        "github-token",
        re.compile(
            r"\b(?:gh[pousr]_(?P<body>[A-Za-z0-9]{30,})|github_pat_(?P<body2>[A-Za-z0-9_]{40,}))"
        ),
        lambda m: not _placeholder_token(m.group("body") or m.group("body2")),
    ),
    (
        "api-token",
        re.compile(
            r"(?<![A-Za-z0-9_-])(?:sk-(?:ant|proj|svcacct|admin|or)-|(?:sk|rk|pk)_(?:live|test)_"
            r"|xox[abprs]-|xapp-|glpat-|npm_|hf_|gsk_|napi_|ntn_|AKIA|ASIA|AIza)"
            r"(?P<body>[A-Za-z0-9_-]{16,})"
        ),
        _token,
    ),
    (
        "email",
        re.compile(
            r"(?<![\w.+%\\-])([\w.+%-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])"
        ),
        _email,
    ),
    (
        "home-path",
        re.compile(r"(?<![\w.-])/(home|Users)/([A-Za-z0-9][A-Za-z0-9._-]*)"),
        _home,
    ),
    ("ip-address", re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"), _ip),
    (
        "uuid",
        re.compile(
            r"(?i)(?<![0-9a-f-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f-])"
        ),
        _uuid,
    ),
]


def layer1(text: str) -> list[str]:
    """The layer-1 classes ``text`` holds, each once."""
    found = []
    for cls, rx, real in LAYER1:
        if cls not in found and any(real(m) for m in rx.finditer(text)):
            found.append(cls)
    return found


# --- layer 2 ------------------------------------------------------------------

_PROBES = ("", "x", "plain words, 12.\n")


class CannotCheck(Exception):
    """Something this check needs is missing or broken: exit 2, never a pass."""


def load_terms(raw: str | None, required: bool) -> list[re.Pattern]:
    """The private list, one pattern per non-comment line. The reasons name a
    line number, never its text."""
    lines = [ln.strip() for ln in (raw or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        if required:
            raise CannotCheck(
                "layer 2 is NOT ARMED: the private list is empty or unset. Set the "
                "LEAK_CHECK_TERMS Actions secret (one case-insensitive pattern per line)."
            )
        return []
    out = []
    for n, p in enumerate(lines, 1):
        try:
            rx = re.compile(f"(?:{p})", re.IGNORECASE)
        except re.error:
            raise CannotCheck(f"private list pattern #{n} does not compile") from None
        if any(m.start() == m.end() for probe in _PROBES for m in rx.finditer(probe)):
            raise CannotCheck(f"private list pattern #{n} can match an empty string")
        out.append(rx)
    return out


def layer2(text: str, terms: list[re.Pattern]) -> list[int]:
    """The 1-based indexes of the private patterns ``text`` holds."""
    return [n for n, rx in enumerate(terms, 1) if rx.search(text)]


# --- the change -----------------------------------------------------------------
#
# A diff is read by its own structure, never by what a line looks like. Inside
# a hunk the counts in its @@ header say how many lines are content, so an added
# line that reads "++ b/some/path" (shown as "+++ b/some/path") is content like
# any other, and a header is honoured only outside a hunk. The text is decoded
# by hand and split on "\n" alone, as git writes it: text mode and
# str.splitlines would also end a line at a carriage return or at a character
# such as U+2028 inside it, and so desynchronise the counts.

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_QUOTED_PAIR = re.compile(r'^("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")$')
_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}

# Where a hit is, when it is not a line of text.
PATH, CONTENT = 0, -1


def _unquote(p: str) -> str:
    """A path as git writes it in a header: C-quoted when it holds a special
    character, and followed by a TAB when it holds a space."""
    if p.endswith("\t"):
        p = p[:-1]
    if len(p) < 2 or p[0] != '"' or p[-1] != '"':
        return p
    out, body, i = bytearray(), p[1:-1], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            octal = body[i + 1 : i + 4]
            if len(octal) == 3 and all(ch in "01234567" for ch in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            out += _ESCAPES.get(body[i + 1], body[i + 1]).encode()
            i += 2
            continue
        out += c.encode()
        i += 1
    return out.decode("utf-8", "replace")


def _new_side(p: str) -> str | None:
    p = _unquote(p)
    if p == "/dev/null":
        return None
    return p[2:] if p.startswith("b/") else p


def _header_name(rest: str) -> str | None:
    """The path a "diff --git a/X b/X" line names, when its two sides agree:
    the only header a new empty file or a mode-only change has."""
    m = _QUOTED_PAIR.match(rest)
    if m:
        a, b = _unquote(m.group(1)), _unquote(m.group(2))
    elif len(rest) % 2 and rest[len(rest) // 2] == " ":
        a, b = rest[: len(rest) // 2], rest[len(rest) // 2 + 1 :]
    else:
        return None
    return b[2:] if a.startswith("a/") and b.startswith("b/") and a[2:] == b[2:] else None


def read_diff(diff: str):
    """What a diff adds: ([(path, line number, text)] for every added line,
    [every path it adds, alters or renames to], [every binary file among them]).
    A deleted file's path is not added content. --git mode takes its paths from
    git's own list instead (changed_paths); a diff file has only its headers."""
    lines, paths, binaries = [], [], []
    path, n, old_left, new_left = None, 0, 0, 0
    named, deleted = None, False  # the current file section's new name, and whether it goes
    rows = diff.split("\n")
    if rows and rows[-1] == "":
        rows.pop()  # the end of the last line, not a line
    for raw in rows:
        if old_left > 0 or new_left > 0:
            tag, body = raw[:1], raw[1:]
            if tag == "\\":  # "\ No newline at end of file"
                continue
            if tag == "+" and new_left > 0:
                if not path:
                    raise CannotCheck("an added line in a section that names no file")
                lines.append((path, n, body))
                n, new_left = n + 1, new_left - 1
            elif tag == "-" and old_left > 0:
                old_left -= 1
            elif tag in (" ", "") and old_left > 0 and new_left > 0:
                n, old_left, new_left = n + 1, old_left - 1, new_left - 1
            else:
                raise CannotCheck("the diff does not match its own hunk counts")
            continue
        m = _HUNK.match(raw)
        if m:
            old_left = 1 if m.group(1) is None else int(m.group(1))
            n = int(m.group(2))
            new_left = 1 if m.group(3) is None else int(m.group(3))
        elif raw.startswith("diff --git "):
            if named and not deleted:
                paths.append(named)
            path, named, deleted = None, _header_name(raw[11:]), False
        elif raw.startswith("deleted file mode "):
            deleted = True
        elif raw.startswith("+++ "):
            path = _new_side(raw[4:])
            if path:
                paths.append(path)
        elif raw.startswith("+"):
            # git never writes an added line outside a hunk: this reader and the
            # diff disagree, and skipping the line would pass it unread
            raise CannotCheck("an added line outside any hunk")
        elif raw.startswith(("rename to ", "copy to ")):
            named = _unquote(raw.split(" to ", 1)[1])
        elif raw.startswith("Binary files ") and raw.endswith(" differ") and not deleted:
            if not named:
                raise CannotCheck("a binary file whose name this reader cannot tell")
            binaries.append(named)
    if old_left > 0 or new_left > 0:
        raise CannotCheck("the diff ends inside a hunk")
    if named and not deleted:
        paths.append(named)
    return lines, paths, binaries


def _git_bytes(*args: str) -> bytes:
    p = subprocess.run(["git", *args], capture_output=True)
    if p.returncode != 0:
        verb = next((a for a in args if not a.startswith("-") and "=" not in a), "?")
        raise CannotCheck(f"git {verb} failed (exit {p.returncode})")
    return p.stdout


def _git(*args: str) -> str:
    return _git_bytes(*args).decode("utf-8", "replace")


def diff_of(base: str, head: str) -> str:
    """The change as data: never a checkout of it, never a program from it, and
    in one format whatever the caller's git configuration."""
    return _git(
        "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
        "--no-textconv", "--no-relative", "--src-prefix=a/", "--dst-prefix=b/",
        "--ignore-submodules=none", "--submodule=short", "--unified=0", "-M",
        f"{base}...{head}", "--",
    )


def changed_paths(base: str, head: str) -> list[str]:
    """Every path the change adds, alters or renames to, git's own list: a pure
    rename, a binary file and a mode-only change have no +++ header to read."""
    out = _git("diff", "--name-only", "-z", "-M", "--diff-filter=d", "--no-relative",
               "--ignore-submodules=none", f"{base}...{head}", "--")
    return [p for p in out.split("\0") if p]


def blob(spec: str) -> str:
    """A binary file's bytes, one character each (latin-1)."""
    return _git_bytes("cat-file", "blob", spec).decode("latin-1")


def all_files():
    """--all: every tracked file as it stands in the working tree, as
    ([(path, line number, text)], [path], [(path, bytes)] for the binary ones)."""
    lines, paths, blobs, missing = [], [], [], 0
    for path in _git("ls-files", "-z").split("\0"):
        if not path:
            continue
        paths.append(path)
        p = Path(path)
        try:
            data = os.readlink(p).encode() if p.is_symlink() else p.read_bytes()
        except OSError:
            missing += 1  # in the index, gone from the working tree
            continue
        if b"\0" in data[:8000]:  # git's own test for a binary file
            blobs.append((path, data.decode("latin-1")))
        else:
            text = data.decode("utf-8", "replace")
            lines += [(path, n, ln) for n, ln in enumerate(text.split("\n"), 1)]
    if missing:
        print(f"leak-check: {missing} tracked file(s) missing from the working tree were not read",
              file=sys.stderr)
    return lines, paths, blobs


# --- the allowlist ----------------------------------------------------------------


def load_allow(text: str) -> list[tuple[str, str]]:
    """(path glob, class) pairs. Every entry names a class and gives a reason:
    an entry without one is refused, since the escape must be reviewable."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split(None, 2)
        if len(parts) < 3 or parts[1] not in CLASSES:
            raise CannotCheck(
                f"allowlist line {n}: want '<path glob> <class> <reason>', with a class from: "
                + ", ".join(CLASSES)
            )
        out.append((parts[0], parts[1]))
    return out


def allowed(path: str, cls: str, allow: list[tuple[str, str]]) -> bool:
    return any(c == cls and fnmatch.fnmatch(path, g) for g, c in allow)


# --- the verdict ------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_COMMAND = re.compile(r"^(\s*)(::|##\[)")


def _shown(path: str) -> str:
    """A path as printed: control characters escaped, and a leading "::" or
    "##[" too, so a crafted name can neither forge a line of this output nor
    start a workflow command."""
    s = _CONTROL.sub(lambda m: f"\\x{ord(m.group(0)):02x}", path)
    return _COMMAND.sub(lambda m: f"{m.group(1)}\\x{ord(m.group(2)[0]):02x}{m.group(2)[1:]}", s)


def _prop(s: str) -> str:
    """A workflow-command property value, encoded as the runner expects."""
    return (s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            .replace(":", "%3A").replace(",", "%2C"))


def _msg(s: str) -> str:
    """A workflow-command message, encoded as the runner expects."""
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def scan(lines, paths, allow, terms, blobs=()):
    """[(path, where, label)]: where is a line number, PATH (the path itself) or
    CONTENT (a binary file's bytes); label is a layer-1 class or ``private term
    #n``. A binary file's bytes are checked against the private list only: raw
    bytes, so text a format compresses or stores as UTF-16 is not seen."""
    hits = []
    for path in dict.fromkeys(paths):
        hits += [(path, PATH, c) for c in layer1(path) if not allowed(path, c, allow)]
        hits += [(path, PATH, f"private term #{i}") for i in layer2(path, terms)]
    for path, n, text in lines:
        hits += [(path, n, c) for c in layer1(text) if not allowed(path, c, allow)]
        hits += [(path, n, f"private term #{i}") for i in layer2(text, terms)]
    for path, data in blobs:
        hits += [(path, CONTENT, f"private term #{i}") for i in layer2(data, terms)]
    return hits


def _in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def note_allowlist_edit(allow_path: str, paths, out=sys.stdout) -> None:
    """The allowlist is read from the change itself, so a change can exempt its
    own hits: say so where a reviewer cannot miss it."""
    rel = os.path.normpath(allow_path)
    if rel not in paths:
        return
    print(f"leak-check: this change edits the allowlist ({_shown(rel)}): review its entries", file=out)
    if _in_actions():
        print(f"::notice file={_prop(rel)}::{_msg('leak-check: this change edits the allowlist; review its entries')}", file=out)
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a") as fh:
            fh.write("**This change edits the allowlist.** Review its entries.\n\n")


def report(hits, out=sys.stdout) -> None:
    for path, n, label in hits:
        shown = _shown(path)
        where = {PATH: f"{shown} (the path)", CONTENT: f"{shown} (binary content)"}.get(n, f"{shown}:{n}")
        print(f"{where}: {label}", file=out)
        if _in_actions():
            line = f",line={n}" if n > 0 else ""
            print(f"::error file={_prop(path)}{line}::{_msg('leak-check: ' + label)}", file=out)
    counts = {}
    for _, _, label in hits:
        counts[label] = counts.get(label, 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
    print(f"leak-check: {len(hits)} hit(s): {summary}", file=out)
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a") as fh:
            fh.write("### leak-check\n\n")
            fh.write("| rule | hits |\n|---|---|\n")
            for k, v in sorted(counts.items()):
                fh.write(f"| {k} | {v} |\n")
            if not counts:
                fh.write("| (none) | 0 |\n")


def advise(allow_path: str, out=sys.stdout) -> None:
    """What to do about a hit, said where the failure is read."""
    text = (f"replace each value with an obvious placeholder. A false positive of a pattern "
            f"class can be allowed in {_shown(allow_path)} as `<path glob> <class> <reason>`, "
            f"reviewed in the same diff; a private term cannot be allowed.")
    print(f"leak-check: {text}", file=out)
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a") as fh:
            fh.write(f"\n{text[0].upper()}{text[1:]}\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--git", nargs=2, metavar=("BASE", "HEAD"))
    src.add_argument("--diff", metavar="FILE")
    src.add_argument("--all", action="store_true")
    ap.add_argument("--allow", default=".github/leak-check-allow.txt",
                    help="allowlist path; with --git it is read from HEAD")
    ap.add_argument("--terms-optional", action="store_true",
                    help="run layer 1 alone when no private list is set (local runs)")
    args = ap.parse_args(argv)
    try:
        terms = load_terms(os.environ.get("LEAK_CHECK_TERMS"), required=not args.terms_optional)
        if args.git:
            base, head = args.git
            try:
                allow_text = _git("show", f"{head}:{args.allow}")
            except CannotCheck:
                allow_text = ""  # no allowlist in the change: nothing is allowed
            lines, _, binaries = read_diff(diff_of(base, head))
            paths = changed_paths(base, head)
            blobs = [(p, blob(f"{head}:{p}")) for p in binaries]
        elif args.diff:
            allow_text = Path(args.allow).read_text() if Path(args.allow).is_file() else ""
            try:
                diff = Path(args.diff).read_bytes().decode("utf-8", "replace")
            except OSError:
                raise CannotCheck("the diff could not be read") from None
            lines, paths, binaries = read_diff(diff)
            blobs = None  # a diff carries no binary content
        else:
            allow_text = Path(args.allow).read_text() if Path(args.allow).is_file() else ""
            lines, paths, blobs = all_files()
            binaries = [p for p, _ in blobs]
        allow = load_allow(allow_text)
    except CannotCheck as exc:
        print(f"leak-check: CANNOT CHECK: {exc}", file=sys.stderr)
        if _in_actions():
            print(f"::error::{_msg('leak-check: ' + str(exc))}", file=sys.stderr)
        return 2
    if not terms:
        print("leak-check: layer 2 not armed (--terms-optional): layer 1 only", file=sys.stderr)
    if binaries:
        how = ("their bytes checked against the private list only" if blobs is not None
               else "not checked: the diff does not carry their content")
        print(f"leak-check: {len(binaries)} binary file(s): {how}", file=sys.stderr)
    if not args.all:
        note_allowlist_edit(args.allow, paths)
    hits = scan(lines, paths, allow, terms, blobs or ())
    report(hits)
    if hits:
        advise(args.allow)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
