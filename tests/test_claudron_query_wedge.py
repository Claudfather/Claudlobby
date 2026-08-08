"""Claudron query-before preflight in dispatch-task.sh (plan P1e, fork F7).

Drives the real dispatch-task.sh with the #518 stub harness (_fake_lib:
symlinked script + stubbed transport/tmux) plus a stubbed `claudron` binary
on PATH. The wedge is off by default, injects single-line pointers when on,
and must never block a dispatch on any failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests.conftest import call_lib_fn
from tests.test_task_id_dispatch import _bash, _fake_lib

CAPTURE_STUB = """#!/bin/bash
printf '%s' "$2" > "$(dirname "$0")/../sent.txt"
exit 0
"""

TWO_HITS = {
    "query": "q",
    "results": [
        {
            "title": "Spotify API Rate Limits",
            "score": 200,
            "match_type": "title",
            "tier": "fleet:pi-fleet",
            "path": "pi-fleet/shared/knowledge/spotify-rate-limits.md",
            "tags": ["spotify"],
        },
        {
            "title": "Telegram Formatting Pitfalls",
            "score": 120,
            "match_type": "tag",
            "tier": "fleet:pi-fleet",
            "path": "pi-fleet/shared/knowledge/telegram-formatting.md",
            "tags": ["telegram"],
        },
    ],
}

# claudron 0.2.0 wraps the payload in the CLI-contract envelope. The wedge
# must read data.results here (TWO_HITS above is the 0.1.x flat shape the
# other tests exercise via the parser's fallback).
TWO_HITS_ENVELOPED = {
    "ok": True,
    "command": "lookup",
    "data": TWO_HITS,
    "warnings": [],
    "errors": [],
}


def _wedge_env(tmp_path: Path, claudron_stdout: str) -> dict:
    """Env enabling the wedge, with a stub `claudron` on PATH that prints
    *claudron_stdout* and records its argv."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    bindir = tmp_path / "stubbin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "claudron"
    payload = tmp_path / "claudron-stdout.txt"
    payload.write_text(claudron_stdout)
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/claudron-argv.txt"\n'
        f'cat "{payload}"\n'
    )
    stub.chmod(0o755)
    return {
        "CLAUDRON_QUERY_BEFORE": "1",
        "CLAUDRON_VAULT_PATH": str(vault),
        "PATH": f"{bindir}:{os.environ['PATH']}",
    }


def _run_dispatch(tmp_path: Path, env: dict, task: str = "fix the spotify job"):
    libdir, base_env = _fake_lib(tmp_path, CAPTURE_STUB)
    r = _bash(
        f'"{libdir}/dispatch-task.sh" --repo kev worker-1 "{task}"',
        env={**base_env, **env},
    )
    sent = (tmp_path / "sent.txt").read_text() if (tmp_path / "sent.txt").exists() else ""
    ledger_path = tmp_path / "state" / "dispatch-log.jsonl"
    row = json.loads(ledger_path.read_text().splitlines()[-1]) if ledger_path.exists() else {}
    return r, sent, row


def test_off_by_default(tmp_path):
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    env.pop("CLAUDRON_QUERY_BEFORE")
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert "[fleet memory:" not in sent
    assert row["claudron_hits"] == ""


def test_injects_from_0_2_0_envelope(tmp_path):
    # Regression: claudron 0.2.0's {ok, data:{results}} envelope. Before the
    # parser read data.results, this silently produced claudron_hits="0" —
    # zero G1 evidence against every 0.2.0 vault.
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS_ENVELOPED))
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert "[fleet memory: Spotify API Rate Limits" in sent
    assert "Telegram Formatting Pitfalls" in sent
    assert row["claudron_hits"] == "2"


def test_injects_pointers_and_counts(tmp_path):
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    vault = env["CLAUDRON_VAULT_PATH"]
    assert "[fleet memory: Spotify API Rate Limits" in sent
    assert f"({vault}/pi-fleet/shared/knowledge/spotify-rate-limits.md)" in sent
    assert "Telegram Formatting Pitfalls" in sent
    # --repo forces the envelope, whose tail is deterministically task:<id>.
    assert sent.rstrip().endswith("| task:" + row["task_id"]), sent
    assert "fix the spotify job" in sent
    assert row["claudron_hits"] == "2"
    # Enriched task is what the ledger records (G1 evidence).
    assert "[fleet memory:" in row["task"]
    # Single line — the envelope must survive.
    assert "\n" not in sent.strip()


