"""Busy-check SSOT — cross-script agreement (goal-aware-fleet plan, Phase 1).

Three scripts historically answered "is this bot busy?" with three different
pane regexes: keepalive.sh (marker-first + `esc to interrupt`), sprint-trigger.sh
(a spinner-verb list), and bot-sweep-cron.sh (a different verb list). The verb
lists silently degrade when Claude Code's rendering changes.

This suite pins the single source of truth in lib-common.sh:

  - `_BUSY_PATTERN_BASE` / `pane_is_busy <text>` — the one busy pattern,
    operator-extensible via KEEPALIVE_BUSY_PATTERNS (mirror of pane_is_idle).
  - `bot_dir_for_session <session>` — session name -> bot dir (the resolution
    tmux_socket_for_session already uses, exposed for marker lookups).
  - `bot_is_busy <socket> <session> [bot_dir]` — marker-first composite:
    a fresh data/.last-tool-call short-circuits BUSY without touching tmux;
    otherwise the pane is captured and pane_is_busy decides.

Mirrors tests/test_lifecycle_names.py: every consumer must resolve the same
verdict from the same fixtures, and no consumer may keep a private regex.
"""

import os
import subprocess
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_DIR / "lib"
FIXTURES = REPO_DIR / "tests" / "fixtures" / "pane-states"

# Pane texts come from the same fixtures the bash harness
# (test_keepalive_classify.sh) asserts against, so the two suites cannot
# drift apart on what a busy/idle pane looks like.
ESC_PANE = (FIXTURES / "busy-spinner.txt").read_text()
VERB_PANE = (FIXTURES / "verb-no-esc.txt").read_text()
IDLE_PANE = (FIXTURES / "idle-prompt.txt").read_text()


def _bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=10,
    )


