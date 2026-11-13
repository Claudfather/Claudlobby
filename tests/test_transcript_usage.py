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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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

    def test_idle_directory_is_observed_zero_and_missing_is_unavailable(self, tmp_path):
        import os
        from claudlobby.isolation import transcript_slug

        fleet, paths = self._fleet(tmp_path, "idle", "gone")
        directory = (tmp_path / "acct" / "idle" / "projects"
                     / transcript_slug(paths.bot_runtime("idle")))
        directory.mkdir(parents=True)
        old = _write(directory, [_ROW1], "old.jsonl")
        os.utime(old, (0, 0))
        since, until = _last_day()
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
        from claudlobby import active_config

        fleet, paths = self._fleet(tmp_path, "gone")
        from claudlobby.active_config import RuntimeReadStamp
        from tests.package_fixtures import git_package
        paths.package = git_package(tmp_path)
        stamp = RuntimeReadStamp("git", source_revision=paths.package.source_revision,
                                 config_fingerprint="c" * 64)
        monkeypatch.setattr(checkin, "_scope", lambda _args: (
            SimpleNamespace(paths=paths, fleet=fleet), None, stamp, None))
        monkeypatch.setattr(active_config, "runtime_read_stamp", lambda *_args, **_kw: stamp)
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


def _cli(rows):
    """An interactive session: Claude Code names `entrypoint` cli on its rows
    from the first attachment on; the header rows before it name none (row keys
    from a live 2.1 transcript, values invented)."""
    head = [{"type": "mode", "mode": "normal", "sessionId": "s1"},
            {"parentUuid": None, "isSidechain": False, "attachment": {"type": "x"},
             "type": "attachment", "uuid": "a1", "timestamp": rows[0]["timestamp"],
             "userType": "external", "entrypoint": "cli", "sessionId": "s1"}]
    return head + [{**row, "entrypoint": "cli"} for row in rows]


