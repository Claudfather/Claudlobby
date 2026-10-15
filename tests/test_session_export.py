"""Consumer invariants over a captured, actual 0.28 store envelope."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from claudlobby import session_export as se
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.plane.contracts import ContractViolation
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.ids import derive_session_uid, derive_uid
from claudlobby.plane.registry_emit import run_generate_scan
from tests.conftest import load_lib_module
from tests.package_fixtures import source_package
from tests.plane_setup import initialize_plane

FIXTURES = Path(__file__).parent / "fixtures/claudna_export"
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def envelope():
    return deepcopy(json.loads((FIXTURES / "envelopes.json").read_text())[0])


@pytest.fixture
def world(tmp_path, monkeypatch, scratch_plane_env):
    root = tmp_path / "root"
    root.mkdir()
    (root / "fleet.yaml").write_text(
        "fleet:\n  name: canary\n  manager: lead\n  bots:\n    lead: {expertise: [orchestration]}\n"
    )
    for key, value in scratch_plane_env(root).items():
        monkeypatch.setenv(key, value)
    initialize_plane(root)
    fleet, _ = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, package=source_package())
    run_generate_scan(paths, fleet, require_commit=True)
    store = paths.bot_runtime("lead") / "data/claudna"
    store.mkdir(parents=True)
    entry = {
        "schema": se.ENTRYPOINT_SCHEMA,
        "plugin_version": "0.28.0",
        "python": "/usr/bin/python3",
        "entrypoint": str(store / "door.py"),
        "agent_cli": "claude",
    }
    (store / "door.py").write_text("# export door stand-in\n")
    (store / "entrypoint.json").write_text(json.dumps(entry))
    return root, paths, fleet, store


class Door:
    def __init__(self, doc=None, log=None):
        self.doc = doc or envelope()
        self.log = log if log is not None else []
        self.fail_ack = False

    def __call__(self, argv, **kwargs):
        assert kwargs["timeout"] == 60.0
        if "--ack" in argv:
            self.log.append(("ack", argv[argv.index("--sid") + 1]))
            return subprocess.CompletedProcess(
                argv, 1 if self.fail_ack else 0, "{}", ""
            )
        self.log.append(("export", argv))
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.doc), "")


def export(world, door, **kwargs):
    root, paths, _, store = world
    return se.export_bot(
        root,
        "canary",
        "lead",
        store,
        run=door,
        now=NOW,
        fleet_state=paths.fleet_state,
        notify=lambda **_: {"notification": "submitted"},
        **kwargs,
    )


def state(world):
    return json.loads((world[1].fleet_state / "session-export/lead.json").read_text())


def test_events_are_reader_visible_and_one_item_lands_before_each_session_ack(world):
    log, raw = [], []
    door = Door(log=log)

    def emit(root, rows, **kw):
        assert len(rows) == 1
        raw.extend(rows)
        log.append(("emit", rows[0]["payload"]["data"]["data"]["session_id"]))
        return emit_batch(root, rows, **kw)

    result = export(world, door, emit=emit)
    assert result.emitted == 2 and result.acked == door.doc["next"]
    assert [x[0] for x in log] == ["export", "emit", "ack", "emit", "ack"]
    assert "--include-skipped" in log[0][1]
    assert "--root" in log[0][1] and "--consumer" in log[0][1]
    for event, item in zip(raw, door.doc["items"]):
        data = event["payload"]["data"]["data"]
        assert event["event_id"] == derive_uid(
            "ev", f"session_summary:canary:{item['sid']}:{item['seg']}"
        )
        assert event["source_ref"].startswith("fleet-events:")
        assert event["payload"]["subject"] == "bot:canary/lead"
        assert (
            event["occurred_at"]
            == datetime.fromisoformat(
                item["segment"]["sealed_at"].replace("Z", "+00:00")
            ).isoformat()
        )
        assert event["observed_at"] == NOW.isoformat()
        assert data["session_uid"] == derive_session_uid(item["sid"], "claude")
    reader = load_lib_module("plane-readers")
    conn = sqlite3.connect(world[0] / "state/plane/plane.db")
    conn.row_factory = sqlite3.Row
    rows = reader.fleet_events(conn, "canary", event_type="session_summary")
    conn.close()
    assert {r["data"]["status"] for r in rows} == {"ok", "skipped"}
    ok = next(r["data"] for r in rows if r["data"]["status"] == "ok")
    assert (
        ok["session_id"] and ok["journey"]["title"] == "Local summary consumer canary"
    )
    skip = next(r["data"] for r in rows if r["data"]["status"] == "skipped")
    assert skip["skipped_reason"] == "disabled" and skip["sealed_by"] == "session_end"
    assert all(
        skip[k] is None
        for k in (
            "turns",
            "transcript_bytes",
            "journey",
            "blocks",
            "procedures",
            "producer",
        )
    )
    assert skip["prompts"] == 1
    again = export(world, door)
    assert again.duplicate == 2 and again.acked == result.acked


def test_replay_of_unacked_segment_does_not_wedge_new_segment(world):
    doc = envelope()
    doc["items"] = doc["items"][:1]
    doc["next"] = {doc["items"][0]["sid"]: 1}
    door = Door(doc)
    door.fail_ack = True
    first = export(world, door)
    assert first.emitted == 1 and first.failed == "ack_failed" and not first.acked
    second = deepcopy(doc["items"][0])
    second["seg"] = 2
    doc["items"].append(second)
    doc["next"][second["sid"]] = 2
    door.fail_ack = False
    result = export(world, door)
    assert (result.emitted, result.duplicate, result.acked[second["sid"]]) == (1, 1, 2)


def test_transient_failure_acks_only_prior_sessions(world):
    door = Door()
    sid = door.doc["items"][1]["sid"]

    def emit(root, rows, **kw):
        if rows[0]["payload"]["data"]["data"]["session_id"] == sid:
            raise sqlite3.OperationalError("temporary")
        return emit_batch(root, rows, **kw)

    result = export(world, door, emit=emit)
    assert list(result.acked) == [door.doc["items"][0]["sid"]]
    assert result.failed == "emit_failed" and state(world)["consecutive_failures"] == 1


@pytest.mark.parametrize("double", [False, True])
def test_contract_refusal_gets_own_fallback_id_and_terminal_refusal_still_acks(
    world, double
):
    ids = []

    def emit(root, rows, **kw):
        event = rows[0]
        ids.append(event["event_id"])
        if len(ids) % 2 or double:
            raise ContractViolation([{"loc": ("event_id",), "msg": "refused"}])
        assert event["payload"]["data"]["data"]["skipped_reason"] == "unexportable"
        return emit_batch(root, rows, **kw)

    door = Door()
    result = export(world, door, emit=emit)
    assert result.unexportable == 2 and result.acked == door.doc["next"]
    for i, item in enumerate(door.doc["items"]):
        assert ids[2 * i] != ids[2 * i + 1]
        assert ids[2 * i + 1] == derive_uid(
            "ev", f"session_summary_unexportable:canary:{item['sid']}:{item['seg']}"
        )
    assert ("last_unexportable" in state(world)) is double


@pytest.mark.parametrize("fallback_refused", [False, True])
def test_real_ledger_family_divergence_uses_separate_id(world, fallback_refused):
    root = world[0]
    doc = envelope()
    doc["items"] = doc["items"][:1]
    sid = doc["items"][0]["sid"]
    doc["next"] = {sid: 1}
    conn = sqlite3.connect(root / "state/plane/plane.db")
    # Keep the genuine ledger row, remove only its family fact, exactly the corruption refused by ingest.
    record, at = se.summary_record(doc["items"][0], fleet="canary", bot="lead", now=NOW)
    for fallback in [False, True] if fallback_refused else [False]:
        raw = se._system_event(
            "session_summary",
            fleet="canary",
            bot="lead",
            sid=sid,
            seg=1,
            occurred_at=at,
            observed_at=NOW,
            data=record,
            fallback=fallback,
        )
        emit_batch(root, [raw], require_commit=True)
        conn.execute("DELETE FROM events WHERE event_id = ?", (raw["event_id"],))
        conn.commit()
    conn.close()
    result = export(world, Door(doc))
    assert result.unexportable == 1 and result.acked == {sid: 1}
    assert ("last_unexportable" in state(world)) is fallback_refused


@pytest.mark.parametrize(
    "reason,change",
    [
        ("no_entrypoint", "missing"),
        ("unknown_schema", {"schema": "bad"}),
        ("old_entrypoint", {"plugin_version": None}),
        ("old_entrypoint", {"plugin_version": "0.27.9"}),
        ("stale_entrypoint", {"entrypoint": "/nonexistent/store"}),
    ],
)
def test_skip_reasons_are_not_failures(world, reason, change):
    path = world[3] / "entrypoint.json"
    if change == "missing":
        path.unlink()
    else:
        doc = json.loads(path.read_text())
        doc.update(change)
        path.write_text(json.dumps(doc))

    def never(*_, **__):
        raise AssertionError("must not guess or run a cached door")

    result = export(world, never)
    assert result.skipped == reason and state(world)["last_skip"] == reason
    assert not state(world).get("consecutive_failures")


@pytest.mark.parametrize("failure", ["timeout", "exit", "json", "shape"])
def test_export_failures_have_own_counters(world, failure):
    def run(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        return subprocess.CompletedProcess(
            argv,
            1 if failure == "exit" else 0,
            "garbage" if failure == "json" else "{}",
            "",
        )

    result = export(world, run)
    reason = "export_timeout" if failure == "timeout" else "export_failed"
    assert result.failed == reason and state(world)["failure_counts"][reason] == 1


def test_stall_record_and_page_retry_independently_then_rearm(world):
    calls, pages = [], []

    def broken(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)

    def emit(root, rows, **kw):
        calls.append(rows[0])
        if len(calls) == 1:
            raise OSError("record down")
        return emit_batch(root, rows, **kw)

    def notify(**kw):
        pages.append(kw)
        return {"notification": "submitted"}

    root, paths, _, store = world
    for n in range(5):
        se.export_bot(
            root,
            "canary",
            "lead",
            store,
            run=broken,
            now=NOW + timedelta(minutes=n),
            emit=emit,
            notify=notify,
            fleet_state=paths.fleet_state,
        )
    assert len(calls) == 2 and len(pages) == 1 and pages[0]["level"] == "alert"
    assert calls[0]["event_id"] == calls[1]["event_id"]
    assert calls[1]["payload"]["event"] == "export_stalled"
    assert state(world)["stall"] == {
        "since": NOW.isoformat(),
        "recorded": True,
        "notified": True,
    }
    export(world, Door())
    assert "stall" not in state(world)


def test_page_failure_is_retried_without_rerecording(world):
    calls = []

    def notify(**kw):
        calls.append(kw)
        return {"notification": "failed" if len(calls) == 1 else "submitted"}

    state_doc = {"last_tick": NOW.isoformat(), "consecutive_failures": 3}
    emits = []

    def emit(*a, **kw):
        emits.append(a)
        return [SimpleNamespace(status="spooled")]

    for _ in range(3):
        se.report_stall(world[0], "canary", "lead", state_doc, emit=emit, notify=notify)
    assert len(calls) == 2 and len(emits) == 1


def test_bounded_nested_journey_and_skipped_fallback_times():
    item = envelope()["items"][0]
    item["summary"]["journey"].update(
        arc=[{"step": "x" * 50000, "result": "é" * 50000}] * 200,
        done=[{"text": "é" * 50000}] * 200,
        next=[{"text": "é" * 50000}] * 200,
        in_progress=[{"text": "é" * 50000}] * 200,
    )
    record, _ = se.summary_record(item, fleet="canary", bot="lead", now=NOW)
    assert len(json.dumps(record, ensure_ascii=False).encode()) <= se.DATA_CAP_BYTES
    assert json.loads(json.dumps(record))["journey"]["title"]
    item["session"].pop("agent_cli")
    item["segment"]["sealed_at"] = "invalid"
    record, at = se.summary_record(item, fleet="canary", bot="lead", now=NOW)
    assert (
        at == NOW
        and record["sealed_at"] == "invalid"
        and record["agent_cli"] == "claude"
    )


def test_dry_run_runs_export_without_state_emit_or_ack(world):
    door = Door()
    result = export(
        world, door, dry_run=True, emit=lambda *a, **kw: pytest.fail("dry-run emitted")
    )
    assert len(result.records) == 2 and not result.acked and len(door.log) == 1
    assert not (world[1].fleet_state / "session-export/lead.json").exists()


def test_off_tick_is_loud_and_runs_nothing(monkeypatch, capsys):
    from claudlobby.commands.session_export import tick

    monkeypatch.setenv("SESSION_EXPORT_ENABLED", "0")
    assert tick(SimpleNamespace()) == 0
    assert "OFF here" in capsys.readouterr().out


def test_doctor_missing_invalid_stalled_stale_and_skip(world):
    from claudlobby.doctor import DoctorReport, check_session_export

    root, paths, fleet, _ = world
    report = DoctorReport()
    check_session_export(fleet, paths, report)
    assert not report.checks
    p = paths.fleet_state / "session-export/lead.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[")
    report = DoctorReport()
    check_session_export(fleet, paths, report)
    assert report.checks[0].status == "warn"
    p.write_text(
        json.dumps(
            {
                "consecutive_failures": 3,
                "pending": 1,
                "last_ack_at": "2020-01-01T00:00:00Z",
                "last_failure": "export_timeout",
                "last_skip": "stale_entrypoint",
            }
        )
    )
    report = DoctorReport()
    check_session_export(fleet, paths, report)
    assert [(c.name, c.status) for c in report.checks] == [
        ("session-export-stalled", "warn"),
        ("session-export-cursor-age", "warn"),
        ("session-export", "pass"),
    ]
    assert "stale_entrypoint" in report.checks[-1].detail


def test_offline_captured_envelope_matches_vendored_contract():
    from claudlobby.conformance import export_schema_errors

    schema = json.loads(
        (Path(se.__file__).parent / "contracts/claudna-export.schema.json").read_text()
    )
    for doc in json.loads((FIXTURES / "envelopes.json").read_text()):
        assert export_schema_errors(doc, schema) == []
        bad = deepcopy(doc)
        del bad["items"][0]["segment"]["counts"]["prompts"]
        assert export_schema_errors(bad, schema)
        bad = deepcopy(doc)
        bad["items"][0]["seg"] = "1"
        assert export_schema_errors(bad, schema)


def test_spooled_events_are_durable_enough_to_ack(world):
    calls = []

    def emit(root, rows, **kw):
        calls.append(rows)
        return [SimpleNamespace(status="spooled")]

    result = export(world, Door(), emit=emit)
    assert result.spooled == 2 and len(calls) == 2 and len(result.acked) == 2


def test_skip_breaks_consecutive_failure_count(world):
    def broken(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)

    export(world, broken)
    export(world, broken)
    p = world[3] / "entrypoint.json"
    saved = p.read_text()
    p.unlink()
    assert export(world, broken).skipped == "no_entrypoint"
    p.write_text(saved)
    export(world, broken)
    assert state(world)["consecutive_failures"] == 1 and "stall" not in state(world)


def test_block_tally_uses_home_and_preserves_producer(world):
    item = envelope()["items"][0]
    item["summary"]["blocks"] = [
        {"home": "lessons"},
        {"home": "lessons"},
        {"home": "decisions"},
    ]
    record, _ = se.summary_record(item, fleet="canary", bot="lead", now=NOW)
    assert record["blocks"] == {"count": 3, "kinds": {"lessons": 2, "decisions": 1}}
    assert record["producer"] == item["summary"]["producer"]


def test_private_tick_json_and_malformed_call(monkeypatch, capsys):
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    from claudlobby.__main__ import main

    root = Path("/example")
    selected = SimpleNamespace(
        paths=SimpleNamespace(
            root=root, fleet_state=root / "runtime", bot_runtime=lambda b: root / b
        ),
        fleet=SimpleNamespace(name="canary", bots={"lead": None}),
    )
    monkeypatch.setattr(
        "claudlobby.operation_context.resolve_operation_scope",
        lambda **kw: (selected, None),
    )
    monkeypatch.setattr(
        se, "export_bot", lambda *a, **kw: se.BotOutcome("lead", acked={"sid": 1})
    )
    assert main(["_session-export-tick", "canary", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fleet"] == "canary" and result["bots"][0]["acked"] == {"sid": 1}
    with pytest.raises(SystemExit) as exc:
        main(["_session-export-tick"])
    assert exc.value.code == 2


def test_tick_estate_silencer_never_exports_or_acks(monkeypatch, capsys):
    from claudlobby.commands.session_export import tick

    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    assert tick(SimpleNamespace()) == 0
    assert "PLANE_EMIT_DISABLED=1" in capsys.readouterr().out


def test_export_contract_absent_skip_and_schema_drift(tmp_path):
    from claudlobby.conformance import check_export_contract

    code, detail = check_export_contract(tmp_path / "absent")
    assert code == 0 and detail.startswith("SKIP")
    clone = tmp_path / "clone"
    source = clone / "lib/claudna/session_store/schemas"
    source.mkdir(parents=True)
    (source / "export.schema.json").write_text("{}")
    code, detail = check_export_contract(clone)
    assert code == 1 and "differs from checkout" in detail
