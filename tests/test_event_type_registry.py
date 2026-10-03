"""#903 — one source of truth for the fleet's event-type vocabulary.

`SYSTEM_EVENT_SEVERITY` (claudlobby/plane/registries.py) is what the plane
stamps severity from at ingest. A type it lacks still ingests, with NULL
severity: `event list --critical`, a bot's brief and fleet-pulse's reads can
never show it, and registering it later does not re-stamp the rows already
stored. So the gate sits on everything that names a type:

  a. every literal type a runtime shell script, or a shell block in the
     library, hands a shell writer is a registry key, and every call whose
     type is a variable is listed below with the values it can take;
  b. every hand-built system row and every Python writer's literal type is a
     registry key;
  c. fleet-pulse's two critical lists equal the registry's PULSE_* sets, each
     a subset of the critical types;
  d. every type the event tables of the observability protocol, the
     fleet-pulse skill and the observability guide name is a registry key,
     and a table labelled critical lists exactly the critical types.

Every gate reads files with the standard library; nothing runs. Each scan has
a positive control, so a pattern that stops matching fails instead of passing
on an empty result.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from claudlobby.plane.registries import (
    PULSE_ESCALATION_TYPES,
    PULSE_SUMMARY_TYPES,
    SYSTEM_EVENT_SEVERITY,
)

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby" / "_runtime_scripts"
RS = "claudlobby/_runtime_scripts/"
CRITICAL = frozenset(t for t, s in SYSTEM_EVENT_SEVERITY.items() if s == "critical")
LITERAL = re.compile(r"""["']([a-z][a-z0-9_]*)["']""")


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


@functools.cache
def _runtime_shell_scripts() -> tuple[Path, ...]:
    """Every shell script the runtime ships: *.sh, and an extension-less file
    whose shebang names a shell (the git credential helper)."""
    out = []
    for p in sorted(LIB.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix == ".sh":
            out.append(p)
        elif not p.suffix:
            first = p.read_bytes().split(b"\n", 1)[0]
            if first.startswith(b"#!") and re.search(rb"\b(ba)?sh\b", first):
                out.append(p)
    return tuple(out)


@functools.cache
def _python_modules() -> tuple[Path, ...]:
    return tuple(sorted((REPO / "claudlobby").rglob("*.py")))


def _code(text: str) -> str:
    """Full-line comments blanked, so a writer named in prose is not a call.
    Lines are kept, so line numbers still point at the file."""
    return "\n".join("" if l.lstrip().startswith("#") else l for l in text.split("\n"))


# --- a. the shell writers ------------------------------------------------------

# Each shell writer and the argument (1-based) that carries its type.
SHELL_WRITERS = {
    "emit_fleet_event": 1,
    "emit_failure_alert": 2,
    "emit_fleet_notice": 2,
    "notify_currency": 2,
    "_emit_fleet_signal": 2,
}
# A script's own wrapper around a writer; its calls are scanned like a writer's.
LOCAL_WRAPPERS = {
    RS + "credential-echo-guard.sh": {"_event": 1},
    RS + "vault-git-guard.sh": {"_event": 1},
}
# A writer call whose type is a variable, keyed (file, writer, the argument as
# written): the pattern that reads, from the script itself, every value the
# variable takes, and those values. The list is checked against the script,
# never trusted: bot-vitals reads its type from the Python it embeds, which
# hands each type to evt()...
VARIABLE_TYPES = {
    (RS + "bot-vitals.sh", "emit_fleet_event", '"$_etype"'):
        (r"\bevt\('([a-z][a-z0-9_]*)'", {"tool_call", "session_event"}),
    (RS + "host-health-check.sh", "emit_failure_alert", '"$KEY"'):
        (r'\bKEY="([a-z][a-z0-9_]*)"', {"host_health", "undervoltage", "storage_stall"}),
}
# ...or the writers that hand their own caller's type through. Their calls are
# scanned too, so the type is checked where it is written as a literal.
FORWARDED_TYPES = {
    (RS + "lib-common.sh", "emit_fleet_event", '"$event_type"'): ("_emit_fleet_signal",),
    (RS + "lib-common.sh", "_emit_fleet_signal", '"$2"'):
        ("emit_failure_alert", "emit_fleet_notice"),
    (RS + "lib-common.sh", "emit_fleet_notice", '"$etype"'): ("notify_currency",),
    (RS + "credential-echo-guard.sh", "emit_fleet_event", '"$1"'): ("_event",),
    (RS + "vault-git-guard.sh", "emit_fleet_event", '"$1"'): ("_event",),
}

FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.M | re.S)


def _call(names) -> re.Pattern:
    """A call of one of *names* at command position: a line start, or after an
    operator, a brace, a case arm's `)` or a keyword, behind any `NAME=value`
    environment prefixes (`FLEET=x emit_fleet_event t` records t). A definition
    (`name()`) and a mention (`command -v name`, a label argument) are not calls."""
    alt = "|".join(sorted(map(re.escape, names), key=len, reverse=True))
    prefix = (r"""(?:[A-Za-z_][A-Za-z0-9_]*=(?:"(?:[^"\\\n]|\\.)*"|'[^'\n]*'|[^\s;&|()"']*)"""
              r"[ \t]+)*")
    return re.compile(
        r"(?:^|[;&|({}!)]|\bthen\b|\bdo\b|\belse\b)[ \t]*" + prefix
        + r"(?P<name>" + alt + r")(?=[ \t]|\\\n)",
        re.M)


def _skip_dquote(s: str, i: int) -> int:
    """Index just past the double quote that closes the string opened before i."""
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif s.startswith("$(", i):
            i = _skip_parens(s, i + 2)
        elif c == "`":
            i = s.index("`", i + 1) + 1
        else:
            i += 1
    raise ValueError("unterminated double quote")


def _skip_parens(s: str, i: int) -> int:
    """Index just past the `)` that closes the `$(` opened before i."""
    depth = 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            i = s.index("'", i + 1) + 1
            continue
        if c == '"':
            i = _skip_dquote(s, i + 1)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unterminated $(")


def _skip_braces(s: str, i: int) -> int:
    """Index just past the `}` that closes the `${` opened before i."""
    depth = 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            i = _skip_dquote(s, i + 1)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unterminated ${")


def _words(s: str, i: int, n: int) -> list[tuple[str, str | None]]:
    """The first *n* shell words of the command that starts at s[i], each as
    (as written, literal value), the literal None when the word expands
    anything. Quotes, `$( )`, `${ }` and line continuations are read the way
    bash reads them; the command ends at a newline, an operator or a comment."""
    out: list[tuple[str, str | None]] = []
    while len(out) < n:
        while i < len(s) and (s[i] in " \t" or s.startswith("\\\n", i)):
            i += 2 if s[i] == "\\" else 1
        if i >= len(s) or s[i] in "\n;&|)#":
            break
        start, parts, expands = i, [], False
        while i < len(s) and s[i] not in " \t\n;&|)" and not s.startswith("\\\n", i):
            c = s[i]
            if c == "\\":
                parts.append(s[i + 1])
                i += 2
            elif c == "'":
                j = s.index("'", i + 1)
                parts.append(s[i + 1:j])
                i = j + 1
            elif c == '"':
                j = _skip_dquote(s, i + 1)
                inner = s[i + 1:j - 1]
                expands = expands or "$" in inner or "`" in inner
                parts.append(inner)
                i = j
            elif s.startswith("$(", i):
                i, expands = _skip_parens(s, i + 2), True
            elif s.startswith("${", i):
                i, expands = _skip_braces(s, i + 2), True
            elif c == "$":
                i, expands = i + 1, True
            elif c == "`":
                i, expands = s.index("`", i + 1) + 1, True
            else:
                parts.append(c)
                i += 1
        out.append((s[start:i], None if expands else "".join(parts)))
    return out


def _calls(where: str, text: str, first_line: int, writers: dict) -> list[tuple]:
    out = []
    for m in _call(writers).finditer(text):
        pos = writers[m["name"]]
        try:
            words = _words(text, m.end(), pos)
        except ValueError:
            words = []
        line = first_line + text.count("\n", 0, m.start("name")) + 1
        out.append((where, line, m["name"], words[pos - 1] if len(words) >= pos else None))
    return out


@functools.cache
def _shell_sites() -> tuple[tuple, ...]:
    """(file, line, writer, (argument, literal) or None) for every writer call
    in a runtime shell script, and in a fenced block of the library's markdown
    (a command a composed skill or protocol has an agent run)."""
    sites = []
    for p in _runtime_shell_scripts():
        where = _rel(p)
        sites += _calls(where, _code(p.read_text()), 0,
                        {**SHELL_WRITERS, **LOCAL_WRAPPERS.get(where, {})})
    for p in sorted((REPO / "library").rglob("*.md")):
        text = p.read_text()
        for m in FENCE.finditer(text):
            sites += _calls(_rel(p), _code(m.group(1)), text.count("\n", 0, m.start(1)),
                            SHELL_WRITERS)
    return tuple(sites)


def test_the_shell_scan_finds_the_writers():
    """Positive control: a pattern that stopped matching would pass every
    check below on nothing."""
    sites = _shell_sites()
    assert len(sites) >= 80, len(sites)
    found = {word[1] for *_, word in sites if word and word[1]}
    for t in ("bridge_down", "keepalive_restart", "disk_high", "orphan_browser_reaped",
              "source_behind", "vault_guard_denied", "auth_mint_failed", "audit_completed"):
        assert t in found, t
    assert {name for _w, _l, name, _word in sites} == set(SHELL_WRITERS) | {"_event"}


def test_the_call_pattern_reads_each_call_shape():
    """Positive control for _call itself, one line per shape it claims: a
    shape it stopped reading would pass every check here on nothing."""
    text = "\n".join((
        "emit_fleet_event plain_call src '{}'",
        "true && emit_fleet_event after_operator src '{}'",
        'FOO=1 BAR="a b" emit_fleet_event env_prefixed src \'{}\' "" >/dev/null 2>&1 || true',
        "if x; then emit_failure_alert \"$d\" after_keyword \"why\"; fi",
        "command -v emit_fleet_event >/dev/null  # a mention, not a call",
        "emit_fleet_event() { :; }  # a definition, not a call",
    ))
    found = [(name, word[1]) for _w, _l, name, word in _calls("x.sh", text, 0, SHELL_WRITERS)]
    assert found == [("emit_fleet_event", "plain_call"), ("emit_fleet_event", "after_operator"),
                     ("emit_fleet_event", "env_prefixed"),
                     ("emit_failure_alert", "after_keyword")], found


def test_every_literal_type_a_shell_writer_records_is_registered():
    missing = [f"{where}:{line} {name} {word[1]}" for where, line, name, word in _shell_sites()
               if word and word[1] is not None and word[1] not in SYSTEM_EVENT_SEVERITY]
    assert missing == [], ("register each in SYSTEM_EVENT_SEVERITY", missing)


def test_every_variable_type_is_listed():
    """A type the scan cannot read is a type nothing checks, unless the table
    names every value it can take, or the writer that forwards its caller's."""
    unlisted, used = [], set()
    for where, line, name, word in _shell_sites():
        if word is None:
            unlisted.append(f"{where}:{line} {name}: no type argument could be read")
        elif word[1] is None:
            key = (where, name, word[0])
            if key in VARIABLE_TYPES or key in FORWARDED_TYPES:
                used.add(key)
            else:
                unlisted.append(f"{where}:{line} {name} {word[0]}")
    assert unlisted == [], ("list each in VARIABLE_TYPES with every value it takes", unlisted)
    assert used == set(VARIABLE_TYPES) | set(FORWARDED_TYPES), "a listed call site is gone"


def test_the_listed_values_are_registered_and_the_forwarders_are_scanned():
    for (where, _name, _arg), (pattern, values) in VARIABLE_TYPES.items():
        unknown = values - SYSTEM_EVENT_SEVERITY.keys()
        assert not unknown, (where, unknown)
        # The values the script gives the variable, read from the script. None
        # read is a pattern that stopped matching, so it fails rather than
        # passing on an empty set.
        taken = set(re.findall(pattern, _code((REPO / where).read_text())))
        assert taken, (where, "the pattern reads no value from the script", pattern)
        assert taken == values, (where, "values the script takes vs the list", taken ^ values)
    for (where, _name, _arg), forwarders in FORWARDED_TYPES.items():
        for f in forwarders:
            assert f in SHELL_WRITERS or f in LOCAL_WRAPPERS.get(where, {}), (where, f)


def test_the_variable_check_reads_an_assignment():
    """Positive control for the assignment cross-check above."""
    text = _code((LIB / "host-health-check.sh").read_text())
    assert set(re.findall(r"\bKEY=\"?([a-z][a-z0-9_]*)\"?", text)) == {
        "host_health", "undervoltage", "storage_stall"}


# --- b. hand-built rows and the Python writers --------------------------------

# A system row built by hand rather than through a writer names its type beside
# its subject, `"event": <type>, "subject_kind": ...`, in a script's JSON (jq's
# unquoted keys too) and in Python alike.
ROW = re.compile(r"""(?:["']event["']|\bevent)\s*:\s*(?P<value>[^,{}\n]+?)\s*,\s*"""
                 r"""(?:["']subject_kind["']|\bsubject_kind\b)""")
CONDITIONAL = re.compile(
    r"""["']([a-z][a-z0-9_]*)["']\s+if\s.+\selse\s+["']([a-z][a-z0-9_]*)["']""")
FAMILY = re.compile(r"""["']event_type["']\s*:\s*["'](\w+)["']""")
# A row of that shape in another family, and the family its event declares.
OTHER_FAMILY_ROWS = {
    ("claudlobby/plane/registry_emit.py", "revision_seen"): "declaration",
    ("claudlobby/plane/registry_emit.py", "scan_completed"): "declaration",
}
# A row whose type is a variable: the writer whose callers supply it (scanned
# in gate a, or as a Python writer below), or None for a site that writes no
# type of its own.
VARIABLE_ROWS = {
    (RS + "lib-common.sh", '"%s"'): "emit_fleet_event",
    ("claudlobby/plane/daemon.py", "event"): "_emit_system",
    ("claudlobby/plane/ingest.py", "payload.event"): None,  # stores a declaration as sent
}
# A Python helper that takes a type as its first argument.
PY_WRITERS = {
    "claudlobby/plane/daemon.py": "_emit_system",
    RS + "heavy-slot.py": "_emit",
}


def _family(text: str, pos: int) -> str | None:
    """The event family a row declares: the nearest `event_type` key at most
    300 characters before it, which is the row's own in every row on the tree."""
    before = list(FAMILY.finditer(text, max(0, pos - 300), pos))
    return before[-1][1] if before else None


@functools.cache
def _row_sites() -> tuple[tuple, ...]:
    """(file, line, value as written, its literal types or None, family)."""
    out = []
    for p in _runtime_shell_scripts() + _python_modules():
        text = p.read_text()
        for m in ROW.finditer(text):
            value = m["value"].strip()
            cond = CONDITIONAL.fullmatch(value)
            lit = LITERAL.fullmatch(value)
            types = {cond[1], cond[2]} if cond else ({lit[1]} if lit else None)
            out.append((_rel(p), text.count("\n", 0, m.start()) + 1, value, types,
                        _family(text, m.start())))
    return tuple(out)


@functools.cache
def _py_writer_sites() -> tuple[tuple, ...]:
    """(file, line, helper, its literal type or None, the argument as written)."""
    out = []
    for where, helper in PY_WRITERS.items():
        text = (REPO / where).read_text()
        for m in re.finditer(r"(?<!def )\b" + re.escape(helper) + r"\(\s*(?P<arg>[^,)]*)", text):
            arg = m["arg"].strip()
            lit = LITERAL.fullmatch(arg)
            out.append((where, text.count("\n", 0, m.start()) + 1, helper,
                        lit[1] if lit else None, arg))
    return tuple(out)


def test_the_row_and_python_scans_find_the_writers():
    """Positive control, both scans."""
    rows = {t for *_, types, _f in _row_sites() if types for t in types}
    for t in ("vault_sync", "session_digest", "operator_first_seen", "task_recheck",
              "task_recheck_noop", "workstream_prune_noop", "reports_acked", "report_status",
              "checkin_decision", "checkin_dispatch", "fleet_alert", "fleet_notice"):
        assert t in rows, t
    helpers = {lit for *_, lit, _arg in _py_writer_sites()}
    for t in ("daemon_started", "daemon_stopping", "spool_drain_completed",
              "heavy_slot_acquired", "heavy_slot_refused"):
        assert t in helpers, t


def test_every_hand_built_system_row_names_a_registered_type():
    missing, unlisted, used = [], [], set()
    for where, line, value, types, family in _row_sites():
        if types is None:
            key = (where, value)
            if key in VARIABLE_ROWS:
                used.add(key)
            else:
                unlisted.append(f"{where}:{line} {value}")
            continue
        for t in types:
            if family not in (None, "system"):
                if OTHER_FAMILY_ROWS.get((where, t)) != family:
                    unlisted.append(f"{where}:{line} {t} ({family})")
            elif t not in SYSTEM_EVENT_SEVERITY:
                missing.append(f"{where}:{line} {t}")
    assert missing == [], ("register each in SYSTEM_EVENT_SEVERITY", missing)
    assert unlisted == [], ("list each in VARIABLE_ROWS or OTHER_FAMILY_ROWS", unlisted)
    assert used == set(VARIABLE_ROWS), "a listed row is gone"
    for (where, _value), writer in VARIABLE_ROWS.items():
        assert writer is None or writer in SHELL_WRITERS or writer == PY_WRITERS.get(where), where


def test_the_other_family_rows_still_declare_their_family():
    seen = {(where, t): family for where, _l, _v, types, family in _row_sites()
            if types for t in types}
    for key, family in OTHER_FAMILY_ROWS.items():
        assert seen.get(key) == family, (key, seen.get(key))


def test_every_python_writer_records_a_registered_literal():
    bad = [f"{where}:{line} {helper}({arg})" for where, line, helper, lit, arg in _py_writer_sites()
           if lit is None or lit not in SYSTEM_EVENT_SEVERITY]
    assert bad == [], ("pass a registered literal type", bad)


# A file that names the system family, or (Python) a subject, may be building a
# system row in a shape the scans above do not read. A script's subject alone
# is no sign: its metric samples carry one too.
SUBJECT_KEY = re.compile(r"""["']subject_kind["']\s*:""")
SYSTEM_FAMILY = re.compile(r"""["']?event_type["']?\s*:\s*["']system["']"""
                           r"""|,\s*["']system["']\s*,\s*\{|\bfamily\s*=\s*["']system["']""")


def _names_a_system_row(path: Path) -> bool:
    text = path.read_text()
    return bool(SYSTEM_FAMILY.search(text) or (path.suffix == ".py" and SUBJECT_KEY.search(text)))


def test_no_file_builds_a_system_row_the_scans_cannot_see():
    """The tripwire for a new writer shape: every runtime script or Python
    module that names the system family (or, in Python, a subject) is one
    where a scan found its writer. A file that trips it needs its writer's
    shape added to a scan, not an exemption.

    Its bound: it works per file. A second row in a file where a scan already
    found one, in a shape no scan reads, is not caught here."""
    marked = {_rel(p) for p in _runtime_shell_scripts() + _python_modules()
              if _names_a_system_row(p)}
    for m in ("claudlobby/brief.py", "claudlobby/task_recheck.py",
              "claudlobby/workstream_operations.py", "claudlobby/report_payload.py",
              RS + "vault-sync.sh", RS + "transcript-digest.sh"):
        assert m in marked, m                                    # the markers still match
    seen = {where for where, *_ in _row_sites()} | set(PY_WRITERS)
    assert marked - seen == set()


# --- c. fleet-pulse's critical lists --------------------------------------------


def _pulse_list(var: str) -> frozenset[str]:
    text = (LIB / "fleet-pulse.sh").read_text()
    assert len(re.findall(rf"^\s*{var}\+?=", text, re.M)) == 1, f"{var}: assigned more than once"
    m = re.search(rf'^{var}="([^"]*)"$', text, re.M)
    assert m, f"{var} not found in fleet-pulse.sh"
    types = m[1].split()
    assert types and len(types) == len(set(types)), (var, types)
    return frozenset(types)


def test_fleet_pulse_lists_equal_the_registry_sets():
    assert _pulse_list("_CRITICAL_ESCALATION_TYPES") == PULSE_ESCALATION_TYPES
    assert _pulse_list("_CRITICAL_SUMMARY_TYPES") == PULSE_SUMMARY_TYPES


def test_the_pulse_sets_are_deliberate_subsets_of_the_critical_types():
    assert PULSE_ESCALATION_TYPES < CRITICAL
    assert PULSE_SUMMARY_TYPES < CRITICAL
    assert "input_held" in PULSE_SUMMARY_TYPES - PULSE_ESCALATION_TYPES


# --- d. the documents -------------------------------------------------------------

DOCS = (
    "library/protocols/fleet-observability.md",
    "library/skills/fleet-pulse/SKILL.md",
    "documentation/guides/observability.md",
)
TYPE_HEADERS = {"type", "event type", "critical type"}
TOKEN = re.compile(r"`([a-z][a-z0-9_]*)`")


@functools.cache
def _event_tables(path: str) -> tuple[tuple, ...]:
    """(heading, header, ((line, types), ...)) for each table whose first column
    names event types: the backticked names in each row's first cell."""
    lines = (REPO / path).read_text().split("\n")
    tables, heading, fenced, i = [], "", False, 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            heading = line.lstrip("#").strip()
        elif (not fenced and line.startswith("|") and i + 1 < len(lines)
              and re.fullmatch(r"\|[\s:|-]+\|", lines[i + 1].strip())):
            header = line.strip().strip("|").split("|")[0].strip()
            rows, j = [], i + 2
            while j < len(lines) and lines[j].startswith("|"):
                first_cell = lines[j].strip().strip("|").split("|")[0]
                rows.append((j + 1, tuple(TOKEN.findall(first_cell))))
                j += 1
            if header.lower() in TYPE_HEADERS:
                tables.append((heading, header, tuple(rows)))
            i = j
            continue
        i += 1
    return tuple(tables)


def _label(heading: str, header: str) -> str | None:
    text = f"{heading} {header}".lower()
    if "critical" in text:
        return "critical"
    if "informational" in text:
        return "notice"
    return None


def test_the_doc_tables_are_found():
    """Positive control: a parser that found no table would pass on nothing."""
    tables = {p: _event_tables(p) for p in DOCS}
    assert all(tables.values()), {p: len(t) for p, t in tables.items()}
    named = {t for ts in tables.values() for *_h, rows in ts for _l, types in rows for t in types}
    for t in ("session_missing", "input_held", "audit_completed", "wip_uncommitted",
              "tool_call", "disk_high"):
        assert t in named, t
    labels = {(p, _label(h, hd)) for p, ts in tables.items() for h, hd, _r in ts}
    assert {(DOCS[0], "critical"), (DOCS[2], "critical"), (DOCS[2], "notice")} <= labels


def test_every_type_a_doc_table_names_is_registered():
    missing = [f"{p}:{line} {t}" for p in DOCS for *_h, rows in _event_tables(p)
               for line, types in rows for t in types if t not in SYSTEM_EVENT_SEVERITY]
    assert missing == [], ("name only registered types", missing)


def test_a_table_labelled_critical_lists_exactly_the_critical_types():
    for p in DOCS:
        for heading, header, rows in _event_tables(p):
            types = {t for _l, ts in rows for t in ts}
            label = _label(heading, header)
            if label == "critical":
                assert types == CRITICAL, (p, heading, "missing:", sorted(CRITICAL - types),
                                           "not critical:", sorted(types - CRITICAL))
            elif label == "notice":
                wrong = sorted(t for t in types if SYSTEM_EVENT_SEVERITY.get(t) != "notice")
                assert wrong == [], (p, heading, wrong)


def test_every_type_a_doc_queries_is_registered():
    """A `--type NAME` command in the docs must name a registered type."""
    queried = {(p, t) for p in DOCS
               for t in re.findall(r"--type\s+([a-z][a-z0-9_]*)", (REPO / p).read_text())}
    assert len({t for p, t in queried if p == DOCS[2]}) >= 3, queried   # positive control
    assert [q for q in sorted(queried) if q[1] not in SYSTEM_EVENT_SEVERITY] == []


def test_the_protocols_per_type_sweep_names_notice_types():
    """The protocol pairs --critical with a loop over the actionable types it
    misses. Each must be registered, and notice: a critical one is already in
    --critical, and the loop would say otherwise."""
    loops = re.findall(r"^for t in ([a-z_ ]+); do$", (REPO / DOCS[0]).read_text(), re.M)
    assert len(loops) == 1, loops
    types = loops[0].split()
    assert types and all(SYSTEM_EVENT_SEVERITY.get(t) == "notice" for t in types), types