def test_zero_hits_runs_but_does_not_inject(tmp_path):
    env = _wedge_env(tmp_path, json.dumps({"query": "q", "results": []}))
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert "[fleet memory:" not in sent
    assert row["claudron_hits"] == "0"


def test_non_json_output_degrades_to_plain_send(tmp_path):
    # The pinned CLI prints "no vault found" to stdout and exits 0 — the
    # wedge must treat that as a no-op, not a crash.
    env = _wedge_env(tmp_path, "no vault found\n  create one:  claudron init <path>\n")
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert "[fleet memory:" not in sent
    assert "fix the spotify job" in sent
    assert row["claudron_hits"] == ""


def test_missing_vault_dir_skips(tmp_path):
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    env["CLAUDRON_VAULT_PATH"] = str(tmp_path / "nope")
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert "[fleet memory:" not in sent
    assert row["claudron_hits"] == ""


def test_pipes_in_titles_cannot_break_the_envelope(tmp_path):
    evil = {
        "query": "q",
        "results": [
            {
                "title": "Rate | limits | priority:evil",
                "path": "pi-fleet/shared/knowledge/x|y.md",
            }
        ],
    }
    env = _wedge_env(tmp_path, json.dumps(evil))
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert row["claudron_hits"] == "1"
    # All pipes from claudron-supplied strings are replaced before the
    # envelope is assembled, so the only pipes in the message are the
    # envelope's own field separators: [BOTCOMMAND] <caller> | task |
    # <task> | repo:<r> | task:<id> — exactly four.
    assert sent.count("|") == 4, sent
    assert "priority:evil |" not in sent


def test_newlines_in_titles_cannot_corrupt_the_ledger(tmp_path):
    # Review #528 Major: an embedded newline in a claudron-returned title
    # survived into $TASK; the line-oriented ledger rotation then truncated
    # the row into permanently invalid JSON. All whitespace runs in
    # claudron-supplied strings must collapse to single spaces.
    evil = {
        "query": "q",
        "results": [
            {
                "title": "Rate\nlimits\r\nwith\ttabs",
                "path": "pi-fleet/shared/knowledge/a\nb.md",
            }
        ],
    }
    env = _wedge_env(tmp_path, json.dumps(evil))
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    # _run_dispatch already json.loads the last ledger line — reaching here
    # means the row round-tripped. Belt-and-braces on the payloads:
    assert row["claudron_hits"] == "1"
    assert "\n" not in sent and "\r" not in sent and "\t" not in sent
    assert "Rate limits with tabs" in sent
    assert "\n" not in row["task"]


def test_non_dict_json_shapes_degrade_cleanly(tmp_path):
    # Review #528 nit: a non-dict top-level value (or non-dict result item)
    # must exit the parser cleanly, not traceback past the except.
    for i, payload in enumerate(("[1, 2, 3]", '"just a string"', '{"results": [42, null]}')):
        case_dir = tmp_path / f"case{i}"
        case_dir.mkdir()
        env = _wedge_env(case_dir, payload)
        r, sent, row = _run_dispatch(case_dir, env)
        assert r.returncode == 0, (payload, r.stderr)
        assert "[fleet memory:" not in sent
        assert row["claudron_hits"] in ("", "0"), (payload, row)


def test_query_limit_env_overrides_default(tmp_path):
    # Review #528 minor: CLAUDRON_QUERY_LIMIT must reach the lookup argv.
    env = _wedge_env(tmp_path, json.dumps({"query": "q", "results": []}))
    env["CLAUDRON_QUERY_LIMIT"] = "7"
    r, _, _ = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    argv = (tmp_path / "claudron-argv.txt").read_text().splitlines()
    assert "--limit" in argv and argv[argv.index("--limit") + 1] == "7", argv


