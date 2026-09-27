"""No plane reader holds one snapshot across a loop's per-row work (#1905).

A read holds a snapshot of the plane for as long as its statement is open, and
while it does the daemon's checkpoint cannot reset the WAL past it. Iterating a
live cursor keeps the statement open for the whole loop. So a loop over
``<conn>.execute(...)`` that runs another query, yields, or writes output per
row holds ONE snapshot for as long as the loop runs: a length set by the data,
or, for a yield or a write to a pipe, by whoever is on the other end. #1905
fetched the rows first in the readers that had the shape; this keeps it out.

Per-row PARSING is not flagged: whether it matters depends on the row count,
which no static check can see (the heartbeat series, tens of thousands of rows,
was fixed by measurement). This catches the shapes whose hold has no bound.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
#: calls that, inside the loop, extend the statement's snapshot to another
#: statement or hand it to an unbounded wait
_EXTENDS = {
    "execute",
    "executemany",
    "executescript",
    "fetchone",
    "fetchall",
    "fetchmany",
    "print",
    "write",
}


def _is_execute(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    )


def _live_cursor_loops(tree: ast.AST):
    """Every loop whose iterable is a live cursor: `x.execute(...)` itself, or
    a name the same function assigned from one."""
    for loop in ast.walk(tree):
        if isinstance(loop, (ast.For, ast.AsyncFor)) and _is_execute(loop.iter):
            yield loop
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cursors = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) and _is_execute(n.value)
                   for t in n.targets if isinstance(t, ast.Name)}
        for loop in ast.walk(fn):
            if (isinstance(loop, (ast.For, ast.AsyncFor)) and isinstance(loop.iter, ast.Name)
                    and loop.iter.id in cursors):
                yield loop


def offenders(source: str, name: str) -> list[str]:
    found = set()
    for loop in _live_cursor_loops(ast.parse(source, filename=name)):
        for sub in (n for stmt in loop.body for n in ast.walk(stmt)):
            if isinstance(sub, (ast.Yield, ast.YieldFrom)):
                found.add(f"{name}:{loop.lineno} yields per row")
            elif isinstance(sub, ast.Call):
                call = sub.func.attr if isinstance(sub.func, ast.Attribute) else getattr(sub.func, "id", "")
                if call in _EXTENDS:
                    found.add(f"{name}:{loop.lineno} calls {call}() per row")
    return sorted(found)


def test_the_check_fires_on_each_shape_it_names():
    """Positive control: a check never shown a positive cannot be told apart
    from one that is broken."""
    bad = (
        "def a(conn):\n"
        "    for r in conn.execute('SELECT 1'):\n"
        "        conn.execute('SELECT 2').fetchone()\n"
        "def b(conn):\n"
        "    cur = conn.execute('SELECT 1')\n"
        "    for r in cur:\n"
        "        yield r\n"
        "def c(conn):\n"
        "    for r in conn.execute('SELECT 1'):\n"
        "        print(r)\n"
        "def fine(conn):\n"
        "    for r in conn.execute('SELECT 1').fetchall():\n"
        "        conn.execute('SELECT 2')\n"
    )
    got = offenders(bad, "x.py")
    assert got == [
        "x.py:2 calls execute() per row",
        "x.py:2 calls fetchone() per row",
        "x.py:6 yields per row",
        "x.py:9 calls print() per row",
    ], got


def test_no_plane_reader_holds_a_snapshot_across_per_row_work():
    files = sorted(REPO.glob("claudlobby/**/*.py")) + sorted(REPO.glob("lib/*.py"))
    assert len(files) > 50, "the scan found almost nothing to scan"
    found = [
        o for f in files for o in offenders(f.read_text(), str(f.relative_to(REPO)))
    ]
    assert found == [], "fetch the rows first, then do the work (#1905):\n" + "\n".join(
        found
    )
