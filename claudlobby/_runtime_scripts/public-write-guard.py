#!/usr/bin/env python3
"""public-write-guard.py: the decider behind ``public-write-guard.sh``, beside it.

Refuses a GitHub-bound write that would put a term from the HOST'S list into a
PUBLIC repository. Composed only for a bot with ``public_write_guard: true``.

The list is host configuration, never repository content:
``~/.config/claudlobby/public-write-terms`` holds one case-insensitive regular
expression per line (``#`` starts a comment line). No term appears in this file,
its tests or its messages; the tests use invented terms.

WHAT A WRITE PUTS IN THE REPOSITORY is all this guard reads for terms:
  - an ``mcp__github__*`` tool that is not a read (``get_``, ``list_``,
    ``search_``): every string in its input except ``owner`` and ``repo``;
  - a ``gh`` command in the issue, pr, release, gist, repo or label group that
    is not a read (view, list, status, diff, checks, checkout, download, clone,
    browse), and ``gh api`` with fields unless it is a GET or a GraphQL query:
    its own words, every file it names as a body, and its standard input;
  - ``git commit``: its messages, and the lines and new paths it adds (the
    staged diff, plus the unstaged one when ``-a``, ``-i``, ``-o``, a pathspec
    or an earlier ``git add`` in the command brings it in), and every new file
    an earlier ``git add`` in the command names, which no diff lists yet;
  - ``git push``: the messages, added lines and new paths of every commit it
    would send, and of every commit made earlier in the same command, which
    does not exist yet when this guard runs. For git this is the complete
    check, since a commit publishes nothing until it is pushed. One gap: an
    annotated tag's own message is not read.
It does not read a directory named by ``cd``, the path of a body file, or the
target repository's name. None of those is content the write puts in the
repository, and reading them would refuse a clean write made from a path that
happens to contain a listed term.

Only a HIT asks where the write goes. The repository's visibility is read LIVE
(``gh api repos/OWNER/REPO``), never from a list, and cached for
``CACHE_LIFETIME_S`` so a burst of writes does not repeat the call. A REST call
that fails fast (a 404, or a REST throttle, which leaves GraphQL working) is
asked again over GraphQL (``gh repo view``); when both fail, an answer cached
up to ``STALE_LIFETIME_S`` ago stands in rather than a refusal, since a
repository that was private an hour ago and cannot be read now is far more
likely private than not.

``--check`` says whether the host's guard is armed (the list absent, broken, or
ok with its pattern count, and whether the off switch is set) and never prints
a term; rollout runs it on each host.

Failure directions, each chosen on purpose:
  - no list file: ALLOW, and record ``public_write_guard_unarmed``. There is
    nothing to match, and refusing every GitHub write on a host that was never
    configured is an outage; the canary checks that the file exists.
  - a broken list (a line that does not compile, or one that can match an
    empty string, so every write would be a hit): REFUSE every guarded write,
    naming the line and column, never the line's text. Someone meant to protect
    this host, and a broken list must not read as none.
  - a payload that is not JSON: ALLOW, loudly (exit 3: the hook leaves a
    breadcrumb). A hook that cannot read the harness's own payload must not
    wedge every call.
  - a hit whose repository cannot be named, or whose visibility cannot be read:
    REFUSE, saying which. Only a hit pays this, so the cost falls where the risk
    is.
  - content that cannot be read counts as a hit. That covers a body file that
    is not there, a program's output piped into the write or substituted into
    its content (``$(...)`` or backticks, except ``cat`` of a file or of a
    heredoc, in a commit message, a body, title, notes, subject, comment or
    description flag, or a ``gh api`` field such as ``body`` or ``query``), and
    a git command run from a directory this guard cannot name (a command
    substitution or a glob). A substitution in any other flag, such as the sha
    in ``--match-head-commit "$(gh api …)"``, is left as written: it is not
    content.
A remote that is not github.com, or a repository with no remote, is out of
scope and allowed.

THE CEILING: this reads the shell the way people write it, not the way a shell
runs it. ``eval``, a function, an alias, or a script file that runs git or gh
are not followed (the commands inside a command substitution are). Three
writes publish content that is not a word of the command, and are not read:
``gh pr create --fill`` (its title and body come from commits already pushed),
``gh repo create --source --push`` (the local history) and the asset files of
``gh release create`` and ``upload``. It keeps accidents out of public
repositories; it is not a boundary against a caller trying to get past it.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

LIB = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "vault_git_decide", LIB / "vault-git-decide.py"
)
_vgd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(
    _vgd
)  # git's option table and path resolver, not a second copy

CACHE_LIFETIME_S = 600
STALE_LIFETIME_S = 86400  # a cached answer stands in when GitHub cannot be read
GIT_TIMEOUT_S = 10
GH_TIMEOUT_S = 10
DEFAULT_LIST = "~/.config/claudlobby/public-write-terms"

_MCP = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>[a-z][a-z0-9_]*)$")
_MCP_READS = ("get_", "list_", "search_")

_GH_GROUPS = {"issue", "pr", "release", "gist", "repo", "label"}
_GH_READS = {
    "view",
    "list",
    "status",
    "diff",
    "checks",
    "checkout",
    "download",
    "clone",
    "browse",
}
_GH_TARGET_FLAGS = {"-R", "--repo", "--hostname"}
_GH_FILE_FLAGS = {"--body-file", "-F", "--notes-file"}
# The other value-taking flags of the issue and pr write verbs (gh 2.92.0, from each verb's
# --help). Their value is a name, a branch or a reference, never the selector, whatever it
# looks like: --duplicate-of takes an issue URL, and a URL there must not read as the target.
_GH_OTHER_VALUE_FLAGS = {
    "--add-assignee", "--add-label", "--add-project", "--add-reviewer", "--assignee",
    "--author-email", "--base", "--branch-repo", "--duplicate-of", "--head", "--label",
    "--match-head-commit", "--milestone", "--project", "--reason", "--recover",
    "--remove-assignee", "--remove-label", "--remove-project", "--remove-reviewer",
    "--reviewer", "--template",
}
_API_SKIP = {
    "-X",
    "--method",
    "-H",
    "--header",
    "-q",
    "--jq",
    "-t",
    "--template",
    "-p",
    "--preview",
    "--hostname",
    "--cache",
}
_API_FIELDS = {"-f", "--raw-field", "-F", "--field"}
# The flags whose value is text the write publishes. A command substitution there
# is output this guard cannot read; in any other flag (a sha, a branch, a merge
# method) it is left as written, since none of those is content.
_GH_CONTENT_FLAGS = {
    "-b",
    "--body",
    "-t",
    "--title",
    "-n",
    "--notes",
    "--subject",
    "-c",
    "--comment",
    "-d",
    "--description",
    "--desc",
}
# In these commands a flag above is a switch that takes no value, so the word after
# it is not its value: from each command's --help, gh 2.92.0. Elsewhere -c and
# --comment carry a closing comment, and -d a description.
_GH_SWITCHES = {
    ("issue", "develop"): {"-c"},
    ("pr", "close"): {"-d"},
    ("pr", "create"): {"-d"},
    ("pr", "merge"): {"-d"},
    ("pr", "revert"): {"-d"},
    ("pr", "review"): {"-c", "--comment"},
    ("release", "create"): {"-d"},
    ("repo", "create"): {"-c"},
}
_API_CONTENT_KEYS = {
    "body",
    "title",
    "message",
    "commit_message",
    "commit_title",
    "description",
    "notes",
    "name",
    "query",
    "content",
    "text",
    "subject",
}
_PUSH_VALUES = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
_COMMIT_VALUES = {
    "--message",
    "--file",
    "--reuse-message",
    "--reedit-message",
    "--template",
    "--author",
    "--date",
    "--fixup",
    "--squash",
    "--cleanup",
    "--trailer",
    "--pathspec-from-file",
}

_GITHUB_URL = re.compile(
    r"(?:^|[/@.])github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_API_REPO = re.compile(r"^/?repos/(?P<owner>[^/\s]+)/(?P<repo>[^/\s?]+)")
_REPO_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_CAT_SUBST = re.compile(r"\$\(\s*(?:cat\s+|<\s*)([^\s()|;&<>]+)\s*\)")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_PLACEHOLDER = re.compile(r"@@pwg-heredoc-(\d+)@@")
_PUNCT = ";&|()<>\n"
_SUBST = re.compile(r"\$@@pwg-subst-(\d+)@@")
_READABLE_SUBST = re.compile(
    r"\s*(?:(?:cat\s+|<\s*)[^\s()|;&<>]+|cat\s*<<-?\s*@@pwg-heredoc-\d+@@)\s*"
)
# an issue or pull request URL, as a whole word
_TARGET_URL = re.compile(
    r"(?i:https?)://(?P<host>[^/\s]+)/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/(?:issues|pull)/\d+(?:[/?#]\S*)?"
)
_VISIBILITIES = ("public", "private", "internal")
# a line that matches any of these with zero width matches at every position of
# every write, so every write would be a hit
_PROBES = ("", "x", "plain words, 12.\n")


# --- the list -------------------------------------------------------------------------


def terms_path() -> Path:
    env = os.environ.get("PUBLIC_WRITE_GUARD_TERMS")
    return Path(env) if env else Path(os.path.expanduser(DEFAULT_LIST))


def _list_label() -> str:
    return os.environ.get("PUBLIC_WRITE_GUARD_TERMS") or DEFAULT_LIST


def load_terms(path: Path) -> tuple[re.Pattern | None, str]:
    """(compiled list, state): state is ``ok``, ``absent`` or ``broken: <why>``.

    A broken list is named by line and column, never by the line's text: the
    reason reaches the bot, and the list is what must not travel."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, f"broken: {exc.strerror or exc}"
    parts = [
        (n, ln.strip())
        for n, ln in enumerate(lines, 1)
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not parts:
        return None, "absent"
    for n, p in parts:
        try:
            one = re.compile(f"(?:{p})", re.IGNORECASE)  # as the list uses it
        except re.error as exc:
            col = "" if exc.colno is None else f", column {max(exc.colno - 3, 1)}"
            return None, f"broken: line {n} does not compile{col}"
        if any(m.start() == m.end() for probe in _PROBES for m in one.finditer(probe)):
            return None, (
                f"broken: line {n} can match an empty string, so every write "
                "would be a hit"
            )
    try:
        return re.compile("|".join(f"(?:{p})" for _, p in parts), re.IGNORECASE), "ok"
    except re.error:
        return None, "broken: its lines do not compile together (a group name used twice?)"


# --- reading the shell ----------------------------------------------------------------


def _heredocs(command: str, bodies: list[str]) -> str:
    """The command with each heredoc body moved into ``bodies``, its opener
    rewritten to a placeholder that names the body.

    A body is prose, not commands: left in, a line such as "then git push" would
    be read as a push, and an apostrophe in it would unbalance the tokenizer.
    The placeholder ties the body back to the command it feeds. An opener that
    is never terminated is left alone: it was quoted text, not a heredoc.
    """
    lines = command.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        pieces, last = [], 0
        for m in _HEREDOC.finditer(line):
            dash, delim = m.group(1) == "-", m.group(3)
            end = next(
                (
                    j
                    for j in range(i, len(lines))
                    if (lines[j].lstrip("\t") if dash else lines[j]) == delim
                ),
                None,
            )
            if end is None:
                continue
            pieces.append(line[last : m.start()] + f"<< @@pwg-heredoc-{len(bodies)}@@")
            bodies.append("\n".join(lines[i:end]))
            last = m.end()
            i = end + 1
        out.append("".join(pieces) + line[last:])
    return "\n".join(out)


def _unfold(text: str, substs: list[str]) -> str:
    """The text as the shell reads it before splitting words.

    - A comment (a ``#`` that starts a word, outside quotes) goes, up to its
      newline. shlex's own comment handling swallows the NEWLINE too, which
      would join the next command's words onto this one's.
    - A backslash-newline outside single quotes is a line continuation and
      goes, so ``gh pr create -R O/R \\`` then an indented ``--body ...`` reads
      as the one command it is.
    - A command substitution outside single quotes (``$(...)`` or backticks)
      whose output this guard cannot know, which is anything but ``cat`` of a
      file or of a heredoc, moves into ``substs`` and leaves a placeholder
      word: its output is content this guard cannot read, and its own commands
      are walked like a subshell's.
    """
    out: list[str] = []
    quote, i, n = "", 0, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            quote = "" if c == "'" else quote
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if text[i + 1] != "\n":
                out.append(text[i : i + 2])
            i += 2
            continue
        end = None
        if c == "$" and text.startswith("(", i + 1) and not text.startswith("((", i + 1):
            end = _close_paren(text, i + 2)
            if end is not None and _READABLE_SUBST.fullmatch(text[i + 2 : end]):
                out.append(text[i : end + 1])  # read where it is used (_CAT_SUBST)
                i = end + 1
                continue
            inner = text[i + 2 : end] if end is not None else ""
        elif c == "`":
            end = _close_tick(text, i + 1)
            inner = text[i + 1 : end] if end is not None else ""
        if end is not None:
            out.append(f"$@@pwg-subst-{len(substs)}@@")
            substs.append(inner)
            i = end + 1
            continue
        if quote:
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (not out or out[-1][-1] in " \t\n;&|()"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _close_paren(text: str, i: int) -> int | None:
    """The ``)`` that closes the ``$(`` whose body starts at ``i``, following
    nesting and quotes; None when it never closes (bash would not run it)."""
    depth, quote = 1, ""
    while i < len(text):
        c = text[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            i += 2
            continue
        elif quote:
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _close_tick(text: str, i: int) -> int | None:
    """The backtick that closes the one before ``i``: the next unescaped one."""
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return i
        i += 1
    return None


def _is_op(tok: str) -> bool:
    return bool(tok) and set(tok) <= set(_PUNCT)


def _is_sep(tok: str) -> bool:
    return _is_op(tok) and not set(tok) & set("<>")


def _words(text: str) -> list[str]:
    """Shell words, with operators AND NEWLINES as tokens of their own.

    vault-git-decide's tokenizer treats a newline as whitespace. That is right
    for a guard that judges one verb and wrong here: a ``git push`` line followed
    by a ``gh pr create`` line would read the second command's words as refs to
    push. Its clean-up of a token (``_unseparate``) is reused unchanged.
    """
    lex = shlex.shlex(text, posix=True, punctuation_chars=_PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        toks = list(lex)
    except ValueError:  # unbalanced quotes: words and line breaks only
        toks = [w for line in text.split("\n") for w in line.split() + ["\n"]]
    return [t if _is_op(t) else _vgd._unseparate(t) for t in toks]


def _lenient(word: str, assigns: dict[str, str]) -> str:
    """The word with every variable this guard knows substituted: for content,
    where an unknown one is left as written."""

    def value(m):
        name = m.group(1) or m.group(2)
        v = assigns.get(name, os.environ.get(name))
        return m.group(0) if v is None else v

    return _VAR.sub(value, word)


def _expand(word: str, assigns: dict[str, str]) -> str | None:
    """A path's value, or None when it holds what this guard cannot know (a
    substitution, a glob). An unset variable is empty, as in the shell."""
    if word == "~" or word.startswith("~/"):
        word = os.path.expanduser(word)
    out = _VAR.sub(
        lambda m: assigns.get(
            m.group(1) or m.group(2), os.environ.get(m.group(1) or m.group(2), "")
        ),
        word,
    )
    return None if any(ch in out for ch in "$`*?") else out


class _Cmd:
    """One simple command: its words, and what its redirects bring in or send out."""

    def __init__(self):
        self.words: list[str] = []
        self.heredocs: list[str] = []
        self.herestrings: list[str] = []
        self.inputs: list[str] = []
        self.outputs: list[str] = []
        self.substs: list[int] = []  # the command substitutions it runs first


class _State:
    """What the walk knows at a point in the command."""

    def __init__(self, cwd: str):
        self.here: str | None = cwd  # None: a directory this guard cannot name
        self.unnamed = ""  # the word that made it so
        self.assigns: dict[str, str] = {}
        self.written: dict[
            str, str | None
        ] = {}  # file -> what the command writes there
        self.adding = False  # an earlier `git add` in this command
        # each earlier `git add` that takes in new files: (its directory, its
        # pathspecs or None when a file names them, whether it forces)
        self.added: list[tuple[str | None, list[str] | None, bool]] = []
        # repository -> what commits made earlier in this command hold (parts,
        # what could not be read): a later push sends them, and they do not
        # exist yet when this guard runs
        self.pending: dict[str, tuple[list[tuple[str, str]], str]] = {}


def _parse_cmd(toks: list[str], st: _State, bodies: list[str]) -> _Cmd:
    c = _Cmd()
    c.substs = [int(m.group(1)) for t in toks for m in _SUBST.finditer(t)]
    lead = True
    k = 0
    while k < len(toks):
        t = toks[k]
        if _is_op(t):  # a redirect: separators never reach here
            if k and c.words and toks[k - 1] == c.words[-1] and c.words[-1].isdigit():
                c.words.pop()  # the descriptor in `2>&1`, not a word of the command
            operand = (
                toks[k + 1] if k + 1 < len(toks) and not _is_op(toks[k + 1]) else None
            )
            if operand is not None:
                if t in ("<<", "<<-"):
                    m = _PLACEHOLDER.fullmatch(operand)
                    if m:
                        c.heredocs.append(bodies[int(m.group(1))])
                elif t == "<<<":
                    c.herestrings.append(operand)
                elif t == "<":
                    c.inputs.append(operand)
                elif ">" in t and not t.endswith("&"):  # `2>&1` names a descriptor
                    c.outputs.append(operand)
            k += 2 if operand is not None else 1
            continue
        if lead:
            m = _ASSIGN.match(t)
            if m:
                st.assigns[m.group(1)] = _lenient(m.group(2), st.assigns)
                k += 1
                continue
            if t in ("export", "local", "declare", "readonly", "typeset"):
                k += 1
                continue
            lead = False
        for m in _PLACEHOLDER.finditer(t):  # a heredoc inside a quoted $(...)
            c.heredocs.append(bodies[int(m.group(1))])
        c.words.append(t)
        k += 1
    return c


def _path(word: str, st: _State) -> str | None:
    """A word as an absolute path, or None when this guard cannot know it."""
    p = _expand(word, st.assigns)
    if p is None or not (os.path.isabs(p) or st.here):
        return None
    return _vgd._resolve(p, st.here)


def _supplied(c: _Cmd, st: _State) -> str | None:
    """The text a command writes when it supplies that text itself: ``echo`` and
    ``printf`` print their words, ``cat`` its heredocs, here-strings and files.
    Any other program's output cannot be known before it runs (None)."""
    prog = os.path.basename(c.words[0]) if c.words else ""
    if prog in ("echo", "printf"):
        words = [_lenient(w, st.assigns) for w in c.words[1:]]
        return None if any(_SUBST.search(w) for w in words) else "\n".join(words)
    if prog == "cat":
        body = _Body(st.here, dict(st.assigns), st.written)
        body.attached(c)
        for w in c.words[1:]:
            if not w.startswith("-"):
                body.file(w)
        return None if body.unreadable else "\n".join(t for _, t in body.parts)
    return None


def _walk(text: str, st: _State, writes: list[Write], bodies: list[str]) -> None:
    substs: list[str] = []
    toks = _words(_unfold(_heredocs(text, bodies), substs))
    stack: list[tuple[str | None, str]] = []
    cmd: list[str] = []
    pipe_in: _Cmd | None = None
    for t in toks + ["\n"]:
        if not _is_sep(t):
            cmd.append(t)
            continue
        c = _parse_cmd(cmd, st, bodies) if cmd else None
        for k in c.substs if c is not None else []:
            if k < len(substs):  # runs before the command, in a subshell
                saved = (st.here, st.unnamed)
                _walk(substs[k], st, writes, bodies)
                st.here, st.unnamed = saved
        if c is not None and c.words:
            _run(c, st, writes, pipe_in, bodies)
        pipe_in = c if t in ("|", "|&") else None
        cmd = []
        for ch in t:
            if ch == "(":
                stack.append((st.here, st.unnamed))
            elif ch == ")" and stack:
                st.here, st.unnamed = stack.pop()


def _run(
    c: _Cmd, st: _State, writes: list[Write], pipe_in: _Cmd | None, bodies: list[str]
) -> None:
    prog = os.path.basename(c.words[0])
    if prog in ("cd", "pushd"):
        dest = next((a for a in c.words[1:] if a == "-" or not a.startswith("-")), None)
        if dest is None:
            st.here, st.unnamed = os.path.expanduser("~"), ""
        else:
            p = None if dest == "-" else _path(dest, st)
            st.here, st.unnamed = (p, "") if p else (None, dest)
        return
    for out in c.outputs:
        p = _path(out, st)
        if p:
            text = _supplied(c, st)
            prior = st.written.get(p, "")
            st.written[p] = None if text is None or prior is None else prior + text
    if prog == "tee":
        for w in c.words[1:]:
            p = None if w.startswith("-") else _path(w, st)
            if p:
                st.written[p] = _supplied(pipe_in, st) if pipe_in else None
        return
    for k, w in enumerate(c.words):
        base = os.path.basename(w)
        if k and prog == "env" and _ASSIGN.match(w):  # `env GH_REPO=O/R gh ...`
            name, value = _ASSIGN.match(w).groups()
            st.assigns[name] = _lenient(value, st.assigns)
            continue
        if base in ("bash", "sh", "zsh") and "-c" in c.words[k + 1 :]:
            j = c.words.index("-c", k + 1)
            if j + 1 < len(c.words):
                saved = (st.here, st.unnamed)
                _walk(c.words[j + 1], st, writes, bodies)
                st.here, st.unnamed = saved
            return
        if base == "gh":
            w_ = _gh_write(c.words[k + 1 :], c, st, pipe_in)
        elif base == "git":
            w_ = _git_write(c.words[k + 1 :], c, st, pipe_in)
        else:
            continue
        if w_ is not None:
            writes.append(w_)
        return


def _positional(args: list[str], value_flags: set[str]) -> list[str]:
    """The words that are neither a flag nor a flag's separate value."""
    out, k = [], 0
    while k < len(args):
        a = args[k]
        if a in value_flags:
            k += 2
            continue
        if a == "-" or not a.startswith("-"):
            out.append(a)
        k += 1
    return out


def _target_url(
    args: list[str], group: str, verb: str, assigns: dict[str, str]
) -> tuple[int, re.Match] | None:
    """(index, match) of the issue or pull request a gh write names by URL: the first
    positional word after the verb that IS such a URL. A text flag's value is never a
    positional, so a URL a body, a title or a comment mentions is text, not the target."""
    if group not in ("issue", "pr"):
        return None
    values = _GH_TARGET_FLAGS | _GH_CONTENT_FLAGS | _GH_FILE_FLAGS | _GH_OTHER_VALUE_FLAGS
    values -= _GH_SWITCHES.get((group, verb), set())
    seen, k = 0, 0
    while k < len(args):
        a = args[k]
        if a in values:
            k += 2
            continue
        if a == "-" or not a.startswith("-"):
            seen += 1
            m = _TARGET_URL.fullmatch(_lenient(a, assigns)) if seen > 2 else None
            if m:  # past the group and the verb
                return k, m
        k += 1
    return None


def _flag_values(args: list[str], names: set[str]) -> list[str]:
    vals = []
    for k, a in enumerate(args):
        for n in names:
            if a == n and k + 1 < len(args):
                vals.append(args[k + 1])
            elif n.startswith("--") and a.startswith(n + "="):
                vals.append(a.split("=", 1)[1])
            elif (
                len(n) == 2
                and a.startswith(n)
                and len(a) > 2
                and not a.startswith("--")
            ):
                vals.append(a[2:])
    return vals


def _content_words(
    args: list[str], skip: set[str], files: set[str]
) -> tuple[list[str], list[str]]:
    """(the words that are content, the files named as content)."""
    words, named, k = [], [], 0
    while k < len(args):
        a = args[k]
        name = a.split("=", 1)[0] if a.startswith("--") else a
        if a in skip or a in files:
            if a in files and k + 1 < len(args):
                named.append(args[k + 1])
            k += 2
            continue
        if name != a and (name in skip or name in files):
            if name in files:
                named.append(a.split("=", 1)[1])
            k += 1
            continue
        words.append(a)
        k += 1
    return words, named


# --- what a write carries ---------------------------------------------------------------


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _added(diff: str) -> str:
    """The lines a diff adds, and the paths it creates or renames to. Removed
    lines are not content the write puts in the repository."""
    out = []
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            if ln != "+++ /dev/null":
                out.append(ln[6:] if ln.startswith("+++ b/") else ln[4:])
        elif ln.startswith(("rename to ", "copy to ")):
            out.append(ln.split(" to ", 1)[1])
        elif ln.startswith("+"):
            out.append(ln[1:])
    return "\n".join(out)


class _Body:
    """What one write would put in the repository, part by part."""

    def __init__(
        self, here: str | None, assigns: dict[str, str], written: dict[str, str | None]
    ):
        self.here, self.assigns, self.written = here, assigns, written
        self.parts: list[tuple[str, str]] = []
        self.unreadable = ""

    def _cannot(self, what: str) -> None:
        self.unreadable = self.unreadable or what

    def _add(self, label: str, text: str) -> None:
        self.parts.append((label, text))
        if _SUBST.search(text):
            self._cannot(f"the output of a command substitution in {label}")

    def text(
        self, label: str, words: list[str], watched: list[str] | None = None
    ) -> None:
        """``watched``: the words whose command substitution makes the text
        unreadable, when that is not all of them."""
        expanded = [_lenient(w, self.assigns) for w in words]
        self.parts.append((label, "\n".join(expanded)))
        seen = expanded if watched is None else [_lenient(w, self.assigns) for w in watched]
        if any(_SUBST.search(w) for w in seen):
            self._cannot(f"the output of a command substitution in {label}")
        for w in expanded:
            for m in _CAT_SUBST.finditer(w):
                self.file(m.group(1))

    def file(self, word: str) -> None:
        if word == "-":
            return
        p = _expand(word, self.assigns)
        if p and (os.path.isabs(p) or self.here):
            p = _vgd._resolve(p, self.here)
        else:
            p = None  # a substitution, a glob, or a variable that is not set
        if p is None:
            self._cannot(word)
            return
        if p in self.written:
            if self.written[p] is None:
                self._cannot(f"{word} (the command writes it from a program's output)")
            else:
                self.parts.append((word, self.written[p]))
            return
        try:
            self.parts.append((word, Path(p).read_text(errors="replace")))
        except OSError:
            self._cannot(word)

    def attached(self, c: _Cmd) -> None:
        for b in c.heredocs:
            self.parts.append(("a heredoc", b))
        for h in c.herestrings:
            self._add("a here-string", _lenient(h, self.assigns))
        for f in c.inputs:
            self.file(f)

    def piped(self, pipe_in: _Cmd | None, st: _State) -> None:
        if pipe_in is None:
            return
        text = _supplied(pipe_in, st)
        if text is None:
            self._cannot(f"the output of {os.path.basename(pipe_in.words[0])}")
        else:
            self.parts.append(("its standard input", text))

    def git(self, label: str, proc: subprocess.CompletedProcess | None,
            diff: bool = True) -> None:
        if proc is None or proc.returncode != 0:
            self._cannot(label)
        else:
            self.parts.append((label, _added(proc.stdout) if diff else proc.stdout))


class Write:
    """One GitHub-bound write: where it goes, and what it would put there.

    ``target`` is ``(owner, repo)``, ``("", "")`` for somewhere that is not
    github.com, None when it cannot be named, or a callable returning one of
    those, so that working out a target costs nothing until there is a hit."""

    def __init__(
        self,
        where: str,
        target,
        parts: list[tuple[str, str]],
        unreadable: str = "",
        known_visibility: str = "",
        hint: str = "",
    ):
        self.where, self.target, self.parts = where, target, parts
        self.unreadable, self.known_visibility, self.hint = (
            _SUBST.sub("$(...)", unreadable),  # a placeholder never reaches a reason
            known_visibility,
            hint,
        )

    def resolve(self):
        return self.target() if callable(self.target) else self.target


def _mcp_write(name: str, tool_input: dict) -> Write | None:
    m = _MCP.match(name)
    if (
        not m
        or "github" not in m.group("server")
        or m.group("tool").startswith(_MCP_READS)
    ):
        return None
    tool = m.group("tool")
    owner, repo = tool_input.get("owner"), tool_input.get("repo")
    target = (
        (owner, repo)
        if isinstance(owner, str) and isinstance(repo, str) and owner and repo
        else None
    )
    known = ""
    if tool == "create_repository":
        known = "private" if tool_input.get("private") else "public"
        target = ("(new)", str(tool_input.get("name", "")))
    content = {k: v for k, v in tool_input.items() if k not in ("owner", "repo")}
    return Write(
        f"mcp {tool}",
        target,
        [("the tool input", "\n".join(_strings(content)))],
        known_visibility=known,
        hint="pass owner and repo",
    )


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _github_repo(url: str) -> tuple[str, str] | None:
    m = _GITHUB_URL.search(url.strip())
    return (m.group("owner"), m.group("repo")) if m else None


def _parse_repo(s: str | None) -> tuple[str, str] | None:
    """``OWNER/REPO``, ``HOST/OWNER/REPO`` or a URL, as gh takes them."""
    if not s:
        return None
    s = s.strip()
    if "github.com" in s:
        return _github_repo(s)
    if "://" in s or s.count("/") >= 2:
        return ("", "")  # another host
    s = s[:-4] if s.endswith(".git") else s
    return tuple(s.split("/")) if _REPO_SLUG.fullmatch(s) else None


def _remote_names(here: str) -> list[str]:
    p = _git(here, "remote")
    return p.stdout.split() if p and p.returncode == 0 else []


def _url_repo(here: str, remote: str, push: bool) -> tuple[str, str]:
    p = _git(here, "remote", "get-url", *(["--push"] if push else []), remote)
    if p is None or p.returncode != 0:
        return ("", "")  # no such remote: nothing goes anywhere through it
    return _github_repo(p.stdout) or ("", "")


def _gh_remote_repo(here: str) -> tuple[str, str]:
    """The repository gh resolves from a directory: the remote ``gh repo
    set-default`` marked, else upstream, github, origin, then any other."""
    names = _remote_names(here)
    base = _git(here, "config", "--get-regexp", r"^remote\..*\.gh-resolved$")
    marked = [
        ln.split(" ")[0][len("remote.") : -len(".gh-resolved")]
        for ln in (base.stdout.splitlines() if base and base.returncode == 0 else [])
        if ln.endswith(" base")
    ]
    order = marked + [n for n in ("upstream", "github", "origin") if n in names] + names
    for name in dict.fromkeys(order):
        repo = _url_repo(here, name, push=False)
        if repo != ("", ""):
            return repo
    return ("", "")


def _git_push_remote(here: str) -> str:
    """The remote a bare ``git push`` uses: branch.<b>.pushRemote,
    remote.pushDefault, branch.<b>.remote, else origin."""
    head = _git(here, "symbolic-ref", "--short", "-q", "HEAD")
    branch = head.stdout.strip() if head and head.returncode == 0 else ""
    keys = (
        ([f"branch.{branch}.pushRemote"] if branch else [])
        + ["remote.pushDefault"]
        + ([f"branch.{branch}.remote"] if branch else [])
    )
    for key in keys:
        p = _git(here, "config", "--get", key)
        if p and p.returncode == 0 and p.stdout.strip() and p.stdout.strip() != ".":
            return p.stdout.strip()
    return "origin"


def _gh_write(
    args: list[str], c: _Cmd, st: _State, pipe_in: _Cmd | None
) -> Write | None:
    pos = _positional(args, _GH_TARGET_FLAGS)
    if not pos:
        return None
    if pos[0] == "api":
        return _gh_api(args, c, st, pipe_in)
    group = pos[0]
    if group not in _GH_GROUPS or len(pos) < 2 or pos[1] in _GH_READS:
        return None
    verb = pos[1]
    here, assigns = st.here, dict(st.assigns)
    body = _Body(here, assigns, st.written)
    url_at = _target_url(args, group, verb, assigns)
    # a URL given as the target says where the write goes, not what it publishes
    kept = args if url_at is None else args[: url_at[0]] + args[url_at[0] + 1 :]
    words, files = _content_words(kept, _GH_TARGET_FLAGS, _GH_FILE_FLAGS)
    body.text("the command", words, _flag_values(args, _GH_CONTENT_FLAGS))
    if group == "gist":
        if verb == "create":
            files += _positional(
                args[args.index("create") + 1 :], {"-d", "--desc", "-f", "--filename"}
            )
        files += _flag_values(args, {"-a", "--add"})
    for f in files:
        body.file(f)
    body.attached(c)
    if "-" in files or (group == "gist" and verb == "create" and not files):
        body.piped(pipe_in, st)

    known = ""
    if group == "gist":
        known = "public"  # a secret gist is still readable by anyone with its URL
    elif group == "repo" and verb == "create":
        known = next(
            (v for v in ("public", "private", "internal") if f"--{v}" in args), ""
        )

    def target():
        if group == "gist":
            return ("(gist)", "(gist)")
        if url_at:  # gh writes where the URL points, whatever directory it runs in and whatever -R says
            m = url_at[1]
            host = re.sub(r"^.*\x40|:\d*$", "", m.group("host").lower())  # the host alone: gh drops userinfo and port
            if host != "github.com" and not host.endswith(".github.com"):
                return ("", "")  # another host
            return (m.group("owner"), m.group("repo"))
        flag = _flag_values(args, {"-R", "--repo"})
        if flag:
            return _parse_repo(_expand(flag[-1], assigns))
        if group == "repo" and len(pos) > 2:
            if verb == "create":
                return ("(new)", pos[2])
            if _REPO_SLUG.fullmatch(pos[2]) or "github.com" in pos[2]:
                return _parse_repo(pos[2])
        env_repo = assigns.get("GH_REPO", os.environ.get("GH_REPO"))
        if env_repo:
            return _parse_repo(env_repo)
        return None if here is None else _gh_remote_repo(here)

    return Write(
        f"gh {group} {verb}",
        target,
        body.parts,
        body.unreadable,
        known,
        hint="name the repository with -R OWNER/REPO",
    )


def _api_key(field: str) -> str:
    """A ``gh api`` field's own name: ``body`` for ``body=…`` and for
    ``comments[][body]=…``."""
    names = re.findall(r"[A-Za-z_]+", field.split("=", 1)[0])
    return names[-1].lower() if names else ""


def _gh_api(args: list[str], c: _Cmd, st: _State, pipe_in: _Cmd | None) -> Write | None:
    method = (_flag_values(args, {"-X", "--method"}) or ["POST"])[-1].upper()
    fields = _flag_values(args, _API_FIELDS)
    inputs = _flag_values(args, {"--input"})
    if method == "GET" or not (fields or inputs):
        return None
    pos = _positional(args, _API_SKIP | _API_FIELDS | {"--input"})
    endpoint = pos[1] if len(pos) > 1 else ""
    here, assigns = st.here, dict(st.assigns)
    body = _Body(here, assigns, st.written)
    files = [v.split("=@", 1)[1] for v in fields if "=@" in v] + inputs
    body.text(
        "the command",
        [v.split("=@", 1)[0] if "=@" in v else v for v in fields],
        [v for v in fields if _api_key(v) in _API_CONTENT_KEYS],
    )
    for f in files:
        body.file(f)
    body.attached(c)
    if "-" in files:
        body.piped(pipe_in, st)
    if endpoint == "graphql" and not body.unreadable and not any(
        "mutation" in t for _, t in body.parts
    ):
        return None  # a GraphQL query reads; only a mutation writes

    def target():
        m = _API_REPO.match(endpoint)
        if not m:
            return None
        if "{owner}" in endpoint or "{repo}" in endpoint:
            env_repo = assigns.get("GH_REPO", os.environ.get("GH_REPO"))
            if env_repo:
                return _parse_repo(env_repo)
            return None if here is None else _gh_remote_repo(here)
        return (m.group("owner"), m.group("repo"))

    return Write(
        "gh api",
        target,
        body.parts,
        body.unreadable,
        hint="this endpoint names no repository",
    )


def _commit_args(rest: list[str]) -> tuple[list[str], list[str], bool]:
    """(messages, message files, whether the unstaged tree is committed too)."""
    msgs: list[str] = []
    files: list[str] = []
    tree = False
    k = 0
    while k < len(rest):
        a = rest[k]
        if a == "--":
            tree = tree or k + 1 < len(rest)
            break
        if _is_op(a):
            k += 2
            continue
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in _COMMIT_VALUES:
                if not eq and k + 1 < len(rest):
                    val = rest[k + 1]
                    k += 1
                if name == "--message":
                    msgs.append(val)
                elif name == "--file":
                    files.append(val)
                elif name == "--pathspec-from-file":
                    tree = True
            elif name in ("--all", "--only", "--include"):
                tree = True
        elif a.startswith("-") and len(a) > 1:
            for idx, ch in enumerate(a[1:]):
                if ch in "aoi":
                    tree = True
                elif ch in "mFCct":
                    val = a[idx + 2 :]
                    if not val and k + 1 < len(rest):
                        val = rest[k + 1]
                        k += 1
                    if ch == "m":
                        msgs.append(val)
                    elif ch == "F":
                        files.append(val)
                    break
        else:
            tree = True  # a pathspec commits the working tree's version of it
        k += 1
    return msgs, files, tree


def _add_args(rest: list[str]) -> tuple[list[str] | None, bool] | None:
    """What a ``git add`` takes in that git does not track yet: ``(pathspecs,
    force)``, the pathspecs None when a file this guard does not read names
    them (``--pathspec-from-file``); None when it takes in no new file (``-u``,
    a dry run, or no pathspec at all)."""
    specs: list[str] = []
    whole = update = force = False
    for k, a in enumerate(rest):
        if a == "--":
            specs += rest[k + 1 :]
            break
        if a.startswith("--"):
            name = a.partition("=")[0]
            if name == "--pathspec-from-file":
                return None, False
            if name == "--dry-run":
                return None
            whole = whole or name in ("--all", "--no-ignore-removal")
            update = update or name == "--update"
            force = force or name == "--force"
        elif a.startswith("-") and len(a) > 1:
            if "n" in a[1:]:
                return None
            whole = whole or "A" in a[1:]
            update = update or "u" in a[1:]
            force = force or "f" in a[1:]
        else:
            specs.append(a)
    if update and not whole or not (specs or whole):
        return None
    return specs or [":/"], force


def _takes_in(spec: str, path: str, d: str, top: str) -> bool:
    """Whether a pathspec given to ``git add`` in ``d`` takes in ``path``."""
    base = os.path.normpath(
        os.path.join(top, spec[2:]) if spec.startswith(":/") else os.path.join(d, spec)
    )
    if any(ch in spec for ch in "*?["):
        return fnmatch.fnmatch(path, base)
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _new_files(body: _Body, st: _State, here: str, top: str) -> None:
    """The files earlier ``git add``s in this command take in that git does not
    track yet. ``git diff`` never lists one, so without this ``git add new.md
    && git commit`` in one command reads nothing of the new file. A file the
    command itself writes first is read from the command: it is not on disk
    yet."""
    top = os.path.realpath(top)
    for d, specs, force in st.added:
        if d is None or specs is None:
            body._cannot("the files an earlier git add in this command names")
            continue
        d = os.path.realpath(d)
        if d != os.path.realpath(here):
            other = _git(d, "rev-parse", "--show-toplevel")
            if other is None:
                body._cannot("the files an earlier git add in this command names")
                continue
            if other.returncode != 0 or os.path.realpath(other.stdout.strip()) != top:
                continue  # another repository: not what this commit takes
        listed = _git(
            d,
            "ls-files",
            "-z",
            "--others",
            *([] if force else ["--exclude-standard"]),
            "--",
            *specs,
        )
        if listed is None or listed.returncode != 0:
            body._cannot("the files an earlier git add in this command names")
            continue
        for rel in filter(None, listed.stdout.split("\0")):
            body.parts.append(("a new file's path", rel))
            try:
                body.parts.append(("a new file", Path(d, rel).read_text(errors="replace")))
            except OSError:
                body._cannot(rel)
        for p, text in st.written.items():
            if any(_takes_in(s, p, d, top) for s in specs):
                rel = os.path.relpath(p, top)
                body.parts.append(("a new file's path", rel))
                if text is None:
                    body._cannot(f"{rel} (the command writes it from a program's output)")
                else:
                    body.parts.append(("a new file", text))


def _git_write(
    args: list[str], c: _Cmd, st: _State, pipe_in: _Cmd | None
) -> Write | None:
    scope, verb, vi, unrec = _vgd._parse_git_args(args)
    if verb is None and unrec:
        vi = next(
            (k for k, a in enumerate(args) if a in ("commit", "push", "add", "stage")),
            None,
        )
        if vi is None:
            return None
        verb = args[vi]
    if verb not in ("add", "stage", "commit", "push"):
        return None
    here, unnamed = st.here, st.unnamed
    if {"--git-dir", "--work-tree"} & set(scope) or {"GIT_DIR", "GIT_WORK_TREE"} & set(
        st.assigns
    ):
        here, unnamed = None, "--git-dir or GIT_DIR"
    elif "-C" in scope:
        p = _path(scope["-C"], st)
        here, unnamed = (p, "") if p else (None, scope["-C"])
    if verb in ("add", "stage"):
        st.adding = True
        added = _add_args(args[vi + 1 :])
        if added is not None:
            st.added.append((here, *added))
        return None
    where = f"git {verb}"
    if here is None:
        return Write(
            where,
            None,
            [],
            f"the repository ({unnamed})",
            hint="run it from a literal path: cd /abs/path, or git -C /abs/path",
        )
    top = _git(here, "rev-parse", "--show-toplevel")
    if top is None:
        return Write(
            where,
            None,
            [],
            "the repository (git did not answer)",
            hint="git did not answer",
        )
    if top.returncode != 0:
        return None  # not a repository: nothing it does reaches GitHub
    rest = args[vi + 1 :]
    body = _Body(here, dict(st.assigns), st.written)
    if verb == "commit":
        msgs, files, tree = _commit_args(rest)
        body.text("the message", msgs)
        for f in files:
            body.file(f)
        body.attached(c)
        if "-" in files:
            body.piped(pipe_in, st)
        body.git(
            "the staged changes",
            _git(here, "diff", "--cached", "--no-color", "-U0", "-M"),
        )
        if tree or st.adding:
            body.git(
                "the unstaged changes", _git(here, "diff", "--no-color", "-U0", "-M")
            )
        repo_top = top.stdout.strip()
        _new_files(body, st, here, repo_top)
        parts, cannot = st.pending.get(repo_top, ([], ""))
        st.pending[repo_top] = (parts + body.parts, cannot or body.unreadable)
        return Write(
            where,
            lambda: _url_repo(here, _git_push_remote(here), push=True),
            body.parts,
            body.unreadable,
        )
    if any(a in ("-d", "--delete") for a in rest):
        return None  # deleting a remote ref carries no content
    pos = _positional([a for a in rest if not _is_op(a)], _PUSH_VALUES)
    remote = pos[0] if pos else None
    specs = pos[1:]
    refs = [r for r in (s.split(":", 1)[0].lstrip("+") for s in specs) if r]
    if specs and not refs:
        return None  # every refspec deletes (":branch")
    if any(a in ("--all", "--branches", "--mirror") for a in rest):
        refs = ["--branches"]
    elif not refs:
        refs = ["HEAD"]
    if "--tags" in rest or "--mirror" in rest:
        refs.append("--tags")
    if remote is None:
        name: str | None = _git_push_remote(here)
    else:
        name = _expand(remote, st.assigns)
        if name is None:
            return Write(
                where,
                None,
                [],
                f"the remote ({remote})",
                hint="name the remote literally",
            )
    if (
        "://" in name or "@" in name or name.endswith(".git")
    ):  # a URL, not a remote's name
        url, name = name, None
        target = _github_repo(url) or ("", "")
    else:
        remote_name = name
        target = lambda: _url_repo(here, remote_name, push=True)  # noqa: E731
    exclude = ["--not", f"--remotes={name}" if name else "--remotes"]
    body.git(
        "the commit messages to push",
        _git(here, "log", "--format=%B", *refs, *exclude, "--"),
        diff=False,
    )
    body.git(
        "the changes to push",
        _git(
            here,
            "log",
            "--format=",
            "-p",
            "-M",
            "-U0",
            "--no-color",
            *refs,
            *exclude,
            "--",
        ),
    )
    parts, cannot = st.pending.get(top.stdout.strip(), ([], ""))
    body.parts += [
        (f"{label} of a commit made earlier in this command", text)
        for label, text in parts
    ]
    if cannot:
        body._cannot(cannot)
    return Write(where, target, body.parts, body.unreadable)


def _bash_writes(command: str, cwd: str) -> list[Write]:
    writes: list[Write] = []
    _walk(command, _State(cwd), writes, [])
    return writes


# --- where a write goes -----------------------------------------------------------------


def _cache_path() -> Path:
    env = os.environ.get("PUBLIC_WRITE_GUARD_CACHE")
    if env:
        return Path(env)
    root = os.environ.get("CLAUDLOBBY_ROOT") or ""
    base = Path(root) / "state" if os.path.isabs(root) else Path.home() / ".cache" / "claudlobby"
    return base / "public-write-guard" / "visibility.json"


def visibility(owner: str, repo: str) -> str:
    """``public``, ``private``, ``internal`` or ``unknown``, read live and cached
    for CACHE_LIFETIME_S. An unknown answer is never cached, and an answer
    stamped in the future (the clock stepped back since) is not trusted. When
    the live read fails, an answer cached up to STALE_LIFETIME_S ago stands in."""
    key = f"{owner}/{repo}".lower()
    cache = _cache_path()
    now = time.time()
    try:
        data = json.loads(cache.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    hit = data.get(key)
    age = None
    if (
        isinstance(hit, list)
        and len(hit) == 2
        and hit[0] in _VISIBILITIES
        and isinstance(hit[1], (int, float))
        and 0 <= now - hit[1] < STALE_LIFETIME_S
    ):
        age = now - hit[1]
    if age is not None and age < CACHE_LIFETIME_S:
        return hit[0]
    vis = _live_visibility(owner, repo)
    if vis == "unknown":
        return hit[0] if age is not None else "unknown"
    data[key] = [vis, now]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, cache)
    except OSError:
        pass
    return vis


def _live_visibility(owner: str, repo: str) -> str:
    """REST first, then GraphQL (``gh repo view``) when REST failed fast: a REST
    throttle leaves GraphQL working. A call that timed out is not repeated,
    since a gh that hung would hang again."""
    for argv in (
        ["gh", "api", f"repos/{owner}/{repo}", "--jq", ".visibility"],
        ["gh", "repo", "view", f"{owner}/{repo}", "--json", "visibility", "--jq", ".visibility"],
    ):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=GH_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        vis = p.stdout.strip().lower() if p.returncode == 0 else ""
        if vis in _VISIBILITIES:
            return vis
    return "unknown"


# --- the decision ---------------------------------------------------------------------


def _deny(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "public-write guard: " + reason,
            }
        }
    )


def _emit(event: str, data: dict) -> None:
    seam = os.environ.get("PUBLIC_WRITE_GUARD_EVENTS_FILE")
    if seam:
        with open(seam, "a") as fh:
            fh.write(json.dumps({"type": event, "data": data}, sort_keys=True) + "\n")
        return
    if os.environ.get("PLANE_EMIT_DISABLED") == "1":
        return
    lib = str(LIB)  # lib-common.sh ships beside this file, in the same release
    # The bot is named rather than left to an ambient BOT_DIR: an event anchored
    # on the fleet is one neither `claudlobby event list --bot` nor its brief reads.
    script = (
        '( . "$1/lib-common.sh" >/dev/null 2>&1 || exit 0; '
        'emit_fleet_event "$2" public-write-guard "$3" "$4" "$5" ) </dev/null >/dev/null 2>&1 &'
    )
    try:
        subprocess.run(
            [
                "bash",
                "-c",
                script,
                "public-write-guard",
                lib,
                event,
                json.dumps(data, sort_keys=True),
                os.environ.get("BOT_DIR", ""),
                os.environ.get("BOT_ID", ""),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def decide(payload: dict) -> str | None:
    """The hook's stdout (a deny decision), or None to let the call through."""
    name = payload.get("tool_name") or ""
    tool_input = (
        payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    )
    cwd = payload.get("cwd") or os.getcwd()
    if name == "Bash":
        command = (
            tool_input.get("command")
            if isinstance(tool_input.get("command"), str)
            else ""
        )
        writes = _bash_writes(command, cwd) if command else []
    else:
        w = _mcp_write(name, tool_input)
        writes = [w] if w else []
    if not writes:
        return None
    rx, state = load_terms(terms_path())
    if state == "absent":
        _emit(
            "public_write_guard_unarmed",
            {"where": writes[0].where, "list": _list_label()},
        )
        return None
    if rx is None:
        _emit("public_write_refused", {"where": writes[0].where, "why": "list"})
        return _deny(
            f"the host's term list ({_list_label()}) is broken "
            f"({state[len('broken: ') :]}); fix the list, then retry"
        )
    for w in writes:
        hits = [
            (label, n)
            for label, text in w.parts
            if (n := sum(1 for _ in rx.finditer(text)))
        ]
        if not hits and not w.unreadable:
            continue
        target = w.resolve()
        if target == ("", ""):
            continue  # not github.com: out of scope
        if target is None:
            vis, dest = "unknown", ""
        else:
            vis = w.known_visibility or visibility(*target)
            dest = {
                "(gist)": "a gist, which anyone with its URL can read",
                "(new)": f"a new repository, {target[1]}",
            }.get(target[0], "/".join(target))
        if vis in ("private", "internal"):
            continue
        found = sum(n for _, n in hits)
        if hits:
            what = (
                f"{found} match(es) for the host's term list ({_list_label()}) in "
                + ", ".join(dict.fromkeys(label for label, _ in hits))
            )
            fix = "Remove the listed text, or write it somewhere private."
        else:
            what = f"this guard could not read {w.unreadable}"
            fix = "Make it readable, then retry."
        if not dest:
            to = f"this guard could not tell which repository it goes to ({w.hint})"
        elif vis == "unknown":
            to = f"it goes to {dest}, whose visibility could not be read (check `gh auth status`)"
        else:
            to = f"it goes to {dest}, which is {vis}"
        _emit(
            "public_write_refused",
            {
                "where": w.where,
                "repo": dest or "unknown",
                "visibility": vis,
                "matches": found,
                "unreadable": bool(w.unreadable and not hits),
            },
        )
        return _deny(f"{w.where}: {what}; {to}. {fix}")
    return None


def _off_switch() -> Path | None:
    """The host-wide off switch the hook script checks before anything else: host
    state under the data root, never beside this release's code. None without an
    absolute CLAUDLOBBY_ROOT, where the hook has no switch to read either."""
    root = os.environ.get("CLAUDLOBBY_ROOT") or ""
    if not os.path.isabs(root):
        return None
    return Path(root) / "state" / "public-write-guard" / "disabled"


def check() -> int:
    """``--check``: whether the host's guard is armed, without printing a term.
    It names the off switch too: that passes every call and records nothing,
    so a host left switched off would otherwise read as a canary with no
    refusals."""
    rx, state = load_terms(terms_path())
    if rx is None:
        print(f"{_list_label()}: {state}")
    else:
        n = sum(
            1
            for ln in terms_path().read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
        print(f"{_list_label()}: ok, {n} pattern(s)")
    off = _off_switch()
    if off is None:
        print("no CLAUDLOBBY_ROOT data directory, so no off switch to read")
    elif off.exists():
        print(f"{off}: set, so every call passes unread; remove it to arm the guard")
    return 0 if rx is not None and (off is None or not off.exists()) else 1


def main() -> int:
    if sys.argv[1:] == ["--check"]:
        return check()
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        print("public-write-guard: unparseable hook payload; allowing", file=sys.stderr)
        return 3
    if not isinstance(payload, dict):
        return 0
    out = decide(payload)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
