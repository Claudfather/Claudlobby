"""Parse gate: every shipped bash script must survive `bash -n` under the
system bash — claudlobby/_runtime_scripts/ plus any helper a library/ item ships (e.g. a skill's
executable, which is symlinked into the bot dir and run as-is).

macOS /bin/bash is bash 3.2 — the shebang target of every lib script, so a
production parser, not a relic. Its command-substitution scanner does not
strip comments inside $( ): an apostrophe there opens a string and corrupts
quoting for the rest of the file (see the warning at the bite site in
lib-common.sh bridge_state). On Linux CI this is a plain syntax gate; on
macOS it also catches the 3.2-only class.

A second check reads the same scripts for a word that bash 3.2 splits and
bash 5 keeps whole: a brace literal in a double-quoted string inside a quoted
$( ) (#2857). `bash -n` passes that on both, so the check is a scan in
Python, which reads the same on every runner.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent


def _lib_scripts() -> list[Path]:
    """Every claudlobby/_runtime_scripts/ file that is bash: *.sh plus suffix-less scripts whose
    shebang names bash."""
    scripts = set((REPO_DIR / "claudlobby/_runtime_scripts").glob("*.sh"))
    for p in (REPO_DIR / "claudlobby/_runtime_scripts").iterdir():
        if p.is_file() and p.suffix == "":
            try:
                first = p.open("rt", errors="ignore").readline()
            except OSError:
                continue
            if first.startswith("#!") and "bash" in first:
                scripts.add(p)
    return sorted(scripts)


# claudlobby/_runtime_scripts/ only. Kept as its own set because tests/test_no_dead_session_command.py
# imports it for a guard that is deliberately lib/-scoped (it is about the
# slash commands start-bot.sh and pre-stop-handoff.sh inject as keystrokes).
# Widening the parse gate below must not silently widen that guard.
LIB_SCRIPTS = _lib_scripts()

# The parse gate's own set: claudlobby/_runtime_scripts/ plus any *.sh a library/ item ships. A skill's
# helper is symlinked into the bot dir rather than rendered, so a syntax error
# in one reaches the bot verbatim — same trust properties as lib/.
SHIPPED_SCRIPTS = sorted(set(LIB_SCRIPTS) | set((REPO_DIR / "library").rglob("*.sh"))
                         | set((REPO_DIR / "harness").rglob("*.sh")))


def _script_id(p: Path) -> str:
    # Repo-relative: bare names now collide across claudlobby/_runtime_scripts/ and library/.
    return str(p.relative_to(REPO_DIR))


@pytest.mark.parametrize("script", SHIPPED_SCRIPTS, ids=_script_id)
def test_shipped_script_parses_under_system_bash(script):
    r = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"/bin/bash -n {_script_id(script)} failed:\n{r.stderr}"


# ---------------------------------------------------------------------------
# The bash 3.2 brace split (#2857). #2851 first pushed
#     _add "$(_metric host.procs "{\"running\":$_running,\"blocked\":$_blocked}")"
# and its macOS jobs failed while every Linux job passed. Brace expansion scans a word for its
# quotes, and bash 3.2's scan pairs the double quotes without stepping over the $( ). So it reads
# the inner string as unquoted and splits the word at the comma: `_add` got two words. bash 5.2.15
# steps over the $( ) and keeps the word whole (no bash 4 was run), and `bash -n` passes the line
# on both.
#
# _BraceScan follows a script's quoting: quotes, $( ), ${ }, backquotes, $(( )), comments and
# heredocs. It reports each `{` that opens a brace expression (one with a comma, or a sequence such
# as {1..3}) inside a double-quoted string, in a $( ) that is itself inside a double-quoted string.
# In that inner string it pairs apostrophes as bash 3.2 does, so "it's {a,b}" stays whole there.
# _SHAPES holds the shapes it was checked on, each run on bash 3.2.57 and 5.2.15.
#
# What it does not tell apart:
# - It reports the shape where bash 3.2 does no brace expansion: a plain assignment (v="$(...)"),
#   an assignment before a command, [[ ]], a case word and a here-string. Telling those apart
#   needs a command parser, and that parser's mistakes would be misses. The fix is one line either
#   way: build the object outside the $( ), in a variable or with printf or awk.
# - It misses a brace that opens in one string of the $( ) and closes in another. It also misses
#   two other shapes bash 3.2 splits: a brace in an unquoted <( ), and a case pattern inside a
#   $( ), which bash 3.2 cannot parse anyway.
# - Where it cannot follow a script's quoting, it fails that script instead of passing it unread.

_SEQUENCE = re.compile(r"[+-]?[0-9]+\.\.[+-]?[0-9]+|[A-Za-z]\.\.[A-Za-z]")
_BREAKS = frozenset(" \t\n;&|()<>")


class _LostPlace(ValueError):
    """The scan could not follow a script's quoting."""


