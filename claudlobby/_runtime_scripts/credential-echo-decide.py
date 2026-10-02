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
outer level and not as a pipeline or background element. That earlier
removal counts only where it always runs: not after `&&`, `||` or `|`, not
inside an `if`, a loop or a `case`, and not in a function body. `unset -f`
and `unset -n` remove no variable. Nothing else counts: whether the hook's
own environment holds the variable says nothing about the shell the Bash
tool starts.

Bounds, so nobody reads this as a fix for the class: it is a command filter.
It follows a shell's `-c` string (in any option cluster), a heredoc or
here-string fed to a shell, the substitutions inside double quotes and
unquoted heredocs, `env` (`-S` included), the wrappers in _WRAPPERS, and the
package runners `npx`, `bunx` and `pnpm|npm|yarn dlx|exec`. A substitution is
delimited as bash reads it; one that still cannot be is read as text, and the
rest of its line is judged (#2097). It does not
follow `eval`, a script, an alias, `xargs`, `find -exec`, a package manager
given options before `dlx`/`exec` or running a bin by name (`pnpm neonctl`),
or a name built by expansion (a variable, a glob, braces). pip's options are
pip 23's, so an option a later pip adds can hide `config list`. Output that
a CLI already printed is out of reach.
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
        "reason": "`{cli}` prints its help here (a bare call, a command group without a "
                  "verb, or an unknown or incomplete option), and that help shows "
                  "NEON_API_KEY from the environment as the default of --api-key. Run the "
                  "same command with the key removed: prefix it with `env -u NEON_API_KEY`.",
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
        "reason": "Let gh read the token itself: run the gh command that needs it instead "
                  "of printing the token. `gh auth token` prints it (GH_TOKEN or GITHUB_TOKEN "
                  "when set, otherwise the stored login's) into the transcript, and removing "
                  "the variables does not help. Only if another program must have it, send "
                  "stdout to a file (`gh auth token > FILE`), where it stays readable on disk.",
    },
}

