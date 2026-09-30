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


def _email(m: re.Match) -> bool:
    local, domain = m.group(1).lower(), m.group(2).lower()
    if _RESERVED_DOMAIN.search(domain) or local in _PLACEHOLDER_LOCALS or "your" in domain:
        return False  # a reserved domain, or a stand-in (you@yourdomain.com)
    if "noreply" in local or "no-reply" in local:
        return False  # a service's sender, not a person
    return not (local == "git" and domain in ("github.com", "gitlab.com"))


_HOME_PLACEHOLDERS = {"me", "you", "runner", "someone", "foo", "bar", "alice", "bob", "operator", "shared", "root"}


def _home(m: re.Match) -> bool:
    """A real account name, not a stand-in: one or two letters, a word such as
    user or your_username, or macOS's own /Users/Shared are stand-ins."""
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


def _uuid(m: re.Match) -> bool:
    """A random UUID uses about 14 of the 16 hex digits and has no long run of
    zeros; a stand-in (all zeros, ``aaaaaaaa-bbbb-…``, ``12345678-1234-5678-…``,
    ``5f0c2d1e-0000-4000-8000-000000000001``) has one or the other."""
    hexes = m.group(0).replace("-", "").lower()
    return len(set(hexes)) > 8 and "0000000" not in hexes


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

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff: str):
    """(path, line number, text) for every line a unified diff adds, and
    (path, 0, path) once per file, for the path itself."""
    path, line, seen = None, 0, set()
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = (
                None
                if target == "/dev/null"
                else target[2:]
                if target.startswith("b/")
                else target
            )
            if path and path not in seen:
                seen.add(path)
                yield path, 0, path
            continue
        m = _HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path and raw.startswith("+"):
            yield path, line, raw[1:]
            line += 1


def _git(*args: str) -> str:
    p = subprocess.run(["git", *args], capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise CannotCheck(f"git {args[0]} failed (exit {p.returncode})")
    return p.stdout


def diff_of(base: str, head: str) -> str:
    """The change as data: never a checkout of it, never a program from it."""
    return _git(
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        "-M",
        f"{base}...{head}",
        "--",
    )


def all_lines():
    for path in _git("ls-files", "-z").split("\0"):
        if not path:
            continue
        yield path, 0, path
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary, or gone
        for n, ln in enumerate(text.splitlines(), 1):
            yield path, n, ln


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


def scan(lines, allow, terms):
    """[(path, line, label)]: a class name for layer 1, ``private term #n`` for layer 2."""
    hits = []
    for path, n, text in lines:
        for cls in layer1(text):
            if not allowed(path, cls, allow):
                hits.append((path, n, cls))
        for i in layer2(text, terms):
            hits.append((path, n, f"private term #{i}"))
    return hits


def report(hits, out=sys.stdout) -> None:
    for path, n, label in hits:
        where = f"{path}:{n}" if n else f"{path} (the path)"
        print(f"{where}: {label}", file=out)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            line = f",line={n}" if n else ""
            print(f"::error file={path}{line}::leak-check: {label}", file=out)
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--git", nargs=2, metavar=("BASE", "HEAD"))
    src.add_argument("--diff", metavar="FILE")
    src.add_argument("--all", action="store_true")
    ap.add_argument(
        "--allow",
        default=".github/leak-check-allow.txt",
        help="allowlist path; with --git it is read from HEAD",
    )
    ap.add_argument(
        "--terms-optional",
        action="store_true",
        help="run layer 1 alone when no private list is set (local runs)",
    )
    args = ap.parse_args(argv)
    try:
        terms = load_terms(
            os.environ.get("LEAK_CHECK_TERMS"), required=not args.terms_optional
        )
        if args.git:
            base, head = args.git
            try:
                allow_text = _git("show", f"{head}:{args.allow}")
            except CannotCheck:
                allow_text = ""  # no allowlist in the change: nothing is allowed
            lines = list(added_lines(diff_of(base, head)))
        elif args.diff:
            allow_text = (
                Path(args.allow).read_text() if Path(args.allow).is_file() else ""
            )
            try:
                lines = list(added_lines(Path(args.diff).read_text(errors="replace")))
            except OSError:
                raise CannotCheck("the diff could not be read") from None
        else:
            allow_text = (
                Path(args.allow).read_text() if Path(args.allow).is_file() else ""
            )
            lines = list(all_lines())
        allow = load_allow(allow_text)
    except CannotCheck as exc:
        print(f"leak-check: CANNOT CHECK: {exc}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::leak-check: {exc}", file=sys.stderr)
        return 2
    if not terms:
        print(
            "leak-check: layer 2 not armed (--terms-optional): layer 1 only",
            file=sys.stderr,
        )
    hits = scan(lines, allow, terms)
    report(hits)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
