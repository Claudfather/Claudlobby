#!/usr/bin/env python3
"""signal-decide.py — the decision half of signal-guard.sh (#1069).

Reads one Bash command on stdin and says whether it sends a signal to a process
the caller did not start. Prints one line:

    allow
    deny<TAB><kinds><TAB><reason>

<kinds> lists what was refused, comma-separated, for the plane record:
selector, fuser, every-process, by-name, lookup, xargs, ancestor.

Killing by pid is safe only for a pid the same command started, so the decision
keys on where a pid came from, not on the verb alone:
- pkill, killall, killall5 and skill pick processes by name or pattern, and
  fuser -k picks whatever holds a file or port: refused outright.
- A kill operand read back from a process lookup (ps, pgrep, pidof, pstree,
  lsof, fuser, ss, netstat, top, tmux, screen, a read under /proc) or from
  $PPID is refused, whether it arrives directly, through a variable, a for
  loop, a read loop or xargs.
- kill -1 (every process the user can signal) and kill NAME (util-linux kills
  by name) are refused.
- A pid or group typed as a number shows no provenance, so its target decides:
  refused when it is an ancestor of this hook (the session's claude, its tmux
  server, the user manager, PID 1), allowed otherwise.
Allowed: $!, $$, $BASHPID, job specs (%1), group 0, a pid read from a file,
and kill -0 and kill -l, which send nothing.

Bounds, so nobody reads this as a fix for the class. It reads the command text:
a tripwire for honest mistakes, not a sandbox. A script file, eval of a
variable, a function or alias, a pid passed through a file, a name built by
expansion or spelled in escapes, a command echoed into a shell, and a signal
sent from another interpreter are out of its reach, and a typed pid of another
bot's process passes. The per-bot subreaper of #2158 is the backstop for those.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

LOOKUPS = frozenset(
    "ps pgrep pidof pstree lsof fuser ss netstat top htop tmux screen".split()
)
SELECTORS = frozenset("pkill killall killall5 skill".split())
SHELLS = frozenset("sh bash dash zsh ksh".split())
# Reserved words skipped where a command starts; the closing ones end a command.
KEYWORDS = frozenset("if then else elif do while until ! { } in fi done".split())
CLOSERS = frozenset("fi done }".split())
SEPARATORS = frozenset([";", "&", "&&", "||", "\n", "(", ")", ";;", ";&", ";;&"])
PIPES = frozenset(["|", "|&"])
REDIRECTS = frozenset(
    ["<", ">", ">>", "<<", "<<-", "<<<", "<&", ">&", "&>", "&>>", "<>", ">|"]
)
OPERATORS = sorted(SEPARATORS | PIPES | REDIRECTS, key=len, reverse=True)
# Programs that run the command after their own options, and the options among
# those that consume the next word.
WRAPPERS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user"},
    "doas": {"-u", "-C"},
    "nohup": set(),
    "exec": {"-a"},
    "command": set(),
    "builtin": set(),
    "time": {"-f", "-o", "--format", "--output"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "--class", "--classdata"},
    "setsid": set(),
    "stdbuf": {"-i", "-o", "-e"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "flock": {"-w", "-E", "--wait", "--timeout", "--conflict-exit-code"},
    "xargs": {
        "-a",
        "-d",
        "-E",
        "-I",
        "-L",
        "-n",
        "-P",
        "-s",
        "--arg-file",
        "--delimiter",
        "--max-args",
        "--max-lines",
        "--max-procs",
        "--max-chars",
        "--replace",
    },
}
POSITIONAL_BEFORE_COMMAND = {"timeout": 1, "flock": 1}  # the duration, the lock file
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^]]*\])?\+?=")
PROCESS_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.+-]*")
PROC_RE = re.compile(r"(?<![\w.])/proc(/|$)")
SAFE = (
    "Bots on one host share a user, so a pid you looked up can belong to another "
    "bot, and an orphaned job's parent is the user manager that runs every bot "
    "(#2158). Stop only what you started: save $! when you start a job "
    '(job & echo $! > job.pid), then kill "$(cat job.pid)"; in the same command '
    "use kill %1 or kill $!; stop a background tool call with its own stop "
    "control. kill -0 is always allowed. Guardrail: "
    "library/guardrails/signal-only-what-you-started.md (#1069)."
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


class Cmd:
    """A simple command, with the pipeline it stands in."""

    def __init__(self, pipe):
        self.words = []
        self.assigns = []
        self.redirects = []  # [operator, target Word, heredoc body, body Word]
        self.pipe = pipe
        self.inline = None  # code-string blocks, parsed once by code_blocks


class Parser:
    def __init__(self, src):
        self.s = src
        self.n = len(src)

    def dollar(self, i, w):
        s, n = self.s, self.n
        nxt = s[i + 1] if i + 1 < n else ""
        if s.startswith("$((", i):  # arithmetic: its names are variables
            depth, j = 0, i + 1
            while j < n:
                depth += {"(": 1, ")": -1}.get(s[j], 0)
                if depth == 0:
                    break
                j += 1
            w.vars.update(NAME_RE.findall(s[i + 3 : j]))
            w.expanded = True
            w.code.append(s[i : j + 1])
            return j + 1
        if nxt == "(":
            block, j = self.block(i + 2, ")")
            w.subs.append(block)
            w.expanded = True
            w.code.append(s[i:j])
            return j
        if nxt == "{":
            depth, j = 0, i + 1
            while j < n:
                depth += {"{": 1, "}": -1}.get(s[j], 0)
                if depth == 0:
                    break
                j += 1
            inner = s[i + 2 : j]
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*|[!$#?@*0-9-]", inner)
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
            w.code.append(s[i : j + 1])
            return j + 1
        if nxt == "'":  # $'...', kept as written
            j = i + 2
            while j < n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            w.literal(s[i + 2 : j], s[i : j + 1])
            w.quoted = True
            return j + 1
        if nxt == '"':
            w.quoted = True
            return self.dquote(i + 2, w, stop='"')
        m = NAME_RE.match(s, i + 1)
        if m:
            w.vars.add(m.group(0))
            w.expanded = True
            w.code.append(s[i : m.end()])
            return m.end()
        if nxt and nxt in "!$#?@*-0123456789":
            w.vars.add(nxt)
            w.expanded = True
            w.code.append(s[i : i + 2])
            return i + 2
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
        w.subs.append(Parser("".join(inner)).block(0, None)[0])
        w.expanded = True
        w.code.append(s[i : j + 1])
        return j + 1

    def dquote(self, i, w, stop):
        s, n = self.s, self.n
        while i < n:
            c = s[i]
            if stop and c == stop:
                return i + 1
            if c == "\\" and i + 1 < n:
                w.literal(
                    s[i + 1] if s[i + 1] in '$`"\\\n' else s[i : i + 2], s[i : i + 2]
                )
                i += 2
            elif c == "$":
                i = self.dollar(i, w)
            elif c == "`":
                i = self.backtick(i, w)
            else:
                w.literal(c)
                i += 1
        return i

    def word(self, i, close):
        s, n = self.s, self.n
        w, start = Word(), i
        m = ASSIGN_RE.match(s, i)
        if m:
            w.assign = m.group(1)
            w.literal(s[i : m.end()])
            i = m.end()
            if i < n and s[i] == "(":  # NAME=(array words)
                block, i = self.block(i + 1, ")")
                for cmd in block:
                    for x in cmd.words:
                        w.vars |= x.vars
                        w.subs += x.subs
                        w.expanded = w.expanded or x.expanded
                w.src = s[start:i]
                return w, i
        while i < n:
            c = s[i]
            if c in " \t\r\n;&|()<>" or (close == "`" and c == "`"):
                break
            if c == "\\":
                if s.startswith("\\\n", i):
                    i += 2
                    continue
                w.literal(s[i + 1 : i + 2], s[i : i + 2])
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
                w.literal(c)
                i += 1
        w.src = s[start:i]
        return w, i

    def block(self, i, close):
        """Parse a command list from i up to an unmatched `close` (")" or "`");
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
            if c == "`" and close == "`":
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
                op = next((o for o in OPERATORS if s.startswith(o, i)), None)
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
                w, i = self.word(i, close)
                if (
                    target is None
                    and w.text
                    and w.text.isdigit()
                    and i < n
                    and s[i] in "<>"
                ):
                    continue  # a descriptor number, as in 2>&1
            if target is not None:
                entry = [target, w, None, None]
                cur.redirects.append(entry)
                if target in ("<<", "<<-"):
                    heredocs.append(
                        ("".join(w.pieces), target == "<<-", not w.quoted, entry)
                    )
                target = None
            elif case and case[1] == "subject":
                case[1] = "in"
            elif case and case[1] == "in":
                case[1] = "pattern"
            elif pattern:
                if w.text == "esac":
                    cases.pop()
                    finish()
            elif w.assign and not cur.words:
                cur.assigns.append(w)
            elif not cur.words and not cur.assigns and w.text == "case":
                cases.append([depth, "subject"])
            elif not cur.words and not cur.assigns and w.text == "esac" and case:
                cases.pop()
                finish()
            elif not cur.words and not cur.assigns and w.text in KEYWORDS:
                if w.text in CLOSERS:
                    finish()
            else:
                cur.words.append(w)
        self.read_heredocs(i, heredocs)
        finish()
        return cmds, i

    def read_heredocs(self, i, heredocs):
        """Read the bodies of the heredocs opened on the line that just ended.
        An unquoted delimiter's body runs its substitutions."""
        s = self.s
        while heredocs:
            delim, strip, expands, entry = heredocs.pop(0)
            lines = []
            while i < self.n:
                j = s.find("\n", i)
                j = self.n if j < 0 else j
                line = s[i:j]
                i = j + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    break
                lines.append(line)
            entry[2] = "\n".join(lines)
            if expands:
                entry[3] = Word()
                Parser(entry[2]).dquote(0, entry[3], stop=None)
        return min(i, self.n)


