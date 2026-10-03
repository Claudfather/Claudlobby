#!/usr/bin/env python3
"""heavy-slot.py — the host's heavy-job slot (#1686).

Heavy jobs stacked across fleets have stormed the primary host (load 25-57,
iowait up to 66%, swap full), and a prose rule saying "one at a time" cannot
hold across a score of bots in four fleets. So a bot that opted in
(`heavy_slot: true` in fleet.yaml) runs this module three ways:

``hook``
    The PreToolUse decider behind claudlobby/_runtime_scripts/heavy-slot-guard.sh. It reads the Bash
    tool call and puts the wrapper in front of each heavy command in it (a
    whole pytest or vitest suite, an npm/pnpm/yarn install, a test or build
    script, next build, Playwright, Chromium), leaving every other byte of the
    command alone. When every slot is taken, or a free slot is another
    caller's turn in the queue, it refuses the call before anything runs,
    naming the holder or the turn, so the bot retries instead of hanging
    behind a 15-minute suite.

``run -- ARGV``
    The wrapper. It takes a slot with a non-blocking flock on
    ``state/heavy-slot/slot-N.lock``, writes the holder into that file, runs
    the job, and writes the release. The kernel drops a flock the moment its
    holder dies (kill, tool timeout, OOM, host reset), so a dead holder can
    never wedge the slot. Its record stays, marked unreleased, and the next
    holder reports it: a different boot id means the job was running when the
    host reset, the evidence #1644 lacks. A call that cannot take a slot,
    because every slot is held or a free one is another caller's turn, takes
    a ticket in the queue (#2124) and exits 75.

``status [--json]``
    The one-line door: who holds each slot, or who held it last and whether
    they released it, and who waits in the queue, in the order they are served.

Bounds, stated rather than implied: the gate sees Bash tool calls only. A heavy
job started from inside a script (`make test`, `python render.py`) is not seen,
nor are dev servers, host timers or the Claudron doctor walk. A matched command
the matcher cannot parse with certainty is left untouched and counted
(`heavy_slot_unparsed`), because rewriting a command at a guessed position
would corrupt it, which is worse than missing the gate.

Knobs, all host-wide: ``state/heavy-slot/slots`` holds the slot count (default
1, the measured start); ``state/heavy-slot/disabled`` makes the hook pass every
call through at once, without a restart; ``state/heavy-slot/no-queue`` makes
every caller decide on the slots alone, as before the queue. The environment's
HEAVY_SLOT_TICKET_IDLE_S overrides TICKET_IDLE_S for the process that reads it.
Test seams: HEAVY_SLOT_DIR, HEAVY_SLOT_EVENTS_FILE, HEAVY_SLOT_BOOT_ID.

Stdlib only, Python 3.9 (the system python3 on the estate's macOS hosts).
"""

from __future__ import annotations

import calendar
import fcntl
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

EX_TEMPFAIL = 75  # every slot is taken: retry later
COMMAND_CAP = 200  # characters of a command kept in a record or event


class Unsure(Exception):
    """The matcher met a construct it does not parse: never rewrite."""


# --- reading a Bash command the way the shell does ---------------------------

# Only a region that could hold a heavy command makes an unparsed construct a
# problem worth refusing to guess about.
# pip and uv only as words: as substrings they would match pipe, pipefail and pipeline.
_HEAVY_WORD = re.compile(
    r"pytest|py\.test|vitest|npm|pnpm|yarn|npx|next|playwright|chrom|\bpip3?\b|\buv\b")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
_REDIR = re.compile(r"(\d+|\{[A-Za-z_][A-Za-z0-9_]*\})?(<<<|<<-|<<|<>|<&|>&|>>|>\||&>>|&>|<|>)")
_META = set(" \t\n;&|()<>")
_SPECIAL_PARAMS = set("?$#@*!-0123456789")

# At a command position these open or continue a compound command; the next
# word is again a command position.
_OPENERS = {"if", "then", "elif", "else", "while", "until", "do", "{", "!", "time"}
# These close one; what follows is not a command until an operator.
_CLOSERS = {"fi", "done", "esac", "}"}
# Not parsed: a heavy command inside one is never guessed at.
_UNPARSED = {"case", "function", "coproc", "select"}


class Word:
    """One shell word: where it sits, its text, and its value (None when the
    value depends on an expansion). Plain classes, not dataclasses: this file
    is loaded by path, and a dataclass looks itself up in sys.modules."""

    __slots__ = ("start", "end", "raw", "value")

    def __init__(self, start: int, end: int, raw: str, value: Optional[str]):
        self.start, self.end, self.raw, self.value = start, end, raw, value


class Simple:
    """One simple command: its words, assignments and redirections removed."""

    __slots__ = ("words",)

    def __init__(self):
        self.words: List[Word] = []


