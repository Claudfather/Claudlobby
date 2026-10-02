#!/usr/bin/env python3
"""credential-echo-decide.py — the decision half of credential-echo-guard.sh (#2090).

Reads one Bash command on stdin and says whether it runs a CLI form that
prints a credential it reads from the environment (table A of #2090) while
that variable is still in place. Prints one line:

    allow
    deny<TAB><row><TAB><cli><TAB><reason>
    unparsed<TAB><detail>

The reason names the safe form and never repeats anything from the command,
which may itself carry a value.

A variable is removed for a command by `env -u VAR` or `env -i` in front of
it, an empty `VAR=` assignment in front of it, or an `unset VAR` (or a bare
`VAR=` or `export VAR=`) earlier in the same command line, at the same or an
outer level and not as a pipeline or background element. Nothing else
counts: whether the hook's own environment holds the variable says nothing
about the shell the Bash tool starts.

Bounds, so nobody reads this as a fix for the class: it is a command filter.
A `bash -c` or `sh -c` string is followed; `eval`, a script, an alias,
`xargs` and a command built from a variable are not. Output that a CLI
already printed is out of reach.
"""

from __future__ import annotations

import os
import re
import sys

# Table A of #2090 as data: one entry per refused form. `vars` must all be
# removed in the same command for an `unset` row to pass; `refuse` rows pass
# never; `stdout-to-file` passes only when stdout goes to a file.
ROWS = {
    "neon-help": {
        "action": "unset", "vars": ("NEON_API_KEY",),
        "reason": "`{cli}` help shows NEON_API_KEY from the environment as the default of "
                  "--api-key, so the key would land in the transcript. Run the same command "
                  "with the key removed: prefix it with `env -u NEON_API_KEY`.",
    },
    "neon-usage": {
        "action": "unset", "vars": ("NEON_API_KEY",),
        "reason": "`{cli}` prints its help here (a bare call, an unknown command, a command "
                  "group without a verb, or an unknown or incomplete option), and that help "
                  "shows NEON_API_KEY from the environment as the default of --api-key. Run "
                  "the same command with the key removed: prefix it with `env -u NEON_API_KEY`.",
    },
    "neon-debug": {
        "action": "refuse", "vars": ("NEON_API_KEY",),
        "reason": "A DEBUG assignment in front of `{cli}` prints its HTTP request headers, "
                  "including the NEON_API_KEY bearer token; removing the key does not help, "
                  "because the call then sends the stored login's token. Drop the DEBUG "
                  "assignment.",
    },
    "pip-config": {
        "action": "unset", "vars": ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"),
        "reason": "`pip config list` and `pip config debug` print PIP_INDEX_URL and "
                  "PIP_EXTRA_INDEX_URL, which can carry a password inside the URL. Run it "
                  "with them removed: prefix it with "
                  "`env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL`.",
    },
    "gh-auth-token": {
        "action": "stdout-to-file", "vars": ("GH_TOKEN", "GITHUB_TOKEN"),
        "reason": "`gh auth token` prints the token (GH_TOKEN or GITHUB_TOKEN when set, "
                  "otherwise the stored login's) into the transcript, and removing the "
                  "variables does not help. Send its stdout to a file (`gh auth token > FILE`), "
                  "or let gh read the token itself.",
    },
}

# neonctl's command tree (2.22.0, from its own help): what prints help.
NEON = {
    "clis": ("neonctl", "neon"),
    "groups": ("orgs", "org", "projects", "project", "ip-allow", "vpc", "branches", "branch",
               "databases", "database", "db", "roles", "role", "operations", "operation"),
    "leaves": ("auth", "login", "me", "connection-string", "cs", "set-context", "init",
               "completion"),
    "valued": ("-o", "--output", "--config-dir", "--api-key", "--context-file"),
    "flags": ("--color", "--no-color", "--analytics", "--no-analytics", "-h", "--help",
              "-v", "--version"),
    "help_never_echoes": ("completion",),
}

