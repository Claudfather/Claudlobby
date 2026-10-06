"""Unit tests for the shared transcript-accounting owner, including the
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

from tests.conftest import write_jsonl
from claudlobby import transcript_usage as tu


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

    def test_repeated_message_id_counts_flat_usage_once_within_session(self, tmp_path):
        first = _turn({"input_tokens": 7, "output_tokens": 3}, [{"type": "text", "text": "a"}])
        first["message"]["id"] = "msg-one"
        second = json.loads(json.dumps(first))
        second["message"]["content"] = [{"type": "text", "text": "b"}]
        parsed = tu.parse_file(str(_write(tmp_path, [first, second])))
        assert parsed.main.turns == 1 and parsed.main.input_tokens == 7
        assert parsed.duplicate_messages == 1 and parsed.conflicting_duplicates == 0


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

    def test_canonical_writes_count_but_reads_and_setup_do_not(self, tmp_path):
        outbound = [
            'claudlobby --json message send --to worker --text "Please check" --request-id UUID',
            'claudlobby --root /tmp/root --fleet demo --json message reply msg_1 --text "Done" --request-id UUID',
            'claudlobby --json fleet reports submit --status completed --summary "Done" --request-id UUID',
            *(f'claudlobby --json assignment {verb} asg_1 --request-id UUID'
              for verb in ("deliver", "progress", "block", "return", "complete", "fail")),
        ]
        controls = [
            'claudlobby --json message show msg_1',
            'claudlobby --json assignment accept asg_1 --request-id UUID',
            'claudlobby --json task admit --title "New work" --request-id UUID',
            'claudlobby --json task assign task_1 --bot worker --request-id UUID',
            'claudlobby message send --help',
            'echo "claudlobby --json message send --to worker --text quoted"',
        ]
        row = _turn({"input_tokens": 1}, [
            {"type": "tool_use", "name": "Bash", "input": {"command": command}}
            for command in outbound + controls
        ])
        r = tu.parse_file(str(_write(tmp_path, [row]))).main
        assert r.comms_blocks == len(outbound)
        assert r.comms_chars == sum(len(command) for command in outbound)


class TestAggregation:
    def test_add_combines_across_files(self, tmp_path):
        a = tu.parse_file(str(_write(tmp_path, [_ROW1], "a.jsonl"))).main
        b = tu.parse_file(str(_write(tmp_path, [_ROW2], "b.jsonl"))).main
        agg = a + b
        assert agg.input_tokens == 150  # 100 + 50
        assert agg.turns == 2


class TestSelectedCoverage:
    """An idle bot's complete scan is observed zero; an unreadable source is not."""

    def _fleet(self, tmp_path, *bots):
        from types import SimpleNamespace

        fleet = SimpleNamespace(name="f", bots={b: SimpleNamespace(account=b) for b in bots},
                                accounts={b: str(tmp_path / "acct" / b) for b in bots})
        paths = SimpleNamespace(root=tmp_path, bot_runtime=lambda b: tmp_path / "runtime" / b)
        return fleet, paths

    def _window(self):
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc)
        return end - timedelta(hours=24), end

    def test_idle_directory_is_observed_zero_and_missing_is_unavailable(self, tmp_path):
        import os
        from claudlobby.isolation import transcript_slug

        fleet, paths = self._fleet(tmp_path, "idle", "gone")
        directory = (tmp_path / "acct" / "idle" / "projects"
                     / transcript_slug(paths.bot_runtime("idle")))
        directory.mkdir(parents=True)
        old = _write(directory, [_ROW1], "old.jsonl")
        os.utime(old, (0, 0))
        since, until = self._window()
        idle = tu.collect_bot_usage(paths, fleet, "idle", since, until)
        assert idle["coverage"]["status"] == "observed"
        assert idle["coverage"]["issues"] == []
        assert idle["coverage"]["older_files_excluded_by_mtime"] == 1
        assert idle["usage"]["input_tokens"] == 0 and idle["usage"]["turns"] == 0
        gone = tu.collect_bot_usage(paths, fleet, "gone", since, until)
        assert gone["coverage"]["status"] == "unavailable"
        fleet_row = tu.collect_fleet_usage(paths, fleet, since, until)
        assert fleet_row["coverage"]["status"] == "partial"
        assert fleet_row["coverage"]["bots_observed"] == 1

    def test_unavailable_usage_refuses_instead_of_zero(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        import pytest
        from claudlobby.command_result import CommandFailure
        from claudlobby.commands import checkin, usage_read
        from claudlobby import activation_state

        fleet, paths = self._fleet(tmp_path, "gone")
        selected = {"release_id": "r1"}
        monkeypatch.setattr(checkin, "_scope", lambda _args: (
            SimpleNamespace(paths=paths, fleet=fleet), None, selected, None))
        monkeypatch.setattr(activation_state, "read_selection", lambda _root: selected)
        for command, extra in (("fleet.usage", {}), ("bot.usage", {"bot_id": "gone"})):
            with pytest.raises(CommandFailure) as caught:
                usage_read.dispatch(SimpleNamespace(since="24h", public_command=command, **extra))
            assert caught.value.error.code == "unavailable"
            assert caught.value.data["usage"] is None
            assert caught.value.data["coverage"]["status"] == "unavailable"


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


# second sidechain turn (distinct usage) for the deeper workflow-subagent file
_SIDE2 = _turn(
    {
        "input_tokens": 3,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 200,
        "output_tokens": 7,
    },
    [{"type": "text", "text": "wf-subagent"}],
    sidechain=True,
)


class TestDirectoryWalk:
    """Subagent transcripts nest under <sess>/subagents/[workflows/wf_*/]agent-*.jsonl
    and carry isSidechain:true — the dir walk must recurse to find them, or ~13% of
    real spend is invisible."""

    def _nested_corpus(self, tmp_path):
        write_jsonl(tmp_path / "sess.jsonl", [_ROW1, _ROW2])  # main session (2 turns)
        sub = tmp_path / "sess" / "subagents"
        sub.mkdir(parents=True)
        write_jsonl(sub / "agent-1.jsonl", [_ROW3_SIDE])  # depth-1 subagent
        wf = sub / "workflows" / "wf_x"
        wf.mkdir(parents=True)
        write_jsonl(wf / "agent-2.jsonl", [_SIDE2])  # deeper workflow subagent
        return tmp_path

    def test_iter_transcripts_recurses_into_nested_subagents(self, tmp_path):
        self._nested_corpus(tmp_path)
        found = {Path(f).name for f in tu._iter_transcripts([str(tmp_path)])}
        assert found == {"sess.jsonl", "agent-1.jsonl", "agent-2.jsonl"}

    def test_cli_buckets_nested_subagents_as_sidechain(self, tmp_path):
        self._nested_corpus(tmp_path)
        out = subprocess.run(
            [sys.executable, str(Path(tu.__file__)), "--json", str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        agg = json.loads(out.stdout)["aggregate"]
        # main = sess.jsonl only (ROW1+ROW2): 2 turns, output 10+20=30
        assert agg["main"]["turns"] == 2
        assert agg["main"]["output_tokens"] == 30
        # with_sidechains folds in BOTH nested subagents: 2+1+1=4 turns, output 30+5+7=42
        assert agg["with_sidechains"]["turns"] == 4
        assert agg["with_sidechains"]["output_tokens"] == 42


class TestCommsShareCostWeighted:
    """Outbound-comms share is reported against cost_weighted_total ('% of spend'),
    matching the #729 read-out — and exercised in BOTH the JSON and printed paths."""

    def test_json_emits_cost_weighted_comms_share(self, tmp_path):
        fixture = _write(tmp_path, PRIMARY)
        out = subprocess.run(
            [
                sys.executable,
                str(Path(tu.__file__)),
                "--json",
                "--comms-share",
                str(fixture),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        agg = json.loads(out.stdout)["aggregate"]["main"]
        comms_est = (len(TG_TEXT) + len(BASH_COMMS_CMD)) // 4
        exp_cw = comms_est * 5.0  # comms text is generated output → output weight
        exp_share = 100.0 * exp_cw / MAIN["cw"]
        assert agg["comms_est_tokens"] == comms_est
        assert abs(agg["comms_cost_weighted"] - exp_cw) < 1e-6
        assert abs(agg["comms_share_of_cost_pct"] - exp_share) < 1e-6

    def test_printed_share_is_cost_weighted(self, tmp_path):
        fixture = _write(tmp_path, PRIMARY)
        out = subprocess.run(
            [sys.executable, str(Path(tu.__file__)), "--comms-share", str(fixture)],
            capture_output=True,
            text=True,
            check=True,
        )
        comms_est = (len(TG_TEXT) + len(BASH_COMMS_CMD)) // 4
        exp_share = 100.0 * comms_est * 5.0 / MAIN["cw"]
        assert f"{exp_share:.2f}% of spend" in out.stdout


# --- the live context and compaction rows (#2206) --------------------------


def _usage(ts, inp, cc, cr, out=9, *, sidechain=False, model=MODEL):
    row = _turn({"input_tokens": inp, "cache_creation_input_tokens": cc,
                 "cache_read_input_tokens": cr, "output_tokens": out},
                [{"type": "text", "text": "ok"}], sidechain=sidechain, model=model)
    row["timestamp"] = ts
    return row


def _said(ts, text="go on"):
    return {"type": "user", "isSidechain": False, "sessionId": "s1", "timestamp": ts,
            "message": {"role": "user", "content": text}}


def _boundary(ts, pre, post=None, trigger="manual", uuid="b1"):
    """A compact_boundary row: keys from a live 2.1 transcript, values invented."""
    meta = {"trigger": trigger, "preTokens": pre}
    if post is not None:
        meta["postTokens"] = post
    return {"parentUuid": None, "logicalParentUuid": "p0", "isSidechain": False,
            "type": "system", "subtype": "compact_boundary",
            "content": "Conversation compacted", "level": "info", "compactMetadata": meta,
            "uuid": uuid, "timestamp": ts, "sessionId": "s1"}


def _one_bot(tmp_path, bot="b"):
    """One bot whose account directory is under tmp_path, and where its
    current-cwd transcripts go."""
    from types import SimpleNamespace
    from claudlobby.isolation import transcript_slug

    fleet = SimpleNamespace(name="f", bots={bot: SimpleNamespace(account=bot)},
                            accounts={bot: str(tmp_path / "acct" / bot)})
    paths = SimpleNamespace(root=tmp_path, bot_runtime=lambda b: tmp_path / "runtime" / b)
    directory = tmp_path / "acct" / bot / "projects" / transcript_slug(paths.bot_runtime(bot))
    return fleet, paths, directory


def _session(directory, name, rows, *, mtime=None):
    import os

    directory.mkdir(parents=True, exist_ok=True)
    path = _write(directory, rows, name)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestCurrentContext:
    """#2206 proposal 1: a bot's live context is its newest main-chain usage
    row (input + cache read + cache creation) with that row's time; null with
    a reason, never 0, when no such row can be read."""

    def test_the_context_is_the_newest_main_chain_usage_row(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", [
            _usage("2026-10-06T20:00:00Z", 5, 100, 1000),
            _said("2026-10-06T20:00:30Z"),
            # 7 + 300 + 503079 = 503386; output (999) is not context
            _usage("2026-10-06T20:31:02.250Z", 7, 300, 503079, out=999),
            _said("2026-10-06T20:31:05Z", "x" * 5000)])
        ctx = tu.current_context(paths, fleet, "b")
        assert ctx["tokens"] == 503386
        assert ctx["at"] == "2026-10-06T20:31:02.250000+00:00"
        assert (ctx["reason"], ctx["session"], ctx["compacted_after"]) == (None, "s1", None)

    def test_a_sidechain_row_is_never_the_context(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", [_usage("2026-10-06T20:00:00Z", 1, 2, 3),
                                 _usage("2026-10-06T20:01:00Z", 50, 60, 70, sidechain=True)])
        assert tu.current_context(paths, fleet, "b")["tokens"] == 6  # 1 + 2 + 3

    def test_only_the_newest_top_level_transcript_is_read(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "old.jsonl", [_usage("2026-10-06T23:00:00Z", 9, 9, 900000)], mtime=1_000)
        _session(d, "new.jsonl", [_usage("2026-10-06T20:00:00Z", 1, 1, 18)], mtime=2_000)
        # A subagent's transcript nests below its session, newer than both.
        _session(d / "new" / "subagents", "agent-1.jsonl",
                 [_usage("2026-10-06T23:30:00Z", 4, 4, 400000)], mtime=3_000)
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["session"]) == (20, "new")

    def test_no_transcript_is_null_with_a_reason_never_zero(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        missing = tu.current_context(paths, fleet, "b")
        assert missing["tokens"] is None
        assert missing["reason"] == "transcript_directory_missing_or_untrusted"
        d.mkdir(parents=True)
        empty = tu.current_context(paths, fleet, "b")
        assert (empty["tokens"], empty["reason"]) == (None, "no_transcript")
        fleet.accounts["b"] = "relative/account"
        unresolved = tu.current_context(paths, fleet, "b")
        assert (unresolved["tokens"], unresolved["reason"]) == (
            None, "account_directory_unresolved")

    def test_a_session_without_a_usage_row_is_null_not_zero(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", [_said("2026-10-06T20:00:00Z")])
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["reason"]) == (None, "no_main_usage_row_in_transcript")

    def test_a_synthetic_zero_usage_row_is_not_the_context(self, tmp_path):
        """Claude Code writes its own assistant row for an error or an
        interrupt (model <synthetic>, every usage field 0). It is no API call,
        so it is no context; three of four real sessions measured hold one."""
        fleet, paths, d = _one_bot(tmp_path)
        synthetic = _usage("2026-10-06T20:01:00Z", 0, 0, 0, out=0, model="<synthetic>")
        _session(d, "s1.jsonl", [_usage("2026-10-06T20:00:00Z", 2, 3, 4), synthetic])
        assert tu.current_context(paths, fleet, "b")["tokens"] == 9
        _session(d, "s2.jsonl", [synthetic], mtime=4_000_000_000)
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["session"]) == (None, "s2")

    def test_the_read_is_bounded_and_runs_from_the_end(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        path = _session(d, "s1.jsonl", [_said("2026-10-06T19:00:00Z", "y" * 200_000),
                                        _usage("2026-10-06T20:00:00Z", 1, 2, 3),
                                        _said("2026-10-06T20:00:01Z", "z" * 3000)])
        found = tu.current_context(paths, fleet, "b")
        assert found["tokens"] == 6
        assert found["bytes_read"] < path.stat().st_size  # the 200 KB head is never read
        capped = tu.current_context(paths, fleet, "b", cap=1024)
        assert (capped["tokens"], capped["reason"]) == (None, "no_main_usage_row_within_cap")
        assert capped["bytes_read"] <= 1024

    def test_a_compaction_after_the_newest_usage_row_is_named(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        before = _usage("2026-10-06T20:31:02Z", 7, 300, 503079)
        boundary = _boundary("2026-10-06T20:33:29.519Z", 503386, 17666)
        _session(d, "s1.jsonl", [before, boundary])
        ctx = tu.current_context(paths, fleet, "b")
        assert ctx["tokens"] == 503386
        assert ctx["compacted_after"] == {"at": "2026-10-06T20:33:29.519000+00:00",
                                          "trigger": "manual", "pre_tokens": 503386,
                                          "post_tokens": 17666}
        # Once a call follows the compaction, that call is the context.
        _session(d, "s1.jsonl", [before, boundary, _usage("2026-10-06T20:34:00Z", 3, 17000, 900)])
        later = tu.current_context(paths, fleet, "b")
        assert (later["tokens"], later["compacted_after"]) == (17903, None)

    def test_an_unreadable_transcript_is_null_with_a_reason(self, tmp_path):
        import os
        import pytest

        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        fleet, paths, d = _one_bot(tmp_path)
        path = _session(d, "s1.jsonl", [_usage("2026-10-06T20:00:00Z", 1, 2, 3)])
        path.chmod(0)
        try:
            ctx = tu.current_context(paths, fleet, "b")
        finally:
            path.chmod(0o600)
        assert (ctx["tokens"], ctx["reason"]) == (None, "unreadable_transcript_file")


class TestCompactionRow:
    def test_a_boundary_row_reads_trigger_tokens_and_time(self):
        row = tu.compaction_row(_boundary("2026-10-06T20:33:29.519Z", 503386, 17666, uuid="u9"))
        assert row == {"at": "2026-10-06T20:33:29.519000+00:00", "trigger": "manual",
                       "pre_tokens": 503386, "post_tokens": 17666, "session": "s1", "uuid": "u9"}

    def test_an_unfinished_boundary_reads_post_tokens_null_not_zero(self):
        assert tu.compaction_row(_boundary("2026-10-06T20:33:29Z", 503386))["post_tokens"] is None

    def test_other_rows_are_not_compactions(self):
        assert tu.compaction_row(_usage("2026-10-06T20:00:00Z", 1, 2, 3)) is None
        assert tu.compaction_row({"type": "system", "subtype": "api_error"}) is None
        undated = _boundary("2026-10-06T20:33:29Z", 1, 2)
        del undated["timestamp"]
        assert tu.compaction_row(undated) is None
