#!/usr/bin/env python3
"""signal-decide.py — the decision half of signal-guard.sh (#1069).

Reads one Bash command on stdin and says whether it sends a signal to a process
the caller did not start. Prints one line:

    allow
    deny<TAB><kinds><TAB><reason>

<kinds> lists what was refused, comma-separated, for the plane record:
selector, fuser, every-process, by-name, ancestor, lookup, unknown.

Killing by pid is safe only for a pid the caller started, so the decision keys
on where each pid comes from, and the safe sources are a short list: $!, $$,
$BASHPID, a job spec (%1), group 0, `jobs -p`, and a pid file read with cat,
head, tail or `<` (and reshaped with tr, cut, sort, uniq, grep, sed or awk)
outside /proc and /sys. A pid from anywhere else is refused, whether it arrives
directly, through a variable, a for loop, a read loop, xargs or find -exec:
`lookup` names a process lookup (ps, pgrep, pidof, pstree, lsof, fuser, ss,
netstat, top, tmux, screen, /proc, $PPID), `unknown` anything else. pkill,
killall, killall5 and skill pick processes by name or pattern, and fuser -k
whatever holds a file or port: refused outright, as are kill -1 (every process
the user can signal) and kill NAME (util-linux kill takes a name). A pid typed
as a number shows no provenance, so its target decides, typed directly or held
in a variable or a loop word: refused when it is an ancestor of this hook (the
session's claude, its tmux server, its subreaper, the user manager, PID 1),
allowed otherwise.
kill -0 and kill -l send nothing and are always allowed.

Bounds, stated once here (the hook, the guardrail and the CHANGELOG point
here). It reads the command text: a tripwire for honest mistakes, not a
sandbox. Out of its reach: a script file; eval of a variable; a function or
alias; a pid passed through a file in the same command; a name built by
expansion; env -S; a command echoed into a shell; a wrapper outside its table
(taskset, chrt, unbuffer, busybox) or a `time { ...; }` group; a signal sent
from another interpreter or by a tool that picks its own targets (npx
kill-port, GNU parallel); process control that is not a signal verb (tmux
kill-server and kill-session, screen -X quit, a stop or kill through the user
manager); a typed pid of another bot's process; and a command run by a tool
other than Bash, which is all the hook matches. A command nesting $( deeply
overflows this decider's stack, from about 110 to 330 levels by the shape of
each level, and the hook then fails open with its script_error breadcrumb.
The per-bot subreaper of #2158 bounds one route among those: an orphaned job's
parent is its own bot's subreaper, not the user manager.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import re
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "credential_echo_decide", LIB / "credential-echo-decide.py"
)
_ced = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(
    _ced
)  # its wrapper table, option scanner, shell-call reader and ANSI-C decoder, not a second copy

SELECTORS = frozenset("pkill killall killall5 skill".split())
LOOKUPS = frozenset(
    "ps pgrep pidof pstree lsof fuser ss netstat top tmux screen".split()
)
READERS = frozenset("cat head tail tr cut sort uniq grep sed awk jobs".split())  # name files
PRINTERS = frozenset(["echo", "printf"])  # print their own arguments
OWN = frozenset(["!", "$", "BASHPID"])  # the shell's own handles
DECLARES = frozenset("export local declare readonly typeset".split())
KEYWORDS = frozenset("if then else elif do while until ! { } in fi done".split())
SEPARATORS = frozenset([";", "&", "&&", "||", "\n", "(", ")", ";;", ";&", ";;&"])
PIPES = frozenset(["|", "|&"])
REDIRECTS = frozenset(
    ["<", ">", ">>", "<<", "<<-", "<<<", "<&", ">&", "&>", "&>>", "<>", ">|"]
)
INPUTS = frozenset(["<", "<<", "<<-", "<<<"])
OPERATORS = sorted(SEPARATORS | PIPES | REDIRECTS, key=len, reverse=True)
# Wrappers beside credential-echo-decide.py's table, in its shape: the short
# options that take a value, and the long options that do.
EXTRA_WRAPPERS = {
    "ionice": ("cn", {"--class": True, "--classdata": True}),
    "env": ("uCS", {"--unset": True, "--chdir": True, "--split-string": True}),
    "flock": ("wE", {"--wait": True, "--timeout": True, "--conflict-exit-code": True}),
    "xargs": (
        "adEILnPs",
        {
            "--arg-file": True,
            "--delimiter": True,
            "--max-args": True,
            "--max-procs": True,
            "--max-chars": True,
            "--process-slot-var": True,
        },
    ),
}
POSITIONAL_BEFORE_COMMAND = {"timeout": 1, "flock": 1}  # the duration, the lock file
WATCH_LONG = {"--interval": True, "--equexit": True}
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PARAM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[!$#?@*0-9-]")
ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^]]*\])?\+?=")
PROCESS_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.+-]*")
SYSTEM_PATH_RE = re.compile(r"(?<![\w.])/(proc|sys)(/|$)")
PLAIN_RE = re.compile(r"[^ \t\r\n;&|()<>\\'\"$`]+")  # a run of characters with no meaning
DQ_PLAIN_RE = {'"': re.compile(r'[^"\\$`]+'), None: re.compile(r"[^\\$`]+")}
# What a backslash escapes in double quotes; a heredoc body keeps it before `"`.
DQ_ESCAPES = {'"': '$`"\\', None: "$`\\"}
SIGNALLERS = SELECTORS | {"kill", "fuser", "find"}
SYSTEM_READ = ("lookup", "a read under /proc or /sys")
GUARDRAIL = (
    LIB.parent
    / "_resources"
    / "library"
    / "guardrails"
    / "signal-only-what-you-started.md"
)


class Word:
    """One shell word: quote-removed literal text, its expansions, its source."""

    def __init__(self):
        self.pieces = []  # quote-removed literal text
        self.code = []  # quotes removed, expansions kept: re-parsed for `sh -c`
        self.vars = set()
        self.subs = []  # parsed blocks of $(...), `...` and <(...)
        self.expanded = False
        self.quoted = False
        self.assign = None  # NAME when the word is NAME=value
        self.src = ""

    @property
    def text(self):
        return None if self.expanded else "".join(self.pieces)

    def literal(self, text, code=None):
        self.pieces.append(text)
        self.code.append(text if code is None else code)

    def expansion(self, text):
        """Keep an expansion live in the code. The outer shell expands it before an
        inner shell reads the code, so an unpaired backslash left in front of it
        (from `\\\\` in double quotes) must not escape it there."""
        joined = "".join(self.code)
        if (len(joined) - len(joined.rstrip("\\"))) % 2:
            self.code = [joined[:-1]]
        self.code.append(text)


class Cmd:
    """A simple command, with the pipeline it stands in."""

    def __init__(self, pipe):
        self.words = []
        self.assigns = []
        self.redirects = []  # [operator, Word, heredoc body]; an expanding body's Word
        self.pipe = pipe
        self.resolved = None  # resolve()'s answer, once
        self.inline = None  # code_blocks()'s answer, once


def code_of(w):
    return "".join(w.code)


class Parser:
    def __init__(self, src):
        self.s = src
        self.n = len(src)

    def closing(self, j, opener, closer):
        """Index of the `closer` that balances the `opener` at s[j], or the end."""
        depth = 0
        while j < self.n:
            depth += {opener: 1, closer: -1}.get(self.s[j], 0)
            if depth == 0:
                return j
            j += 1
        return j

    def dollar(self, i, w):
        s, n = self.s, self.n
        nxt = s[i + 1] if i + 1 < n else ""
        if s.startswith("$((", i):  # arithmetic: its names are variables
            j = self.closing(i + 1, "(", ")")
            w.vars.update(NAME_RE.findall(s[i + 3 : j]))
            w.expanded = True
            w.expansion(s[i : j + 1])
            return j + 1
        if nxt == "(":
            block, j = self.block(i + 2, ")")
            w.subs.append(block)
            w.expanded = True
            w.expansion(s[i:j])
            return j
        if nxt == "{":
            j = self.closing(i + 1, "{", "}")
            inner = s[i + 2 : j]
            m = PARAM_RE.match(inner)
            rest = inner
            if m and not (inner.startswith("!") and len(inner) > 1):  # not ${!x}
                w.vars.add(m.group(0))
                rest = inner[m.end() :]
            if rest:  # ${x:-$(...)} runs its default
                nested = Word()
                Parser(rest).dquote(0, nested, stop=None)
                w.vars |= nested.vars
                w.subs += nested.subs
            w.expanded = True
            w.expansion(s[i : j + 1])
            return j + 1
        if nxt == "'":  # $'...', decoded as bash does: a name can be spelled in escapes
            try:
                text, j = _ced._ansi_c(s, i + 2)
            except _ced.Unparsed:
                text, j = s[i + 2 :], n
            w.literal(text)
            w.quoted = True
            return j
        if nxt == '"':
            w.quoted = True
            return self.dquote(i + 2, w, stop='"')
        m = PARAM_RE.match(s, i + 1)
        if m:
            name = m.group(0) if NAME_RE.match(m.group(0)) else m.group(0)[0]
            w.vars.add(name)
            w.expanded = True
            w.expansion(s[i : i + 1 + len(name)])
            return i + 1 + len(name)
        w.literal("$")
        return i + 1

    def backtick(self, i, w):
        s, n = self.s, self.n
        j, inner = i + 1, []
        while j < n and s[j] != "`":
            if s[j] == "\\" and j + 1 < n:
                inner.append(s[j + 1])
                j += 2
                continue
            inner.append(s[j])
            j += 1
        w.subs.append(parse("".join(inner)))
        w.expanded = True
        w.expansion(s[i : j + 1])
        return j + 1

    def dquote(self, i, w, stop):
        s, n = self.s, self.n
        while i < n:
            c = s[i]
            if stop and c == stop:
                return i + 1
            if c == "\\" and i + 1 < n:  # quote removal, so a `sh -c` string is re-read as the shell gets it
                nxt = s[i + 1]
                w.literal("" if nxt == "\n" else nxt if nxt in DQ_ESCAPES[stop] else s[i : i + 2])
                i += 2
            elif c == "$":
                i = self.dollar(i, w)
            elif c == "`":
                i = self.backtick(i, w)
            else:
                j = DQ_PLAIN_RE[stop].match(s, i).end()
                w.literal(s[i:j])
                i = j
        return i

    def word(self, i):
        s, n = self.s, self.n
        w, start = Word(), i
        m = ASSIGN_RE.match(s, i)
        if m:
            w.assign = m.group(1)
            w.literal(s[i : m.end()])
            i = m.end()
            if i < n and s[i] == "(":  # NAME=(array words)
                block, i = self.block(i + 1, ")")
                for x in (x for cmd in block for x in all_words(cmd)):
                    w.vars |= x.vars
                    w.subs += x.subs
                    w.expanded = w.expanded or x.expanded
        while i < n:
            c = s[i]
            if c in " \t\r\n;&|()<>":
                break
            if c == "\\":
                if s.startswith("\\\n", i):
                    i += 2
                    continue
                w.literal(s[i + 1 : i + 2])
                w.quoted = True
                i += 2
            elif c == "'":
                j = s.find("'", i + 1)
                j = n if j < 0 else j
                w.literal(s[i + 1 : j])
                w.quoted = True
                i = j + 1
            elif c == '"':
                w.quoted = True
                i = self.dquote(i + 1, w, stop='"')
            elif c == "$":
                i = self.dollar(i, w)
            elif c == "`":
                i = self.backtick(i, w)
            else:
                j = PLAIN_RE.match(s, i).end()
                w.literal(s[i:j])
                i = j
        w.src = s[start:i]
        return w, i

    def block(self, i, close):
        """Parse a command list from i up to an unmatched `close` (")" or None);
        return ([Cmd], the index after it)."""
        s, n = self.s, self.n
        cmds, pipe = [], 0
        cur = Cmd(pipe)
        target, heredocs, depth = None, [], 0
        cases = []  # [depth, state] per open `case`: subject, in, pattern, body

        def finish():
            nonlocal cur
            if cur.words or cur.assigns or cur.redirects:
                cmds.append(cur)
            cur = Cmd(pipe)

        while i < n:
            c = s[i]
            case = cases[-1] if cases and cases[-1][0] == depth else None
            pattern = case is not None and case[1] == "pattern"
            if c in " \t\r":
                i += 1
                continue
            if s.startswith("\\\n", i):
                i += 2
                continue
            if c == ")" and pattern:  # a case pattern ends, its body starts
                case[1] = "body"
                i += 1
                continue
            if c == ")" and close == ")" and depth == 0:
                finish()
                return cmds, i + 1
            if c == "#" and target is None:  # a word starts here: a comment
                j = s.find("\n", i)
                i = n if j < 0 else j
                continue
            if c in "<>" and s.startswith("(", i + 1):  # process substitution
                w = Word()
                sub, j = self.block(i + 2, ")")
                w.subs.append(sub)
                w.expanded = True
                w.src = s[i:j]
                i = j
            else:
                op = c in ";&|\n()<>" and next((o for o in OPERATORS if s.startswith(o, i)), None)
                if op:
                    i += len(op)
                    if pattern:  # `(` before a pattern, `|` between patterns
                        continue
                    if op in REDIRECTS:
                        target = op
                        continue
                    if op == "\n":
                        i = self.read_heredocs(i, heredocs)
                    if case and op in (";;", ";&", ";;&"):
                        finish()
                        case[1] = "pattern"
                        continue
                    depth += {"(": 1, ")": -1}.get(op, 0)
                    finish()
                    if op not in PIPES:
                        pipe += 1
                        cur.pipe = pipe
                    continue
                w, i = self.word(i)
                if (
                    target is None
                    and w.text
                    and w.text.isdigit()
                    and i < n
                    and s[i] in "<>"
                ):
                    continue  # a descriptor number, as in 2>&1
            if target is not None:
                entry = [target, w, None]
                cur.redirects.append(entry)
                if target in ("<<", "<<-"):
                    heredocs.append(entry)
                target = None
            elif case and case[1] in ("subject", "in"):
                case[1] = "in" if case[1] == "subject" else "pattern"
            elif pattern:
                if w.text == "esac":
                    cases.pop()
            elif cur.words or cur.assigns:
                cur.words.append(w)
            elif w.assign:
                cur.assigns.append(w)
            elif w.text == "case":
                cases.append([depth, "subject"])
            elif w.text == "esac" and case:
                cases.pop()
            elif w.text not in KEYWORDS:
                cur.words.append(w)
        self.read_heredocs(i, heredocs)
        finish()
        return cmds, i

    def read_heredocs(self, i, heredocs):
        """Read the bodies of the heredocs opened on the line that just ended.
        An unquoted delimiter's body runs its substitutions and quote removal, so
        its Word replaces the delimiter's and a shell reading it gets that Word's code."""
        s = self.s
        while heredocs:
            entry = heredocs.pop(0)
            op, delim = entry[0], entry[1]
            lines = []
            while i < self.n:
                j = s.find("\n", i)
                j = self.n if j < 0 else j
                line = s[i:j]
                i = j + 1
                if (line.lstrip("\t") if op == "<<-" else line) == "".join(
                    delim.pieces
                ):
                    break
                lines.append(line)
            entry[2] = "\n".join(lines)
            if not delim.quoted:
                entry[1] = Word()
                Parser(entry[2]).dquote(0, entry[1], stop=None)
                entry[2] = code_of(entry[1])
        return min(i, self.n)


def parse(src):
    return Parser(src).block(0, None)[0]


def all_words(cmd):
    return cmd.assigns + cmd.words + [w for _, w, _ in cmd.redirects if w is not None]


def resolve(cmd):
    """(program, its arguments, fed by xargs) after a command's wrappers;
    program None when nothing runs (`command -v` looks a name up)."""
    if cmd.resolved is None:
        words, texts = cmd.words, [w.text or "" for w in cmd.words]
        i, fed = 0, False
        while i < len(words):
            name = os.path.basename(texts[i])
            table = _ced._WRAPPERS.get(name) or EXTRA_WRAPPERS.get(name)
            if table is None:
                break
            j = _ced._options_end(texts, i + 1, *table)
            if name == "command" and any(
                t[:1] == "-" and t != "--" and ("v" in t or "V" in t)
                for t in texts[i + 1 : j]
            ):
                i = len(words)
                break
            while name == "env" and j < len(words) and words[j].assign:
                j += 1
            fed = fed or name == "xargs"
            i = j + POSITIONAL_BEFORE_COMMAND.get(name, 0)
            if name == "flock" and i < len(words) and texts[i] in ("-c", "--command"):
                cmd.resolved = (
                    "sh",
                    words[i:],
                    fed,
                )  # the -c form runs its string in a shell
                return cmd.resolved
        if i >= len(words):
            cmd.resolved = (None, [], fed)
        else:
            cmd.resolved = (os.path.basename(texts[i]), words[i + 1 :], fed)
    return cmd.resolved


def stages(cmd, block):
    """The commands before this one in its pipeline: what feeds its stdin."""
    return [o for o in block[: block.index(cmd)] if o.pipe == cmd.pipe]


def code_blocks(cmd, block):
    """Code a command runs from a string: a shell's -c string or stdin, eval,
    trap, su -c, watch, flock -c."""
    if cmd.inline is None:
        name, args, _ = resolve(cmd)
        texts = []
        if name in _ced._SHELLS:
            kind, code = _ced._shell_call([code_of(a) for a in args])
            if kind == "c" and code:
                texts.append(code)
            elif kind == "stdin":
                texts += [
                    body if body is not None else code_of(w)
                    for c in stages(cmd, block) + [cmd]
                    for op, w, body in c.redirects
                    if body is not None or op == "<<<"
                ]
        elif name == "eval":
            texts.append(" ".join(code_of(a) for a in args))
        elif name == "trap" and args and args[0].text not in ("-p", "-l"):
            texts.append(
                code_of(args[1] if args[0].text == "--" and len(args) > 1 else args[0])
            )
        elif name == "su":
            texts += [
                code_of(b)
                for a, b in zip(args, args[1:])
                if a.text in ("-c", "--command")
            ]
        elif name == "watch":
            j = _ced._options_end([a.text or "" for a in args], 0, "nq", WATCH_LONG)
            texts.append(" ".join(code_of(a) for a in args[j:]))
        cmd.inline = [parse(t) for t in texts]
    return cmd.inline


def blocks(block):
    """This block, then every block nested in it: substitutions and code strings."""
    yield block
    for cmd in block:
        for w in all_words(cmd):
            for sub in w.subs:
                yield from blocks(sub)
        for sub in code_blocks(cmd, block):
            yield from blocks(sub)


class Trace:
    """Where the pids each variable can hold come from, and the numbers typed
    into it, followed to a fixed point over the whole command."""

    def __init__(self, top):
        defs = [
            d for blk in blocks(top) for cmd in blk for d in self.definitions(cmd, blk)
        ]
        self.defined = {name for name, _, _ in defs}
        self.verdicts, self.values = {}, {}
        for _ in range(16):  # bounded: a=$b; b=-$a grows a value every round
            before = (len(self.verdicts), sum(map(len, self.values.values())))
            for name, feed, reader in defs:
                if name not in self.verdicts:
                    verdict = (
                        self.stdin(*reader, wide=True) if reader else self.first(feed)
                    )
                    if verdict:
                        self.verdicts[name] = verdict
                for w in feed:
                    self.values.setdefault(name, set()).update(self.typed(w))
            if (len(self.verdicts), sum(map(len, self.values.values()))) == before:
                return

    @staticmethod
    def definitions(cmd, blk):
        """(variable, the words it takes its value from, (cmd, blk) when it reads stdin)."""
        name, args, _ = resolve(cmd)
        declared = [a for a in args if a.assign] if name in DECLARES else []
        out = [(w.assign, [w], None) for w in cmd.assigns + declared]
        words = cmd.words
        if len(words) > 1 and words[0].text in ("for", "select"):
            feed = words[3:] if len(words) > 2 and words[2].text == "in" else []
            out.append((words[1].text or "", feed, None))
        if name in ("read", "mapfile", "readarray"):
            names = [a.text for a in args if a.text and NAME_RE.fullmatch(a.text)]
            out += [(n, [], (cmd, blk)) for n in names or ["REPLY", "MAPFILE"]]
        return out

    def first(self, words):
        return next((v for v in map(self.word, words) if v), None)

    def word(self, w):
        """None when every pid this word can carry is the caller's own, else (kind, where from)."""
        if SYSTEM_PATH_RE.search("".join(w.pieces)):
            return SYSTEM_READ
        for v in sorted(w.vars - OWN):
            if v in self.verdicts:
                return self.verdicts[v]
            if v == "PPID":
                return (
                    "lookup",
                    "$PPID, the tool shell's parent: this session's own claude",
                )
            if v not in self.defined:
                return "unknown", f"${v}, which is not set in this command"
        return next((v for v in (self.block(sub) for sub in w.subs) if v), None)

    def block(self, blk):
        return next((v for v in (self.command(cmd, blk) for cmd in blk) if v), None)

    def command(self, cmd, blk):
        """None when all this command can print is the caller's own pids."""
        for op, w, _ in cmd.redirects:
            if op in INPUTS and w is not None and (verdict := self.input(op, w)):
                return verdict
        if not cmd.words or cmd.words[0].text in ("for", "select"):
            return (
                None  # an assignment, a redirect alone or a loop header prints nothing
            )
        name, args, _ = resolve(cmd)
        if name in PRINTERS:
            return self.first(args)
        if name in READERS:
            return next((v for v in map(self.path, args) if v), None)
        if name in LOOKUPS:
            return "lookup", f"`{name}`, a process lookup"
        return (
            "unknown",
            f"`{name or cmd.words[0].src}`, which the guard does not know as yours",
        )

    def stdin(self, cmd, blk, wide):
        """None when all this command can read on stdin is the caller's own pids:
        its pipeline's earlier stages and its input redirects (a loop's redirect
        sits after `done`, so a read takes the whole block's: `wide`)."""
        feeds = [self.command(o, blk) for o in stages(cmd, blk)]
        feeds += [
            self.input(op, w)
            for c in (blk if wide else [cmd])
            for op, w, _ in c.redirects
            if op in INPUTS
        ]
        if not feeds:
            return "unknown", "a read with no input the guard can see"
        return next((v for v in feeds if v), None)

    def path(self, w):
        """A reader's argument names a file, and the file's content is what is read,
        so a name is a lookup only when it points under /proc or /sys, wherever it
        came from (W=$(mktemp -d); cat $W/pid is a pid file). A process
        substitution is content, not a name."""
        if w.src.startswith(("<(", ">(")):
            return self.word(w)
        names = ["".join(w.pieces), *self.typed(w)]
        names += ["".join(x.pieces) for sub in w.subs for b in blocks(sub) for c in b for x in all_words(c)]
        if any(map(SYSTEM_PATH_RE.search, names)) or any(self.verdicts.get(v) == SYSTEM_READ for v in w.vars):
            return SYSTEM_READ
        return None

    def input(self, op, w):
        """An input redirect: `<` names a file; a here-string or heredoc is content."""
        return self.path(w) if op == "<" else self.word(w)

    def typed(self, w):
        """The literal values a word can hold: its own text, or one variable's typed values."""
        text = "".join(w.pieces)
        if w.assign:
            text = text.split("=", 1)[1]
        if not w.expanded:
            return {text}
        if not w.subs and len(w.vars) == 1 and text in ("", "-"):
            return {text + v for v in self.values.get(next(iter(w.vars)), ())}
        return set()