_OPS = re.compile(r"&>>|&>|>>|>\||>&|<<<|<<-|<<|<&|<>|&&|\|\||\|&|;;&|;;|;&|>|<|[;&|()]")
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_SPECIAL = {"/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1", "/dev/fd/2",
            "/proc/self/fd/1", "/proc/self/fd/2"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_PERSIST = {";", "&&", "||", "\n", None}


class Unparsed(Exception):
    pass


def _tokens(command: str) -> list:
    """("word", text, quoted) / ("op", op) / ("redir", fd, op): quotes removed,
    line continuations joined, comments and heredoc bodies skipped."""
    s = command.replace("\\\n", "")
    i, n = 0, len(s)
    out, word, quoted, heredocs = [], None, False, []

    def flush():
        nonlocal word, quoted
        if word is not None:
            out.append(("word", word, quoted))
            if heredocs and heredocs[-1] is None:
                heredocs[-1] = word  # the first word after << is its delimiter
        word, quoted = None, False

    while i < n:
        c = s[i]
        if c in " \t\r":
            flush()
            i += 1
        elif c == "\n":
            flush()
            out.append(("op", "\n"))
            i += 1
            for delim in heredocs:  # a heredoc body is data, never commands
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:] if j < 0 else s[i:j]
                    i = n if j < 0 else j + 1
                    if line.strip() == delim:
                        break
            heredocs = []
        elif c == "#" and word is None:
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise Unparsed("unbalanced single quote")
            word, quoted, i = (word or "") + s[i + 1:j], True, j + 1
        elif c == "$" and s.startswith("$'", i):
            j, buf = i + 2, []
            while j < n and s[j] != "'":
                buf.append(s[j + 1] if s[j] == "\\" and j + 1 < n else s[j])
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                raise Unparsed("unbalanced ANSI-C quote")
            word, quoted, i = (word or "") + "".join(buf), True, j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n and s[j + 1] in '"\\$`':
                    buf.append(s[j + 1])
                    j += 2
                else:
                    buf.append(s[j])
                    j += 1
            if j >= n:
                raise Unparsed("unbalanced double quote")
            word, quoted, i = (word or "") + "".join(buf), True, j + 1
        elif c == "\\":
            word, i = (word or "") + (s[i + 1] if i + 1 < n else ""), i + 2
        elif c == "$" and s.startswith("$(", i) and not s.startswith("$((", i):
            flush()
            out.append(("op", "("))
            i += 2
        elif c == "`":
            flush()
            out.append(("op", "`"))
            i += 1
        elif c in "();|&<>":
            op = _OPS.match(s, i).group(0)
            if op in (">", "<") and s.startswith("(", i + 1):  # process substitution
                flush()
                out.append(("op", "("))
                i += 2
                continue
            if op[0] in "<>" or op.startswith("&>"):
                fd = word if (word is not None and word.isdigit() and not quoted) else ""
                if fd:
                    word = None
                flush()
                out.append(("redir", fd, op))
                if op in ("<<", "<<-"):
                    heredocs.append(None)  # filled by the delimiter word
            else:
                flush()
                out.append(("op", op))
            i += len(op)
        else:
            word = (word or "") + c
            i += 1
    flush()
    return out


def _commands(tokens: list) -> list:
    """Simple commands: dict(words, redirs, depth, sep) in order."""
    cmds, cur, depth, backtick, want_target = [], None, 0, False, None

    def close(sep):
        nonlocal cur
        if cur is not None and (cur["words"] or cur["redirs"]):
            cur["sep"] = sep
            cmds.append(cur)
        cur = None

    for tok in tokens:
        if cur is None:
            cur = {"words": [], "redirs": [], "depth": depth, "sep": None}
        if tok[0] == "word":
            if want_target is not None:
                want_target.append(tok[1])
                want_target = None
            elif tok[1] in ("{", "}") and not tok[2] and not cur["words"]:
                continue
            else:
                cur["words"].append(tok[1])
        elif tok[0] == "redir":
            want_target = [tok[1], tok[2]]
            cur["redirs"].append(want_target)
        else:
            op = tok[1]
            if op == "(" or (op == "`" and not backtick):
                close(None)
                depth += 1
                backtick = backtick or op == "`"
            elif op == ")" or op == "`":
                close(None)
                depth = max(0, depth - 1)
                if op == "`":
                    backtick = False
            else:
                close(op)
    close(None)
    return cmds


def _strip(words: list, state: dict):
    """Leading assignments and wrappers: update `state` (name -> "removed" or
    "set", "*" -> cleared) and return the words that remain."""
    i = 0
    while i < len(words):
        m = _ASSIGN.match(words[i])
        if m:
            state[m.group(1)] = "removed" if m.group(2) == "" else "set"
            i += 1
            continue
        base = os.path.basename(words[i])
        if base == "env":
            i += 1
            while i < len(words):
                w = words[i]
                if w in ("-i", "--ignore-environment", "-"):
                    state.clear()
                    state["*"] = "removed"
                elif w == "-u" or w == "--unset":
                    if i + 1 < len(words):
                        state[words[i + 1]] = "removed"
                        i += 1
                elif w.startswith("--unset="):
                    state[w.split("=", 1)[1]] = "removed"
                elif w.startswith("-u") and len(w) > 2:
                    state[w[2:]] = "removed"
                elif w in ("-C", "--chdir"):
                    i += 1
                elif w.startswith("-") and w != "--":
                    pass
                else:
                    break
                i += 1
            continue
        if base in ("command", "builtin", "nohup", "exec", "time", "sudo", "doas"):
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1 + (words[i] in ("-a", "-u", "-g", "-p", "-C", "-D", "-h", "-U"))
            continue
        if base == "nice":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1 + (words[i] in ("-n", "--adjustment"))
            continue
        if base == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1 + (words[i] in ("-s", "--signal", "-k", "--kill-after"))
            i += 1  # the duration
            continue
        if base == "stdbuf":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1 + (words[i] in ("-i", "-o", "-e"))
            continue
        if base == "npx":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1 + (words[i] in ("-p", "--package"))
            if i < len(words):
                words = words[:i] + [words[i].split("@", 1)[0] or words[i]] + words[i + 1:]
            continue
        break
    return words[i:]