def _in_quoted_substitution(chain):
    """Whether the string that ends `chain` is in a $( ) that is inside a double-quoted string."""
    quoted = False
    for kind in chain[:-1]:
        if kind == "dq":
            quoted = True
        elif kind == "cs" and quoted:
            return True
    return False


class _BraceScan:
    """One pass over a script. `chain` names the constructs around a position: "dq" a
    double-quoted string, "cs" a $( ), "pe" a ${ }, "bq" a backquote and "hd" a heredoc body."""

    def __init__(self, text):
        self.text = text
        self.hits = []  # offset of each `{` that bash 3.2 expands
        self.heredocs = []  # (delimiter, quoted, strips tabs), each waiting for its line to end

    def lost(self, i, what):
        raise _LostPlace(f"{what}, from line {self.text.count(chr(10), 0, i) + 1}")

    def code(self, i, chain, stop):
        """Shell code up to `stop`: ")" ends a $( ), "`" a backquote, None the script."""
        t = self.text
        depth = 0
        word_start = True
        while i < len(t):
            c = t[i]
            if c == "\\":
                i += 2
                word_start = False
                continue
            if c == stop == "`":
                return i + 1
            if c == "#" and word_start:
                end = t.find("\n", i)
                i = len(t) if end < 0 else end
                continue
            if c == "\n":
                i = self.heredoc_bodies(i + 1, chain)
                word_start = True
                continue
            if c == "'":
                i = self.single(i + 1)
            elif c == '"':
                i = self.double(i + 1, chain + ("dq",))
            elif t.startswith("$'", i):
                i = self.ansi(i + 2)
            elif t.startswith("$((", i):
                i = self.arith(i + 3, chain)
            elif t.startswith("$(", i):
                i = self.code(i + 2, chain + ("cs",), ")")
            elif t.startswith("${", i):
                i = self.param(i + 2, chain + ("pe",))
            elif c == "`":
                i = self.code(i + 1, chain + ("bq",), "`")
            elif word_start and t.startswith("((", i):
                i = self.arith(i + 2, chain)
            elif t.startswith("<<<", i):
                i += 3
            elif t.startswith("<<", i):
                i = self.heredoc(i + 2)
            elif c == ")" and stop == ")" and depth == 0:
                return i + 1
            else:
                depth += (c == "(") - (c == ")")
                i += 1
            word_start = c in _BREAKS
        if stop is not None:
            self.lost(i, "unterminated $(" if stop == ")" else "unterminated backquote")
        return i

    def single(self, i):
        end = self.text.find("'", i)
        if end < 0:
            self.lost(i, "unterminated single quote")
        return end + 1

    def ansi(self, i):
        t = self.text
        while i < len(t):
            if t[i] == "\\":
                i += 2
            elif t[i] == "'":
                return i + 1
            else:
                i += 1
        self.lost(i, "unterminated $'")

    def arith(self, i, chain):
        """$(( )) or (( )), up to the closing "))"."""
        t = self.text
        depth = 0
        while i < len(t):
            c = t[i]
            if c == "\\":
                i += 2
            elif t.startswith("$((", i):
                i = self.arith(i + 3, chain)
            elif t.startswith("$(", i):
                i = self.code(i + 2, chain + ("cs",), ")")
            elif t.startswith("${", i):
                i = self.param(i + 2, chain + ("pe",))
            elif c == '"':
                i = self.double(i + 1, chain + ("dq",))
            elif c == ")" and depth == 0:
                if not t.startswith("))", i):
                    self.lost(i, "unbalanced arithmetic")
                return i + 2
            else:
                depth += (c == "(") - (c == ")")
                i += 1
        self.lost(i, "unterminated arithmetic")

    def param(self, i, chain):
        """A ${ }: bash ends it at its first unquoted `}`, so a `{` in it does not nest."""
        t = self.text
        while i < len(t):
            c = t[i]
            if c == "\\":
                i += 2
            elif c == "}":
                return i + 1
            elif c == '"':
                i = self.double(i + 1, chain + ("dq",))
            elif c == "'" and chain[-2:-1] != ("dq",):
                i = self.single(i + 1)
            elif t.startswith("$((", i):
                i = self.arith(i + 3, chain)
            elif t.startswith("$(", i):
                i = self.code(i + 2, chain + ("cs",), ")")
            elif t.startswith("${", i):
                i = self.param(i + 2, chain + ("pe",))
            elif c == "`":
                i = self.code(i + 1, chain + ("bq",), "`")
            else:
                i += 1
        self.lost(i, "unterminated ${")

    def double(self, i, chain):
        """A double-quoted string, from after its opening quote."""
        t = self.text
        watch = _in_quoted_substitution(chain)
        braces = []  # [offset of `{`, saw a comma] for each brace still open in this string
        apostrophe = False
        while i < len(t):
            c = t[i]
            if c == "\\":
                i += 2
            elif c == '"':
                return i + 1
            elif t.startswith("$((", i):
                i = self.arith(i + 3, chain)
            elif t.startswith("$(", i):
                i = self.code(i + 2, chain + ("cs",), ")")
            elif t.startswith("${", i):
                i = self.param(i + 2, chain + ("pe",))
            elif c == "`":
                i = self.code(i + 1, chain + ("bq",), "`")
            else:
                if watch and c == "'":
                    apostrophe = not apostrophe
                elif watch and not apostrophe:
                    self.brace(i, braces)
                i += 1
        self.lost(i, "unterminated double quote")

    def brace(self, i, braces):
        """bash's rule for a brace expression: a `{` with blanks both sides (or a blank before
        and `}` after) opens none, and it needs a comma or a sequence before its `}`."""
        t = self.text
        c = t[i]
        if c == "{" and not (t[i - 1] in " \t\n" and t[i + 1 : i + 2] in (" ", "\t", "\n", "}")):
            braces.append([i, False])
        elif c == "," and braces:
            braces[-1][1] = True
        elif c == "}" and braces:
            start, comma = braces.pop()
            if comma or _SEQUENCE.fullmatch(t[start + 1 : i]):
                self.hits.append(start)

    def heredoc(self, i):
        """After `<<`: note the delimiter. Its body starts on the next line."""
        t = self.text
        strip = t.startswith("-", i)
        i += strip
        while i < len(t) and t[i] in " \t":
            i += 1
        end = i
        while end < len(t) and t[end] not in _BREAKS:
            end += 1
        word = t[i:end]
        delimiter = re.sub(r"[\"'\\]", "", word)
        if not delimiter:
            self.lost(i, "heredoc without a delimiter")
        self.heredocs.append((delimiter, delimiter != word, strip))
        return end

    def heredoc_bodies(self, i, chain):
        """At the start of a line: read past the body of each heredoc its line opened."""
        t = self.text
        while self.heredocs:
            delimiter, quoted, strip = self.heredocs.pop(0)
            start = i
            while True:
                if i >= len(t):
                    self.lost(start, f"heredoc {delimiter} never ends")
                end = t.find("\n", i)
                end = len(t) if end < 0 else end
                if (t[i:end].lstrip("\t") if strip else t[i:end]) == delimiter:
                    break
                i = end + 1
            if not quoted:
                self.heredoc_body(start, i, chain + ("hd",))
            i = end + 1
        return i

    def heredoc_body(self, i, end, chain):
        """An unquoted heredoc's body: no quotes, but its $( ) and ${ } run."""
        t = self.text
        while i < end:
            if t[i] == "\\":
                i += 2
            elif t.startswith("$((", i):
                i = self.arith(i + 3, chain)
            elif t.startswith("$(", i):
                i = self.code(i + 2, chain + ("cs",), ")")
            elif t.startswith("${", i):
                i = self.param(i + 2, chain + ("pe",))
            elif t[i] == "`":
                i = self.code(i + 1, chain + ("bq",), "`")
            else:
                i += 1


