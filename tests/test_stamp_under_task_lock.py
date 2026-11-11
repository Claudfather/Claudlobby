"""Each task verb names its task's workstream under the task's lock, held through the write (#2871).

`_prepare` names each task row and each message about one task with the task's workstream
(`_name_workstreams`, #1053), and `_commit` writes the rows it froze. A move, the backfill and a
close take the same task lock (`_locked_task`), and a held lock refuses, so a verb that holds its
task's lock from that read to its write records the workstream the task is in when the write
lands. That holds by how the verbs are built, so this test reads the source: every `_prepare` call
sits in the body of a `with _locked_task(...)` block of its own function, with no function or
lambda in between that could run it later, and the same block calls `_commit` after it. The
recheck is the one exception, and its docstring says so: it takes no task lock, so its asks name
each task's workstream as of its read. A planted control shows the reader tells each case apart.
"""
import ast
from pathlib import Path

import claudlobby

PACKAGE = Path(claudlobby.__file__).parent
#: The modules whose `_prepare` calls take no task lock, by design.
LOCKLESS = {"task_recheck.py"}


def _name(func):
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _calls(node, name):
    """The calls to `name` under `node`, not counting any inside a nested function or lambda."""
    found, pending = [], [node]
    while pending:
        current = pending.pop()
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call) and _name(child.func) == name:
                found.append(child)
            pending.append(child)
    return found


def _verdicts(source):
    """Each `_prepare` call's verdict, keyed by its enclosing named function and its line:
    "held": in the body of a `with _locked_task(...)` block of its own function, and that block
    calls `_commit` after it; "released": in such a block, but the block calls no `_commit` after
    it; "deferred": in a function or lambda nested in such a block, so it can run after it;
    "unlocked": in no such block."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    verdicts = {}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call) and _name(node.func) == "_prepare"):
        node, crossed, block = call, False, None
        while node in parents:
            parent = parents[node]
            if (isinstance(parent, (ast.With, ast.AsyncWith)) and node in parent.body
                    and any(isinstance(item.context_expr, ast.Call)
                            and _name(item.context_expr.func) == "_locked_task" for item in parent.items)):
                block = parent
                break
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                crossed = True  # a function of its own: the block, if any, is around its definition
            node = parent
        owner = call
        while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[owner]
        if block is None:
            verdict = "unlocked"
        elif crossed:
            verdict = "deferred"
        elif any(write.lineno > call.lineno for write in _calls(block, "_commit")):
            verdict = "held"
        else:
            verdict = "released"
        verdicts[(owner.name, call.lineno)] = verdict
    return verdicts


def _sources():
    """task_operations.py and every module that imports `_prepare` from it, by path in the package."""
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source, str(path))
        imports = [alias for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("task_operations")
                   for alias in node.names if alias.name == "_prepare"]
        assert all(alias.asname is None for alias in imports), f"{path.name} renames _prepare on import"
        if imports or path == PACKAGE / "task_operations.py":
            found[path.relative_to(PACKAGE).as_posix()] = source
    return found


CONTROL = '''
def held(store):
    with _locked_task(store, "t") as check, _reader() as conn:
        with _read_snapshot(conn):
            receipt = _prepare(store)
        return _commit(store, receipt, check)

def unlocked(store):
    receipt = _prepare(store)
    with _locked_task(store, "t") as check:
        return _commit(store, receipt, check)

def deferred(store):
    with _locked_task(store, "t") as check:
        later = lambda: _prepare(store)
        _commit(store, None, check)
    return later()

def deferred_by_a_def(store):
    with _locked_task(store, "t") as check:
        def later():
            return _prepare(store)
        _commit(store, None, check)
    return later()

def released(store):
    with _locked_task(store, "t") as check:
        receipt = _prepare(store)
    with _locked_task(store, "t") as check:
        return _commit(store, receipt, check)
'''


def test_the_reader_tells_a_held_call_from_each_call_that_is_not():
    assert {owner: verdict for (owner, _line), verdict in _verdicts(CONTROL).items()} == {
        "held": "held", "unlocked": "unlocked", "deferred": "deferred", "later": "deferred",
        "released": "released"}


def test_every_prepare_call_holds_its_task_lock_through_the_write():
    sources = _sources()
    assert {"task_operations.py", "task_permissions.py"} | LOCKLESS <= set(sources)
    held = {}
    for name, source in sources.items():
        verdicts = _verdicts(source)
        if name in LOCKLESS:
            assert set(verdicts.values()) == {"unlocked"}, f"{name} now takes a task lock: drop it from LOCKLESS"
            continue
        assert {key: verdict for key, verdict in verdicts.items() if verdict != "held"} == {}, name
        held[name] = {owner for owner, _line in verdicts}
    # Every verb that calls it today is among them, so a reader that finds nothing fails here.
    assert held["task_operations.py"] >= {"admit", "assign", "accept", "_assignment_report", "withdraw", "move",
                                          "escalate", "reschedule", "nudge", "feedback", "reassign"}
    assert held["task_permissions.py"] >= {"request_permission", "decide_permission"}