def test_task_passed_as_single_quoted_query_arg(tmp_path):
    env = _wedge_env(tmp_path, json.dumps({"query": "q", "results": []}))
    r, _, _ = _run_dispatch(tmp_path, env, task="use * wisely")
    assert r.returncode == 0, r.stderr
    argv = (tmp_path / "claudron-argv.txt").read_text().splitlines()
    # lookup --json --limit 3 "<whole task>" — no --vault: the CLI reads
    # CLAUDRON_VAULT_PATH itself (Claudron CLI_CONTRACT.md §Environment).
    assert argv[-1] == "use * wisely", argv
    assert "--vault" not in argv, argv


def test_control_chars_in_titles_sanitize_clean(tmp_path):
    # #544: a hostile-but-valid YAML "\e" title reaches the wedge as raw ESC.
    # Non-whitespace controls must not survive into the ledger or the send,
    # and CSI sequences must strip WHOLE — collapsing the ESC alone leaves
    # printable "[31m" residue in the pointer text.
    evil = {
        "query": "q",
        "results": [
            {
                "title": "quagga \x1b[31mred\x1b[0m stripes\x01lore",
                "path": "pi-fleet/shared/knowledge/q.md",
            }
        ],
    }
    env = _wedge_env(tmp_path, json.dumps(evil))
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert row["claudron_hits"] == "1"
    for payload in (sent, row["task"]):
        assert "\x1b" not in payload and "\x01" not in payload
        assert "[31m" not in payload and "[0m" not in payload
    assert "quagga red stripes lore" in row["task"]