def _brace_splits(text):
    """The line of each brace literal in `text` that bash 3.2 splits and bash 5 keeps whole."""
    scan = _BraceScan(text)
    scan.code(0, (), None)
    return sorted({text.count("\n", 0, i) + 1 for i in scan.hits})


@pytest.mark.parametrize("script", SHIPPED_SCRIPTS, ids=_script_id)
def test_shipped_script_has_no_brace_literal_bash_3_2_splits(script):
    text = script.read_text(encoding="utf-8", errors="replace")
    try:
        found = _brace_splits(text)
    except _LostPlace as e:
        pytest.fail(f"the brace scan cannot follow the quoting of {_script_id(script)} ({e}): "
                    "teach _BraceScan the construct, so that the script is read and not passed")
    rows = text.splitlines()
    assert not found, (
        "each line below has a brace literal in a double-quoted string inside a quoted $( ). "
        "bash 3.2, which is macOS /bin/bash, splits such a word at the brace and bash 5 keeps it "
        "whole. bash 3.2 keeps it whole too in five shapes the scan does not tell apart: a plain "
        "assignment, an assignment before a command, [[ ]], a case word and a here-string. Either "
        "way, build the object outside the $( ), in a variable or with printf or awk (#2857):\n"
        + "\n".join(f"{_script_id(script)}:{n}: {rows[n - 1].strip()}" for n in found))