def parse(src):
    return Parser(src).block(0, None)[0]


def resolve(cmd):
    """(program, its arguments, xargs replace-string, fed by xargs) for a command."""
    words, i, fed, replace = cmd.words, 0, False, None
    while i < len(words):
        name = os.path.basename(words[i].text or "")
        if name not in WRAPPERS:
            break
        takes = WRAPPERS[name]
        fed = fed or name == "xargs"
        i += 1
        while i < len(words):
            t = words[i].text or ""
            if t == "--":
                i += 1
                break
            if name == "env" and words[i].assign:
                i += 1
            elif t.startswith("-") and len(t) > 1:
                if name == "xargs" and (t == "--replace" or t[:2] in ("-I", "-i")):
                    nxt = (
                        words[i + 1].text if t == "-I" and i + 1 < len(words) else None
                    )
                    replace = t[2:] or nxt or "{}"
                i += 2 if t in takes else 1
            else:
                break
        i += POSITIONAL_BEFORE_COMMAND.get(name, 0)
    if i >= len(words):
        return None, [], replace, fed
    return os.path.basename(words[i].text or ""), words[i + 1 :], replace, fed


def stdin_texts(cmd, block):
    """Text a command reads on stdin: its own heredocs and here-strings, and
    those of the stages before it in its pipeline (cat <<EOF | bash)."""
    texts = []
    for other in block:
        if other is cmd or other.pipe == cmd.pipe:
            for op, w, body, _ in other.redirects:
                if body is not None:
                    texts.append(body)
                elif op == "<<<":
                    texts.append("".join(w.code))
        if other is cmd:
            break
    return texts


