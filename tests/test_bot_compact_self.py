"""#1053 PR 2: a bot's `bot compact` on itself records a request and types nothing.

The CLI runs over test_bot_operations' fake native adapter, which records every
native call, so "types nothing" is an empty call list. The plane is a stand-in
that holds each row's key, as the plane does, and answers a repeated key with
`duplicate`.
"""

from types import SimpleNamespace
import json

import pytest

from claudlobby.plane import emit_api
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401
from tests.test_bot_operations import _worker_cli

RID1 = "11111111-1111-4111-8111-111111111111"
RID2 = "22222222-2222-4222-8222-222222222222"
RID3 = "33333333-3333-4333-8333-333333333333"


def _plane(monkeypatch, *, refuse=False):
    """Rows the door hands the plane; a key the plane already holds reads duplicate."""
    rows, keys = [], set()

    def emit(_root, events, require_commit=False):
        assert require_commit is True
        if refuse:
            raise OSError("the plane is unavailable")
        out = []
        for event in events:
            ref = event["source_ref"]
            out.append(SimpleNamespace(status="duplicate" if ref in keys else "committed"))
            if ref not in keys:
                keys.add(ref)
                rows.append(event)
        return out
    monkeypatch.setattr(emit_api, "emit_batch", emit)
    return rows


def _as(monkeypatch, worker, bot):
    """Run the CLI as the generated session of `bot` in the example fleet."""
    root = worker.root
    for key, value in (("FLEET_NAME", "example"), ("BOT_ID", bot),
                       ("BOT_DIR", str(root / f"runtime/bots/{bot}")), ("FLEET_ROOT", str(root)),
                       ("CLAUDLOBBY_ROOT", str(root)),
                       ("CLAUDLOBBY_INSTALLATION", str(worker.installation.directory))):
        monkeypatch.setenv(key, value)


def _record(worker, bot="worker"):
    path = worker.root / f"runtime/bots/{bot}/data/.compact-request"
    return json.loads(path.read_text()) if path.exists() else None


@pytest.fixture
def worker(cold, monkeypatch, capsys):  # noqa: F811
    cli = _worker_cli(cold, monkeypatch, capsys)
    cli.rows = _plane(monkeypatch)
    _as(monkeypatch, cli, "worker")
    cli.native.calls.clear()
    return cli


def test_a_bots_compact_on_itself_records_one_request_and_types_nothing(worker):
    data = worker.call("bot", "compact", "worker", "--request-id", RID1, use_root=False)["data"]
    assert (data["outcome"], data["control"], data["native_outcome"]) == ("requested", "compact", "unattempted")
    assert data["recording"] == "committed"
    saved = _record(worker)
    assert saved == data["request"]
    assert (saved["by"], saved["request_id"]) == ("bot:example/worker", RID1)
    assert saved["requested_at"].endswith("Z") and isinstance(saved["requested_epoch"], int)
    assert set(saved["context"]) == {"tokens", "at", "reason"}
    (row,) = worker.rows
    assert row["payload"]["event"] == "compact_requested"
    assert row["payload"]["subject"] == "bot:example/worker"
    assert row["payload"]["data"]["data"] == saved
    assert worker.native.calls == []  # no session read, no keys

    # The same call again, and another request ID while one is pending: both get the
    # pending request back, and neither records anything.
    for rid in (RID1, RID2):
        again = worker.call("bot", "compact", "worker", "--request-id", rid, use_root=False)["data"]
        assert (again["outcome"], again["request"], again["recording"]) == ("pending", saved, None)
    assert _record(worker) == saved and len(worker.rows) == 1
    assert worker.native.calls == []


