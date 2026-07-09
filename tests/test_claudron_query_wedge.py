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
    # --vault <path> lookup --json --limit 3 "<whole task>"
    assert argv[-1] == "use * wisely", argv