def _headless(ts, prompt="summarize the segment", *, cr=35188):
    """A hook's detached `claude -p` session: two queue rows, the prompt row
    naming `entrypoint` sdk-cli after its message, then one small call (row keys
    from a live 2.1 transcript, values invented). The prompt rides the enqueue
    row and the message, both ahead of the first `entrypoint`."""
    return [{"type": "queue-operation", "operation": "enqueue", "timestamp": ts,
             "sessionId": "h1", "content": prompt},
            {"type": "queue-operation", "operation": "dequeue", "timestamp": ts,
             "sessionId": "h1"},
            {"parentUuid": None, "isSidechain": False, "promptId": "p1", "type": "user",
             "message": {"role": "user", "content": prompt}, "uuid": "u1", "timestamp": ts,
             "permissionMode": "default", "promptSource": "sdk", "turnOrigin": "sdk",
             "userType": "external", "entrypoint": "sdk-cli", "sessionId": "h1"},
            {**_usage(ts, 21, 0, cr), "entrypoint": "sdk-cli", "sessionId": "h1"}]


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

    def test_a_hooks_headless_session_is_never_the_context(self, tmp_path):
        """#2212 review, finding 1: a hook's detached `claude -p` session lands
        beside the bot's own and stays the newest file for minutes. Its prompt
        rides ahead of its first `entrypoint`, here past 64 KiB, as in the
        larger real ones."""
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _cli([_usage("2026-10-07T21:37:13Z", 9, 1200, 443091)]),
                 mtime=1_000)
        _session(d, "h1.jsonl", _headless("2026-10-07T21:37:39Z", "p" * 70_000), mtime=2_000)
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["session"], ctx["reason"]) == (444300, "s1", None)

    def test_headless_sessions_alone_have_no_transcript(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "h1.jsonl", _headless("2026-10-07T21:37:39Z"))
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["reason"]) == (None, "no_transcript")

    def test_a_session_naming_no_entrypoint_is_the_bots_own(self, tmp_path):
        """A transcript without the field (an older Claude Code, or a session
        whose first rows are still to come) counts as the bot's own."""
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _cli([_usage("2026-10-07T21:00:00Z", 1, 2, 3)]), mtime=1_000)
        _session(d, "s2.jsonl", [_usage("2026-10-07T21:10:00Z", 4, 5, 6)], mtime=2_000)
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["session"]) == (15, "s2")

    def test_a_newer_session_that_cannot_be_told_apart_is_no_answer(self, tmp_path):
        """Whether a newer session is the bot's is unknown when its head runs
        past the cap before naming an entrypoint, or ends in a line still
        being written: no answer then, never the older session's figure."""
        import os

        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _cli([_usage("2026-10-07T21:00:00Z", 1, 2, 3)]), mtime=1_000)
        _session(d, "h1.jsonl", _headless("2026-10-07T21:10:00Z", "p" * 10_000), mtime=2_000)
        capped = tu.current_context(paths, fleet, "b", head_cap=4096)
        assert (capped["tokens"], capped["reason"]) == (None, "session_entrypoint_beyond_cap")
        assert tu.current_context(paths, fleet, "b")["session"] == "s1"
        partial = d / "s3.jsonl"
        partial.write_text(json.dumps(_headless("2026-10-07T21:20:00Z")[0])[:40])
        os.utime(partial, (3_000, 3_000))
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["reason"]) == (None, "session_head_incomplete")

    def test_an_unreadable_newer_session_is_no_answer(self, tmp_path):
        import os
        import pytest

        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _cli([_usage("2026-10-07T21:00:00Z", 1, 2, 3)]), mtime=1_000)
        newer = _session(d, "s2.jsonl", [_usage("2026-10-07T21:10:00Z", 4, 5, 6)], mtime=2_000)
        newer.chmod(0)
        try:
            ctx = tu.current_context(paths, fleet, "b")
        finally:
            newer.chmod(0o600)
        assert (ctx["tokens"], ctx["reason"]) == (None, "unreadable_transcript_file")

    def test_the_session_walk_examines_at_most_its_limit(self, tmp_path, monkeypatch):
        """#2212 review, the walk's bound: each session newer than the bot's
        own costs a read of its head, so the walk examines at most
        SESSION_WALK_LIMIT of the newest. When all of those are headless and
        an older session is left, that is no answer, never the older one's
        figure; when none is left, there is none."""
        limit = tu.SESSION_WALK_LIMIT
        heads = []
        first_entrypoint = tu._first_entrypoint

        def counted(fh, size, cap):
            heads.append(fh.name)
            return first_entrypoint(fh, size, cap)

        monkeypatch.setattr(tu, "_first_entrypoint", counted)
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _cli([_usage("2026-10-07T21:00:00Z", 1, 2, 3)]), mtime=1_000)
        for n in range(1, limit):
            _session(d, f"h{n}.jsonl", _headless("2026-10-07T21:10:00Z"), mtime=1_000 + n)
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["session"], len(heads)) == (6, "s1", limit)
        heads.clear()
        _session(d, f"h{limit}.jsonl", _headless("2026-10-07T21:10:00Z"), mtime=1_000 + limit)
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["reason"], len(heads)) == (
            None, "no_own_session_within_limit", limit)
        heads.clear()
        (d / "s1.jsonl").unlink()
        ctx = tu.current_context(paths, fleet, "b")
        assert (ctx["tokens"], ctx["reason"], len(heads)) == (None, "no_transcript", limit)


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


# --- the usage read runs back from each transcript's end (#717) -------------


def _last_day():
    """The window `bot usage --since 24h` reads: the 24 hours to now."""
    end = datetime.now(timezone.utc)
    return end - timedelta(hours=24), end