def test_cancel_withdraws_the_pending_request_with_its_own_row(worker):
    worker.call("bot", "compact", "worker", "--request-id", RID1, use_root=False)
    saved = _record(worker)
    data = worker.call("bot", "compact", "worker", "--cancel", "--request-id", RID3,
                       use_root=False)["data"]
    assert (data["outcome"], data["request"], data["recording"]) == ("cancelled", saved, "committed")
    assert _record(worker) is None
    assert not list((worker.root / "runtime/bots/worker/data").glob(".compact-request*"))
    row = worker.rows[-1]
    assert row["payload"]["event"] == "compact_cancelled"
    assert row["payload"]["data"]["data"]["cancelled_request_id"] == RID1
    assert row["payload"]["data"]["data"]["request_id"] == RID3

    nothing = worker.call("bot", "compact", "worker", "--cancel", "--request-id", RID3,
                          use_root=False)["data"]
    assert (nothing["outcome"], nothing["request"], nothing["recording"]) == ("none_pending", None, None)
    assert len(worker.rows) == 2 and worker.native.calls == []


def test_a_request_id_the_plane_holds_records_nothing_once_its_request_is_gone(worker):
    worker.call("bot", "compact", "worker", "--request-id", RID1, use_root=False)
    worker.call("bot", "compact", "worker", "--cancel", "--request-id", RID3, use_root=False)
    replayed = worker.call("bot", "compact", "worker", "--request-id", RID1, use_root=False)["data"]
    assert (replayed["outcome"], replayed["request"]) == ("replayed", None)
    assert _record(worker) is None and len(worker.rows) == 2
    # A new request ID asks again.
    fresh = worker.call("bot", "compact", "worker", "--request-id", RID2, use_root=False)["data"]
    assert fresh["outcome"] == "requested" and _record(worker)["request_id"] == RID2


def test_a_plane_that_refuses_the_row_leaves_the_request_standing(worker, monkeypatch):
    _plane(monkeypatch, refuse=True)
    data = worker.call("bot", "compact", "worker", "--request-id", RID1, use_root=False)["data"]
    assert (data["outcome"], data["recording"]) == ("requested", "degraded")
    assert _record(worker)["request_id"] == RID1 and worker.native.calls == []


def test_a_self_call_without_a_request_id_is_refused_and_records_nothing(worker):
    refused = worker.call("bot", "compact", "worker", expected=4, use_root=False)
    assert refused["error"]["code"] == "conflict"
    assert "--request-id" in refused["error"]["message"]
    bad = worker.call("bot", "compact", "worker", "--request-id", "not-a-uuid", expected=2,
                      use_root=False)
    assert bad["error"]["code"] == "invalid_argument"
    assert _record(worker) is None and worker.rows == [] and worker.native.calls == []


def test_a_worker_naming_another_bot_is_still_refused(worker):
    for extra in ((), ("--request-id", RID1)):
        refused = worker.call("bot", "compact", "manager", *extra, expected=4, use_root=False)
        assert refused["error"]["code"] == "conflict"
    assert "svc_bot_control_exact" not in worker.native.calls
    assert _record(worker) is None and _record(worker, "manager") is None and worker.rows == []


def test_a_managers_call_on_its_worker_still_types_and_on_itself_records(worker, monkeypatch):
    _as(monkeypatch, worker, "manager")
    submitted = worker.call("bot", "compact", "worker", use_root=False)["data"]
    assert (submitted["outcome"], submitted["native_outcome"]) == ("submitted", "submitted")
    assert worker.native.calls.count("svc_bot_control_exact") == 1
    assert _record(worker) is None and worker.rows == []
    refused = worker.call("bot", "compact", "worker", "--cancel", "--request-id", RID1, expected=4,
                          use_root=False)
    assert "own compaction" in refused["error"]["message"]
    assert worker.native.calls.count("svc_bot_control_exact") == 1

    own = worker.call("bot", "compact", "manager", "--request-id", RID2, use_root=False)["data"]
    assert own["outcome"] == "requested" and _record(worker, "manager")["by"] == "bot:example/manager"
    assert worker.native.calls.count("svc_bot_control_exact") == 1