# neonctl's command tree (2.22.0, from its own help): what prints help. A group
# prints it when called without a verb. An unknown command, at the top or in a
# group, prints only an error (measured with a canary key), so it passes.
NEON = {
    "clis": ("neonctl", "neon"),
    "groups": ("orgs", "org", "projects", "project", "ip-allow", "vpc", "branches", "branch",
               "databases", "database", "db", "roles", "role", "operations", "operation"),
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

# pip's own long options (pip 23's `pip --help` and `pip config --help`, the
# hidden ones and two later ones included), and whether each takes a value.
# optparse takes any unambiguous prefix (`--tim 5`), and pip has no short
# option that takes one.
PIP_LONG = {
    "--cache-dir": True, "--cert": True, "--client-cert": True, "--default-timeout": True,
    "--editor": True, "--exists-action": True, "--keyring-provider": True,
    "--local-log": True, "--log": True, "--log-file": True, "--proxy": True,
    "--python": True, "--resume-retries": True, "--retries": True, "--timeout": True,
    "--trusted-host": True, "--use-deprecated": True, "--use-feature": True,
    "--debug": False, "--disable-pip-version-check": False, "--global": False,
    "--help": False, "--isolated": False, "--no-cache-dir": False, "--no-color": False,
    "--no-input": False, "--no-python-version-warning": False, "--quiet": False,
    "--require-venv": False, "--require-virtualenv": False, "--site": False,
    "--user": False, "--verbose": False, "--version": False,
}

# GNU env's long options, as the short option each one is; "" is a flag.
_ENV_LONG = {"--ignore-environment": "i", "--null": "0", "--unset": "u", "--chdir": "C",
             "--split-string": "S", "--argv0": "a", "--debug": "v", "--block-signal": "",
             "--default-signal": "", "--ignore-signal": "", "--list-signal-handling": "",
             "--help": "", "--version": ""}
_ENV_VALUED = {"u", "C", "S", "a"}

# Wrappers that run the command after their own options: the short options
# that take a value, and the long options with whether each does. getopt_long
# takes any unambiguous prefix, so `--adj 5` still takes its word.
_WRAPPERS = {
    "command": ("", {}),
    "builtin": ("", {}),
    "nohup": ("", {"--help": False, "--version": False}),
    "setsid": ("", {"--ctty": False, "--fork": False, "--wait": False, "--help": False,
                    "--version": False}),
    "exec": ("a", {}),
    "time": ("fo", {"--format": True, "--output": True, "--append": False,
                    "--portability": False, "--verbose": False, "--quiet": False,
                    "--help": False, "--version": False}),
    "nice": ("n", {"--adjustment": True, "--help": False, "--version": False}),
    "timeout": ("sk", {"--signal": True, "--kill-after": True, "--preserve-status": False,
                       "--foreground": False, "--verbose": False, "--help": False,
                       "--version": False}),
    "stdbuf": ("ioe", {"--input": True, "--output": True, "--error": True, "--help": False,
                       "--version": False}),
    "sudo": ("CDghpRrTtUu", {"--chdir": True, "--chroot": True, "--close-from": True,
                             "--command-timeout": True, "--group": True, "--host": True,
                             "--other-user": True, "--prompt": True, "--role": True,
                             "--type": True, "--user": True, "--askpass": False,
                             "--background": False, "--bell": False, "--edit": False,
                             "--help": False, "--list": False, "--login": False,
                             "--non-interactive": False, "--preserve-env": False,
                             "--preserve-groups": False, "--remove-timestamp": False,
                             "--reset-timestamp": False, "--set-home": False,
                             "--shell": False, "--stdin": False, "--validate": False,
                             "--version": False}),
    "doas": ("Cu", {}),
}

# npx, bunx and `pnpm|npm|yarn dlx|exec`: the long options that take a value.
_RUNNER_LONG = {"--package": True, "--call": True, "--prefix": True, "--workspace": True,
                "--dir": True, "--shell-mode": False, "--yes": False, "--no": False,
                "--bun": False, "--silent": False, "--quiet": False, "--help": False}

_ANSI_C = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
           "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
_OCTAL = re.compile(r"[0-7]{1,3}")
_HEX = {"x": re.compile(r"[0-9A-Fa-f]{0,2}"), "u": re.compile(r"[0-9A-Fa-f]{0,4}"),
        "U": re.compile(r"[0-9A-Fa-f]{0,8}")}


class Unparsed(Exception):
    pass


def _long(word: str, names) -> tuple:
    """(the long option `word` names, its `=value` or None). getopt_long and
    optparse take any unambiguous prefix; an unknown or ambiguous one is None."""
    name, eq, value = word.partition("=")
    if name not in names:
        hits = [n for n in names if n.startswith(name)]
        name = hits[0] if len(hits) == 1 else None
    return name, (value if eq else None)


def _options_end(words: list, i: int, short: str, longs: dict) -> int:
    """Index of the first word after the options that start at words[i]:
    `short` holds the short options that take a value, `longs` maps each long
    option to whether it does."""
    while i < len(words):
        w = words[i]
        if w == "--":
            return i + 1
        if not w.startswith("-") or w == "-":
            return i
        i += 1
        if w.startswith("--"):
            name, value = _long(w, longs)
            i += bool(value is None and longs.get(name))
            continue
        for k, c in enumerate(w[1:], 1):
            if c in short:
                i += k == len(w) - 1  # its value is the next word unless attached
                break
    return i


def _ansi_c(s: str, j: int):
    """Decode a $'...' body that starts at s[j], as bash does: (text, index
    after the closing quote). A name can be spelled in its escapes."""
    out, n = [], len(s)
    while j < n and s[j] != "'":
        if s[j] != "\\" or j + 1 >= n:
            out.append(s[j])
            j += 1
            continue
        e = s[j + 1]
        if e in _ANSI_C:
            out.append(_ANSI_C[e])
            j += 2
        elif e in "01234567":
            digits = _OCTAL.match(s, j + 1).group(0)
            out.append(chr(int(digits, 8) & 0xFF))
            j += 1 + len(digits)
        elif e in _HEX:
            digits = _HEX[e].match(s, j + 2).group(0)
            if not digits:
                out.append("\\" + e)
            elif int(digits, 16) > 0x10FFFF:
                raise Unparsed("ANSI-C escape out of range")
            else:
                out.append(chr(int(digits, 16)))
            j += 2 + len(digits)
        elif e == "c" and j + 2 < n:
            out.append(chr(ord(s[j + 2]) & 0x1F))
            j += 3
        else:
            out.append("\\" + e)
            j += 2
    if j >= n:
        raise Unparsed("unbalanced ANSI-C quote")
    return "".join(out), j + 1


# Words after which a command can start, so that a `case` there is the keyword.
_LEADERS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "time"}
_WORD_END = " \t\n;&|()<>"