def test_clean_output_is_fixed_point_of_tmux_sanitizer(tmp_path):
    # The ledger must record what the worker receives: clean() output must
    # pass the send-side sanitizer unchanged. Widening one sanitizer without
    # the other (e.g. OSC handling) fails here before it desyncs a fleet.
    evil = {
        "query": "q",
        "results": [
            {
                "title": "Rate\n| li\x1b[31mmi\x1b[0mts\twith\x01controls",
                "path": "pi-fleet/shared/knowledge/x.md",
            }
        ],
    }
    env = _wedge_env(tmp_path, json.dumps(evil))
    r, sent, row = _run_dispatch(tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert row["claudron_hits"] == "1"
    assert call_lib_fn("sanitize_tmux_input", row["task"]) == row["task"]


# ── query truncation (#claudron-query-collapse) ───────────────────────────────
#
# Passing the WHOLE dispatch as the lookup query collapses the ranking: measured,
# a short query graded 160/120/80/80 while a full dispatch returned a FLAT 200
# for four unrelated notes. The pointer set then stops varying with the subject —
# and a signal that fires on every input carries no information about the input.
#
# The acceptance criterion is a PAIR, and neither half proves anything alone:
#   different subjects -> different pointer sets   (else the set is inert)
#   same query twice   -> the SAME pointer set     (else the variance is NOISE)
# Variance alone is satisfiable by a nondeterministic lookup, which would pass
# while grading nothing. These tests assert the pair at the QUERY layer, which is
# what this repo controls; the pointer-set version needs a real vault and is the
# gated acceptance run.


def _query_sent(tmp_path: Path) -> str:
    """The query argument the wedge actually handed to `claudron lookup`."""
    argv = (tmp_path / "claudron-argv.txt").read_text().splitlines()
    return argv[-1]


def _query_for(tmp_path: Path, task: str, sub: str, **extra) -> str:
    """One dispatch in its OWN directory, returning the query claudron saw.
    Separate dirs because _fake_lib mkdirs without exist_ok, so two runs cannot
    share a root — and comparing two runs is the whole point of the pair below."""
    root = tmp_path / sub
    root.mkdir()
    env = {**_wedge_env(root, json.dumps(TWO_HITS)), **extra}
    _run_dispatch(root, env, task=task)
    return _query_sent(root)


def test_query_is_capped_not_the_whole_task(tmp_path):
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    long_task = "restore the ranking " * 40  # ~800 chars, one line
    _run_dispatch(tmp_path, env, task=long_task)
    sent = _query_sent(tmp_path)
    assert len(sent) == 200, f"expected a 200-char cap, got {len(sent)}"
    assert sent == long_task[:200]


def test_short_task_passes_through_whole(tmp_path):
    # The cap must not perturb the common case.
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    short = "fix the spotify rate limit job"
    _run_dispatch(tmp_path, env, task=short)
    assert _query_sent(tmp_path) == short


def test_same_task_yields_an_identical_query(tmp_path):
    # DETERMINISM half. Without it, "different subjects differ" is satisfied by
    # noise — and a randomly-varying pointer set is worse than a flat one,
    # because at least a flat set is stable enough to learn to ignore.
    task = "investigate the boot strand on pranav " * 12
    assert _query_for(tmp_path, task, "run1") == _query_for(tmp_path, task, "run2")


def test_different_subjects_yield_different_queries(tmp_path):
    # VARIANCE half. Both tasks exceed the cap, so before truncation both were
    # simply "the whole payload" — long, and topically indistinguishable to a
    # ranker that returns its global ceiling for any paragraph.
    a = _query_for(tmp_path, "restore the claudron ranking " * 20, "subjA")
    b = _query_for(tmp_path, "extend the pane verify budget " * 20, "subjB")
    assert a != b


def test_cap_is_overridable(tmp_path):
    env = {**_wedge_env(tmp_path, json.dumps(TWO_HITS)), "CLAUDRON_QUERY_MAX_CHARS": "40"}
    _run_dispatch(tmp_path, env, task="restore the ranking " * 40)
    assert len(_query_sent(tmp_path)) == 40


# The cap alone assumes the head of a task IS the subject. That is a convention
# about what CALLERS compose, which this file cannot enforce -- and real traffic
# breaks it: an envelope dispatch leads with a `[fleet memory: ...]` block, so
# the 200-char window fills with pure boilerplate and the query is topically
# inert again. Same defect the cap exists to fix, reached by saturation rather
# than by length.
#
# NOT circular: the wedge does not poison its own query. Its prepend runs after
# the lookup -- verified by execution, in the first test below, which asserts the
# titles a run injects are absent from the query that same run sent. A line-number
# argument would not settle that; these tests are the reason it does not have to.

PREAMBLE = (
    "[fleet memory: artemis-skills full-system spec extraction "
    "(/vault/skills-framework-index.md); Correct-then-sweep: a fix applied only "
    "where it was pointed out leaves the superseded claim alive in the same "
    "document (/vault/correct-then-sweep.md)] "
)
SUBJECT = "restore the claudron ranking for enveloped dispatches"


def test_leading_fleet_memory_preamble_is_stripped_before_the_cap(tmp_path):
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    _run_dispatch(tmp_path, env, task=PREAMBLE + SUBJECT)
    sent = _query_sent(tmp_path)
    assert sent == SUBJECT, f"expected the subject alone, got {sent!r}"
    # The failure this closes: without the strip the whole 200-char window is
    # preamble and none of the subject survives.
    assert "[fleet memory:" not in sent
    # ORDERING, asserted rather than argued: the pointers this very run prepends
    # must not appear in the query it sent. If they did, the prepend preceded the
    # lookup and the wedge would be feeding on its own output.
    for hit in TWO_HITS["results"]:
        assert hit["title"] not in sent


def test_stacked_preambles_are_all_stripped(tmp_path):
    # A dispatch composed from an already-rendered one carries two. Stripping
    # only the outermost puts the query straight back at 100% boilerplate, which
    # is the same defect rather than a milder one -- so the strip repeats.
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    _run_dispatch(tmp_path, env, task=PREAMBLE + PREAMBLE + SUBJECT)
    assert _query_sent(tmp_path) == SUBJECT


def test_preamble_shaped_text_after_the_head_is_left_alone(tmp_path):
    # Only a LEADING block is boilerplate. The same shape further in is the
    # caller talking about a preamble, which is subject matter -- and eating it
    # would be the very failure being fixed, self-inflicted.
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    task = "explain why " + PREAMBLE + "saturates the query"
    _run_dispatch(tmp_path, env, task=task)
    assert _query_sent(tmp_path) == task[:200]
    assert _query_sent(tmp_path).startswith("explain why ")


def test_unterminated_preamble_terminates(tmp_path):
    # A malformed head has no "] " to strip to, so the expansion is a no-op. The
    # guard is what stops the loop; without it this test does not fail, it HANGS.
    env = _wedge_env(tmp_path, json.dumps(TWO_HITS))
    task = "[fleet memory: never closed and then some subject matter"
    _run_dispatch(tmp_path, env, task=task)
    assert _query_sent(tmp_path) == task