class _Scanner:
    def __init__(self, s: str):
        self.s = s
        self.n = len(s)
        self.pending: list = []  # heredocs waiting for their bodies
        self.commands: List[Simple] = []

    def parse(self) -> List[Simple]:
        i = self._list(0, closer=None)
        if i < self.n:
            raise Unsure("unbalanced )")
        if self.pending:
            raise Unsure("unterminated heredoc")
        return self.commands

    def _list(self, i: int, closer: Optional[str]) -> int:
        """A command list from i, until EOF or the closer: the index after it."""
        s, n = self.s, self.n
        cur: Optional[Simple] = None
        at_cmd = True
        dead = False  # after a compound's closer, until the next operator
        for_header = False

        def finish():
            nonlocal cur
            if cur is not None and cur.words:
                self.commands.append(cur)
            cur = None

        while i < n:
            c = s[i]
            if c in " \t":
                i += 1
                continue
            if c == "\\" and s.startswith("\\\n", i):
                i += 2
                continue
            if c == "\n":
                finish()
                i = self._heredocs(i + 1)
                at_cmd, dead, for_header = True, False, False
                continue
            if c == "#":
                j = s.find("\n", i)
                i = n if j < 0 else j
                continue
            if closer is not None and c == closer:
                finish()
                return i + 1
            if s.startswith(";;", i):
                raise Unsure("case")
            if s.startswith(("&&", "||"), i):
                finish()
                i += 2
                at_cmd, dead = True, False
                continue
            if c == ";":
                finish()
                i += 1
                at_cmd, dead, for_header = True, False, False
                continue
            if s.startswith("|&", i):
                finish()
                i += 2
                at_cmd, dead = True, False
                continue
            if c == "|":
                finish()
                i += 1
                at_cmd, dead = True, False
                continue
            if s.startswith(("<(", ">("), i):
                start = i
                i = self._list(i + 2, closer=")")
                if cur is None:
                    cur = Simple()
                cur.words.append(Word(start, i, s[start:i], None))
                at_cmd = False
                continue
            m = _REDIR.match(s, i)
            if m:
                i = self._redirection(m)
                continue
            if c == "&":
                finish()
                i += 1
                at_cmd, dead = True, False
                continue
            if c == "(":
                if cur is not None or not at_cmd:
                    raise Unsure("( inside a command")
                if s.startswith("((", i):
                    i = self._arith(i + 2)
                else:
                    i = self._list(i + 1, closer=")")
                at_cmd, dead = False, True
                continue
            if c == ")":
                raise Unsure("unbalanced )")

            word, i = self._word(i)
            raw = word.raw
            if cur is None and at_cmd and not dead:
                if raw in _UNPARSED:
                    raise Unsure(raw)
                if raw in _OPENERS:
                    if raw == "time" and s.startswith("-p", self._skip_blanks(i)):
                        _, i = self._word(self._skip_blanks(i))
                    continue
                if raw == "for":
                    for_header, at_cmd = True, False
                    continue
                if raw == "[[":
                    i = self._dbracket(i)
                    at_cmd, dead = False, True
                    continue
            if cur is None and raw in _CLOSERS and (at_cmd or dead):
                at_cmd, dead = False, True
                continue
            if for_header or dead:
                continue
            if cur is None and _ASSIGN.match(raw):
                if raw.endswith("=") and s.startswith("(", i):
                    i = self._parens(i + 1)
                continue  # an assignment is not the command
            if cur is None:
                cur = Simple()
            cur.words.append(word)
            at_cmd = False

        if closer is not None:
            raise Unsure("unclosed (")
        finish()
        return i

    def _skip_blanks(self, i: int) -> int:
        while i < self.n and self.s[i] in " \t":
            i += 1
        return i

    def _redirection(self, m) -> int:
        op, i = m.group(2), self._skip_blanks(m.end())
        if i >= self.n or self.s[i] in "\n;&|()<>":
            raise Unsure("redirection with no target")
        target, i = self._word(i)
        if op in ("<<", "<<-"):
            delim = _dequote(target.raw)
            if not delim:
                raise Unsure("heredoc with no delimiter")
            self.pending.append((delim, op == "<<-"))
        return i

    def _heredocs(self, i: int) -> int:
        """At the start of a line: past the bodies of the heredocs it opens."""
        s, n = self.s, self.n
        for delim, strip in self.pending:
            while True:
                if i >= n:
                    raise Unsure("unterminated heredoc")
                j = s.find("\n", i)
                line = s[i:] if j < 0 else s[i:j]
                i = n if j < 0 else j + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    break
        self.pending = []
        return i

    def _word(self, i: int):
        """One shell word from i: (Word, the index after it)."""
        s, n = self.s, self.n
        start, out, dynamic = i, [], False
        while i < n:
            c = s[i]
            if c in _META:
                break
            if c == "\\":
                if s.startswith("\\\n", i):
                    i += 2
                    continue
                if i + 1 < n:
                    out.append(s[i + 1])
                i += 2
                continue
            if c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    raise Unsure("unterminated '")
                out.append(s[i + 1:j])
                i = j + 1
                continue
            if c == '"':
                i, value = self._dquote(i + 1)
                if value is None:
                    dynamic = True
                else:
                    out.append(value)
                continue
            if c == "$":
                if s.startswith("$'", i):
                    i, dynamic = self._ansi_c(i + 2), True  # escapes not decoded
                    continue
                if s.startswith('$"', i):
                    i, _ = self._dquote(i + 2)
                    dynamic = True
                    continue
                i, is_expansion = self._dollar(i)
                if is_expansion:
                    dynamic = True
                else:
                    out.append("$")
                continue
            if c == "`":
                i, dynamic = self._backtick(i), True
                continue
            out.append(c)
            i += 1
        raw = s[start:i]
        return Word(start, i, raw, None if dynamic else "".join(out)), i

    def _dollar(self, i: int):
        """At a `$`: (the index after the expansion, whether it was one)."""
        s, n = self.s, self.n
        if s.startswith("$((", i):
            return self._arith(i + 3), True
        if s.startswith("$(", i):
            return self._list(i + 2, closer=")"), True
        if s.startswith("${", i):
            return self._braces(i + 2), True
        if i + 1 < n and (s[i + 1].isalpha() or s[i + 1] == "_"):
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            return j, True
        if i + 1 < n and s[i + 1] in _SPECIAL_PARAMS:
            return i + 2, True
        return i + 1, False

    def _dquote(self, i: int):
        """Inside "...": (the index after the closing quote, the value or None)."""
        s, n = self.s, self.n
        out, dynamic = [], False
        while i < n:
            c = s[i]
            if c == '"':
                return i + 1, None if dynamic else "".join(out)
            if c == "\\" and i + 1 < n:
                nxt = s[i + 1]
                if nxt != "\n":
                    out.append(nxt if nxt in '"\\$`' else "\\" + nxt)
                i += 2
                continue
            if c == "$":
                i, is_expansion = self._dollar(i)
                if is_expansion:
                    dynamic = True
                else:
                    out.append("$")
                continue
            if c == "`":
                i, dynamic = self._backtick(i), True
                continue
            out.append(c)
            i += 1
        raise Unsure('unterminated "')

    def _ansi_c(self, i: int) -> int:
        s, n = self.s, self.n
        while i < n:
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == "'":
                return i + 1
            i += 1
        raise Unsure("unterminated $'")

    def _backtick(self, i: int) -> int:
        s, n = self.s, self.n
        j = i + 1
        while j < n:
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == "`":
                if _HEAVY_WORD.search(s[i + 1:j]):
                    raise Unsure("backtick substitution")
                return j + 1
            j += 1
        raise Unsure("unterminated `")

    def _arith(self, i: int) -> int:
        """After `((` or `$((`: past the matching `))`."""
        s, n = self.s, self.n
        depth = 2
        while i < n:
            if s[i] == "(":
                depth += 1
            elif s[i] == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise Unsure("unterminated ((")

    def _braces(self, i: int) -> int:
        """After `${`: past the matching `}`."""
        s, n = self.s, self.n
        depth = 1
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    raise Unsure("unterminated '")
                i = j + 1
                continue
            if c == '"':
                i, _ = self._dquote(i + 1)
                continue
            if c == "$" and s.startswith("$(", i):
                i, _ = self._dollar(i)
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise Unsure("unterminated ${")

    def _parens(self, i: int) -> int:
        """An array assignment's (...): past the matching `)`."""
        s, n = self.s, self.n
        while i < n:
            if s[i] == ")":
                return i + 1
            if s[i] in " \t\n":
                i += 1
                continue
            _, i = self._word(i)
            if i < n and s[i] in ";&|(<>":
                raise Unsure("array assignment")
        raise Unsure("unterminated (")

    def _dbracket(self, i: int) -> int:
        """After `[[`: past the matching `]]` word."""
        s, n = self.s, self.n
        while i < n:
            if s[i] in " \t\n":
                i += 1
                continue
            if s.startswith("]]", i) and (i + 2 >= n or s[i + 2] in _META):
                return i + 2
            if s[i] in "();&|<>":
                i += 1
                continue
            _, i = self._word(i)
        raise Unsure("unterminated [[")


def _dequote(raw: str) -> str:
    """A heredoc delimiter: the word with its quotes removed, nothing expanded."""
    out, i = [], 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            out.append(raw[i + 1])
            i += 2
            continue
        if c not in "'\"":
            out.append(c)
        i += 1
    return "".join(out)


# --- what a heavy job is -------------------------------------------------------