@functools.lru_cache(maxsize=None)
def ancestry():
    """(pids, process groups) from this process up to PID 1: /proc on Linux,
    else one ps snapshot (macOS). supervisor-caller.py's ancestry() forks ps
    for every step and keeps no groups, so it is not called here."""
    if os.path.exists("/proc/self/stat"):
        parent = _proc_stat
    else:
        import subprocess  # only here: a cost every allowed command would pay

        try:
            rows = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=5
            ).stdout.split("\n")
        except (OSError, subprocess.SubprocessError):
            rows = []
        table = {int(r[0]): (int(r[1]), int(r[2])) for r in map(str.split, rows) if len(r) == 3}
        parent = table.get
    pids, pgids, pid = {1}, set(), os.getpid()
    for _ in range(64):
        st = parent(pid)
        if st is None:
            break
        pids.add(pid)
        pgids.add(st[1])
        if pid <= 1:
            break
        pid = st[0]
    return pids, pgids


def _proc_stat(pid):
    """(parent pid, process group) of pid from /proc, or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return int(fields[1]), int(fields[2])
    except (OSError, IndexError, ValueError):
        return None


def kill_operands(args):
    """(sends a signal, operands) for kill's arguments."""
    sig, i = "TERM", 0
    while i < len(args):
        t = args[i].text
        if t is None:
            if i == 0 and code_of(args[0]).startswith("-"):
                sig, i = "?", 1  # -$SIG: a signal, whatever it expands to
                continue
            break
        if t == "--":
            i += 1
            break
        if t in ("-l", "-L", "--list", "--table", "-p", "--pid") or t.startswith(
            "--list="
        ):
            return False, []
        if t in ("-s", "-n", "--signal"):
            sig = (args[i + 1].text or "?") if i + 1 < len(args) else "?"
            i += 2
        elif t.startswith("--signal="):
            sig, i = t.split("=", 1)[1], i + 1
        elif t in ("-q", "--queue"):
            i += 2
        elif t == "--timeout":
            i += 3
        elif t in ("-a", "--all", "--verbose", "-r", "--require-handler"):
            i += 1
        elif t.startswith("-") and len(t) > 1 and i == 0:
            sig, i = t[1:], 1
        else:
            break
    return sig.upper().removeprefix("SIG") != "0", args[i:]


