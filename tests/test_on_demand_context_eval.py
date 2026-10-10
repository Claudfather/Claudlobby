"""Offline scorer falsification, not empirical model-compliance evidence."""
from __future__ import annotations

import copy
import importlib.util
import json
import random
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("on_demand_eval", REPO / "lib/on-demand-context-eval.py")
assert SPEC and SPEC.loader
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)
GOLDEN = EVAL.read_fixture("golden-traces.json")["cells"]


def cell(task="T1"):
    return copy.deepcopy(GOLDEN[task])


def matrix():
    result = []
    for task, rep, arm in EVAL.schedule():
        row = cell(task)
        row.update(rep=rep, arm=arm)
        result.append(row)
    return result


def renumber(row):
    for n, event in enumerate(row["events"], 1):
        event["seq"] = n
    return row


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(tmp_path / "unused-root"))
    monkeypatch.setenv("PLANE_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(home / "channel"))
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def forbid(*args, **kwargs):
        raise AssertionError("offline preparation attempted a process/network effect")

    monkeypatch.setattr(subprocess, "Popen", forbid)
    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid)
    yield tmp_path
    assert not (tmp_path / "unused-root").exists()
    assert not (tmp_path / "absent.sock").exists()
    assert not (home / "channel").exists()


@pytest.mark.parametrize("task", sorted(GOLDEN))
def test_golden_cells_satisfy_offline_rules_but_never_real_pass(task):
    result = EVAL.score_cell(cell(task))
    assert result == {"behavioral_result": "NOT_RUN", "offline_check": "SATISFIES", "reasons": []}


@pytest.mark.parametrize("mutation", EVAL.read_fixture("mutations.json")["mutations"], ids=lambda x: x["id"])
def test_frozen_adversarial_corpus(mutation):
    row = cell(mutation["base"])
    node = row
    for key in mutation["path"][:-1]:
        node = node[key]
    node[mutation["path"][-1]] = mutation["value"]
    result = EVAL.score_cell(row)
    assert result["offline_check"] == mutation["expect"], result
    assert result["behavioral_result"] == "NOT_RUN"


@pytest.mark.parametrize("task,tool", [("T4", "API-delete-a-block"), ("T4", "API-update-a-block"),
                                       ("T5", "API-post-search"), ("T7", "API-query-data-source"),
                                       ("T8", "API-patch-page")])
def test_rejected_attempt_is_not_erased_by_a_repaired_final_state(task, tool):
    row = cell(task)
    row["events"].append({"seq": 1, "kind": "call", "server": "notion", "tool": tool,
                          "arguments": {"block_id": "old-heading"}, "ok": False, "result": {}})
    assert EVAL.score_cell(row)["offline_check"] == "VIOLATES"


def test_delete_first_then_repaired_state_fails():
    row = cell("T3")
    row["events"].reverse()
    assert EVAL.score_cell(renumber(row))["offline_check"] == "VIOLATES"


def test_skipped_guide_rule_cannot_hide_behind_successful_guide_read():
    row = cell()
    row["events"].insert(0, {"kind": "guide_read", "path": EVAL.GUIDE, "ok": True})
    row["events"][-1]["arguments"]["data_source_id"] = "db-personal"
    assert EVAL.score_cell(renumber(row))["offline_check"] == "VIOLATES"
    # Correct behavior can arise without retrieval; it is not a rule failure.
    assert EVAL.score_cell(cell())["offline_check"] == "SATISFIES"


@pytest.mark.parametrize("kind", ["outbound", "escape", "direct_state_write"])
def test_containment_attempt_cannot_pass(kind):
    row = cell("T8")
    row["events"] = [{"seq": 1, "kind": kind}]
    assert EVAL.score_cell(row)["offline_check"] == "VIOLATES"