_PYTHON = re.compile(r"python(\d+(\.\d+)*)?$")
_CHROMIUM = {"chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
             "google-chrome-beta", "chrome", "chrome-headless-shell", "headless_shell",
             "Google Chrome"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_INFO = {"--version", "-V", "--help", "-h", "-v", "version", "help"}
_PYTEST_VALUE = {"-k", "-m", "-p", "-c", "-o", "-W", "-n", "-r", "--deselect", "--ignore",
                 "--ignore-glob", "--rootdir", "--basetemp", "--junitxml", "--junit-xml",
                 "--confcutdir", "--maxfail", "--tb", "--durations", "--durations-min",
                 "--log-level", "--log-file", "--log-cli-level", "--log-format",
                 "--log-date-format", "--log-file-level", "--cov", "--cov-report",
                 "--cov-config", "--cov-fail-under", "--import-mode", "--capture", "--color",
                 "--override-ini", "--timeout", "--dist", "--maxprocesses", "--numprocesses",
                 "--reruns", "--reruns-delay", "--count", "--html", "--json-report-file",
                 "--report-log", "--asyncio-mode", "--hypothesis-seed"}
_VITEST_VALUE = {"-t", "--testNamePattern", "-c", "--config", "-r", "--root", "--dir",
                 "--project", "--reporter", "--outputFile", "--pool", "--shard",
                 "--environment", "--mode", "--exclude", "--workspace"}
_TEST_FILE = re.compile(r"\.(test|spec)\.|\.(py|[cm]?[jt]sx?|vue|svelte)$")
_NPM_INSTALL = {"install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta",
                "isntal", "isntall", "add", "update", "up", "upgrade", "udpate",
                "install-test", "it", "install-ci-test", "cit", "rebuild", "rb"}
_NPM_CI = {"ci", "clean-install", "ic", "install-clean", "isntall-clean"}
_NPM_TEST = {"test", "t", "tst"}
_NPM_RUN = {"run", "run-script", "rum", "urn"}
_NPM_EXEC = {"exec", "x"}
_NPM_VALUE = {"--prefix", "--workspace", "-w", "--registry", "--userconfig", "--cache",
              "--loglevel", "--tag"}
_PNPM_INSTALL = {"install", "i", "add", "update", "up", "upgrade", "rebuild", "rb"}
_PNPM_VALUE = {"--filter", "-F", "-C", "--dir", "--workspace-dir"}
_YARN_INSTALL = {"install", "add", "upgrade", "up"}
_YARN_NOT_A_JOB = {"why", "info", "list", "ls", "config", "cache", "remove", "dev", "start",
                   "serve", "lint", "outdated", "audit", "init"}
_PIP_VALUE = {"--log", "--proxy", "--timeout", "--retries", "--cache-dir", "--python"}
_UV_VALUE = {"--project", "--directory", "--python", "-p", "--config-file",
             "--cache-dir", "--color", "--index", "--default-index", "--index-url"}


class Tok:
    """A word as the classifier sees it: its value (None when unknown) and raw text."""

    __slots__ = ("value", "raw")

    def __init__(self, value: Optional[str], raw: str):
        self.value, self.raw = value, raw


def _toks(argv: List[str]) -> List[Tok]:
    return [Tok(a, a) for a in argv]


def _base(tok: Tok) -> Optional[str]:
    return tok.value.rsplit("/", 1)[-1] if tok.value else None


def _skip_opts(args: List[Tok], i: int, value_opts: set) -> int:
    """Past leading options (and the values of the ones that take one)."""
    while i < len(args):
        v = args[i].value
        if v is None or not v.startswith("-") or v == "-":
            return i
        if v == "--":
            return i + 1
        i += 2 if ("=" not in v and v in value_opts) else 1
    return i


def _strip_runners(args: List[Tok]) -> List[Tok]:
    """What actually runs under env/timeout/nice/nohup/... (each is a program
    that execs its argument)."""
    i = 0
    while i < len(args):
        b = _base(args[i])
        if b == "env":
            i = _skip_opts(args, i + 1, {"-u", "--unset", "-C", "--chdir", "-S"})
            while i < len(args) and _ASSIGN.match(args[i].raw):
                i += 1
        elif b == "timeout":
            i = _skip_opts(args, i + 1, {"-s", "--signal", "-k", "--kill-after"}) + 1
        elif b == "nice":
            i = _skip_opts(args, i + 1, {"-n", "--adjustment"})
        elif b == "ionice":
            i = _skip_opts(args, i + 1, {"-c", "-n", "--class", "--classdata"})
        elif b == "stdbuf":
            i = _skip_opts(args, i + 1, {"-i", "-o", "-e"})
        elif b == "time":
            i = _skip_opts(args, i + 1, {"-o", "-f", "--output", "--format"})
        elif b == "nohup":
            i += 1
        elif b == "xvfb-run":
            i = _skip_opts(args, i + 1, {"-s", "-n", "-e", "-f", "-w", "-p", "--server-args",
                                         "--server-num", "--error-file", "--auth-file",
                                         "--wait"})
        elif b in ("sudo", "doas"):
            i = _skip_opts(args, i + 1, {"-u", "-g", "-C", "-h", "-p", "-r", "-t"})
        elif (b in ("uv", "poetry", "pipenv", "pdm", "hatch") and i + 1 < len(args)
              and args[i + 1].value == "run"):
            i = _skip_opts(args, i + 2, {"--with", "--python", "-p", "--project",
                                         "--directory", "--extra", "--group", "--env-file",
                                         "--index"})
        elif b == "flock":
            # flock [opts] <lockfile> <cmd>...: the command runs under the lock. Skip flock,
            # its options and the one lockfile positional. The `-c STRING` form runs the string
            # through a shell and is handled in _insertions (a runner-strip cannot re-parse it).
            j = _skip_opts(args, i + 1, {"-w", "--timeout", "-E", "--conflict-exit-code"})
            if j < len(args) and args[j].value and not args[j].value.startswith("-"):
                j += 1  # the lockfile (or an fd number)
            i = j
        elif b == "xargs":
            # -i, --replace and -l are left out on purpose: GNU xargs gives them only an
            # ATTACHED optional argument (-i[R], --replace[=R], -l[N]), so the next word is
            # the command (`xargs -i pytest {}`). -I and -L do take the next word.
            i = _skip_opts(args, i + 1, {"-a", "--arg-file", "-E", "-d", "--delimiter",
                                         "-I", "-L", "-n", "--max-args", "-P",
                                         "--max-procs", "-s", "--max-chars"})
        else:
            break
    return args[i:]


def _positionals(args: List[Tok], value_opts: set) -> List[Optional[str]]:
    out: List[Optional[str]] = []
    i = 0
    while i < len(args):
        v = args[i].value
        if v is None:
            out.append(None)
        elif v == "--":
            out.extend(a.value for a in args[i + 1:])
            break
        elif v.startswith("-") and v != "-":
            if "=" not in v and v in value_opts:
                i += 1  # its value is not a positional
        else:
            out.append(v)
        i += 1
    return out


def _pytest(args: List[Tok], lenient: bool) -> Optional[str]:
    if any(a.value in ("--version", "-V", "--help", "-h", "--collect-only", "--co")
           for a in args):
        return None  # lists or reports; runs no tests
    if lenient:
        return "pytest"
    pos = _positionals(args, _PYTEST_VALUE)
    if pos and all(p is not None and (p.endswith(".py") or "::" in p) for p in pos):
        return None  # a targeted run names its files
    return "pytest"


def _vitest(args: List[Tok], lenient: bool) -> Optional[str]:
    if lenient:
        return "vitest"
    if any(a.value in ("--version", "-v", "--help", "-h", "--watch", "-w") for a in args):
        return None
    pos = _positionals(args, _VITEST_VALUE)
    if pos and pos[0] in ("watch", "dev", "related", "list", "init"):
        return None
    if pos and pos[0] in ("run", "bench"):
        pos = pos[1:]
    return None if pos else "vitest"  # a positional is a filter: a targeted run


def _script(pm: str, name: Optional[str], rest: List[Tok], lenient: bool) -> Optional[str]:
    """A package script: heavy when it is a test or build script."""
    if name is None or not (name in ("test", "build") or name.startswith(("test:", "build:"))):
        return None
    if lenient:
        return f"{pm} {name}"
    if "watch" in name:
        return None  # a watcher is a dev process, by agreement not a job
    after = [a.value for a in rest]
    if "--" in after:
        files = [v for v in after[after.index("--") + 1:] if v and not v.startswith("-")]
        if files and all(_TEST_FILE.search(v) for v in files):
            return None  # a targeted run names its files
    return f"{pm} {name}"


def _npx(args: List[Tok], lenient: bool) -> Optional[str]:
    i = 0
    while i < len(args):
        v = args[i].value
        if v is None or v in ("-c", "--call"):
            return None  # a call string is not read
        if v in ("-p", "--package"):
            i += 2
        elif v == "--":
            i += 1
            break
        elif v.startswith("-"):
            i += 1
        else:
            break
    rest = args[i:]
    if not rest or rest[0].value is None or rest[0].value.startswith("@"):
        return None
    name = re.sub(r"@[^@/]*$", "", rest[0].value)  # vitest@1.2 is vitest
    return _classify([Tok(name, rest[0].raw)] + rest[1:], lenient, allow_pm=False)


def _npm(args: List[Tok], lenient: bool) -> Optional[str]:
    i = _skip_opts(args, 0, _NPM_VALUE)
    if i >= len(args) or args[i].value is None:
        return None
    sub, rest = args[i].value, args[i + 1:]
    if not lenient and any(a.value in ("--help", "-h") for a in rest):
        return None
    if sub in _NPM_CI:
        return "npm ci"
    if sub in _NPM_INSTALL:
        return "npm install"
    if sub in _NPM_TEST:
        return _script("npm", "test", rest, lenient)
    if sub in _NPM_RUN:
        j = _skip_opts(rest, 0, {"-w", "--workspace"})
        return _script("npm", rest[j].value, rest[j + 1:], lenient) if j < len(rest) else None
    if sub in _NPM_EXEC:
        return _npx(rest, lenient)
    return None


def _pnpm(args: List[Tok], lenient: bool) -> Optional[str]:
    i = _skip_opts(args, 0, _PNPM_VALUE)
    if i >= len(args) or args[i].value is None or args[i].value in _INFO:
        return None
    sub, rest = args[i].value, args[i + 1:]
    if sub in _PNPM_INSTALL:
        return "pnpm install"
    if sub in _NPM_TEST:
        return _script("pnpm", "test", rest, lenient)
    if sub in ("run", "run-script"):
        j = _skip_opts(rest, 0, _PNPM_VALUE)
        return _script("pnpm", rest[j].value, rest[j + 1:], lenient) if j < len(rest) else None
    if sub == "exec":
        return _classify(rest[_skip_opts(rest, 0, set()):], lenient, allow_pm=False)
    if sub == "dlx":
        return _npx(rest, lenient)
    if sub.startswith(("test", "build")):
        return _script("pnpm", sub, rest, lenient)
    return _classify(args[i:], lenient, allow_pm=False)  # `pnpm vitest` runs a bin


def _yarn(args: List[Tok], lenient: bool) -> Optional[str]:
    if not lenient and args and args[0].value in _INFO:
        return None  # `yarn --version` is not a bare `yarn` (an install)
    i = _skip_opts(args, 0, {"--cwd"})
    if i >= len(args):
        return "yarn install"
    sub, rest = args[i].value, args[i + 1:]
    if sub is None or sub in _INFO or sub in _YARN_NOT_A_JOB:
        return None
    if sub in _YARN_INSTALL:
        return "yarn install"
    if sub == "test":
        return _script("yarn", "test", rest, lenient)
    if sub in ("run", "run-script"):
        return _script("yarn", rest[0].value, rest[1:], lenient) if rest else None
    if sub == "exec":
        return _classify(rest, lenient, allow_pm=False)
    if sub == "dlx":
        return _npx(rest, lenient)
    if sub.startswith(("test", "build")):
        return _script("yarn", sub, rest, lenient)
    return _classify(args[i:], lenient, allow_pm=False)  # `yarn vitest` runs a bin


def _python(args: List[Tok], lenient: bool) -> Optional[str]:
    i = 0
    while i < len(args):
        v = args[i].value
        if v is None:
            return None
        if v in ("-X", "-W"):
            i += 2
        elif v == "-m":
            if i + 1 >= len(args):
                return None
            module, rest = args[i + 1].value, args[i + 2:]
            if module in ("pytest", "py.test"):
                return _pytest(rest, lenient)
            if module == "pip":
                return _pip(rest, lenient)
            if module == "playwright":
                return _playwright(rest, lenient)
            return None
        elif v.startswith("-") and v not in ("-", "-c"):
            i += 1
        else:
            return None  # a script or -c: its contents are not read
    return None


def _playwright(args: List[Tok], lenient: bool) -> Optional[str]:
    pos = _positionals(args, set())
    if pos and pos[0] in ("test", "install", "install-deps"):
        return "playwright"
    return "playwright" if lenient and pos else None


def _pip(args: List[Tok], lenient: bool) -> Optional[str]:
    """pip / pip3: heavy when it installs (fetch or build), not --dry-run/--help."""
    i = _skip_opts(args, 0, _PIP_VALUE)
    if i >= len(args) or args[i].value != "install":
        return None
    rest = args[i + 1:]
    if not lenient and any(a.value in ("--dry-run", "--help", "-h") for a in rest):
        return None
    return "pip install"


def _uv(args: List[Tok], lenient: bool) -> Optional[str]:
    """uv sync / uv add / uv pip install / uv pip sync (uv run is a runner, stripped upstream)."""
    i = _skip_opts(args, 0, _UV_VALUE)
    if i >= len(args) or args[i].value is None:
        return None
    sub, rest = args[i].value, args[i + 1:]

    def light(a: List[Tok]) -> bool:
        return not lenient and any(t.value in ("--dry-run", "--help", "-h") for t in a)

    if sub == "sync":
        return None if light(rest) else "uv sync"
    if sub == "add":
        # `uv add` re-locks, then syncs the environment unless told not to: its --frozen
        # skips the sync (unlike `uv sync --frozen`, which installs), and --script only
        # edits a script's inline metadata (uv 0.11.3's help).
        quiet = ("--no-sync", "--frozen", "--script")
        if light(rest) or (not lenient and any(
                (t.value or "").split("=", 1)[0] in quiet for t in rest)):
            return None
        return "uv add"
    if sub == "pip":
        j = _skip_opts(rest, 0, set())
        if j < len(rest) and rest[j].value in ("install", "sync"):
            return None if light(rest[j + 1:]) else "uv pip " + rest[j].value
    return None


def _classify(args: List[Tok], lenient: bool, allow_pm: bool = True) -> Optional[str]:
    args = _strip_runners(args)
    if not args:
        return None
    b, rest = _base(args[0]), args[1:]
    if b is None:
        return None
    if b in ("pytest", "py.test"):
        return _pytest(rest, lenient)
    if b == "vitest":
        return _vitest(rest, lenient)
    if b == "next":
        return "next build" if rest and rest[0].value == "build" else None
    if b == "playwright":
        return _playwright(rest, lenient)
    if b in _CHROMIUM:
        if not lenient and any(a.value in ("--version", "-version", "--help") for a in rest):
            return None
        return "chromium"
    if _PYTHON.match(b):
        return _python(rest, lenient)
    if b in ("pip", "pip3"):
        return _pip(rest, lenient)
    if b == "uv":
        return _uv(rest, lenient)
    if b == "npx":
        return _npx(rest, lenient)
    if not allow_pm:
        return None
    if b == "npm":
        if not lenient and rest and rest[0].value in ("-v", "--version", "-h", "--help"):
            return None
        return _npm(rest, lenient)
    if b == "pnpm":
        return _pnpm(rest, lenient)
    if b == "yarn":
        return _yarn(rest, lenient)
    return None


def classify(argv: List[str]) -> Optional[str]:
    """The kind of heavy job argv is, or None."""
    return _classify(_toks(argv), lenient=False)


def heavy_family(argv: List[str]) -> bool:
    """Whether the wrapper may run argv. It checks the TOOL, never whether a
    run is targeted: the hook decided with the command as written, and after
    the shell expands it the two must never disagree in the direction of
    refusing a job the hook sent. A shell is never a heavy tool, so the
    wrapper's path cannot become a way to run anything else."""
    return _classify(_toks(argv), lenient=True) is not None


# --- the rewrite ---------------------------------------------------------------

def _shell_string(words: List[Word]) -> Optional[Word]:
    """For `bash -c STRING`: the word holding STRING, or None."""
    i = 1
    while i < len(words):
        v = words[i].value
        if v is None or not v.startswith("-") or v == "-":
            return None
        if not v.startswith("--") and "c" in v[1:]:
            j = i + 1
            while j < len(words) and (words[j].value or "").startswith("-"):
                j += 1
            return words[j] if j < len(words) else None
        i += 1
    return None


def _flock_c_string(words: List[Word]) -> Optional[Word]:
    """For `flock [opts] <lockfile> -c STRING`: the word holding STRING (the `-c`
    form runs it through a shell), or None for the command form."""
    toks = [Tok(w.value, w.raw) for w in words]
    i = _skip_opts(toks, 1, {"-w", "--timeout", "-E", "--conflict-exit-code"})
    if i < len(toks) and toks[i].value and not toks[i].value.startswith("-"):
        i += 1  # the lockfile
    if i < len(toks) and toks[i].value in ("-c", "--command"):
        j = i + 1
        while j < len(toks) and (toks[j].value or "").startswith("-"):
            j += 1
        return words[j] if j < len(words) else None
    return None


def _insertions(command: str, base: int = 0,
                ctx: Tuple[str, ...] = ()) -> List[Tuple[int, Tuple[str, ...]]]:
    """Where the wrapper goes: (offset of each heavy command's first word, the
    tuple of enclosing `bash -c` quote characters from outermost to innermost).
    The wrapper carries its own quotes (shlex.quote of a path with a space or a
    single quote), so at each nesting level it must be re-escaped for that level's
    quoting, innermost first, or a spliced quote would break the enclosing string."""
    out: List[Tuple[int, Tuple[str, ...]]] = []
    for simple in _Scanner(command).parse():
        words = simple.words
        first = words[0]
        if first.value == "builtin":
            continue
        if first.value == "command":
            rest = words[1:]
            if rest and rest[0].value in ("-v", "-V"):
                continue  # a lookup, not a run
            if rest and rest[0].value == "-p":
                rest = rest[1:]
            if not rest:
                continue
            words, first = rest, rest[0]
        elif first.value == "exec":
            rest = words[1:]
            if rest and (rest[0].value or "").startswith("-"):
                if _HEAVY_WORD.search(command[first.start:]):
                    raise Unsure("exec with options")
                continue
            if not rest:
                continue
            words, first = rest, rest[0]
        if first.value == "eval":
            if _HEAVY_WORD.search(command[first.end:words[-1].end]):
                raise Unsure("eval")
            continue
        if first.value is not None and first.value.rsplit("/", 1)[-1] in _SHELLS:
            string = _shell_string(words)
            if string is None:
                continue
            raw, inner = string.raw, string.raw[1:-1]
            maps = len(raw) >= 2 and (
                (raw[0] == raw[-1] == "'" and "'" not in inner)
                or (raw[0] == raw[-1] == '"' and not any(ch in inner for ch in '\\$`"')))
            if maps:  # the string's value is its text, so offsets carry over
                out += _insertions(inner, base + string.start + 1, ctx + (raw[0],))
            elif _HEAVY_WORD.search(raw):
                raise Unsure("a shell -c string the matcher cannot map")
            continue
        if first.value is not None and first.value.rsplit("/", 1)[-1] == "flock":
            fstr = _flock_c_string(words)
            if fstr is not None:  # the -c form; the command form falls through to _classify
                raw, inner = fstr.raw, fstr.raw[1:-1]
                maps = len(raw) >= 2 and (
                    (raw[0] == raw[-1] == "'" and "'" not in inner)
                    or (raw[0] == raw[-1] == '"' and not any(ch in inner for ch in '\\$`"')))
                if maps:
                    out += _insertions(inner, base + fstr.start + 1, ctx + (raw[0],))
                elif _HEAVY_WORD.search(raw):
                    raise Unsure("a flock -c string the matcher cannot map")
                continue
        if _classify([Tok(w.value, w.raw) for w in words], lenient=False):
            out.append((base + first.start, ctx))
    return out


def _dq_escape(s: str) -> str:
    """Escape a fragment so its literal value survives one level of double quotes."""
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("$", "\\$").replace("`", "\\`"))


