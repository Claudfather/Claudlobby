"""A report listing's cursor and the viewer's read position (#2837).

Only an `--unacknowledged` read depends on the viewer's read position: it starts
there. Every other listing returns the same reports wherever the position is, so
an acknowledgement by the same viewer between two of its pages leaves its cursor
valid, and the next page is the one unbroken paging returns.

An `--unacknowledged` read does depend on it, so once an acknowledgement moves it the
next page of that read is refused as `conflict`, filtered or not (#3006), and the caller
starts again from the first page. Any other difference between a cursor and the read it
is presented to stays `invalid_argument`.
"""

from __future__ import annotations

import base64
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
MOVED = "report read position advanced after this page"
FILTERED = [name for name in FILTERS if name != "unfiltered"]


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


def _recoded(cursor, change):
    """A listing cursor with `change` applied to its decoded token. The token is plain base64url
    JSON, so a test can make the differences between a cursor and the read it is presented to
    that no real listing makes by itself."""
    value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    change(value)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def test_an_unacknowledged_read_keeps_its_read_position_in_its_cursor(active, monkeypatch, capsys):  # noqa: F811
    """An --unacknowledged read starts at the read position, so its cursor keeps it, and once an
    acknowledgement moves it the next page is refused as `conflict`, for the unfiltered read and for
    a filtered one alike. A filtered read answered `invalid_argument` here before #3006."""
    _root, listed, ack = _viewer(active, monkeypatch, capsys)
    plain = ("--unacknowledged",)
    filtered = ("--unacknowledged", *FILTERS["bot"])
    cursors = {flags: listed(*flags, "--limit", "2")["data"]["next_cursor"] for flags in (plain, filtered)}
    assert all(cursors.values())
    for flags, cursor in cursors.items():
        assert listed(*flags, "--limit", "2", "--cursor", cursor)["data"]["items"], flags
    ack()
    for flags, cursor in cursors.items():
        moved = listed(*flags, "--limit", "2", "--cursor", cursor, expected=4)["error"]
        assert (moved["code"], moved["message"]) == ("conflict", MOVED), flags


@pytest.mark.parametrize("name", FILTERED)
def test_a_filtered_unacknowledged_read_answers_a_moved_position_with_conflict(active, monkeypatch, capsys, name):  # noqa: F811
    """Each filter of an --unacknowledged read: the viewer acknowledges between two pages, and the
    next page is refused as `conflict`, the unfiltered read's answer and message, so the caller knows
    to start again from the first page. That first page is served, and lists the report that arrived
    after the acknowledgement."""
    root, listed, ack = _viewer(active, monkeypatch, capsys)
    flags = ("--unacknowledged", *FILTERS[name], "--limit", "2")
    first = listed(*flags)["data"]
    assert len(first["items"]) == 2 and first["next_cursor"]
    ack()
    _report(root, 7, workstream="release-review", minute=6)
    moved = listed(*flags, "--cursor", first["next_cursor"], expected=4)["error"]
    assert (moved["code"], moved["message"]) == ("conflict", MOVED)
    again = listed(*flags)["data"]
    assert [item["message_id"] for item in again["items"]] == [f"msg_{7:032x}"]
    assert again["next_cursor"] is None


def test_only_a_moved_position_is_a_conflict_for_a_filtered_unacknowledged_cursor(active, monkeypatch, capsys):  # noqa: F811
    """`conflict` is for the read position alone. A filtered --unacknowledged cursor presented without
    --unacknowledged, as the unfiltered read, under another value of its filter, with another filter
    in its place or added, or as another viewer, is refused as `invalid_argument`: before the
    acknowledgement, and after it, when the read position differs too. A fresh cursor, taken after
    the acknowledgement, still continues."""
    root, listed, ack = _viewer(active, monkeypatch, capsys)
    own = {name: ("--unacknowledged", *FILTERS[name]) for name in FILTERED}
    another_value = {"bot": ("--bot", "manager"), "status": ("--status", "failed"),
                     "since": ("--since", "2026-09-28T12:01:30Z"), "workstream": ("--workstream", "other-stream")}

    def refused(flags, cursor, what):
        error = listed(*flags, "--limit", "2", "--cursor", cursor, expected=2)["error"]
        assert error["code"] == "invalid_argument", what

    def elsewhere(name):
        other = FILTERED[(FILTERED.index(name) + 1) % len(FILTERED)]
        return {"without --unacknowledged": FILTERS[name], "the unfiltered read": ("--unacknowledged",),
                "another value": ("--unacknowledged", *another_value[name]),
                f"{other} in its place": own[other], f"{other} added": (*own[name], *FILTERS[other])}

    stale = {name: listed(*flags, "--limit", "2")["data"]["next_cursor"] for name, flags in own.items()}
    assert all(stale.values())
    for name, cursor in stale.items():
        refused(("--unacknowledged", *another_value[name]), cursor, (name, "another value", "before"))
    ack()
    for name, cursor in stale.items():
        for what, flags in elsewhere(name).items():
            refused(flags, cursor, (name, what, "after the acknowledgement"))
    for number in (7, 8, 9):
        _report(root, number, workstream="release-review", minute=number - 1)
    fresh = {name: listed(*flags, "--limit", "2")["data"]["next_cursor"] for name, flags in own.items()}
    assert all(fresh.values())
    for name, cursor in fresh.items():
        rest = listed(*own[name], "--limit", "2", "--cursor", cursor)["data"]["items"]
        assert [item["message_id"] for item in rest] == [f"msg_{9:032x}"], name
    _generated(monkeypatch, root, "worker")
    for name, cursor in fresh.items():
        refused(own[name], cursor, (name, "as another viewer"))