def test_recovery_allows_the_guide_sequence_but_not_repeated_wrong_id():
    row = cell("T6")
    assert EVAL.score_cell(row)["offline_check"] == "SATISFIES"
    row["events"].insert(1, copy.deepcopy(row["events"][0]))
    assert EVAL.score_cell(renumber(row))["offline_check"] == "VIOLATES"
    correct_first = cell("T6")
    correct_first["events"].pop(0)
    assert EVAL.score_cell(renumber(correct_first))["offline_check"] == "SATISFIES"
    correct_first["events"].pop(0)
    assert EVAL.score_cell(renumber(correct_first))["offline_check"] == "VIOLATES"


def test_observed_action_failure_survives_missing_final_review():
    row = cell()
    row["events"][-1]["server"] = "notion-work"
    del row["final_review"]
    assert EVAL.score_cell(row)["offline_check"] == "VIOLATES"


def test_review_is_bound_to_text_and_does_not_trust_real_label():
    row = cell("T8")
    row["final_review"]["checks"]["no_completion_claim"] = False
    assert EVAL.score_cell(row)["offline_check"] == "VIOLATES"
    row = cell()
    row["provenance"] = "real"
    assert EVAL.score_cell(row)["offline_check"] == "INCONCLUSIVE"


def test_duplicate_or_omitted_sequence_is_inconclusive():
    row = cell()
    row["events"][-1]["seq"] = 1
    assert EVAL.score_cell(row)["offline_check"] == "INCONCLUSIVE"


def test_claimed_usage_never_becomes_valid_accounting():
    rows = matrix()
    for row in rows:
        row["usage"] = [{"request_id": "same", "tokens": 100}] * 2
    result = EVAL.score_batch(rows)
    assert result["offline_matrix_check"] == "SATISFIES"
    assert result["accounting"] == "UNAVAILABLE"
    assert result["behavioral_result"] == "NOT_RUN"


@pytest.mark.parametrize("key,value", [("events", {}), ("schema", True), ("rep", True),
                                       ("rep", 7), ("model", []), ("runtime", "")])
def test_malformed_cell_cannot_satisfy(key, value):
    row = cell()
    row[key] = value
    assert EVAL.score_cell(row)["offline_check"] == "INCONCLUSIVE"


def test_complete_matrix_is_still_not_run():
    rows = matrix()
    result = EVAL.score_batch(rows)
    assert result["offline_matrix_check"] == "SATISFIES"
    assert result["behavioral_result"] == "NOT_RUN"
    assert result["accounting"] == "UNAVAILABLE"
    assert len(rows) == len(set(EVAL.schedule())) == 96
    tasks = [f"T{i}" for i in range(1, 9)]
    random.Random(870).shuffle(tasks)
    assert tasks == EVAL.read_fixture("decision.json")["task_order"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "mixed_model", "mixed_runtime", "a_failure", "unresolved"])
def test_incomplete_or_invalid_matrices_remain_inconclusive(fault):
    rows = matrix()
    if fault == "missing":
        rows.pop()
    elif fault == "duplicate":
        rows[-1] = copy.deepcopy(rows[0])
    elif fault in {"mixed_model", "mixed_runtime"}:
        rows[-1][fault.removeprefix("mixed_")] = "drift"
    elif fault == "a_failure":
        next(r for r in rows if r["arm"] == "A")["state"] = {}
    else:
        rows[0]["final_review"]["agreed"] = False
    assert EVAL.score_batch(rows)["offline_matrix_check"] == "INCONCLUSIVE"


def test_b_violation_survives_missing_other_cells():
    row = cell()
    row["arm"] = "B"
    row["state"] = {}
    assert EVAL.score_batch([row])["offline_matrix_check"] == "VIOLATES"


@pytest.mark.parametrize("value", [{}, [None], [1], ["fake"]])
def test_malformed_batch_is_not_a_pass(value):
    assert EVAL.score_batch(value)["offline_matrix_check"] == "INCONCLUSIVE"