def _removed(name: str, state: dict) -> bool:
    if name in state:
        return state[name] == "removed"
    return state.get("*") == "removed"


def _positionals(args: list, valued: tuple = ()) -> list:
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and a != "-":
            i += 1 + (a in valued)
            continue
        out.append(a)
        i += 1
    return out


def _neon(args: list):
    """None, or the row this neonctl invocation trips."""
    if any(a in ("-h", "--help") or a.startswith("--help=") for a in args):
        first = _positionals(args, NEON["valued"])
        if not first or first[0] not in NEON["help_never_echoes"]:
            return "neon-help"
    i, positionals, version = 0, [], False
    while i < len(args):
        a = args[i]
        if not positionals and a.startswith("-") and a != "-":
            name = a.split("=", 1)[0]
            if name in ("-v", "--version"):
                version = True
            elif name in NEON["valued"]:
                if "=" not in a:
                    if i + 1 >= len(args) or args[i + 1].startswith("-"):
                        return "neon-usage"  # an option with no value prints the help
                    i += 1
            elif name in ("--color", "--analytics"):
                if i + 1 < len(args) and args[i + 1] in ("true", "false"):
                    i += 1
            elif name not in NEON["flags"]:
                return "neon-usage"  # an unknown top-level option prints the help
        elif not a.startswith("-") or a == "-":
            positionals.append(a)
        i += 1
    if not positionals:
        return None if version else "neon-usage"
    if positionals[0] not in NEON["groups"] + NEON["leaves"]:
        return "neon-usage"
    if positionals[0] in NEON["groups"] and len(positionals) < 2:
        return "neon-usage"
    return None


def _stdout_to_file(redirs: list) -> bool:
    for fd, op, *target in redirs:
        target = target[0] if target else ""
        if op in ("&>", "&>>") or (op in (">", ">>", ">|") and fd in ("", "1")):
            if target and target not in _SPECIAL:
                return True
    return False


def _judge(cmd: dict, state: dict, depth_budget: int):
    """None, or (row, cli) for one simple command."""
    words = _strip(list(cmd["words"]), state)
    if not words:
        return None
    base = os.path.basename(words[0])
    args = words[1:]
    if base in _SHELLS and depth_budget > 0:
        for j, w in enumerate(args):
            if w == "-c" and j + 1 < len(args):
                return decide(args[j + 1], dict(state), depth_budget - 1, inner=True)
    cli = base
    if re.fullmatch(r"python[0-9.]*", base) and args[:2] == ["-m", "pip"]:
        cli, args = "pip", args[2:]
    elif re.fullmatch(r"pip[0-9.]*", base):
        cli = "pip"
    if cli in NEON["clis"]:
        if state.get("DEBUG") == "set":
            return "neon-debug", cli
        row = _neon(args)
    elif cli == "pip":
        row = "pip-config" if _positionals(args)[:2] in (["config", "list"], ["config", "debug"]) else None
    elif cli == "gh":
        row = "gh-auth-token" if _positionals(args)[:2] == ["auth", "token"] else None
    else:
        return None
    if row is None:
        return None
    action = ROWS[row]["action"]
    if action == "unset" and all(_removed(v, state) for v in ROWS[row]["vars"]):
        return None
    if action == "stdout-to-file" and _stdout_to_file(cmd["redirs"]):
        return None
    return row, cli


def decide(command: str, outer: dict | None = None, depth_budget: int = 2, inner: bool = False):
    """None, or (row, cli): the first command line element that would echo."""
    tokens = _tokens(command)
    scopes = {0: dict(outer or {})}
    for cmd in _commands(tokens):
        d = cmd["depth"]
        for k in [k for k in scopes if k > d]:
            del scopes[k]
        base = scopes.get(d) or dict(scopes[max(k for k in scopes if k <= d)])
        scopes[d] = base
        words = cmd["words"]
        if words and words[0] in ("unset", "export") or (
                words and all(_ASSIGN.match(w) for w in words)):
            if cmd["sep"] in _PERSIST:
                for w in words[1:] if words[0] in ("unset", "export") else words:
                    m = _ASSIGN.match(w)
                    if m:
                        base[m.group(1)] = "removed" if m.group(2) == "" else (
                            "set" if words[0] == "export" else base.get(m.group(1), "shell"))
                    elif words[0] == "unset" and not w.startswith("-"):
                        base[w] = "removed"
            continue
        hit = _judge(cmd, dict(base), depth_budget)
        if hit:
            return hit
    return None


def main() -> int:
    command = sys.stdin.read()
    try:
        hit = decide(command)
    except Unparsed as exc:
        print(f"unparsed\t{exc}")
        return 0
    if hit is None:
        print("allow")
        return 0
    row, cli = hit
    reason = ROWS[row]["reason"].format(cli=cli) + " (credential-echo guard, #2090)"
    print(f"deny\t{row}\t{cli}\t{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