def code_blocks(cmd, block):
    """Code a command runs from a string: sh -c, a shell's stdin, eval, trap,
    su -c, watch."""
    if cmd.inline is not None:
        return cmd.inline
    name, args, _, _ = resolve(cmd)
    texts = []
    if name in SHELLS:
        for k, a in enumerate(args):
            t = a.text or ""
            if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
                if k + 1 < len(args):
                    texts.append("".join(args[k + 1].code))
                break
            if not t.startswith("-"):
                break  # a script file: out of reach
        else:
            texts += stdin_texts(cmd, block)
    elif name == "eval":
        texts.append(" ".join("".join(a.code) for a in args))
    elif name == "trap" and args and (args[0].text or "") not in ("-p", "-l"):
        first = args[1] if args[0].text == "--" and len(args) > 1 else args[0]
        texts.append("".join(first.code))
    elif name == "su":
        for k, a in enumerate(args[:-1]):
            if a.text in ("-c", "--command"):
                texts.append("".join(args[k + 1].code))
    elif name == "watch":
        rest = [a for a in args if not (a.text or "").startswith("-")]
        texts.append(" ".join("".join(a.code) for a in rest))
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


def all_words(cmd):
    redirected = [x for _, w, _, b in cmd.redirects for x in (w, b) if x is not None]
    return cmd.assigns + cmd.words + redirected


def word_tainted(w, tainted):
    return bool(w.vars & tainted) or any(looks_up(sub, tainted) for sub in w.subs)


def looks_up(block, tainted):
    """True when a block's output can carry pids read back from a lookup."""
    for blk in blocks(block):
        for cmd in blk:
            if resolve(cmd)[0] in LOOKUPS:
                return True
            for w in all_words(cmd):
                if PROC_RE.search("".join(w.pieces)) or word_tainted(w, tainted):
                    return True
    return False


def fed_by_lookup(cmd, block, tainted):
    """True when a lookup feeds this command's stdin: an earlier stage of its
    pipeline, or a redirected source such as < <(pgrep x)."""
    for other in block:
        if other is cmd:
            break
        if other.pipe == cmd.pipe and looks_up([other], tainted):
            return True
    return any(
        op in ("<", "<<<") and word_tainted(w, tainted)
        for c in block
        for op, w, _, _ in c.redirects
    )