def _word_end(s: str, i: int) -> int:
    """Index just past the shell word that starts at s[i]; its quotes and
    escapes belong to it."""
    n = len(s)
    while i < n and s[i] not in _WORD_END:
        if s[i] == "\\":
            i += 2
        elif s[i] == "'" or s.startswith("$'", i):
            ansi = s[i] == "$"
            j = i + 1 + ansi
            while j < n and s[j] != "'":
                j += 2 if ansi and s[j] == "\\" else 1
            if j >= n:
                raise Unparsed("unbalanced quote in a command substitution")
            i = j + 1
        elif s[i] == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                raise Unparsed("unbalanced quote in a command substitution")
            i = j + 1
        else:
            i += 1
    return min(i, n)


def _balanced(s: str, i: int) -> int:
    """Index of the `)` that closes a `$(` whose body starts at s[i], reading
    the body as bash reads it: a parenthesis inside quotes, a comment, a
    heredoc body or a case pattern does not count (#2097). Unparsed when it
    still cannot be delimited."""
    depth, n = 1, len(s)
    heredocs = []  # (delimiter, strip_tabs): bodies that start at the next newline
    cases = []  # [depth, state] per open `case`: subject, in, pattern or body
    command = True  # the next word stands where a command can start
    while i < n:
        c = s[i]
        top = cases[-1] if cases and cases[-1][0] == depth else None
        if c in " \t":
            i += 1
        elif c == "\n":
            i += 1
            for delim, strip in heredocs:
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:] if j < 0 else s[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            heredocs, command = [], True
        elif c == "#":  # reached only where a word starts: a comment
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif s.startswith("<<", i) and not s.startswith("<<<", i):
            j = i + 2 + s.startswith("<<-", i)
            while j < n and s[j] in " \t":
                j += 1
            k = _word_end(s, j)
            delim = re.sub(r"[\"'\\]", "", s[j:k])
            if delim:
                heredocs.append((delim, s.startswith("<<-", i)))
            i, command = k, False
        elif c in ";&|<>":
            op = _OPS.match(s, i).group(0)
            if top and op in (";;", ";&", ";;&"):
                top[1] = "pattern"
            i, command = i + len(op), c in ";&|"
        elif c == "(":
            if top and top[1] == "pattern":
                i += 1  # a pattern's optional opening parenthesis
            else:
                depth, i, command = depth + 1, i + 1, True
        elif c == ")":
            if top and top[1] == "pattern":
                top[1], i, command = "body", i + 1, True  # the pattern ends
                continue
            depth -= 1
            if depth == 0:
                return i
            i, command = i + 1, False
        else:
            k = _word_end(s, i)
            word = s[i:k]
            if command and word == "case":
                cases.append([depth, "subject"])
            elif top and top[1] == "subject":
                top[1] = "in"
            elif top and top[1] == "in" and word == "in":
                top[1] = "pattern"
            elif top and word == "esac" and (command or top[1] == "pattern"):
                cases.pop()
            i, command = k, word in _LEADERS
    raise Unparsed("unbalanced command substitution")


def _substitutions(text: str) -> list:
    """The command strings of the `$(...)` and backtick substitutions in text,
    which the shell runs even inside double quotes and unquoted heredocs."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text.startswith("$(", i) and not text.startswith("$((", i):
            try:
                j = _balanced(text, i + 2)
            except Unparsed:
                # It cannot be delimited (#2097). Read it as text: bash still
                # runs the commands after the heredoc, so they are judged.
                i += 2
                continue
            out.append(text[i + 2:j])
            i = j + 1
        elif text[i] == "`":
            j = text.find("`", i + 1)
            if j < 0:
                i += 1  # a lone backtick is text here, for the same reason
                continue
            out.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return out


def _tokens(command: str) -> list:
    """("word", text, quoted) / ("op", op) / ("redir", fd, op) / ("sub", command)
    / ("heredoc", body, delimiter_quoted): quotes removed, line continuations
    joined, comments dropped. A substitution inside double quotes comes back as
    its own "sub" token, and a heredoc body follows the line that opened it."""
    s = command.replace("\\\n", "")
    i, n = 0, len(s)
    out, word, quoted, heredocs = [], None, False, []

    def flush():
        nonlocal word, quoted
        if word is not None:
            out.append(("word", word, quoted))
            if heredocs and heredocs[-1][0] is None:
                heredocs[-1] = [word, quoted]  # the first word after << is its delimiter
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
            for delim, delim_quoted in heredocs:
                body = []
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:] if j < 0 else s[i:j]
                    i = n if j < 0 else j + 1
                    if line.strip() == delim:
                        break
                    body.append(line)
                out.append(("heredoc", "\n".join(body), delim_quoted))
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
            text, i = _ansi_c(s, i + 2)
            word, quoted = (word or "") + text, True
        elif c == "$" and s.startswith('$"', i):
            i += 1  # a translated string reads as a double-quoted one
        elif c == '"':
            j, buf = i + 1, []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n and s[j + 1] in '"\\$`':
                    buf.append(s[j + 1])
                    j += 2
                elif s.startswith("$(", j) and not s.startswith("$((", j):
                    try:
                        k = _balanced(s, j + 2)
                    except Unparsed:
                        # It cannot be delimited (#2097). Read the `$(` as text,
                        # so the rest of the line is still judged.
                        buf.append(s[j:j + 2])
                        j += 2
                        continue
                    out.append(("sub", s[j + 2:k]))
                    buf.append(s[j:k + 1])
                    j = k + 1
                elif s[j] == "`":
                    k = s.find("`", j + 1)
                    if k < 0:
                        raise Unparsed("unbalanced backtick")
                    out.append(("sub", s[j + 1:k]))
                    buf.append(s[j:k + 1])
                    j = k + 1
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
                    heredocs.append([None, False])  # filled by the delimiter word
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
    """Simple commands in order: dict(words, redirs, subs, heredocs, depth, sep,
    prev), sep the operator after the command and prev the one before it."""
    cmds, cur, depth, backtick, want_target, pending, last_op = [], None, 0, False, None, [], None

    def close(sep):
        nonlocal cur
        if cur is not None and (cur["words"] or cur["redirs"] or cur["subs"]):
            cur["sep"] = sep
            cmds.append(cur)
        cur = None

    for tok in tokens:
        if tok[0] == "heredoc":
            owner = pending.pop(0) if pending else (cmds[-1] if cmds else None)
            if owner is not None:
                owner["heredocs"].append((tok[1], tok[2]))
            continue
        if cur is None:
            cur = {"words": [], "redirs": [], "subs": [], "heredocs": [], "depth": depth, "sep": None,
                   "prev": last_op}
        if tok[0] == "word":
            if want_target is not None:
                want_target.append(tok[1])
                want_target = None
            elif tok[1] in ("{", "}") and not tok[2] and not cur["words"]:
                continue
            else:
                cur["words"].append(tok[1])
        elif tok[0] == "sub":
            cur["subs"].append(tok[1])
        elif tok[0] == "redir":
            want_target = [tok[1], tok[2]]
            cur["redirs"].append(want_target)
            if tok[2] in ("<<", "<<-"):
                pending.append(cur)
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
            last_op = op
    close(None)
    return cmds


def _env(words: list, i: int, state: dict):
    """env's options from words[i]: update `state` and return (words, the
    command's index). `-S STRING` splits STRING into words in place, as env does."""
    while i < len(words):
        w = words[i]
        if w == "--":
            return words, i + 1
        if w == "-":
            opts = [("i", None)]
        elif w.startswith("--"):
            name, value = _long(w, _ENV_LONG)
            opts = [(_ENV_LONG.get(name, ""), value)]
        elif w.startswith("-") and len(w) > 1:
            opts = []
            for k, c in enumerate(w[1:], 1):
                opts.append((c, (w[k + 1:] or None) if c in _ENV_VALUED else None))
                if c in _ENV_VALUED:
                    break
        else:
            return words, i
        for letter, value in opts:
            if letter in _ENV_VALUED and value is None:
                i += 1
                value = words[i] if i < len(words) else ""
            if letter == "i":
                state.clear()
                state["*"] = "removed"
            elif letter == "u":
                state[value] = "removed"
            elif letter == "S":
                split = [t[1] for t in _tokens(value) if t[0] == "word"]
                words = words[:i + 1] + split + words[i + 1:]
        i += 1
    return words, i


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
            words, i = _env(words, i + 1, state)
            continue
        if base in _WRAPPERS:
            short, longs = _WRAPPERS[base]
            j = _options_end(words, i + 1, short, longs)
            if base == "command" and any(w.startswith("-") and w != "--" and ("v" in w or "V" in w)
                                         for w in words[i + 1:j]):
                return []  # `command -v` / `-V` looks a name up; it runs nothing
            i = j + (base == "timeout")  # timeout's duration comes before the command
            continue
        if base in ("pnpm", "npm", "yarn") and words[i + 1:i + 2] in (["dlx"], ["exec"], ["x"]):
            i, base = i + 1, "npx"  # the runner's options follow, as npx takes them
        if base in ("npx", "bunx"):
            i += 1
            while i < len(words) and words[i].startswith("-") and words[i] != "-":
                w = words[i]
                i += 1
                if w == "--":
                    break
                name, value = _long(w, _RUNNER_LONG) if w.startswith("--") else (w, None)
                if name in ("-c", "--call", "--shell-mode"):  # the rest is a shell string
                    return ["sh", "-c", " ".join(([value] if value else []) + words[i:])]
                if value is None and (_RUNNER_LONG.get(name) or name in ("-p", "-w", "-C")):
                    i += 1
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


def _pip_positionals(args: list) -> list:
    """pip's sub-command words, past its options (PIP_LONG, prefixes included)."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            return out + args[i:]
        if a.startswith("--"):
            name, value = _long(a, PIP_LONG)
            i += bool(value is None and PIP_LONG.get(name))
        elif not a.startswith("-") or a == "-":
            out.append(a)
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


# Words that open or continue a compound command; the simple command follows them.
_RESERVED = {"if", "then", "elif", "else", "while", "until", "do", "!", "{"}
# Words that open and close a compound whose body may not run: a removal
# inside one is no removal for what follows.
_OPENS = {"if", "while", "until", "for", "select", "case"}
_CLOSES = {"fi", "done", "esac"}


def _unreserve(words: list) -> list:
    i = 0
    while i < len(words) and words[i] in _RESERVED:
        i += 1
    return words[i:]


def _shell_call(args: list):
    """("c", STRING) for `sh -c STRING` (any option cluster holding c),
    ("stdin", None) for a shell that reads its commands from stdin, or
    ("script", None) for a shell running a script file."""
    i, has_c, has_s = 0, False, False
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        if a[:1] in ("-", "+") and len(a) > 1:
            has_c = has_c or "c" in a[1:]
            has_s = has_s or "s" in a[1:]
            i += 1
            continue
        break
    rest = args[i:]
    if has_c:
        return "c", (rest[0] if rest else None)
    if has_s or not rest:
        return "stdin", None
    return "script", None


def _judge(cmd: dict, words: list, state: dict, depth_budget: int, stdin_texts: list):
    """None, or (row, cli) for one simple command."""
    words = _strip(list(words), state)
    if not words:
        return None
    base = os.path.basename(words[0])
    args = words[1:]
    if base in _SHELLS:
        kind, script = _shell_call(args)
        texts = [script] if kind == "c" and script else (stdin_texts if kind == "stdin" else [])
        for text in texts:
            if depth_budget > 0:
                hit = decide(text, dict(state), depth_budget - 1)
                if hit:
                    return hit
        return None
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
        row = "pip-config" if _pip_positionals(args)[:2] in (["config", "list"], ["config", "debug"]) else None
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


def decide(command: str, outer: dict | None = None, depth_budget: int = 3):
    """None, or (row, cli): the first element of the command line that would echo."""
    tokens = _tokens(command)
    scopes = {0: dict(outer or {})}
    pipe = []  # what the earlier elements of this pipeline write to stdin
    nest = 0  # open if, loop and case compounds
    for cmd in _commands(tokens):
        d = cmd["depth"]
        for k in [k for k in scopes if k > d]:
            del scopes[k]
        base = scopes.get(d) or dict(scopes[max(k for k in scopes if k <= d)])
        scopes[d] = base
        for w in cmd["words"]:
            if w in _OPENS:
                nest += 1
            elif w in _CLOSES:
                nest = max(0, nest - 1)
            elif w not in _RESERVED:
                break
        words = _unreserve(cmd["words"])
        # Substitutions run first: inside double quotes, and in an unquoted heredoc body.
        inner = list(cmd["subs"]) + [t for body, q in cmd["heredocs"] if not q
                                     for t in _substitutions(body)]
        for text in inner:
            if depth_budget > 0:
                hit = decide(text, dict(base), depth_budget - 1)
                if hit:
                    return hit
        own = [body for body, _ in cmd["heredocs"]]
        own += [r[2] for r in cmd["redirs"] if r[1] == "<<<" and len(r) > 2]
        if words and words[0] in ("echo", "printf"):
            own.append(" ".join(words[1:]))
        stdin_texts, pipe = pipe + own, (own if cmd["sep"] in ("|", "|&") else [])
        if words and (words[0] in ("unset", "export") or all(_ASSIGN.match(w) for w in words)):
            # It counts for what follows only where it always runs; a ")" right
            # before it closes a function's name, so it is a function body.
            always = (nest == 0 and cmd["sep"] in _PERSIST
                      and cmd["prev"] not in ("&&", "||", "|", "|&", ")"))
            if words[0] == "unset" and any(w.startswith("-") and set(w[1:]) & {"f", "n"}
                                           for w in words[1:]):
                always = False  # -f removes a function, -n a reference
            if always:
                for w in words[1:] if words[0] in ("unset", "export") else words:
                    m = _ASSIGN.match(w)
                    if m:
                        base[m.group(1)] = "removed" if m.group(2) == "" else (
                            "set" if words[0] == "export" else base.get(m.group(1), "shell"))
                    elif words[0] == "unset" and not w.startswith("-"):
                        base[w] = "removed"
            continue
        hit = _judge(cmd, words, dict(base), depth_budget, stdin_texts)
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