def _at(hours):
    """A row time ``hours`` from now, as Claude Code writes it."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _call(ts, mid, inp, cr, out=9, *, text="ok", sidechain=False):
    """One identified assistant call: ``_usage`` with a message id."""
    row = _usage(ts, inp, 0, cr, out, sidechain=sidechain)
    row["message"]["id"] = mid
    row["message"]["content"] = [{"type": "text", "text": text}]
    return row


class TestWindowRead:
    """`bot usage` reads each transcript from its end back to the window's
    start, so a transcript past the byte limit costs what its window costs,
    and the coverage says what was read and what was not (#717)."""

    def test_a_transcript_past_the_byte_limit_is_read_back_to_the_window_start(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(tu, "MAX_BYTES", 128 * 1024)
        fleet, paths, d = _one_bot(tmp_path)
        d.mkdir(parents=True)
        path = d / "s1.jsonl"
        old = [_call(_at(-72), f"old{i}", 1, 1, text="x" * 10_000) for i in range(30)]
        with open(path, "w") as fh:
            fh.write('{"type": "assistant", "cut off mid-row\n')  # never read: older than the window
            for row in old + [_call(_at(-3), "m1", 100, 1000), _said(_at(-2)),
                              _call(_at(-2), "m2", 20, 2000), _call(_at(-1), "m3", 3, 3000)]:
                fh.write(json.dumps(row) + "\n")
        size = path.stat().st_size
        assert size > tu.MAX_BYTES
        since, until = _last_day()
        row = tu.collect_bot_usage(paths, fleet, "b", since, until)
        coverage = row["coverage"]
        assert coverage["status"] == "observed" and coverage["issues"] == []
        assert row["usage"]["turns"] == 3
        assert row["usage"]["input_tokens"] == 123  # 100 + 20 + 3
        assert row["usage"]["cache_read_input_tokens"] == 6000  # 1000 + 2000 + 3000
        # One 64 KiB block holds the three calls and the newest older row.
        assert coverage["bytes_read"] == 64 * 1024
        assert coverage["bytes_before_window"] == size - coverage["bytes_read"]
        assert coverage["bytes_unread_at_limit"] == 0
        assert coverage["diagnostics"]["malformed_lines"] == 0
        assert coverage["limits"]["bytes"] == 128 * 1024

    def test_the_window_read_counts_what_a_whole_file_read_counts(self, tmp_path):
        rows = [_call(_at(-72), f"old{i}", 1, 1, text="x" * 20_000) for i in range(10)]
        rows += [_said(_at(-5)),
                 _call(_at(-5), "m1", 10, 100),
                 _call(_at(-5), "m1", 10, 100),  # a second content-block line of m1
                 _call(_at(-4), "m2", 7, 70, out=5),
                 _call(_at(-4), "m2", 7, 70, out=50),  # same message, other usage: the first counts
                 _call(_at(-4), "s1", 2, 20, sidechain=True),
                 _call(_at(-3), "m3", 1, 10, text="y" * 150_000)]  # one row across three blocks
        tg = _call(_at(-3), "m4", 4, 40)
        tg["message"]["content"] = [{"type": "tool_use", "name": "mcp__plugin_telegram_telegram__reply",
                                     "input": {"chat_id": "x", "text": TG_TEXT}}]
        bash = _call(_at(-3), "m4", 4, 40)  # m4's second line carries a canonical report
        bash["message"]["content"] = [{"type": "tool_use", "name": "Bash", "input": {
            "command": "claudlobby --json assignment progress asg_1 --summary go --request-id u"}}]
        no_usage = _call(_at(-2), "m5", 1, 1)
        no_usage["message"].pop("usage")
        unkeyed = _call(_at(-2), "m6", 5, 50)
        unkeyed["message"].pop("id")
        untimed = _call(_at(-2), "m7", 6, 60)
        untimed.pop("timestamp")
        rows += [tg, bash, no_usage, unkeyed, untimed, _call(_at(1), "later", 9, 90)]
        path = tmp_path / "t.jsonl"
        with open(path, "w") as fh:
            for row in rows[:-1]:
                fh.write(json.dumps(row) + "\n")
            fh.write('{"type": "assistant", "cut off mid-row\n')
            fh.write(json.dumps(rows[-1]) + "\n")
        since, until = _last_day()
        whole = tu.parse_file(str(path), since=since, until=until)
        size = path.stat().st_size
        window, taken, ended = tu.parse_window(str(path), size, since, until, tu.MAX_BYTES)
        assert ended == "window_start" and taken < size
        for bucket in ("main", "sidechain"):
            assert (getattr(window, bucket).to_dict(include_comms=True)
                    == getattr(whole, bucket).to_dict(include_comms=True))
        for name in ("malformed_lines", "missing_timestamps", "missing_usage", "unkeyed_turns",
                     "duplicate_messages", "conflicting_duplicates"):
            assert getattr(window, name) == getattr(whole, name), name
        # The arithmetic both reads agree on: m1, m2 (its first line), m3, m4 and
        # the unkeyed call; m5 has no usage and m7 no time.
        assert window.main.turns == 5 and window.main.output_tokens == 9 + 5 + 9 + 9 + 9
        assert window.main.input_tokens == 10 + 7 + 1 + 4 + 5
        assert window.sidechain.turns == 1 and window.main.comms_blocks == 2
        assert (window.duplicate_messages, window.conflicting_duplicates) == (3, 1)
        assert (window.malformed_lines, window.missing_usage, window.missing_timestamps,
                window.unkeyed_turns) == (1, 1, 1, 1)

    def test_the_byte_limit_leaves_a_partial_count_that_says_what_went_unread(
            self, tmp_path, monkeypatch):
        import time

        monkeypatch.setattr(tu, "MAX_BYTES", 128 * 1024)
        fleet, paths, d = _one_bot(tmp_path)
        sizes = {}
        # Named against their write order: the most recently written is read first.
        for name, count, age in (("a", 5, 300), ("b", 10, 200), ("c", 10, 100)):
            path = _session(d, f"{name}.jsonl", [
                _call(_at(-2), f"{name}{i}", 1, 10, text="z" * 10_000) for i in range(count)],
                mtime=time.time() - age)
            sizes[name] = path.stat().st_size
        since, until = _last_day()
        row = tu.collect_bot_usage(paths, fleet, "b", since, until)
        coverage = row["coverage"]
        assert coverage["status"] == "partial"
        assert coverage["issues"] == ["byte_limit_reached"]
        # c is read whole, b from its end until the limit, a, the oldest, not at all.
        assert coverage["files_read"] == 2 and coverage["files_skipped_at_least"] == 1
        assert coverage["bytes_read"] == 128 * 1024
        assert coverage["bytes_unread_at_limit"] == sum(sizes.values()) - 128 * 1024
        assert coverage["additional_files_after_limit"] == 0  # every file was listed
        assert 10 < row["usage"]["turns"] < 20  # all of a and the newest of b

    def test_bytes_written_after_the_listing_are_not_read(self, tmp_path, monkeypatch):
        fleet, paths, d = _one_bot(tmp_path)
        path = _session(d, "s1.jsonl", [_call(_at(-2), "m1", 1, 10), _call(_at(-1), "m2", 2, 20)])
        listed = path.stat().st_size
        real = tu._bounded_files

        def listed_then_written(directory, since):
            found = real(directory, since)
            with open(path, "a") as fh:  # the bot writes on after the listing
                fh.write(json.dumps(_call(_at(1), "m3", 3, 30)) + "\n")
            return found

        monkeypatch.setattr(tu, "_bounded_files", listed_then_written)
        since, until = _last_day()
        row = tu.collect_bot_usage(paths, fleet, "b", since, until)
        assert row["coverage"]["status"] == "observed" and row["coverage"]["issues"] == []
        assert row["coverage"]["bytes_read"] == listed
        assert row["usage"]["turns"] == 2

    def test_the_file_limit_is_named_apart_from_the_byte_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tu, "MAX_FILES", 1)
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "a.jsonl", [_call(_at(-2), "a1", 1, 10)])
        _session(d, "b.jsonl", [_call(_at(-2), "b1", 1, 10)])
        since, until = _last_day()
        coverage = tu.collect_bot_usage(paths, fleet, "b", since, until)["coverage"]
        assert coverage["issues"] == ["file_limit_reached"]
        assert coverage["files_read"] == 1 and coverage["additional_files_after_limit"] == "unknown"