def tainted_vars(top):
    """Variables that hold pids read back from a lookup, to a fixed point."""
    tainted = {"PPID"}
    while True:
        before = len(tainted)
        for blk in blocks(top):
            for cmd in blk:
                name, args, _, _ = resolve(cmd)
                for w in cmd.assigns + [a for a in args if a.assign]:
                    if word_tainted(w, tainted):
                        tainted.add(w.assign)
                words = cmd.words
                if len(words) > 3 and words[0].text in ("for", "select"):
                    if words[2].text == "in" and any(
                        word_tainted(x, tainted) for x in words[3:]
                    ):
                        tainted.add(words[1].text or "")
                if name in ("read", "mapfile", "readarray") and fed_by_lookup(
                    cmd, blk, tainted
                ):
                    names = [
                        a.text for a in args if a.text and NAME_RE.fullmatch(a.text)
                    ]
                    tainted.update(names or ["REPLY", "MAPFILE"])
        if len(tainted) == before:
            return tainted


class Ancestors:
    """Pids and process groups from this process up to PID 1, read on demand."""

    def __init__(self):
        self.pids = None
        self.pgids = set()

    @staticmethod
    def _stat(pid):
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                fields = fh.read().rsplit(b")", 1)[1].split()
            return int(fields[1]), int(fields[2])
        except OSError:
            out = subprocess.run(
                ["ps", "-o", "ppid=,pgid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.split()
            return (int(out[0]), int(out[1])) if len(out) == 2 else None

    def hit(self, n, group):
        if self.pids is None:
            self.pids, pid = {1}, os.getpid()
            for _ in range(64):
                st = self._stat(pid)
                if st is None:
                    break
                self.pids.add(pid)
                self.pgids.add(st[1])
                if pid <= 1:
                    break
                pid = st[0]
        return n in self.pids or (group and n in self.pgids)


def kill_operands(args):
    """(sends a signal, operands) for kill's arguments."""
    sig, i = "TERM", 0
    while i < len(args):
        t = args[i].text
        if t is None:
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


def _quote(w):
    src = " ".join(w.src.split())
    return src if len(src) <= 60 else src[:57] + "..."


def operand_finding(w, tainted, ancestors, replace):
    t = w.text
    if t is not None:
        if t.startswith("%") or t == "0" or (replace and replace in t):
            return None
        if t == "-1":
            return (
                "every-process",
                "`kill -1` signals every process this user can reach",
            )
        m = re.fullmatch(r"(-?)(\d+)", t)
        if m:
            if ancestors.hit(int(m.group(2)), bool(m.group(1))):
                return (
                    "ancestor",
                    f"pid {t} is an ancestor of this session: its claude, "
                    "its tmux server, the user manager or PID 1",
                )
            return None
        if PROCESS_NAME_RE.fullmatch(t):
            return "by-name", f"`kill {t}` picks processes by name"
        return None
    if w.vars & tainted:
        return (
            "lookup",
            f"`{_quote(w)}` holds a pid read back from a process lookup or $PPID",
        )
    if any(looks_up(sub, tainted) for sub in w.subs):
        return "lookup", f"`{_quote(w)}` reads pids back from a process lookup"
    return None


def sends_signal(cmd):
    name, args, _, _ = resolve(cmd)
    return name in SELECTORS or (name == "kill" and kill_operands(args)[0])


def command_findings(cmd, block, tainted, ancestors):
    name, args, replace, fed = resolve(cmd)
    found = []
    if fed and fed_by_lookup(cmd, block, tainted):
        inner = (c for sub in code_blocks(cmd, block) for b in blocks(sub) for c in b)
        if sends_signal(cmd) or any(sends_signal(c) for c in inner):
            found.append(
                ("xargs", "`xargs` signals pids read back from a process lookup")
            )
    if name in SELECTORS:
        found.append(("selector", f"`{name}` picks processes by name or pattern"))
    elif name == "fuser" and any(
        a.text == "--kill" or re.fullmatch(r"-[a-zA-Z]*k[a-zA-Z]*", a.text or "")
        for a in args
    ):
        found.append(("fuser", "`fuser -k` signals whatever holds the file or port"))
    elif name == "kill":
        sends, operands = kill_operands(args)
        for w in operands if sends else []:
            finding = operand_finding(w, tainted, ancestors, replace)
            if finding:
                found.append(finding)
    return found


def decide(command):
    """The findings that refuse a command: [(kind, reason)], empty to allow."""
    top = parse(command)
    tainted = tainted_vars(top)
    ancestors, found = Ancestors(), []
    for blk in blocks(top):
        for cmd in blk:
            for finding in command_findings(cmd, blk, tainted, ancestors):
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
    kinds = ",".join(dict.fromkeys(kind for kind, _ in found))
    reason = "; ".join(r for _, r in found)
    reason = f"signal-guard refused this command: {reason}. {SAFE}"
    print(f"deny\t{kinds}\t{' '.join(reason.split())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