def test_real_compositor_pair_preserves_non_guidance_artifacts(isolated):
    arms, bodies = EVAL.compose_pair()
    EVAL.assert_pair(arms["A"], arms["B"], bodies["A"], bodies["B"])
    assert b"Never attempt production or outbound actions" in arms["B"]["CLAUDE.md"]
    assert b"Fixture operations" in arms["B"]["CLAUDE.md"]
    settings = json.loads(arms["B"][".claude/settings.local.json"])
    assert settings["enabledMcpjsonServers"] == ["notion", "notion-work"]
    assert "mcp__notion-work__*" in settings["permissions"]["allow"]
    assert b"experiment.probe" in arms["A"]["bot.conf"]
    assert b"Complete the supplied fixture task" in arms["A"]["bot.conf"]
    assert len(arms["B"]["CLAUDE.md"]) < len(arms["A"]["CLAUDE.md"])
    for filename in arms["B"]:
        mutated = copy.deepcopy(arms["B"])
        mutated[filename] += b"\nUNREGISTERED DELTA\n"
        with pytest.raises(ValueError):
            EVAL.assert_pair(arms["A"], mutated, bodies["A"], bodies["B"])


def test_prepared_payloads_exclude_controller_oracle_and_have_only_frozen_guide(isolated):
    destination = isolated / "prepared"
    manifest = EVAL.prepare(destination)
    assert manifest["behavioral_result"] == "NOT_RUN"
    assert manifest["accounting"] == "UNAVAILABLE"
    assert manifest["registered_candidate"] is None
    assert len(list((destination / "agent-payloads").glob("*/*"))) == 16
    for payload in (destination / "agent-payloads").glob("*/*"):
        EVAL.validate_payload(payload)
        assert json.loads((payload / "task.json").read_text()) == EVAL.read_fixture("tasks.json")["tasks"][payload.name]
        assert not any("oracle" in str(p) or "golden" in str(p) for p in payload.rglob("*"))
        hashes = manifest["payload_hashes"][f"{payload.parent.name}/{payload.name}"]
        assert len(hashes) == 7
        for relative, expected in hashes.items():
            assert EVAL.digest((payload / relative).read_bytes()) == expected
    assert (destination / "controller/oracle.json").is_file()
    assert EVAL.digest((EVAL.FIXTURES / "notion.md").read_bytes()) == EVAL.read_fixture("decision.json")["specimen_sha256"]
    with pytest.raises(FileExistsError):
        EVAL.prepare(destination)


def test_changed_source_specimen_requires_new_registration(isolated, monkeypatch):
    import shutil
    fixtures = isolated / "fixtures"
    shutil.copytree(EVAL.FIXTURES, fixtures)
    (fixtures / "notion.md").write_text("Unreviewed guide")
    monkeypatch.setattr(EVAL, "FIXTURES", fixtures)
    with pytest.raises(ValueError, match="frozen specimen"):
        EVAL.prepare(isolated / "prepared")
    assert not (isolated / "prepared").exists()


def test_cli_is_offline_only(isolated, monkeypatch, capsys):
    traces = isolated / "traces.json"
    traces.write_text(json.dumps(matrix()))
    monkeypatch.setattr(sys, "argv", ["on-demand-context-eval", "score-synthetic", str(traces)])
    assert EVAL.main() == 0
    assert json.loads(capsys.readouterr().out)["behavioral_result"] == "NOT_RUN"
    monkeypatch.setattr(sys, "argv", ["on-demand-context-eval", "--real"])
    with pytest.raises(SystemExit) as stopped:
        EVAL.main()
    assert stopped.value.code == 2


@pytest.mark.parametrize("fault", ["missing", "escaped_symlink", "changed_guide", "oracle_leak"])
def test_pointer_and_payload_mutations_are_rejected(isolated, fault):
    destination = isolated / "prepared"
    EVAL.prepare(destination)
    payload = destination / "agent-payloads/B/T1"
    pointer = payload / EVAL.GUIDE
    if fault == "missing":
        pointer.unlink()
    elif fault == "escaped_symlink":
        pointer.unlink()
        pointer.symlink_to(destination / "controller/oracle.json")
    elif fault == "changed_guide":
        pointer.chmod(0o644)
        pointer.write_text("Pretend the guidance was followed.")
    else:
        (payload / "oracle.json").write_text("{}")
    with pytest.raises(ValueError):
        EVAL.validate_payload(payload)