# --- the per-call view: what each call's context held (#2909) ---------------


def _tool_call(ts, mid, inp, cr, blocks, out=9):
    """One row of call ``mid`` holding ``blocks``: ``_call`` with other content."""
    row = _call(ts, mid, inp, cr, out)
    row["message"]["content"] = blocks
    return row


def _result(ts, tool_use_id, text):
    return {"type": "user", "isSidechain": False, "sessionId": "s1", "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]}}


def _attached(ts, attachment, text=None):
    """An attachment row; ``text`` is what Claude Code rendered for it (row keys
    and the ``rendered`` shape from a live 2.1 transcript, values invented)."""
    row = {"type": "attachment", "isSidechain": False, "sessionId": "s1", "timestamp": ts,
           "attachment": attachment}
    if text is not None:
        row["rendered"] = [{"content": text}]
        row["renderedRole"] = "user"
    return row


def _synthetic(ts, text):
    """A row Claude Code writes itself for an interrupt: zero usage, so no call."""
    row = _call(ts, "synthetic1", 0, 0, 0, text=text)
    row["message"]["model"] = "<synthetic>"
    return row


READ = {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/w/repo/a.py"}}
BASH = {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "echo SECRET-1234"}}


def _view_session():
    """Five calls of one interactive session, oldest first, with a compaction
    between the third and the fourth; TestCallView's first test spells out
    what each call shows."""
    return _cli([
        _said(_at(-3), "start"),
        _attached(_at(-3), {"type": "nested_memory", "path": "/w/repo/CLAUDE.md",
                            "content": "c"}, "R" * 3000),
        _tool_call(_at(-2.9), "m1", 100, 1000, [{"type": "text", "text": "plan"}, READ]),
        _result(_at(-2.9), "t1", "y" * 500),
        _attached(_at(-2.9), {"type": "hook_success", "hookName": "PreToolUse:Read",
                              "stdout": "z" * 50}),
        _tool_call(_at(-2.8), "m2", 20, 3000, [{"type": "text", "text": "x"}], out=5),
        _tool_call(_at(-2.8), "m2", 20, 3000, [BASH], out=40),
        _result(_at(-2.8), "t2", "ok"),
        _call(_at(-2.7), "m3", 30, 3100),
        _boundary(_at(-2.6), 3130, 900),
        {**_said(_at(-2.6), "S" * 700), "isCompactSummary": True},
        _attached(_at(-2.6), {"type": "instructions"}, "I" * 2000),
        _call(_at(-2.5), "m4", 10, 900),
        _call(_at(-2.5), "side", 1, 1, sidechain=True),
        _synthetic(_at(-2.4), "Request interrupted"),
        _call(_at(-2.3), "m5", 5, 950)])


def _group(kind, target, chars, count=1, images=0):
    return {"kind": kind, "target": target, "count": count, "chars": chars, "images": images}


class TestCallView:
    """`bot usage --calls` (#2909): the newest main-chain calls of a bot's own
    session, each with its context, its growth over the previous call and the
    rows the transcript stored before it, bounded by a call limit, a group cap
    and a byte cap that the result names when it cuts."""

    def test_each_call_shows_its_context_growth_and_the_rows_stored_before_it(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _view_session())
        since, until = _last_day()
        view = tu.collect_bot_calls(paths, fleet, "b", since, until)
        assert (view["session"], view["ended"], view["complete"]) == ("s1", "session_start", True)
        calls = {call["message"]: call for call in view["calls"]}
        # Oldest first; the sidechain row and the zero-usage synthetic row are no calls.
        assert [call["message"] for call in view["calls"]] == ["m1", "m2", "m3", "m4", "m5"]
        # context = input + cache read + cache write; growth over the call before.
        assert [(call["context"], call["growth"]) for call in view["calls"]] == [
            (1100, None), (3020, 1920), (3130, 110), (910, None), (955, 45)]
        assert calls["m2"]["output_tokens"] == 40  # the largest of m2's two rows (5, 40)
        assert [call["after_compaction"] for call in view["calls"]] == [
            False, False, False, True, False]
        # m1 holds the session's opening rows; the header's record-only
        # attachment is tallied apart, since it stores no rendered text.
        assert calls["m1"]["step"]["groups"] == [
            _group("attachment:nested_memory", "/w/repo/CLAUDE.md", 3000),
            _group("user", "text", 5)]
        assert calls["m1"]["step"]["unrendered"] == {"count": 1, "chars": 0}
        # m2: m1's own output and the Read result, each named by its file; the
        # hook record is 15 + 50 characters of fields.
        assert calls["m2"]["step"]["groups"] == [
            _group("tool_result", "Read /w/repo/a.py", 500),
            _group("output", "Read /w/repo/a.py", len('{"file_path": "/w/repo/a.py"}')),
            _group("output", "text", 4)]
        assert calls["m2"]["step"]["unrendered"] == {"count": 1, "chars": 65}
        assert (calls["m2"]["step"]["count"], calls["m2"]["step"]["chars"]) == (3, 533)
        # m3: a Bash call is named by its tool alone; its command is never reported.
        assert calls["m3"]["step"]["groups"] == [
            _group("output", "Bash", len('{"command": "echo SECRET-1234"}')),
            _group("tool_result", "Bash", 2), _group("output", "text", 1)]
        assert "SECRET-1234" not in json.dumps(view)
        # m4 follows the compaction: only the rows after its boundary row.
        assert calls["m4"]["step"]["groups"] == [
            _group("attachment:instructions", None, 2000),
            _group("user", "compaction_summary", 700)]
        # m5: m4's output and the synthetic row's text, one group of two parts.
        assert calls["m5"]["step"]["groups"] == [_group("output", "text", 21, count=2)]
        assert (view["limit"], view["cap"], view["groups_per_call"]) == (
            tu.CALLS_DEFAULT, tu.CALLS_TAIL_BYTES, tu.CALL_GROUPS)

    def test_the_newest_call_reads_as_the_live_context(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _view_session())
        since, until = _last_day()
        view = tu.collect_bot_calls(paths, fleet, "b", since, until)
        assert view["calls"][-1]["context"] == tu.current_context(paths, fleet, "b")["tokens"] == 955

    def test_the_call_limit_keeps_the_newest_and_says_older_calls_are_cut(self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _view_session())
        since, until = _last_day()
        view = tu.calls_in(d, since, until, limit=2)
        assert [call["message"] for call in view["calls"]] == ["m4", "m5"]
        assert (view["ended"], view["complete"]) == ("call_limit", False)
        assert [call["growth"] for call in view["calls"]] == [None, 45]

    def test_a_call_before_the_window_is_read_only_as_the_previous_call(self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        path = _session(d, "s1.jsonl", _cli([
            _said(_at(-31), "x" * 200_000),
            _call(_at(-30), "old1", 1, 100), _said(_at(-30), "x" * 100),
            _call(_at(-29), "old2", 1, 200), _said(_at(-2), "go"),
            _call(_at(-2), "new1", 1, 260), _call(_at(-1), "new2", 1, 300)]))
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        assert [call["message"] for call in view["calls"]] == ["new1", "new2"]
        assert (view["ended"], view["complete"]) == ("window_start", True)
        # The read stops at old1, the call before old2, so the older rows are
        # never read: one 64 KiB block holds everything from old1 on.
        assert view["bytes_read"] == 64 * 1024 < path.stat().st_size
        # new1 grew over old2, the call before it, which is older than the window.
        assert [call["growth"] for call in view["calls"]] == [60, 40]
        assert view["calls"][0]["step"]["groups"] == [_group("output", "text", 2),
                                                      _group("user", "text", 2)]

    def test_the_byte_cap_stops_the_read_and_every_call_shown_has_its_whole_step(
            self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        rows = []
        for index in range(8):
            rows += [_call(_at(-3 + index / 10), f"c{index}", 1, 1000 + 100 * index),
                     _said(_at(-3 + index / 10), "u" * 10_000)]
        d.mkdir(parents=True)
        path, starts, cap = d / "s1.jsonl", {}, 64 * 1024
        with open(path, "wb") as fh:
            for row in _cli(rows):
                if row["type"] == "assistant":
                    starts[row["message"]["id"]] = fh.tell()
                fh.write((json.dumps(row) + "\n").encode())
        since, until = _last_day()
        view = tu.calls_in(d, since, until, cap=cap)
        assert (view["ended"], view["complete"], view["bytes_read"]) == ("byte_cap", False, cap)
        # The read takes each whole line inside the last 64 KiB. The two oldest
        # calls it takes are previous calls only, so each call shown has its
        # whole step and its growth.
        taken = [mid for mid, start in starts.items() if start > path.stat().st_size - cap]
        assert len(taken) > 3
        assert [call["message"] for call in view["calls"]] == taken[2:]
        assert {call["growth"] for call in view["calls"]} == {100}
        assert view["calls"][0]["step"]["groups"] == [_group("user", "text", 10_000),
                                                      _group("output", "text", 2)]

    def test_groups_past_the_cap_are_summed(self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        files = [{"type": "tool_use", "id": f"r{n}", "name": "Read",
                  "input": {"file_path": f"/w/{n}.py"}} for n in range(7)]
        rows = [_tool_call(_at(-2), "m1", 1, 100, files)]
        rows += [_result(_at(-2), f"r{n}", "q" * (1000 * (n + 1))) for n in range(7)]
        rows += [_call(_at(-1), "m2", 1, 9000)]
        _session(d, "s1.jsonl", _cli(rows))
        since, until = _last_day()
        step = tu.calls_in(d, since, until)["calls"][-1]["step"]
        assert [group["chars"] for group in step["groups"]] == [7000, 6000, 5000, 4000, 3000]
        # Seven results and seven calls make 14 groups: 9 cut, the two
        # smallest results (2000 + 1000) and every Read call's input among them.
        reads = sum(len(json.dumps(block["input"])) for block in files)
        assert (step["groups_cut"], step["chars_cut"]) == (9, 2000 + 1000 + reads)
        assert step["chars"] == 28_000 + reads and step["count"] == 14

    def test_a_newer_headless_session_is_passed_over(self, tmp_path):
        import time

        _fleet, _paths, d = _one_bot(tmp_path)
        _session(d, "own.jsonl", _cli([_call(_at(-2), "m1", 1, 100)]), mtime=time.time() - 60)
        _session(d, "hook.jsonl", _headless(_at(-1)))
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        assert view["session"] == "own"
        assert [call["message"] for call in view["calls"]] == ["m1"]

    def test_no_transcript_is_a_reason_not_an_empty_view(self, tmp_path):
        fleet, paths, d = _one_bot(tmp_path)
        since, until = _last_day()
        assert tu.collect_bot_calls(paths, fleet, "b", since, until)["reason"] == (
            "transcript_directory_missing_or_untrusted")
        d.mkdir(parents=True)
        view = tu.calls_in(d, since, until)
        assert (view["reason"], view["calls"], view["ended"]) == ("no_transcript", [], None)

    def test_the_text_lines_say_what_each_call_held_and_where_the_read_stopped(
            self, tmp_path):
        from claudlobby.commands.usage_read import call_lines

        _fleet, _paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _view_session())
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        lines = call_lines(view)
        assert len(lines) == 7
        assert lines[2].endswith(
            "context 3020 (+1920), output 40: tool_result Read /w/repo/a.py 500; "
            "output Read /w/repo/a.py 29; output text 4; unrendered attachment rows x1 65")
        assert lines[4].endswith("context 910 (after compaction), output 9: "
                                 "attachment:instructions - 2000; user compaction_summary 700")
        assert lines[-1] == (f"calls read: ended=session_start, every call in the window is "
                             f"shown; limit=20, bytes={view['bytes_read']} of "
                             f"{tu.CALLS_TAIL_BYTES}; reason=-")

    def test_the_flag_is_on_bot_usage_only_and_its_help_names_the_caps(self, capsys):
        import argparse
        import pytest
        from claudlobby.commands._orientation_parsers import register_orientation_subparsers

        parser = argparse.ArgumentParser()
        register_orientation_subparsers(parser.add_subparsers())
        assert parser.parse_args(["bot", "usage", "b"]).calls is None
        assert parser.parse_args(["bot", "usage", "b", "--calls"]).calls == tu.CALLS_DEFAULT
        assert parser.parse_args(["bot", "usage", "b", "--calls", "7"]).calls == 7
        with pytest.raises(SystemExit):
            parser.parse_args(["fleet", "usage", "--calls", "7"])
        capsys.readouterr()
        with pytest.raises(SystemExit):
            parser.parse_args(["bot", "usage", "--help"])
        text = " ".join(capsys.readouterr().out.split())
        assert (f"({tu.CALLS_DEFAULT} when N is left out, at most {tu.CALLS_MAX})" in text
                and f"the {tu.CALL_GROUPS} largest groups" in text
                and f"most {tu.CALLS_TAIL_BYTES // 2 ** 20} MiB back" in text)

    def test_bot_usage_adds_the_view_and_refuses_a_limit_out_of_range(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        import pytest
        from claudlobby.command_result import CommandFailure
        from claudlobby.commands import checkin, usage_read
        from claudlobby import active_config
        from claudlobby.active_config import RuntimeReadStamp
        from tests.package_fixtures import git_package

        fleet, paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _view_session())
        paths.package = git_package(tmp_path)
        stamp = RuntimeReadStamp("git", source_revision=paths.package.source_revision,
                                 config_fingerprint="c" * 64)
        monkeypatch.setattr(checkin, "_scope", lambda _args: (
            SimpleNamespace(paths=paths, fleet=fleet), None, stamp, None))
        monkeypatch.setattr(active_config, "runtime_read_stamp", lambda *_args, **_kw: stamp)
        args = SimpleNamespace(since="24h", public_command="bot.usage", bot_id="b", calls=2)
        out = usage_read.dispatch(args)
        assert [call["message"] for call in out.data["calls"]["calls"]] == ["m4", "m5"]
        # The totals read is unchanged: m1 to m5 and the zero-usage synthetic
        # row on the main chain, the one sidechain row apart.
        assert (out.data["main"]["turns"], out.data["sidechain"]["turns"]) == (6, 1)
        assert out.lines[1].startswith("calls: 2 main-chain calls of session s1")
        assert "calls" not in usage_read.dispatch(
            SimpleNamespace(since="24h", public_command="bot.usage", bot_id="b")).data
        for calls in (0, tu.CALLS_MAX + 1):
            with pytest.raises(CommandFailure) as caught:
                usage_read.dispatch(SimpleNamespace(since="24h", public_command="bot.usage",
                                                    bot_id="b", calls=calls))
            assert caught.value.error.code == "invalid_argument"


# --- shapes the view and the live context read apart, or alike (#2909) ------


def _line(row) -> bytes:
    return (json.dumps(row) + "\n").encode()


def _raw_session(directory, lines, name="s1.jsonl"):
    """A session written as raw lines, for what no JSON writer produces: a
    byte that is not UTF-8, a line that is not JSON, a last line cut short."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"".join(lines))
    return path


class TestCallViewShapes:
    """Transcript shapes pinned as the per-call view (`calls_in`) and the live
    context (`context_in`, the figure `fleet status` shows) read each today,
    so a change to either reader shows here."""

    def test_a_bad_byte_in_the_newest_row_is_a_call_but_not_the_live_context(self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        lines = [_line(row) for row in _cli([_call(_at(-2), "m1", 1, 100),
                                             _call(_at(-1), "m2", 1, 200, text="BAD")])]
        lines[-1] = lines[-1].replace(b"BAD", b"\xff")
        _raw_session(d, lines)
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        # The view decodes the line with replacement, as the window totals do,
        # so m2 is a call; the live context cannot decode it and reads m1.
        assert [(call["message"], call["context"]) for call in view["calls"]] == [
            ("m1", 101), ("m2", 201)]
        assert view["diagnostics"]["malformed_lines"] == 0
        assert tu.context_in(d)["tokens"] == 101

    def test_a_newest_usage_row_with_no_message_id_is_the_live_context_but_no_call(
            self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _cli([_call(_at(-2), "m1", 1, 100), _usage(_at(-1), 1, 0, 200)]))
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        # The view groups rows into calls by message id, so the row is no call;
        # the live context reads its usage alone.
        assert [(call["message"], call["context"]) for call in view["calls"]] == [("m1", 101)]
        assert tu.context_in(d)["tokens"] == 201

    def test_a_newest_call_with_no_time_empties_the_view_and_the_live_context_skips_it(
            self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        untimed = _call(_at(-1), "m2", 1, 200)
        del untimed["timestamp"]
        _session(d, "s1.jsonl", _cli([_call(_at(-2), "m1", 1, 100), untimed]))
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        # The view takes the untimed call for the call before those it shows:
        # the read stops at the next call, m1, shows nothing and says
        # call_limit. The live context reads only a timed row: m1.
        assert (view["calls"], view["ended"], view["complete"]) == ([], "call_limit", False)
        assert view["diagnostics"]["untimed_calls"] == 1
        assert tu.context_in(d)["tokens"] == 101

    def test_a_call_whose_rows_disagree_reads_its_first_row_and_the_live_context_its_last(
            self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        first, last = _at(-1), _at(-0.9)
        _session(d, "s1.jsonl", _cli([_call(_at(-2), "m1", 1, 100),
                                     _call(first, "m2", 1, 200), _call(last, "m2", 1, 250)]))
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        # The view takes a call's context and time from its first row; the
        # live context takes both from the newest call's last row.
        assert [(call["message"], call["context"], call["growth"])
                for call in view["calls"]] == [("m1", 101, None), ("m2", 201, 100)]
        assert view["calls"][-1]["at"] == first.replace("Z", "+00:00")
        live = tu.context_in(d)
        assert (live["tokens"], live["at"]) == (251, last.replace("Z", "+00:00"))

    def test_rows_out_of_time_order_stop_the_view_at_the_first_older_row(self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        _session(d, "s1.jsonl", _cli([_call(_at(-3), "m1", 1, 100), _call(_at(-2), "m2", 1, 200),
                                     _call(_at(-30), "m3", 1, 300), _call(_at(-1), "m4", 1, 400)]))
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        # The view trusts time order as the window totals do (#2756): m3, timed
        # before the window, is read as the call before it, so m1 and m2,
        # inside the window, are not shown, and the view still says it reached
        # the window's start with every call shown.
        assert [(call["message"], call["growth"]) for call in view["calls"]] == [("m4", 100)]
        assert (view["ended"], view["complete"]) == ("window_start", True)
        assert tu.context_in(d)["tokens"] == 401

    def test_lines_that_are_not_json_are_counted_and_both_readers_pass_over_them(
            self, tmp_path):
        _fleet, _paths, d = _one_bot(tmp_path)
        lines = [_line(row) for row in _cli([_call(_at(-2), "m1", 1, 100),
                                             _call(_at(-1), "m2", 1, 200)])]
        cut = _line(_call(_at(-0.5), "m3", 1, 300))
        # A line that is not JSON between the calls, and a last row cut short
        # with no newline, as a write in progress leaves it.
        _raw_session(d, lines[:-1] + [b"not json\n"] + lines[-1:] + [cut[:len(cut) // 2]])
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        assert [(call["message"], call["context"]) for call in view["calls"]] == [
            ("m1", 101), ("m2", 201)]
        assert view["diagnostics"]["malformed_lines"] == 2
        assert tu.context_in(d)["tokens"] == 201

    @pytest.mark.parametrize("usage", ["absent", "empty"])
    def test_a_newest_row_with_no_usage_is_no_call_for_either_reader(self, tmp_path, usage):
        _fleet, _paths, d = _one_bot(tmp_path)
        newest = _call(_at(-1), "m3", 1, 300)
        if usage == "absent":
            del newest["message"]["usage"]
        else:
            newest["message"]["usage"] = {}
        _session(d, "s1.jsonl", _cli([_call(_at(-3), "m1", 1, 100), _call(_at(-2), "m2", 1, 200),
                                     newest]))
        since, until = _last_day()
        view = tu.calls_in(d, since, until)
        assert [call["message"] for call in view["calls"]] == ["m1", "m2"]
        assert view["calls"][-1]["context"] == tu.context_in(d)["tokens"] == 201