def test_a_cursor_that_differs_from_its_read_only_in_the_read_position_is_a_moved_position(active, monkeypatch, capsys):  # noqa: F811
    """The answer follows the cursor's scope, not an acknowledgement having been recorded: a scope
    that is the read's own but for `prior_seq`, `prior_ack_event` or both is a moved position."""
    _root, listed, _ack = _viewer(active, monkeypatch, capsys)
    flags = ("--unacknowledged", *FILTERS["bot"], "--limit", "2")
    cursor = listed(*flags)["data"]["next_cursor"]
    assert cursor and listed(*flags, "--cursor", cursor)["data"]["items"]
    for moved in ({"prior_seq": 7}, {"prior_ack_event": 9}, {"prior_seq": 7, "prior_ack_event": 9}):
        edited = _recoded(cursor, lambda value: value["scope"].update(moved))
        error = listed(*flags, "--cursor", edited, expected=4)["error"]
        assert (error["code"], error["message"]) == ("conflict", MOVED), moved


def test_a_cursor_that_is_more_than_a_moved_position_is_refused_as_invalid(active, monkeypatch, capsys):  # noqa: F811
    """Whatever a cursor differs in beside the read position is not a moved position: another key of
    its scope, a position no read writes, or a damaged token is `invalid_argument`, also when its
    position differs too. Starting again from the first page is the wrong answer for these."""
    _root, listed, _ack = _viewer(active, monkeypatch, capsys)
    flags = ("--unacknowledged", *FILTERS["bot"], "--limit", "2")
    cursor = listed(*flags)["data"]["next_cursor"]
    assert cursor
    gone = object()
    moved = {"prior_seq": 7, "prior_ack_event": 9}

    def scope(**keys):
        def change(value):
            for key, new in keys.items():
                if new is gone:
                    del value["scope"][key]
                else:
                    value["scope"][key] = new
        return change

    def damaged(**fields):
        """The position moved, and the token's own fields damaged."""
        def change(value):
            value["scope"].update(moved)
            value.update(fields)
        return change

    edits = {
        # The position moved, and so did something else.
        "position-and-author": scope(**moved, author="manager"),
        "position-and-status": scope(**moved, status="failed"),
        "position-and-since": scope(**moved, since="2026-09-28T12:01:30Z"),
        "position-and-workstream": scope(**moved, workstream="release-review"),
        "position-and-viewer": scope(**moved, viewer_uid="actor_other"),
        "position-and-fleet": scope(**moved, fleet_uid="fleet_other"),
        "position-and-release": scope(**moved, release_id="release_other"),
        "position-and-source-revision": scope(**moved, source_revision="0" * 40),
        "position-and-fingerprint": scope(**moved, config_fingerprint="0" * 64),
        "position-and-flag": scope(**moved, unacknowledged=False),
        "position-and-extra-key": scope(**moved, extra="x"),
        "extra-key-alone": scope(extra="x"),
        # A position no read writes.
        "seq-a-word": scope(prior_seq="7"),
        "seq-a-bool": scope(prior_seq=True),
        "seq-a-float": scope(prior_seq=1.5),
        "seq-a-list": scope(prior_seq=[7]),
        "ack-event-a-word": scope(prior_ack_event="9"),
        "ack-event-a-bool": scope(prior_ack_event=True),
        "ack-event-an-object": scope(prior_ack_event={}),
        "seq-missing": scope(prior_seq=gone),
        "ack-event-missing": scope(prior_ack_event=gone),
        "position-missing": scope(prior_seq=gone, prior_ack_event=gone),
        # The position moved, and the token is damaged.
        "moved-and-after-a-word": damaged(after="x"),
        "moved-and-after-zero": damaged(after=[0, "msg"]),
        "moved-and-after-no-id": damaged(after=[1, ""]),
        "moved-and-after-a-bool": damaged(after=[True, "msg"]),
        "moved-and-after-short": damaged(after=[1]),
        "moved-and-version-2": damaged(v=2),
        "moved-and-version-a-bool": damaged(v=True),
        "moved-and-extra-field": damaged(extra="x"),
        "moved-and-scope-a-word": damaged(scope="x"),
        "moved-and-scope-null": damaged(scope=None),
    }
    for name, change in edits.items():
        error = listed(*flags, "--cursor", _recoded(cursor, change), expected=2)["error"]
        assert error["code"] == "invalid_argument", name
    for text in ("", "not a token", "!!!!", "e30", "W10", "bm90IGpzb24", "A" * 4097):
        assert listed(*flags, "--cursor", text, expected=2)["error"]["code"] == "invalid_argument", text
    # A listing without --unacknowledged has no read position to move: a position added to its cursor is no moved position.
    since = ("--since", "2026-09-28T12:00:30Z", "--limit", "2")
    plain = listed(*since)["data"]["next_cursor"]
    assert plain
    edited = _recoded(plain, lambda value: value["scope"].update(moved))
    assert listed(*since, "--cursor", edited, expected=2)["error"]["code"] == "invalid_argument"
