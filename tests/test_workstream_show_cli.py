"""`workstream show` reads a task's own workstream through the command (#2865).

`task admit` opens a workstream for every task, with the ID `ws_` and the task ID's 32 hex digits. `workstream list`,
the brief and the lookup door show it, so `workstream show` takes that ID as well as the `ws-<slug>` ID that
`workstream open` mints. The verbs that change a workstream do not: `workstream open` refuses the form for `--id`
because `task admit` opens these, and a task's own workstream ends with its task. They run on an installed release,
as `task move`'s command test does.
"""
from uuid import uuid4

from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_move_cli import readers  # noqa: F401 — the test tree's readers
from tests.test_task_write_cli import _call, active  # noqa: F401 — the installed-release CLI fixture
from tests.test_workstream_backfill_cli import _rows, _text

SLUG_ONLY = "workstream ID must use ws- followed by a slug"
EITHER_FORM = SLUG_ONLY + ", or ws_ and a task's 32 hex digits"


def _admit(capsys, root, title):
    """A task, and the ID of the workstream `task admit` opened for it."""
    admitted = _call(capsys, root, "task", "admit", "--title", title, "--request-id", str(uuid4()))["data"]
    assert admitted["workstream_id"] == "ws_" + admitted["task_id"][3:]
    return admitted["task_id"], admitted["workstream_id"]


def _listed(capsys, root, workstream_id):
    return _call(capsys, root, "workstream", "list")["data"]["workstreams"][workstream_id]


def test_show_reads_a_tasks_own_workstream_and_an_opened_one_as_list_does(readers, capsys):
    root, _ = readers
    task_id, own = _admit(capsys, root, "Read by its own workstream's ID")
    shown = _call(capsys, root, "workstream", "show", own)
    assert shown["command"] == "workstream.show"
    entry = shown["data"]["workstream"]
    assert entry == _listed(capsys, root, own)
    assert (entry["id"], entry["title"], entry["status"]) == (own, "Read by its own workstream's ID", "active")
    assert (entry["task_ids"], entry["open_task_ids"]) == ([task_id], [task_id])
    assert _text(capsys, root, "workstream", "show", own)[0] == f"{own} — Read by its own workstream's ID"
    # The ID that `workstream open` mints is read as before.
    opened = _call(capsys, root, "workstream", "open", "Opened, not a task's own",
                   "--request-id", str(uuid4()))["data"]["workstream_ids"][0]
    assert _call(capsys, root, "workstream", "show", opened)["data"]["workstream"] == _listed(capsys, root, opened)


def test_show_of_an_own_workstream_id_no_task_has_is_not_found(readers, capsys):
    root, _ = readers
    refused = _call(capsys, root, "workstream", "show", "ws_" + uuid4().hex, expected=3)
    assert refused["error"]["code"] == "not_found"
    assert refused["error"]["message"] == "workstream is not live in selected fleet"


def test_show_refuses_an_id_that_is_neither_form_and_names_both(readers, capsys):
    root, _ = readers
    _, own = _admit(capsys, root, "Its ID is one edit from a refusal")
    digits = own[3:]
    for bad in ("", "x", "ws-", "ws_slug", "WS-" + digits, "wi_" + digits, "ws_" + digits[:-1],
                "ws_" + digits + "0", "ws_" + digits + "-x", "ws_" + "A" * 32, "ws_" + "g" * 32):
        refused = _call(capsys, root, "workstream", "show", bad, expected=2)
        assert (refused["error"]["code"], refused["error"]["message"]) == ("invalid_argument", EITHER_FORM), bad


def test_the_verbs_that_change_a_workstream_still_refuse_a_tasks_own_workstream_id(readers, capsys):
    root, _ = readers
    _, own = _admit(capsys, root, "Its workstream is not for the manager's verbs")
    listed, rows = _listed(capsys, root, own), _rows(root)
    note = ("--note", "Not for a task's own workstream")
    attempts = (("open", "A title", "--id", own), ("progress", own), ("renew", own, *note),
                ("block", own, *note, "--on", "human:reviewer"), ("unblock", own, *note),
                ("close", own), ("close", own, "--dry-run"))
    for argv in attempts:
        refused = _call(capsys, root, "workstream", *argv, "--request-id", str(uuid4()), expected=2)
        assert (refused["error"]["code"], refused["error"]["message"]) == ("invalid_argument", SLUG_ONLY), argv
    assert (_listed(capsys, root, own), _rows(root)) == (listed, rows)