def sends_signal(cmd):
    name, args, _ = resolve(cmd)
    return name in SELECTORS or (name == "kill" and kill_operands(args)[0])


def _quote(w):
    src = " ".join(w.src.split())
    return src if len(src) <= 60 else src[:57] + "..."


def literal_finding(t, fed, held_by=None):
    """A typed operand's finding: its own text, or a value a variable holds."""
    held = f"`{_quote(held_by)}` holds {t}: " if held_by else ""
    if t.startswith("%") or t == "0":
        return None
    if t == "-1":
        return (
            "every-process",
            f"{held}kill -1 signals every process this user can reach",
        )
    m = re.fullmatch(r"(-?)(\d+)", t)
    if m:
        pids, pgids = ancestry()
        n = int(m.group(2))
        if n in pids or (m.group(1) and n in pgids):
            return (
                "ancestor",
                f"{held}pid {t} is an ancestor of this session: its claude, "
                "its tmux server, its subreaper, the user manager or PID 1",
            )
        return None
    if not fed and PROCESS_NAME_RE.fullmatch(t):  # under xargs a word is a placeholder
        return "by-name", f"{held}kill {t} picks processes by name"
    return None


def operand_finding(w, trace, fed):
    if not w.expanded:
        return literal_finding(w.text, fed)
    for value in sorted(trace.typed(w)):
        finding = literal_finding(value, fed, held_by=w)
        if finding:
            return finding
    verdict = trace.word(w)
    if verdict:
        return verdict[0], f"`{_quote(w)}` carries a pid from {verdict[1]}"
    return None