def _sourced(fn_call: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return _bash(f'. "{LIB_DIR}/lib-common.sh"; {fn_call}', env=env)


# --- pane_is_busy: the one pattern ---------------------------------------------


def test_pane_is_busy_esc_to_interrupt():
    assert _sourced(f'pane_is_busy "{ESC_PANE}"').returncode == 0


def test_fixture_contents_still_match_their_names():
    """The constants above are only meaningful while the fixtures keep their
    defining trait — assert it so a fixture edit can't silently hollow out
    every test built on them."""
    assert "esc to interrupt" in ESC_PANE
    assert "esc to interrupt" not in VERB_PANE and "Thinking" in VERB_PANE
    assert IDLE_PANE.rstrip("\n").endswith(">")
    for pane in (ESC_PANE, VERB_PANE, IDLE_PANE):
        # These interpolate into bash double-quoted strings in _sourced calls.
        assert not set(pane) & set('"$`\\'), (
            "pane fixtures must stay bash-double-quote-safe"
        )


def test_pane_is_busy_capitalized_esc():
    assert _sourced('pane_is_busy "Esc to interrupt"').returncode == 0


def test_pane_is_busy_idle_prompt_is_not_busy():
    assert _sourced(f'pane_is_busy "{IDLE_PANE}"').returncode == 1


def test_pane_is_busy_verb_list_is_not_busy():
    """Deliberate semantics change: churning verbs are NOT the busy signal.

    The verb lists are exactly what drifted across three scripts; the SSOT
    matches only the stable `esc to interrupt` affordance (keepalive's
    documented rationale). Marker recency covers the rest.
    """
    assert _sourced(f'pane_is_busy "{VERB_PANE}"').returncode == 1


def test_pane_is_busy_operator_extension():
    rc = _sourced(
        'pane_is_busy "CUSTOM_BUSY_MARKER"',
        env={"KEEPALIVE_BUSY_PATTERNS": "CUSTOM_BUSY_MARKER"},
    ).returncode
    assert rc == 0


# --- bot_dir_for_session: session name -> bot dir ------------------------------


def _make_fleet(tmp_path: Path, fleet: str = "tf", bot: str = "mgr") -> Path:
    # Resolution under test is purely directory-name based; no bot.conf needed.
    bot_dir = tmp_path / "local" / fleet / "runtime" / "bots" / bot
    (bot_dir / "data").mkdir(parents=True)
    return bot_dir


def test_bot_dir_for_session_resolves_fleet_bot(tmp_path):
    bot_dir = _make_fleet(tmp_path)
    r = _sourced(
        'bot_dir_for_session "mgr"',
        env={"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "tf"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(bot_dir)


def test_bot_dir_for_session_unknown_session_fails_soft(tmp_path):
    _make_fleet(tmp_path)
    r = _sourced(
        'bot_dir_for_session "ghost"',
        env={"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "tf"},
    )
    assert r.returncode != 0
    assert r.stdout.strip() == ""


# --- bot_is_busy: marker-first composite ----------------------------------------


def _tmux_stub(tmp_path: Path, pane_file: Path, witness: Path) -> Path:
    """A fake tmux: records every invocation, serves pane_file on capture-pane."""
    stub = tmp_path / "bin" / "tmux"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{witness}"\n'
        'for a in "$@"; do [ "$a" = "capture-pane" ] && '
        f'{{ cat "{pane_file}"; exit 0; }}; done\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub


def _busy_env(tmp_path: Path, pane_text: str) -> tuple[dict, Path]:
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text(pane_text)
    witness = tmp_path / "tmux-calls.log"
    witness.write_text("")
    stub = _tmux_stub(tmp_path, pane_file, witness)
    return {"TMUX_BIN": str(stub)}, witness


def test_bot_is_busy_fresh_marker_short_circuits_without_tmux(tmp_path):
    bot_dir = _make_fleet(tmp_path)
    (bot_dir / "data" / ".last-tool-call").touch()
    env, witness = _busy_env(tmp_path, IDLE_PANE)  # pane says idle; marker wins
    env.update({"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "tf"})
    r = _sourced(f'bot_is_busy "com.example.tf.mgr" "mgr" "{bot_dir}"', env=env)
    assert r.returncode == 0, "fresh marker must read BUSY"
    assert witness.read_text() == "", "marker hit must not touch tmux at all"


def test_bot_is_busy_stale_marker_esc_pane_is_busy(tmp_path):
    bot_dir = _make_fleet(tmp_path)
    marker = bot_dir / "data" / ".last-tool-call"
    marker.touch()
    stale = time.time() - 3600
    os.utime(marker, (stale, stale))
    env, _ = _busy_env(tmp_path, ESC_PANE)
    env.update({"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "tf"})
    r = _sourced(f'bot_is_busy "com.example.tf.mgr" "mgr" "{bot_dir}"', env=env)
    assert r.returncode == 0


def test_bot_is_busy_stale_marker_idle_pane_is_not_busy(tmp_path):
    bot_dir = _make_fleet(tmp_path)
    marker = bot_dir / "data" / ".last-tool-call"
    marker.touch()
    stale = time.time() - 3600
    os.utime(marker, (stale, stale))
    env, _ = _busy_env(tmp_path, IDLE_PANE)
    env.update({"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "tf"})
    r = _sourced(f'bot_is_busy "com.example.tf.mgr" "mgr" "{bot_dir}"', env=env)
    assert r.returncode == 1, "stale marker must not read BUSY; idle pane decides"


def test_bot_is_busy_verb_pane_is_not_busy(tmp_path):
    """The regression the SSOT exists for: a verb-painted pane with no marker
    and no esc affordance is NOT busy — no consumer may resurrect the verb list."""
    bot_dir = _make_fleet(tmp_path)
    env, _ = _busy_env(tmp_path, VERB_PANE)
    env.update({"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "tf"})
    r = _sourced(f'bot_is_busy "com.example.tf.mgr" "mgr" "{bot_dir}"', env=env)
    assert r.returncode == 1


def test_bot_is_busy_resolves_bot_dir_from_session(tmp_path):
    bot_dir = _make_fleet(tmp_path)
    (bot_dir / "data" / ".last-tool-call").touch()
    env, witness = _busy_env(tmp_path, IDLE_PANE)
    env.update({"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "tf"})
    r = _sourced('bot_is_busy "com.example.tf.mgr" "mgr"', env=env)  # no bot_dir arg
    assert r.returncode == 0, "reverse lookup must find the marker"
    assert witness.read_text() == ""


def test_bot_is_busy_unresolvable_session_falls_back_to_pane(tmp_path):
    _make_fleet(tmp_path)
    env, _ = _busy_env(tmp_path, ESC_PANE)
    env.update({"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "tf"})
    r = _sourced('bot_is_busy "com.example.tf.ghost" "ghost"', env=env)
    assert r.returncode == 0, "no marker available -> pane decides"


# --- cross-script agreement: no private regexes left ----------------------------


def test_sprint_trigger_uses_ssot():
    src = (LIB_DIR / "sprint-trigger.sh").read_text()
    assert "bot_is_busy" in src
    assert "Prestidigitating" not in src, "private verb regex must be gone"


def test_bot_sweep_cron_uses_ssot():
    src = (LIB_DIR / "bot-sweep-cron.sh").read_text()
    assert "bot_is_busy" in src
    assert "thinking|running" not in src, "private verb regex must be gone"


def test_busy_pattern_defined_exactly_once():
    """`esc to interrupt` may appear as a pattern definition only in
    lib-common.sh (_BUSY_PATTERN_BASE) — no consumer re-declares the literal."""
    hits = [
        f"{sh.name}:{i}"
        for sh in LIB_DIR.glob("*.sh")
        for i, line in enumerate(sh.read_text().splitlines(), 1)
        if "sc to interrupt" in line
        and "=" in line
        and not line.lstrip().startswith("#")
    ]
    assert [h.split(":", 1)[0] for h in hits] == ["lib-common.sh"], (
        f"busy pattern must be defined once, in lib-common.sh; found: {hits}"
    )


def test_keepalive_classify_delegates_to_ssot_functions():
    """classify_pane must be BUILT ON pane_is_busy/pane_is_idle (the SSOT's
    public surface), not re-compose the pattern variables around grep."""
    src = (LIB_DIR / "keepalive.sh").read_text()
    assert "pane_is_busy" in src
    assert "pane_is_idle" in src
