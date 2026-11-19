"""A report listing's cursor and the viewer's read position (#2837).

Only an `--unacknowledged` read depends on the viewer's read position: it starts
there. Every other listing returns the same reports wherever the position is, so
an acknowledgement by the same viewer between two of its pages leaves its cursor
valid, and the next page is the one unbroken paging returns.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from claudlobby.__main__ import main
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_message_read_cli import _generated, _readers_for_test_process
from tests.test_reads_group_by_workstream import _report
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_read_cli import active  # noqa: F401 — active release fixture

# Six reports, a minute apart. Each filter below keeps five of them.
REPORTS = (
    # number, author, status, workstream
    (1, "worker", "completed", "release-review"),
    (2, "worker", "completed", "release-review"),
    (3, "manager", "completed", "release-review"),
    (4, "worker", "failed", "release-review"),
    (5, "worker", "completed", None),
    (6, "worker", "completed", "release-review"),
)
FILTERS = {
    "since": ("--since", "2026-09-28T12:00:30Z"),
    "bot": ("--bot", "worker"),
    "status": ("--status", "completed"),
    "workstream": ("--workstream", "release-review"),
    "unfiltered": (),
}
LISTED = {
    "since": [2, 3, 4, 5, 6],
    "bot": [1, 2, 4, 5, 6],
    "status": [1, 2, 3, 5, 6],
    "workstream": [1, 2, 3, 4, 6],
    "unfiltered": [1, 2, 3, 4, 5, 6],
}


def _viewer(active, monkeypatch, capsys):  # noqa: F811
    """The fleet's manager as the generated viewer, over REPORTS. Returns the root, a
    `fleet reports list` caller and an acknowledgement of every unread report."""
    root, host = active
    _readers_for_test_process(host.package, monkeypatch)
    _generated(monkeypatch, root, "manager")
    (root / "state/plane/capture.json").write_text(json.dumps({"*": "full"}))
    for number, author, status, workstream in REPORTS:
        _report(root, number, author=author, status=status, workstream=workstream, minute=number - 1)

    def listed(*flags, expected=0):
        code = main(["--root", str(root), "--json", "fleet", "reports", "list", *flags])
        result = json.loads(capsys.readouterr().out)
        assert code == expected, (flags, result)
        assert result["command"] == "fleet.reports.list" and result["ok"] is (expected == 0)
        return result

    def ack():
        cursor = listed("--unacknowledged")["data"]["ack_cursor"]
        assert cursor
        code = main(["--root", str(root), "--json", "fleet", "reports", "ack", "--through", cursor,
                     "--request-id", str(uuid4())])
        result = json.loads(capsys.readouterr().out)
        assert code == 0, result
        assert result["data"]["recording"] == "committed" and result["data"]["count"] >= 1
        # The read position moved past every report.
        assert listed("--unacknowledged")["data"]["items"] == []

    return root, listed, ack


def _pages(listed, flags, first=None):
    """Every page of a listing, two reports a page, from its first page on."""
    pages = [first if first is not None else listed(*flags, "--limit", "2")["data"]]
    for _ in range(10):
        if pages[-1]["next_cursor"] is None:
            return pages
        pages.append(listed(*flags, "--limit", "2", "--cursor", pages[-1]["next_cursor"])["data"])
    raise AssertionError("a listing of six reports ran past ten pages")


@pytest.mark.parametrize("name", list(FILTERS))
def test_a_listing_pages_through_an_acknowledgement_by_the_same_viewer(active, monkeypatch, capsys, name):  # noqa: F811
    """The same viewer acknowledges every report between the first and the second page of a
    listing that is not --unacknowledged: each later page is still served, and is the page,
    cursor and all, that unbroken paging returns."""
    _root, listed, ack = _viewer(active, monkeypatch, capsys)
    flags = FILTERS[name]
    unbroken = _pages(listed, flags)
    assert [item["message_id"] for page in unbroken for item in page["items"]] == [
        f"msg_{number:032x}" for number in LISTED[name]]
    assert len(unbroken) == 3 and all(page["ack_cursor"] is None for page in unbroken)
    first = listed(*flags, "--limit", "2")["data"]
    assert first == unbroken[0]
    ack()
    assert _pages(listed, flags, first=first) == unbroken


def test_after_an_acknowledgement_a_listing_cursor_still_refuses_another_filter_or_viewer(active, monkeypatch, capsys):  # noqa: F811
    """The cursor leaves out only the read position. After an acknowledgement it still refuses
    a page under another filter, another value of its own filter, the --unacknowledged mode or
    another viewer, and still continues under its own."""
    root, listed, ack = _viewer(active, monkeypatch, capsys)
    since = FILTERS["since"]
    cursor = listed(*since, "--limit", "2")["data"]["next_cursor"]
    assert cursor
    ack()
    for flags in (("--since", "2026-09-28T12:01:30Z"), (), FILTERS["bot"], FILTERS["status"],
                  FILTERS["workstream"], (*since, *FILTERS["bot"]), (*since, *FILTERS["status"]),
                  (*since, *FILTERS["workstream"]), ("--unacknowledged",), ("--unacknowledged", *since)):
        refused = listed(*flags, "--limit", "2", "--cursor", cursor, expected=2)["error"]
        assert refused["code"] == "invalid_argument", flags
    _generated(monkeypatch, root, "worker")
    assert listed(*since, "--limit", "2", "--cursor", cursor, expected=2)["error"]["code"] == "invalid_argument"
    _generated(monkeypatch, root, "manager")
    assert [item["message_id"] for item in listed(*since, "--limit", "2", "--cursor", cursor)["data"]["items"]] == [
        f"msg_{number:032x}" for number in LISTED["since"][2:4]]


def test_an_unacknowledged_read_keeps_its_read_position_in_its_cursor(active, monkeypatch, capsys):  # noqa: F811
    """Unchanged by #2837: an --unacknowledged read starts at the read position, so its cursor
    keeps it, and once an acknowledgement moves it the next page is refused, `conflict` for the
    unfiltered read and `invalid_argument` for a filtered one, as before."""
    _root, listed, ack = _viewer(active, monkeypatch, capsys)
    plain = ("--unacknowledged",)
    filtered = ("--unacknowledged", *FILTERS["bot"])
    cursors = {flags: listed(*flags, "--limit", "2")["data"]["next_cursor"] for flags in (plain, filtered)}
    assert all(cursors.values())
    for flags, cursor in cursors.items():
        assert listed(*flags, "--limit", "2", "--cursor", cursor)["data"]["items"], flags
    ack()
    moved = listed(*plain, "--limit", "2", "--cursor", cursors[plain], expected=4)["error"]
    assert (moved["code"], moved["message"]) == ("conflict", "report read position advanced after this page")
    stale = listed(*filtered, "--limit", "2", "--cursor", cursors[filtered], expected=2)["error"]
    assert stale["code"] == "invalid_argument"
