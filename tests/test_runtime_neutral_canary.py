"""``harness/runtime-neutral-canary.py``: the P0 instrument records shapes and ids, never content.

The receiver and the hook logger sit on live sessions, so the property that matters is what they
refuse to keep: a prompt, a tool input or a response must never reach a log line. These tests feed
canned OTLP/JSON and hook payloads shaped like the live capture of 2026-10-05 (Claude Code 2.1.289;
identifiers replaced) and assert what lands.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("rnc", ROOT / "harness" / "runtime-neutral-canary.py")
rnc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rnc)

SECRET = "please summarise the quarterly numbers for acme"
SID = "11111111-2222-4333-8444-555555555555"


def _s(key, value):
    return {"key": key, "value": {"stringValue": value}}


LOGS = {"resourceLogs": [{
    "resource": {"attributes": [_s("service.name", "claude-code"), _s("agent.runtime", "claude"),
                                _s("claudlobby.bot", "bot:canary/c6a")]},
    "scopeLogs": [{"logRecords": [
        {"timeUnixNano": "1791227500000000000", "body": {"stringValue": "claude_code.user_prompt"},
         "attributes": [_s("event.name", "user_prompt"), _s("session.id", SID), _s("prompt", SECRET),
                        {"key": "prompt_length", "value": {"intValue": "47"}}]},
        {"timeUnixNano": "1791227500000000000", "body": {"stringValue": "claude_code.assistant_response"},
         "attributes": [_s("event.name", "assistant_response"), _s("response", "<REDACTED>")]},
        {"timeUnixNano": "1791227500000000000", "body": {"stringValue": SECRET}, "attributes": []},
    ]}],
}]}

METRICS = {"resourceMetrics": [{
    "resource": {"attributes": [_s("agent.runtime", "claude")]},
    "scopeMetrics": [{"metrics": [{
        "name": "claude_code.token.usage",
        "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
            {"asDouble": 12, "attributes": [_s("session.id", SID), _s("type", "input")]}]},
    }]}],
}]}


class TestShapes:
    def test_a_prompt_never_lands_but_its_length_and_ids_do(self):
        rows = rnc.logs_shape(LOGS, received=1791227505.0)
        dumped = json.dumps(rows)
        assert SECRET not in dumped
        prompt = rows[0]
        assert prompt["event"] == "user_prompt" and prompt["body"] == "claude_code.user_prompt"
        assert prompt["attrs"]["session.id"] == SID
        assert prompt["attrs"]["prompt"] == f"stringValue:{len(SECRET)}"
        assert prompt["lag_s"] == 5.0

    def test_the_exporters_redaction_placeholder_is_kept_as_the_measurement(self):
        assert rnc.logs_shape(LOGS, received=0)[1]["attrs"]["response"] == "<REDACTED>"

    def test_a_body_that_is_not_an_event_name_is_kept_as_a_length(self):
        assert rnc.logs_shape(LOGS, received=0)[2]["body"] == f"string:{len(SECRET)}"

    def test_metrics_keep_name_temporality_and_session_ids(self):
        (row,) = rnc.metrics_shape(METRICS)
        assert row["metric"] == "claude_code.token.usage" and row["temporality"] == "delta"
        assert row["session_ids"] == [SID] and row["point_attrs"] == ["session.id", "type"]


class TestHook:
    def test_a_hook_record_keeps_ids_and_marker_values_only(self):
        payload = {"hook_event_name": "PostToolUse", "session_id": SID, "tool_name": "Bash",
                   "agent_id": "a1", "tool_input": {"command": SECRET}, "tool_response": SECRET}
        env = {"CLAUDE_CODE_SESSION_ID": SID, "CLAUDE_CODE_CHILD_SESSION": "1",
               "CLAUDE_CODE_OAUTH_TOKEN": "sk-should-never-land", "HOME": "/x"}
        line = rnc.hook_record(payload, env, now=1.0)
        dumped = json.dumps(line)
        assert SECRET not in dumped and "sk-should-never-land" not in dumped
        assert line["agent_id"] == "a1" and line["env"]["values"]["CLAUDE_CODE_CHILD_SESSION"] == "1"
        assert "CLAUDE_CODE_OAUTH_TOKEN" in line["env"]["names"]  # its name, never its value


class TestCodexHook:
    """The Codex batch (C1-C3) uses the same hook logger: field names and shapes, never values."""

    def test_a_codex_payload_keeps_key_names_and_codex_ids_only(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "0199aaaa-bbbb", "source": "startup",
                   "transcript_path": "/Users/someone/.codex/sessions/rollout.jsonl", "prompt": SECRET,
                   "turn_id": "t1"}
        env = {"CODEX_SESSION_ID": "0199aaaa-bbbb", "CODEX_THREAD_ID": "th-1", "CODEX_API_KEY": "sk-never"}
        line = rnc.hook_record(payload, env, now=1.0, lineage=[{"pid": 2, "comm": "codex"}])
        dumped = json.dumps(line)
        assert SECRET not in dumped and "sk-never" not in dumped and "someone" not in dumped
        assert line["keys"]["transcript_path"].startswith("str:") and line["keys"]["prompt"] == f"str:{len(SECRET)}"
        assert line["env"]["values"] == {"CODEX_SESSION_ID": "0199aaaa-bbbb", "CODEX_THREAD_ID": "th-1"}
        assert "CODEX_API_KEY" in line["env"]["names"] and line["ancestors"] == [{"pid": 2, "comm": "codex"}]

    def test_stdout_and_hold_are_written_and_the_report_sees_a_completed_hold(self, tmp_path, capsys):
        log = tmp_path / "hooks.jsonl"
        payload = json.dumps({"hook_event_name": "SessionEnd", "session_id": SID, "reason": "other"})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sys, "stdin", __import__("io").StringIO(payload))
            assert rnc.main(["hook", "--log", str(log), "--stdout", "nonce-123", "--hold", "10"]) == 0
        assert capsys.readouterr().out.strip() == "nonce-123"
        out = rnc.report(tmp_path)
        assert out["holds"] == [{"event": "SessionEnd", "session_id": SID, "completed": True}]
        assert out["payload_keys_by_event"]["SessionEnd"] == ["hook_event_name", "reason", "session_id"]


class TestReport:
    def test_ids_join_across_hook_bash_and_exporter(self, tmp_path):
        (tmp_path / "otlp").mkdir()
        (tmp_path / "hooks.jsonl").write_text(json.dumps(
            {"hook_event_name": "SessionStart", "session_id": SID, "source": "startup",
             "env": {"names": [], "values": {}}}) + "\n")
        (tmp_path / "otlp" / "metrics.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rnc.metrics_shape(METRICS)))
        (tmp_path / "bash-main.env").write_text(f"CLAUDE_CODE_SESSION_ID={SID}\nCLAUDE_CODE_CHILD_SESSION=1\n")
        out = rnc.report(tmp_path)
        assert out["otel_ids_matching_a_hook"] == [SID] and out["otel_ids_without_a_hook"] == []
        assert out["bash-main_matches_start"] is True and out["temporality"] == ["delta"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_c10_reports_markers_and_never_keeps_the_full_env(tmp_path):
    # A short socket dir: macOS caps a unix socket path near 104 bytes, and tmp_path there is long.
    sockets = tempfile.mkdtemp(prefix="rnc", dir="/tmp")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "CLAUDE_CODE_CHILD_SESSION": "1",
           "CLAUDE_CODE_OAUTH_TOKEN": "sk-should-never-land", "TMUX_TMPDIR": sockets}
    try:
        proc = subprocess.run([sys.executable, str(ROOT / "harness" / "runtime-neutral-canary.py"), "c10",
                               "--out", str(tmp_path / "out")], env=env, capture_output=True, text=True, timeout=60)
    finally:
        shutil.rmtree(sockets, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in proc.stdout.splitlines()]
    assert {r["arm"]: r["pane"].get("CLAUDE_CODE_CHILD_SESSION") for r in rows} == {"bare": "1", "scrubbed": None}
    assert "sk-should-never-land" not in (tmp_path / "out" / "c10.jsonl").read_text()
    assert not list((tmp_path / "out").glob("c10-*.env"))


def test_env_block_labels_the_bots_real_fleet():
    """A borrowed production bot is labelled with its own fleet, not `canary` (Pi run, 2026-10-05)."""
    attrs = rnc.env_block(14319, "rajan", "crog-eng-team")["OTEL_RESOURCE_ATTRIBUTES"]
    assert "claudlobby.bot=rajan" in attrs and "claudlobby.fleet=crog-eng-team" in attrs
    assert "claudlobby.fleet=canary" in rnc.env_block(14319, "c")["OTEL_RESOURCE_ATTRIBUTES"]