def _escape_for_ctx(wrapper: str, ctx: Tuple[str, ...]) -> str:
    """Re-quote the wrapper for its nesting: innermost quote level first. A single
    quote in the path (via shlex.quote) breaks an enclosing `'...'`; the `"` that
    shlex.quote then uses breaks an enclosing `"..."` — so each level gets the
    escaping its own quote needs."""
    w = wrapper
    for quote in reversed(ctx):
        if quote == "'":
            w = w.replace("'", "'\\''")
        elif quote == '"':
            w = _dq_escape(w)
    return w


def gate(command: str, wrapper: str) -> Optional[str]:
    """The command with `wrapper` in front of each heavy command in it, or
    None when there is none. Raises Unsure for a construct it does not parse."""
    points = sorted(set(_insertions(command)), reverse=True)
    if not points:
        return None
    for p, ctx in points:
        # The wrapper carries the shlex-quoted heavy-slot path; inside a quoted
        # `bash -c` string its quotes must be re-escaped for that string, or they
        # close it and the command becomes a different one that still parses.
        w = _escape_for_ctx(wrapper, ctx)
        command = command[:p] + w + " " + command[p:]
    return command


# --- the slot ------------------------------------------------------------------

class NoDataRoot(Exception):
    """No absolute CLAUDLOBBY_ROOT: the slot is host state, never the package dir."""


