"""Unit tests for lib/transcript-usage.py — per-session token accounting from
Claude Code transcripts (the prize-sizing instrument for the token-efficiency
comms protocol, #716 / #729 stage A).

The fixture is hand-built (write_jsonl) so every expected number is
hand-computable from the constants below; the arithmetic is spelled out in
comments beside each assertion.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.conftest import load_lib_module, write_jsonl

tu = load_lib_module("transcript-usage")

MODEL = "claude-opus-4-8"

# Two outbound-comms payloads, measured by their exact literal length. The
# parser must find these and estimate tokens as chars // 4.
TG_TEXT = "worker: model shipped, tests green, PR up — standing by for next task"
BASH_COMMS_CMD = "bash /home/x/lib/report-back.sh branden completed 'model shipped'"


def _turn(usage, content, sidechain=False, model=MODEL):
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "sessionId": "s1",
        "timestamp": "2026-07-24T00:00:00Z",
        "message": {
            "role": "assistant",
            "model": model,
            "usage": usage,
            "content": content,
        },
    }


# --- primary fixture rows -------------------------------------------------
# main turn 1: carries a Bash report-back (comms)
_ROW1 = _turn(
    {
        "input_tokens": 100,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 300,
        "output_tokens": 10,
    },
    [
        {"type": "text", "text": "working"},
        {"type": "tool_use", "name": "Bash", "input": {"command": BASH_COMMS_CMD}},
    ],
)
# main turn 2: carries a Telegram reply (comms)
_ROW2 = _turn(
    {
        "input_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1000,
        "output_tokens": 20,
    },
    [
        {
            "type": "tool_use",
            "name": "mcp__plugin_telegram_telegram__reply",
            "input": {"chat_id": "x", "text": TG_TEXT},
        }
    ],
)
# sidechain turn (subagent) — excluded from main totals
_ROW3_SIDE = _turn(
    {
        "input_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 500,
        "output_tokens": 5,
    },
    [{"type": "text", "text": "subagent"}],
    sidechain=True,
)
# main turn 3: flat usage differs from the iterations[] sum (double-count trap).
# flat cache_read=1000/output=8; iterations sum to 2000/16 — parser must use flat.
_ROW4_ITERS = _turn(
    {
        "input_tokens": 10,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1000,
        "output_tokens": 8,
        "iterations": [
            {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 8},
            {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 8},
        ],
    },
    [{"type": "text", "text": "multi"}],
)
_ROW5_USER = {"type": "user", "message": {"role": "user", "content": "hi"}}
_ROW6_SYSTEM = {"type": "system", "subtype": "info"}

PRIMARY = [_ROW1, _ROW2, _ROW3_SIDE, _ROW4_ITERS, _ROW5_USER, _ROW6_SYSTEM]

# Hand-computed MAIN totals (turns 1,2,4 — sidechain and non-assistant excluded):
#   input  = 100 + 50 + 10 = 160
#   cache_creation = 200 + 0 + 0 = 200
#   cache_read = 300 + 1000 + 1000 = 2300
#   output = 10 + 20 + 8 = 38
#   turns  = 3
#   protocol_sensitive = 160 + 38 = 198
#   cost_weighted = 160*1.0 + 200*1.25 + 2300*0.1 + 38*5.0
#                 = 160 + 250 + 230 + 190 = 830.0
MAIN = dict(inp=160, cc=200, cr=2300, out=38, turns=3, ps=198, cw=830.0)
# with sidechain added (turn 3: +5 input, +500 cache_read, +5 output, +1 turn):
#   input=165, cache_read=2800, output=43, turns=4
#   protocol_sensitive = 165 + 43 = 208
#   cost_weighted = 165 + 250 + 2800*0.1 + 43*5.0 = 165 + 250 + 280 + 215 = 910.0
COMBINED = dict(inp=165, cc=200, cr=2800, out=43, turns=4, ps=208, cw=910.0)


def _write(tmp_path, rows, name="t.jsonl"):
    p = tmp_path / name
    write_jsonl(p, rows)
    return p


class TestComponentSums:
    def test_main_excludes_sidechain_and_non_assistant(self, tmp_path):
        r = tu.parse_file(str(_write(tmp_path, PRIMARY))).main
        assert r.input_tokens == MAIN["inp"]
        assert r.cache_creation_input_tokens == MAIN["cc"]
        assert r.cache_read_input_tokens == MAIN["cr"]
        assert r.output_tokens == MAIN["out"]
        assert r.turns == MAIN["turns"]

    def test_with_sidechains_included(self, tmp_path):
        res = tu.parse_file(str(_write(tmp_path, PRIMARY)))
        c = res.main + res.sidechain
        assert c.input_tokens == COMBINED["inp"]
        assert c.cache_read_input_tokens == COMBINED["cr"]
        assert c.output_tokens == COMBINED["out"]
        assert c.turns == COMBINED["turns"]

    def test_models_collected(self, tmp_path):
        r = tu.parse_file(str(_write(tmp_path, PRIMARY))).main
        assert MODEL in r.models


class TestIterationsNotDoubleCounted:
    def test_flat_usage_wins_over_iterations_sum(self, tmp_path):
        # single-turn fixture: flat cr=1000/out=8; iterations sum to 2000/16.
        r = tu.parse_file(str(_write(tmp_path, [_ROW4_ITERS]))).main
        assert r.cache_read_input_tokens == 1000  # NOT 2000
        assert r.output_tokens == 8  # NOT 16
        assert r.input_tokens == 10  # NOT 20


class TestRobustness:
    def test_non_assistant_lines_ignored(self, tmp_path):
        r = tu.parse_file(str(_write(tmp_path, [_ROW5_USER, _ROW6_SYSTEM]))).main
        assert r.turns == 0
        assert r.input_tokens == 0

    def test_malformed_line_tolerated(self, tmp_path):
        p = tmp_path / "bad.jsonl"
        # garbage line contains "assistant" so it survives the substring pre-filter
        # and actually exercises the json.loads except path.
        p.write_text(
            json.dumps(_ROW1)
            + '\n{"type":"assistant" broken {{{\n'
            + json.dumps(_ROW2)
            + "\n"
        )
        r = tu.parse_file(str(p)).main
        # both valid turns still counted; garbage line skipped
        assert r.turns == 2
        assert r.input_tokens == 150  # 100 + 50


class TestAxes:
    def test_protocol_sensitive(self, tmp_path):
        r = tu.parse_file(str(_write(tmp_path, PRIMARY))).main
        assert r.protocol_sensitive == MAIN["ps"]

    def test_cost_weighted_total(self, tmp_path):
        r = tu.parse_file(str(_write(tmp_path, PRIMARY))).main
        assert abs(r.cost_weighted_total - MAIN["cw"]) < 1e-6

    def test_weights_constant_documents_billing_ratios(self):
        assert tu.WEIGHTS["input"] == 1.0
        assert tu.WEIGHTS["cache_creation"] == 1.25
        assert tu.WEIGHTS["cache_read"] == 0.1
        assert tu.WEIGHTS["output"] == 5.0


class TestCommsShare:
    def test_detects_telegram_and_bash_comms(self, tmp_path):
        r = tu.parse_file(str(_write(tmp_path, PRIMARY))).main
        assert r.comms_blocks == 2  # one Bash report-back + one telegram reply
        assert r.comms_chars == len(TG_TEXT) + len(BASH_COMMS_CMD)
        assert r.comms_est_tokens == (len(TG_TEXT) + len(BASH_COMMS_CMD)) // 4

    def test_ignores_non_comms_bash(self, tmp_path):
        row = _turn(
            {
                "input_tokens": 1,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 1,
            },
            [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "ls -la /tmp && cat foo"},
                }
            ],
        )
        r = tu.parse_file(str(_write(tmp_path, [row]))).main
        assert r.comms_blocks == 0
        assert r.comms_chars == 0


class TestAggregation:
    def test_add_combines_across_files(self, tmp_path):
        a = tu.parse_file(str(_write(tmp_path, [_ROW1], "a.jsonl"))).main
        b = tu.parse_file(str(_write(tmp_path, [_ROW2], "b.jsonl"))).main
        agg = a + b
        assert agg.input_tokens == 150  # 100 + 50
        assert agg.turns == 2


class TestCli:
    def test_json_matches_hand_computed_sums(self, tmp_path):
        fixture = _write(tmp_path, PRIMARY)
        script = Path(tu.__file__)
        out = subprocess.run(
            [sys.executable, str(script), "--json", "--comms-share", str(fixture)],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(out.stdout)
        agg = data["aggregate"]["main"]
        assert agg["input_tokens"] == MAIN["inp"]
        assert agg["output_tokens"] == MAIN["out"]
        assert agg["cache_read_input_tokens"] == MAIN["cr"]
        assert agg["protocol_sensitive"] == MAIN["ps"]
        assert abs(agg["cost_weighted_total"] - MAIN["cw"]) < 1e-6
        assert (
            data["aggregate"]["with_sidechains"]["cache_read_input_tokens"]
            == COMBINED["cr"]
        )
        assert agg["comms_blocks"] == 2
