"""Public request inspection retains read-only scope when Plane is absent."""

import builtins
import json
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import activation, context
from claudlobby.__main__ import main
from claudlobby.plane.db import db_file
from tests.test_activation import cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold


@pytest.fixture
def active(cold, monkeypatch):  # noqa: F811 — pytest fixture parameter
    root, _, plan, host = cold
    for key in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR", "FLEET_ROOT"):
        monkeypatch.delenv(key, raising=False)
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    monkeypatch.setattr(context, "get_resources", lambda: host.package)
    return root, host


def _call(capsys, root, *argv, expected=0):
    assert main(["--root", str(root), "--json", *argv]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["ok"] is (expected == 0)
    assert result["request_id"] is None
    return result


def _counts(root):
    with sqlite3.connect(db_file(root)) as conn:
        return (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])


def test_task_request_shows_recorded_and_independent_proof_even_when_plane_disappears(active, capsys):
    root, host = active
    request_id = str(uuid4())
    admitted = _call(capsys, root, "task", "admit", "--title", "Inspect retained work",
                     "--request-id", request_id)
    task_id = admitted["data"]["task_id"]
    before = _counts(root)
    directory = root / "state/requests"
    before_paths = tuple(sorted(str(path) for path in directory.rglob("*")))
    shown = _call(capsys, root, "request", "show", request_id)
    assert shown["release_id"] == host.release.release_id
    assert shown["data"]["fleet"] == "example"
    view = shown["data"]["request"]
    assert view["request_id"] == request_id and view["operation"] == "task.admit"
    assert view["operation_version"] == 1 and view["task_id"] == task_id
    assert view["stages"][0]["recorded_status"] == "committed"
    assert view["stages"][0]["proof"]["status"] == "committed"
    assert view["transmissions"] == [] and _counts(root) == before
    assert tuple(sorted(str(path) for path in directory.rglob("*"))) == before_paths

    db_file(root).rename(root / "plane-offline")
    offline = _call(capsys, root, "request", "show", request_id)["data"]["request"]
    assert offline["stages"][0]["recorded_status"] == "committed"
    assert offline["stages"][0]["proof"]["status"] == "unknown"
    assert not db_file(root).exists()


def test_missing_receipt_and_bad_uuid_do_not_create_request_paths_or_identity(active, capsys):
    root, host = active
    requests = root / "state/requests"
    assert not requests.exists()
    before = _counts(root)
    missing = _call(capsys, root, "request", "show", str(uuid4()), expected=3)
    assert missing["release_id"] == host.release.release_id
    assert missing["error"]["code"] == "not_found" and missing["data"] == {}
    assert "cannot prove" in missing["error"]["hint"]
    invalid = _call(capsys, root, "request", "show", "not-a-uuid", expected=2)
    assert invalid["error"]["code"] == "invalid_argument" and invalid["data"] == {}
    assert not requests.exists() and _counts(root) == before


def test_foreign_or_corrupt_receipt_never_exposes_data(active, capsys):
    root, _ = active
    request_id = str(uuid4())
    _call(capsys, root, "task", "admit", "--title", "Bound work", "--request-id", request_id)
    matches = list((root / "state/requests").glob(f"*/{request_id}.json"))
    assert len(matches) == 1
    path = matches[0]
    raw = json.loads(path.read_bytes())
    actual = raw["intent"]["host_uid"]
    raw["intent"]["host_uid"] = next("host_" + digit * 32 for digit in "01"
                                        if "host_" + digit * 32 != actual)
    path.write_text(json.dumps(raw))
    foreign = _call(capsys, root, "request", "show", request_id, expected=4)
    assert foreign["error"]["code"] == "conflict" and foreign["data"] == {}
    path.write_text("{")
    corrupt = _call(capsys, root, "request", "show", request_id, expected=4)
    assert corrupt["error"]["code"] == "conflict" and corrupt["data"] == {}


def test_request_help_and_syntax_remain_dependency_light(monkeypatch, capsys):
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name.split(".")[0] in {"yaml", "jinja2", "pydantic"} or name.endswith(".request_read"):
            raise ImportError("blocked request read dependency")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject)
        with pytest.raises(SystemExit) as exited:
            main(["request", "show", "--help"])
        assert exited.value.code == 0
        assert "REQUEST_ID" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exited:
        main(["--json", "request", "show", "--bad=private-value"])
    output = capsys.readouterr().out
    assert exited.value.code == 2 and "private-value" not in output
    assert json.loads(output)["command"] == "request.show"