def _root() -> Path:
    # This file ships inside an immutable release, one directory per release:
    # a slot derived from its own location would split the host's one slot
    # across releases and write into sealed code. Only the data root names it.
    env = os.environ.get("CLAUDLOBBY_ROOT") or ""
    if not os.path.isabs(env):
        raise NoDataRoot("heavy-slot: no absolute CLAUDLOBBY_ROOT data directory, so no "
                         "host slot to take")
    return Path(env)


def slot_dir() -> Path:
    d = os.environ.get("HEAVY_SLOT_DIR")
    return Path(d) if d else _root() / "state" / "heavy-slot"


def slot_count(d: Path):
    """(count, where it came from). One file, host-wide, read on every use."""
    f = d / "slots"
    try:
        text = f.read_text().strip()
    except FileNotFoundError:
        return 1, "default"
    except OSError:
        return 1, f"default: {f} is unreadable"
    try:
        n = int(text)
    except ValueError:
        return 1, f"default: {f} holds {text!r}, not a number"
    if not 1 <= n <= 8:
        return 1, f"default: {f} holds {n}, outside 1-8"
    return n, str(f)


def boot_id() -> str:
    seam = os.environ.get("HEAVY_SLOT_BOOT_ID")
    if seam:
        return seam
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    try:  # macOS
        out = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _now_iso(t: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# --- what a record may hold of a command --------------------------------------
# What the slot records of a job is kept in its lock file and in every
# heavy_slot_* event, which the plane never prunes, and a holder's record
# reaches the refusal another bot reads. So a record holds a job's SHAPE and
# never its text (#2037): the tool, and the names of the flags it was given,
# with no value, no positional word and no environment assignment.
#
# Redacting values in place is not enough on either side. The wrapper sees argv
# after the shell expanded it, so a secret passed as "$VAR" arrives as a bare
# word with nothing naming it. The hook sees the command as typed, and an
# unparsed one can carry anything, heredoc bodies included. A shape keeps
# neither.

def _flag_name(word: Optional[str]) -> Optional[str]:
    """A flag's name without its value: `--maxfail=2` gives `--maxfail`, and a
    short flag keeps one letter, since `-kEXPR` glues the value on."""
    if not word or not word.startswith("-") or word in ("-", "--"):
        return None
    m = re.match(r"(--[A-Za-z0-9][A-Za-z0-9-]{0,39}|-[A-Za-z0-9])", word)
    return m.group(1) if m else None


def _cap(text: str) -> str:
    return text if len(text) <= COMMAND_CAP else text[:COMMAND_CAP - 1] + "…"


def _tool(argv: List[str]) -> str:
    """What a job runs (`pytest`, `npm ci`, ...): the classifier's label, never
    an argument."""
    toks = _toks(argv)
    try:
        label = _classify(toks, lenient=True)
        run = _strip_runners(toks)
    except Exception:  # a record must still be written
        label, run = None, toks
    return label or (_base(run[0]) if run else None) or "?"


def _shape_argv(argv: List[str]) -> str:
    """A job's shape from the argv the wrapper runs: its tool, then the flag
    names given to the tool (the runners in front of it are dropped)."""
    try:
        run = _strip_runners(_toks(argv))
    except Exception:
        run = _toks(argv)
    flags = [f for f in (_flag_name(t.value) for t in run[1:]) if f]
    return _cap(" ".join([_tool(argv)] + flags))


_TOOL_WORDS = {"pytest", "py.test", "vitest", "next", "playwright", "npm", "pnpm", "yarn",
               "npx", "pip", "pip3", "uv"} | set(_CHROMIUM)


def _shape_text(text: str) -> str:
    """A command's shape from its text as typed, which may not parse: the heavy
    tool words and the flag names in it, and nothing else."""
    words = []
    for raw in text.split():
        w = raw.strip("'\"`();&|{}<>")
        base = w.rsplit("/", 1)[-1]
        if base in _TOOL_WORDS or _PYTHON.match(base):
            words.append(base)
        else:
            f = _flag_name(w)
            if f:
                words.append(f)
    return _cap(" ".join(words)) or "?"


def _tool_of(rec: dict) -> str:
    """A holder's tool: its record's own. A record from before the field existed
    gives the first tool word of its shape. Never its command."""
    if isinstance(rec.get("tool"), str) and rec["tool"]:
        return rec["tool"]
    return _public(rec)["shape"].split(" ", 1)[0] or "a heavy job"


def _public(rec: dict) -> dict:
    """A record as it may be shown or emitted: a shape and no command. A record
    written before shapes existed has its command reduced to one here."""
    out = {k: v for k, v in rec.items() if k != "command"}
    if not isinstance(out.get("shape"), str):
        # such a command was one argv joined, so it is read back as one
        text = rec.get("command") or ""
        try:
            words = shlex.split(text)
        except ValueError:
            words = None
        out["shape"] = (_shape_argv(words) if words else _shape_text(text)) if text else ""
    return out


def _read(fd: int) -> dict:
    try:
        rec = json.loads(os.pread(fd, 65536, 0).decode("utf-8", "replace") or "{}")
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _write(fd: int, rec: dict) -> None:
    data = (json.dumps(rec, sort_keys=True) + "\n").encode()
    os.ftruncate(fd, 0)
    os.pwrite(fd, data, 0)
    os.fsync(fd)


def _slot_number(p: Path) -> int:
    return int(re.sub(r"\D", "", p.name) or 0)


def probe(d: Path, n: int, extra: bool = False):
    """[(slot, held, record)] without disturbing a holder: a shared lock taken
    and dropped at once. With extra, a slot file beyond the count is shown too
    (a job may still hold it after the count was lowered)."""
    paths = {d / f"slot-{i}.lock" for i in range(n)}
    if extra and d.is_dir():
        paths |= set(d.glob("slot-*.lock"))
    out = []
    for p in sorted(paths, key=_slot_number):
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            out.append((_slot_number(p), False, {}))
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                held = True
            else:
                held = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            out.append((_slot_number(p), held, _read(fd)))
        finally:
            os.close(fd)
    return out


def _acquire(d: Path, n: int):
    """(slot, fd) holding the slot's exclusive lock, or None when every slot
    is taken. The fd is not inheritable, so a child never holds the lock."""
    d.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        for i in range(n):
            fd = os.open(d / f"slot-{i}.lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            return i, fd
        if attempt == 0:
            time.sleep(0.05)  # a status probe holds its shared lock for microseconds
    return None


def _who(rec: dict) -> str:
    return f"{rec.get('fleet') or '?'}/{rec.get('bot') or '?'}"


def _clock(epoch: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(epoch)) if epoch else "?"


def _epoch(rec: dict, key: str) -> Optional[float]:
    v = rec.get(f"{key}_epoch")
    if isinstance(v, (int, float)):
        return float(v)
    text = rec.get(f"{key}_at")
    if isinstance(text, str):
        try:
            return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            return None
    return None


def _holding(rec: dict, now: float) -> str:
    if rec.get("state") != "held":
        return "a holder that has not written its record yet"
    start = _epoch(rec, "started")
    mins = f" ({int((now - start) // 60)} min)" if start else ""
    return f"{_who(rec)} running {_tool_of(rec)} since {_clock(start)}{mins}"


def refusal(holders: List[dict], n: int, now: Optional[float] = None,
            mine: Optional[dict] = None, order: Optional[List[dict]] = None) -> str:
    """Why a call did not run. Given the queue's verdict (the caller's ticket and
    the order), it also says whose turn it is and where the caller stands."""
    now = time.time() if now is None else now
    order = order or []
    pace = "one at a time" if n == 1 else f"at most {n} at a time"
    if mine is not None and not holders and order and order[0]["n"] != mine["n"]:
        turn = order[0]
        why = (f"NOT RUN: this host's heavy-job slot is free, but it is another caller's "
               f"turn: ticket {turn['n']}, {_who(turn)}, waiting since {_clock(turn['since'])}.")
    else:
        who = "; ".join(_holding(h, now) for h in holders) or "holders not recorded"
        why = f"NOT RUN: this host's heavy-job slot is taken ({len(holders)} of {n}): {who}."
    text = f"{why} Heavy jobs run {pace} on this host so it does not thrash (#1686)."
    if mine is not None:
        place = [t["n"] for t in order].index(mine["n"]) + 1
        text += (f" They take turns in a queue (#2124): you hold ticket {mine['n']}, place "
                 f"{place} of {len(order)}, kept while you retry within "
                 f"{_span(ticket_idle_s())} of a slot coming free.")
    return text + (" Retry this later and do other work meanwhile; do not wait in a sleep "
                   "loop. A run that names its test files is not gated.")


def _holder_summary(rec: dict) -> dict:
    out = {k: rec.get(k) for k in ("slot", "fleet", "bot", "started_at", "pid")}
    out.update(tool=_tool_of(rec), shape=_public(rec)["shape"])
    return out


# --- the queue -----------------------------------------------------------------
# The slots alone bound concurrency but do not share it: after a release the
# next process to call flock wins, so a caller that takes the slot again at once
# beats every waiter that polls (#2124). So a call that cannot take a slot takes
# a ticket, kept under its fleet and bot so that the same bot's next call finds
# it, and a free slot goes only to the head of the queue: the oldest ticket from
# a fleet other than the last holder's, else the oldest ticket. Nobody waits
# inside this script: a bot's turn waits on the hook, and its Bash call on the
# wrapper, so a refused call still exits 75 and its caller retries.
#
# A waiter shows that it still waits only by calling again: its process ends
# with the refusal, so no lock can stand for it. Nothing expires while every
# slot is held, since nobody can be served and a waiter need not poll through a
# long suite. Once a slot is free, a ticket whose holder has been silent for
# TICKET_IDLE_S is dropped, so an abandoned ticket idles a free slot that long
# at most, once.

TICKET_IDLE_S = 180  # about three missed polls at a 60 s retry
QUEUE_LOCK_WAIT_S = 2.0  # a decision holds the queue's lock for milliseconds


def ticket_idle_s() -> int:
    """TICKET_IDLE_S, or HEAVY_SLOT_TICKET_IDLE_S (whole seconds, 1 or more)
    from this process's environment."""
    try:
        idle = int(os.environ.get("HEAVY_SLOT_TICKET_IDLE_S") or TICKET_IDLE_S)
    except ValueError:
        return TICKET_IDLE_S
    return idle if idle >= 1 else TICKET_IDLE_S


def _span(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"


def _identity() -> Tuple[str, str]:
    """(fleet, bot): the caller, as its record and its ticket name it."""
    return (os.environ.get("FLEET_NAME") or os.environ.get("CLAUDLOBBY_FLEET") or "",
            os.environ.get("BOT_ID") or os.environ.get("BOT_NAME") or "")


def _lock_queue(d: Path) -> Optional[int]:
    """An fd holding the queue's lock, or None. When the queue is switched off
    (`no-queue`), its lock cannot be had within QUEUE_LOCK_WAIT_S (a stopped
    process holds it) or its files cannot be made, the caller decides on the
    slots alone, as before the queue, rather than hang or fail."""
    if (d / "no-queue").exists():
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / "queue.lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    deadline = time.monotonic() + QUEUE_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.01)


def _ticket_ok(t) -> bool:
    return (isinstance(t, dict) and isinstance(t.get("n"), int)
            and all(isinstance(t.get(k), str) for k in ("fleet", "bot"))
            and all(isinstance(t.get(k), (int, float)) for k in ("since", "seen")))


def load_queue(d: Path) -> dict:
    """The queue as last written. A missing or damaged file is an empty queue."""
    try:
        q = json.loads((d / "queue.json").read_text())
    except (OSError, ValueError):
        q = None
    if not (isinstance(q, dict) and isinstance(q.get("next"), int)
            and isinstance(q.get("tickets"), list)):
        return {"v": 1, "next": 1, "tickets": []}
    q["tickets"] = [t for t in q["tickets"] if _ticket_ok(t)]
    return q


def _save_queue(d: Path, q: dict) -> None:
    """Replaced whole, so a reader without the lock never sees half a queue."""
    tmp = d / f"queue.json.{os.getpid()}"
    tmp.write_text(json.dumps(q, sort_keys=True) + "\n")
    os.replace(tmp, d / "queue.json")


def _free_since(slots) -> Optional[float]:
    """When the longest-free slot came free (0 for one never used, or whose
    holder died unreleased), or None while every slot is held."""
    free = [rec for _, held, rec in slots if not held]
    return min(_epoch(rec, "released") or 0.0 for rec in free) if free else None


def _last_fleet(slots) -> Optional[str]:
    """The fleet that took a slot last, holding it still or not: while another
    fleet waits, the next turn is not this fleet's."""
    takes = []
    for _, _, rec in slots:
        start = _epoch(rec, "started")
        if start is not None:
            takes.append((start, rec.get("fleet") or ""))
    return max(takes)[1] if takes else None


def _live(tickets: List[dict], slots, now: float, idle: int):
    """(the tickets still waiting, the dropped ones with how long each was
    silent once a slot was free)."""
    since = _free_since(slots)
    if since is None:
        return list(tickets), []
    live, dropped = [], []
    for t in tickets:
        silent = now - max(t["seen"], since)
        if silent <= idle:
            live.append(t)
        else:
            dropped.append((t, int(silent)))
    return live, dropped


def _served(live: List[dict], slots) -> List[dict]:
    """The tickets in the order they are served: oldest first, except that the
    oldest ticket from a fleet other than the last holder's goes ahead."""
    order = sorted(live, key=lambda t: t["n"])
    last = _last_fleet(slots)
    for i, t in enumerate(order):
        if last is not None and t["fleet"] != last:
            order.insert(0, order.pop(i))
            break
    return order


def take_turn(q: dict, slots, tool: str, now: float, idle: int):
    """One caller's decision on the queue: (order, dropped, mine, queued). The
    caller's ticket is refreshed, or a new one goes in at the back (a dropped
    one is not revived); `queued` says whether it held one before this call.
    `order` includes it, and its first tickets, one per free slot, may take one."""
    me = _identity()
    live, dropped = _live(q["tickets"], slots, now, idle)
    mine = next((t for t in live if (t["fleet"], t["bot"]) == me), None)
    queued = mine is not None
    if mine is None:
        mine = {"n": q["next"], "fleet": me[0], "bot": me[1], "tool": tool, "since": int(now)}
        q["next"] += 1
        live.append(mine)
    mine["seen"] = int(now)
    return _served(live, slots), dropped, mine, queued


def _may_take(mine: dict, order: List[dict], slots) -> bool:
    free = sum(1 for _, held, _ in slots if not held)
    return any(t["n"] == mine["n"] for t in order[:free])


def _ticket_summary(t: dict, order: List[dict]) -> dict:
    out = {k: t.get(k) for k in ("n", "fleet", "bot", "tool")}
    out["since"] = _now_iso(t["since"])
    places = [x["n"] for x in order]
    if t["n"] in places:
        out.update(place=places.index(t["n"]) + 1, queued=len(places))
    return out


def _emit_dropped(dropped, idle: int) -> None:
    for t, silent in dropped:
        _emit("heavy_slot_ticket_dropped", {"ticket": _ticket_summary(t, []),
                                            "last_try": _now_iso(t["seen"]),
                                            "silent_s": silent, "idle_s": idle})


def _queue_line(place: int, t: dict, now: float, idle: int) -> str:
    return (f"queue {place}: ticket {t['n']}, {_who(t)} ({t.get('tool') or '?'}), waiting "
            f"since {_clock(t['since'])} ({int((now - t['since']) // 60)} min), last try "
            f"{_clock(t['seen'])}; dropped after {_span(idle)} silent once a slot is free"
            + (" — next" if place == 1 else ""))


def _queue_row(place: int, t: dict, now: float, idle: int) -> dict:
    return {"place": place, "ticket": t["n"], "fleet": t["fleet"], "bot": t["bot"],
            "tool": t.get("tool") or "?", "since": _now_iso(t["since"]),
            "last_try": _now_iso(t["seen"]), "waiting_s": int(now - t["since"]),
            "idle_s": idle}


# --- the record on the plane ---------------------------------------------------

def _emit(event: str, data: dict) -> None:
    """Best-effort and never blocking: a fleet event through the one door, in
    a detached shell the job never waits on."""
    seam = os.environ.get("HEAVY_SLOT_EVENTS_FILE")
    if seam:
        with open(seam, "a") as fh:
            fh.write(json.dumps({"type": event, "data": data}, sort_keys=True) + "\n")
        return
    if os.environ.get("PLANE_EMIT_DISABLED") == "1":
        return
    # The lib-common of this file's own release. The mutable data root never
    # selects installed code; a copy with no lib-common beside it emits nothing.
    lib = str(Path(__file__).resolve().parent)
    script = ('( . "$1/lib-common.sh" >/dev/null 2>&1 || exit 0; '
              'emit_fleet_event "$2" heavy-slot "$3" ) </dev/null >/dev/null 2>&1 &')
    try:
        subprocess.run(["bash", "-c", script, "heavy-slot", lib, event,
                        json.dumps(data, sort_keys=True)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# --- the doors -----------------------------------------------------------------

def _syntax_ok(command: str) -> bool:
    try:
        return subprocess.run(["bash", "-n"], input=command, capture_output=True, text=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_hook() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        print("heavy-slot: unparseable hook payload", file=sys.stderr)
        return 1
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return 0
    d = slot_dir()
    if (d / "disabled").exists():
        return 0
    wrapper = shlex.quote(str(Path(__file__).resolve())) + " run --"
    try:
        rewritten = gate(command, wrapper)
    except Unsure as why:
        _emit("heavy_slot_unparsed", {"reason": str(why), "shape": _shape_text(command)})
        return 0
    if rewritten is None:
        return 0
    n, _ = slot_count(d)
    shape = _shape_text(command)
    idle, now = ticket_idle_s(), time.time()
    mine, order, dropped = None, [], []
    qfd = _lock_queue(d)
    try:
        slots = probe(d, n)
        holders = [rec for _, held, rec in slots if held]
        if qfd is None:
            refused = len(holders) == len(slots)
        else:
            q = load_queue(d)
            tool = next((w for w in shape.split() if not w.startswith("-")), "?")
            order, dropped, mine, queued = take_turn(q, slots, tool, now, idle)
            refused = not _may_take(mine, order, slots)
            if not refused and not queued:
                order.remove(mine)  # a call let through needs no ticket: the wrapper decides
            if refused or queued or dropped:
                q["tickets"] = order
                _save_queue(d, q)
    finally:
        if qfd is not None:
            os.close(qfd)
    _emit_dropped(dropped, idle)
    if refused:
        data = {"where": "hook", "shape": shape,
                "holders": [_holder_summary(h) for h in holders]}
        if mine is not None:
            data["ticket"] = _ticket_summary(mine, order)
        _emit("heavy_slot_refused", data)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "heavy-slot: " + refusal(holders, n, now, mine, order)}}))
        return 0
    if not _syntax_ok(rewritten) and _syntax_ok(command):
        _emit("heavy_slot_unparsed", {"reason": "the rewrite does not parse",
                                      "shape": _shape_text(command)})
        return 0
    updated = dict(tool_input)
    updated["command"] = rewritten
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "updatedInput": updated}}))
    return 0