# The two lines #2851 first pushed: plane-host-probe.sh at c4e229a8, lines 161 and 171. On bash
# 3.2 each `_add` got two words; #2851 then built both objects with awk.
_FIRST_PUSHED_2851 = r"""[ -n "$_swp_out" ] && _add "$(_metric host.swap_pages "{\"in\":$_swp_in,\"out\":$_swp_out}")"
[ -n "$_blocked" ] && _add "$(_metric host.procs "{\"running\":$_running,\"blocked\":$_blocked}")"
"""


def test_the_brace_scan_reports_both_lines_2851_first_pushed():
    assert _brace_splits(_FIRST_PUSHED_2851) == [1, 2]


# Each shape with the lines where bash 3.2.57 and 5.2.15 differ when it runs (#2857).
_SHAPES = [
    # bash 3.2 splits these
    ("text-before-the-brace", r'_add "$(_metric x "p{a,b}")"', [1]),
    ("a-blank-before-the-brace", r'_add "$(_metric x " {a,b}")"', [1]),
    ("a-parameter-before-the-comma", r'_add "$(_metric x "{\"a\":${n},\"b\":2}")"', [1]),
    ("a-sequence", r'_add "$(_metric x "{1..3}")"', [1]),
    ("a-pair-of-apostrophes-before", r'''_add "$(_metric x "'q' {a,b}")"''', [1]),
    ("a-python-dict", r'''_add "$(python3 -c "print({'a': 1, 'b': 2})")"''', [1]),
    ("a-parenthesis-before", r'_add "$(_metric "$(printf x)" "{\"a\":1,\"b\":2}")"', [1]),
    ("a-continued-line", '_add "$(_metric x \\\n  "{\\"a\\":1,\\"b\\":2}")"', [2]),
    ("a-substitution-over-lines", '_add "$(\n  _metric x "{\\"a\\":1,\\"b\\":2}"\n)"', [2]),
    ("local", r'_l() { local v="$(_metric x "{\"a\":1,\"b\":2}")"; _add "$v"; }; _l', [1]),
    ("a-nested-substitution", r'_add "$(_metric a "$(_metric b "{c,d}")")"', [1]),
    ("a-substitution-in-a-default", r'_add "${unset_v:-$(_metric y "{a,b}")}"', [1]),
    ("a-heredoc-in-a-quoted-substitution", '_add "$(cat <<EOF\n$(_metric x "{a,b}")\nEOF\n)"', [2]),
    # bash 3.2 keeps these whole
    ("an-unquoted-substitution", r'_add $(_metric x "{\"a\":1,\"b\":2}")', []),
    ("a-comment", r'# _add "$(_metric x "{a,b}")"', []),
    ("single-quoted-text", r'''_add '"$(_metric x "{a,b}")"' ''', []),
    ("a-heredoc", 'cat <<EOF\n$(_metric x "{a,b}")\nEOF', []),
    ("a-quoted-heredoc", "cat <<'EOF'\n_add \"$(_metric x \"{a,b}\")\"\nEOF", []),
    ("an-escaped-comma", r'_add "$(_metric x "{a\,b}")"', []),
    ("a-single-quoted-inner-string", r'''_add "$(_metric x '{"a":1,"b":2}')"''', []),
    ("a-brace-between-blanks", r'_add "$(_metric x " { a,b }")"', []),
    ("no-comma", r'_add "$(_metric x "{\"a\":1}")"', []),
    ("an-apostrophe-before", r'''_add "$(_metric x "it's {a,b}")"''', []),
    ("a-brace-in-a-parameter", r'_add "$(_metric x "${n:+{a,b}}")"', []),
    ("an-unquoted-brace-in-the-substitution", r'''_add "$(printf '%s,' {a,b})"''', []),
    # and the scan keeps its place past these
    ("after-an-arithmetic-shift", 'echo $((1 << 2))\n_add "$(_metric x "{a,b}")"', [2]),
    ("after-a-here-string", 'cat <<< "x"\n_add "$(_metric x "{a,b}")"', [2]),
    ("after-a-brace-in-a-default", 'v="$(_f "${out:-{\\}}")"\n_add "$(_metric x "{a,b}")"', [2]),
]


@pytest.mark.parametrize(("shape", "lines"), [s[1:] for s in _SHAPES], ids=[s[0] for s in _SHAPES])
def test_the_brace_scan_reports_where_bash_3_2_splits(shape, lines):
    assert _brace_splits(shape) == lines


# Inputs the scan cannot follow, each with the error it raises: the construct, and the line the
# scan stopped on. The per-script test fails such a script by name. If `lost` stopped raising,
# the scan would go on without its place: the first and third inputs would end in a TypeError,
# and the second would be read to its end and passed. An unterminated heredoc or single quote is
# left out: there the scan would loop forever.
_LOST = [
    ("an-unterminated-double-quote", 'echo ok\necho "a', "unterminated double quote, from line 2"),
    ("an-unterminated-substitution", "echo ok\necho $(date", "unterminated $(, from line 2"),
    ("an-unterminated-parameter", "echo ok\necho ${v", "unterminated ${, from line 2"),
]


@pytest.mark.parametrize(("text", "error"), [s[1:] for s in _LOST], ids=[s[0] for s in _LOST])
def test_the_brace_scan_names_what_it_cannot_follow(text, error):
    with pytest.raises(_LostPlace) as lost:
        _brace_splits(text)
    assert str(lost.value) == error