def command_findings(cmd, blk, trace):
    name, args, fed = resolve(cmd)
    found = []
    inner = (c for sub in code_blocks(cmd, blk) for b in blocks(sub) for c in b)
    if fed and (sends_signal(cmd) or any(map(sends_signal, inner))):
        verdict = trace.stdin(cmd, blk, wide=False)
        if verdict:
            found.append((verdict[0], f"`xargs` feeds a signal pids from {verdict[1]}"))
    if name in SELECTORS:
        found.append(("selector", f"`{name}` picks processes by name or pattern"))
    elif name == "fuser" and any(
        a.text == "--kill" or re.fullmatch(r"-[a-zA-Z]*k[a-zA-Z]*", a.text or "")
        for a in args
    ):
        found.append(("fuser", "`fuser -k` signals whatever holds the file or port"))
    elif name == "find" and any(
        a.text in ("-exec", "-execdir", "-ok", "-okdir")
        and (os.path.basename(b.text or "") in SELECTORS or b.text == "kill")
        for a, b in zip(args, args[1:])
    ):
        found.append(("unknown", "`find -exec` signals whatever find matched"))
    elif name == "kill":
        sends, operands = kill_operands(args)
        for w in operands if sends else []:
            finding = operand_finding(w, trace, fed)
            if finding:
                found.append(finding)
    return found