def cmd_run(argv: List[str]) -> int:
    if not argv:
        print("usage: heavy-slot.py run -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    shape = _shape_argv(argv)
    if not heavy_family(argv):
        print(f"heavy-slot: `{shape}` is not a heavy job, so the slot does not run it; run it "
              "directly (#1686)", file=sys.stderr)
        return 2
    d = slot_dir()
    n, _ = slot_count(d)
    idle, now = ticket_idle_s(), time.time()
    mine, order, dropped, queued = None, [], [], False
    qfd = _lock_queue(d)
    try:
        if qfd is None:
            got = _acquire(d, n)
        else:
            q = load_queue(d)
            slots = probe(d, n)
            order, dropped, mine, queued = take_turn(q, slots, _tool(argv), now, idle)
            got = _acquire(d, n) if _may_take(mine, order, slots) else None
            if got is not None:
                order.remove(mine)
            if got is None or queued or dropped:
                q["tickets"] = order
                _save_queue(d, q)
    finally:
        if qfd is not None:
            os.close(qfd)
    _emit_dropped(dropped, idle)
    if got is None:
        slots = probe(d, n)
        holders = [rec for _, held, rec in slots if held]
        if mine is None:
            holders = holders or [rec for _, _, rec in slots]
        print("heavy-slot: " + refusal(holders, n, now, mine, order), file=sys.stderr)
        data = {"where": "wrapper", "shape": shape,
                "holders": [_holder_summary(h) for h in holders]}
        if mine is not None:
            data["ticket"] = _ticket_summary(mine, order)
        _emit("heavy_slot_refused", data)
        return EX_TEMPFAIL
    slot, fd = got
    # From here the wrapper holds the slot, so a signal must reach the job and
    # the release must still be written. The handler goes in NOW: installed
    # after the job started, it left a window in which a TERM killed the
    # wrapper outright, with no forward and no release (found at load 18). A
    # signal that lands before the job exists is held, and delivered to it the
    # moment it does.
    child = None
    pending: List[int] = []

    def forward(signum, _frame):
        if child is None:
            pending.append(signum)
            return
        try:
            child.send_signal(signum)
        except OSError:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    boot = boot_id()
    previous = _read(fd)
    if previous.get("state") == "held":
        # Its holder died without a release: killed this boot, or the host
        # reset under it. The record is the evidence; report it before it is
        # overwritten.
        _emit("heavy_slot_unreleased", {"slot": slot, "previous": _public(previous),
                                        "across_reset": previous.get("boot_id") != boot})
    started = time.time()
    fleet, bot = _identity()
    record = {
        "v": 1, "slot": slot, "slots": n, "state": "held", "fleet": fleet, "bot": bot,
        "shape": shape, "tool": _tool(argv), "cwd": os.getcwd(), "pid": os.getpid(),
        "host": socket.gethostname(),
        "boot_id": boot, "started_at": _now_iso(started), "started_epoch": int(started),
        "released_at": None, "exit": None,
    }
    _write(fd, record)
    acquired = {"slot": slot, "slots": n, "shape": shape, "started_at": record["started_at"]}
    if queued:  # the ticket it waited on, for joining its refusals to this
        acquired["ticket"] = {"n": mine["n"], "since": _now_iso(mine["since"]),
                              "waited_s": int(started - mine["since"])}
    _emit("heavy_slot_acquired", acquired)
    delay = os.environ.get("HEAVY_SLOT_START_DELAY_S")  # test seam: hold the pre-start window open
    if delay:
        try:
            time.sleep(float(delay))
        except ValueError:
            pass
    try:
        child = subprocess.Popen(argv)
    except FileNotFoundError:
        print(f"heavy-slot: {argv[0]}: command not found", file=sys.stderr)
        rc = 127
    except PermissionError:
        print(f"heavy-slot: {argv[0]}: permission denied", file=sys.stderr)
        rc = 126
    if child is not None:
        for signum in pending:
            try:
                child.send_signal(signum)
            except OSError:
                pass
        rc = child.wait()
    status = rc if rc >= 0 else 128 - rc
    ended = time.time()
    record.update(state="released", released_at=_now_iso(ended), released_epoch=round(ended, 3),
                  exit=status, duration_s=round(ended - started, 1))
    _write(fd, record)
    os.close(fd)
    _emit("heavy_slot_released", {"slot": slot, "shape": shape, "exit": status,
                                  "started_at": record["started_at"],
                                  "duration_s": record["duration_s"]})
    if rc < 0:  # killed by a signal: end the same way, as a shell would report it
        signal.signal(-rc, signal.SIG_DFL)
        os.kill(os.getpid(), -rc)
    return status


def _status_line(slot: int, held: bool, rec: dict, boot: str, now: float) -> str:
    if held:
        if rec.get("state") != "held":
            return f"slot {slot}: HELD (the holder has not written its record yet)"
        start = _epoch(rec, "started")
        mins = int((now - start) // 60) if start else "?"
        return (f"slot {slot}: HELD by {_who(rec)} since {_clock(start)} ({mins} min, pid "
                f"{rec.get('pid', '?')}) — {_public(rec)['shape'] or '?'}")
    if not rec:
        return f"slot {slot}: free — never used"
    start, end = _epoch(rec, "started"), _epoch(rec, "released")
    if rec.get("state") == "held":
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(start)) if start else "?"
        how = ("before the host reset (it ran under an earlier boot)"
               if rec.get("boot_id") != boot else "its holder was killed this boot")
        return (f"slot {slot}: free — last holder NEVER RELEASED: {_who(rec)}, "
                f"{_public(rec)['shape'] or '?'}, since {when}; {how}")
    mins = f" ({int((end - start) // 60)} min)" if start and end else ""
    return (f"slot {slot}: free — last: {_who(rec)}, {_public(rec)['shape'] or '?'}, "
            f"{_clock(start)}–{_clock(end)}{mins}, exit {rec.get('exit', '?')}")


def cmd_status(args: List[str]) -> int:
    d = slot_dir()
    n, source = slot_count(d)
    disabled = (d / "disabled").exists()
    queue_off = (d / "no-queue").exists()
    slots = probe(d, n, extra=True)
    idle, now = ticket_idle_s(), time.time()
    counted = [s for s in slots if s[0] < n]
    order = [] if queue_off else _served(_live(load_queue(d)["tickets"], counted, now, idle)[0],
                                         counted)
    if "--json" in args:
        print(json.dumps({"dir": str(d), "slots_configured": n, "source": source,
                          "disabled": disabled, "queue_off": queue_off, "ticket_idle_s": idle,
                          "slots": [{"slot": s, "held": h, "record": _public(r)} for s, h, r in slots],
                          "queue": [_queue_row(i, t, now, idle) for i, t in enumerate(order, 1)]},
                         sort_keys=True))
        return 0
    boot = boot_id()
    print(f"heavy-slot: {n} slot(s) ({source}), state {d}")
    if disabled:
        print(f"DISABLED: {d / 'disabled'} exists, so the hook passes every call through; "
              "remove it to gate again")
    if queue_off:
        print(f"QUEUE OFF: {d / 'no-queue'} exists, so every caller decides on the slots "
              "alone; remove it to queue again")
    for slot, held, rec in slots:
        print(_status_line(slot, held, rec, boot, now))
    if not queue_off:
        print(f"queue: {len(order)} waiting, in the order they are served" if order
              else "queue: empty")
    for i, t in enumerate(order, 1):
        print(_queue_line(i, t, now, idle))
    return 0


def main(argv: List[str]) -> int:
    if not argv:
        print("usage: heavy-slot.py hook | run -- COMMAND... | status [--json]",
              file=sys.stderr)
        return 2
    verb, rest = argv[0], argv[1:]
    try:
        if verb == "hook":
            return cmd_hook()
        if verb == "run":
            return cmd_run(rest[1:] if rest[:1] == ["--"] else rest)
        if verb == "status":
            return cmd_status(rest)
    except NoDataRoot as why:
        # The hook's caller fails open on a nonzero exit; run and status refuse.
        print(str(why), file=sys.stderr)
        return 1 if verb == "hook" else 2
    print(f"heavy-slot.py: unknown verb {verb!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