def decide(command):
    """The findings that refuse a command: [(kind, reason)], empty to allow."""
    top = parse(command)
    if not any(resolve(cmd)[0] in SIGNALLERS for blk in blocks(top) for cmd in blk):
        return []  # nothing here can send a signal: skip the trace
    trace, found = Trace(top), []
    for blk in blocks(top):
        for cmd in blk:
            for finding in command_findings(cmd, blk, trace):
                if finding not in found:
                    found.append(finding)
    return found


def main() -> int:
    command = sys.stdin.read()
    if command.endswith("\n"):
        command = command[:-1]  # the here-string's newline
    found = decide(command)
    if not found:
        print("allow")
        return 0
    guardrail = (
        GUARDRAIL
        if GUARDRAIL.is_file()
        else "signal-only-what-you-started.md in the library"
    )
    reason = (
        f"signal-guard refused this command: {'; '.join(r for _, r in found)}. Bots on one host "
        "share a user, so a pid you looked up can belong to another bot, and an orphaned job's "
        "parent can be the user manager that runs every bot (#2158). Stop only what you started: save "
        '$! when you start a job (job & echo $! > job.pid), then kill "$(cat job.pid)"; in the '
        "same command use kill %1 or kill $!; stop a background tool call with its own stop "
        f"control. kill -0 is always allowed. Guardrail: {guardrail} (#1069)."
    )
    kinds = ",".join(dict.fromkeys(kind for kind, _ in found))
    print(f"deny\t{kinds}\t{' '.join(reason.split())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
