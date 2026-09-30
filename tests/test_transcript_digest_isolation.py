"""A real digest run leaves no needs-auth cache entry behind (#1972), on the REAL binary.

``tests/test_transcript_digest.sh`` pins that ``claudlobby/_runtime_scripts/transcript-digest.sh`` passes
the isolation flags. What the flags DO is Claude Code's behaviour, and no stub
can show it: the needs-auth cache writer lives inside the binary. So this runs
the real hook with the real ``claude``, at zero spend, in a throwaway HOME built
to reproduce #1972:

- a local plugin marketplace with two plugins whose MCP server exits at once, the
  way the Telegram channel exits when it defers to a live poller (#1586). One is
  enabled at the USER tier, where the estate enables telegram, and one at the
  LOCAL tier of a bot-shaped directory, where the composer enables claudna and
  superpowers;
- a failing ``.mcp.json`` server, approved the way the composer approves github;
- a SessionStart and a UserPromptSubmit hook at both tiers, each leaving a marker;
- ``ANTHROPIC_BASE_URL`` at a closed local port and a placeholder API key, so the
  model call fails at connect: nothing is spent and nothing leaves the host.

The CONTROL arm runs the same hook through a wrapper that strips the three
isolation flags and then execs the same binary, so only the flags differ. It must
write the cache entries and leave every marker, or the treatment's clean result
could just mean this fixture cannot produce the defect on this binary.

Opt-in (``DIGEST_ISOLATION_REAL=1``) because CI has no ``claude``. The binary is
``DIGEST_ISOLATION_CLAUDE_BIN``, else ``claude`` on PATH; the verdict is about
that binary's version, which the failure messages name.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parent.parent
DIGEST = REPO / "claudlobby/_runtime_scripts" / "transcript-digest.sh"

# Not conftest.realboot_skip_reason: that gate requires host auth, jq and claudron,
# and this test needs none of them (zero spend, no credential, no vault).
CLAUDE = os.environ.get("DIGEST_ISOLATION_CLAUDE_BIN") or shutil.which("claude") or ""
if os.environ.get("DIGEST_ISOLATION_REAL") != "1":
    SKIP = "gated: set DIGEST_ISOLATION_REAL=1 to run against a real claude binary"
elif not os.access(CLAUDE, os.X_OK):
    SKIP = "DIGEST_ISOLATION_REAL=1 but no claude binary (set DIGEST_ISOLATION_CLAUDE_BIN)"
else:
    SKIP = ""

# The digest's model timeout in both arms. The control's servers start about 1-2 s
# in (measured on a Pi 5, 2.1.281 and 2.1.283), so each arm outlives startup.
MODEL_TIMEOUT_S = 15

# The control arm's model binary: strips exactly the three #1972 flags, with
# their values, and runs the real binary with everything else unchanged.
STRIP_FLAGS = r"""#!/bin/bash
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --strict-mcp-config|--mcp-config=*|--setting-sources=*) shift ;;
        --mcp-config|--setting-sources) shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
exec "$REAL_CLAUDE" "${args[@]}"
"""

MARKERS = {
    "userchan", "localchan", "mcpjson",
    "user-SessionStart", "user-UserPromptSubmit",
    "local-SessionStart", "local-UserPromptSubmit",
}


def _env(tmp: Path, **extra) -> dict:
    return constructed_env(
        HOME=tmp / "home",
        TMPDIR=tmp / "tmp",
        TERM="dumb",
        DISABLE_AUTOUPDATER="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        ANTHROPIC_BASE_URL="http://127.0.0.1:9",
        ANTHROPIC_API_KEY="placeholder-not-a-credential",
        **extra,
    )


def _fixture(tmp: Path) -> Path:
    """Build the throwaway HOME and bot dir; return the bot dir."""
    marks, mkt, bot = tmp / "marks", tmp / "mkt", tmp / "bot"
    for d in (marks, mkt / ".claude-plugin", tmp / "home", tmp / "tmp", bot / ".claude"):
        d.mkdir(parents=True)

    def server(name: str) -> dict:  # leaves a marker, then exits before the handshake
        return {"command": "bash", "args": ["-c", f"echo spawned >> '{marks / name}'; exit 0"]}

    for name in ("userchan", "localchan"):
        (mkt / name / ".claude-plugin").mkdir(parents=True)
        (mkt / name / ".claude-plugin" / "plugin.json").write_text(json.dumps({
            "name": name, "version": "0.0.1", "description": "#1972 fixture",
            "mcpServers": {name: server(name)},
        }))
    (mkt / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "fixture", "owner": {"name": "test"},
        "plugins": [{"name": n, "source": f"./{n}", "description": n}
                    for n in ("userchan", "localchan")],
    }))
    for cmd in (["plugin", "marketplace", "add", str(mkt)],
                ["plugin", "install", "userchan@fixture"],
                ["plugin", "install", "localchan@fixture", "--scope", "local"]):
        r = subprocess.run([CLAUDE, *cmd], cwd=bot, env=_env(tmp), capture_output=True,
                           text=True, timeout=180)
        assert r.returncode == 0, f"fixture: claude {' '.join(cmd)}:\n{r.stdout}{r.stderr}"

    def hooks(tier: str) -> dict:
        return {ev: [{"hooks": [{"type": "command",
                                 "command": f"echo fired >> '{marks / f'{tier}-{ev}'}'"}]}]
                for ev in ("SessionStart", "UserPromptSubmit")}

    user = tmp / "home" / ".claude" / "settings.json"
    local = bot / ".claude" / "settings.local.json"
    for path, tier in ((user, "user"), (local, "local")):
        settings = json.loads(path.read_text())
        settings["hooks"] = hooks(tier)
        if tier == "local":
            settings["enabledMcpjsonServers"] = ["mcpjson"]
        path.write_text(json.dumps(settings))
    (bot / ".mcp.json").write_text(json.dumps({"mcpServers": {"mcpjson": server("mcpjson")}}))
    return bot


def _run_digest(tmp: Path, bot: Path, model_bin: str, arm: str) -> dict:
    """Run the real hook as a SessionEnd would (cwd = the bot dir); return its plane row."""
    tx = tmp / f"{arm}.jsonl"
    tx.write_text("".join(
        json.dumps({"type": role, "message": {"content": f"{role} turn {i}"}}) + "\n"
        for i in range(3) for role in ("user", "assistant")))
    root = tmp / f"{arm}-root"
    (root / "state" / "plane").mkdir(parents=True)
    # The socket is down, so the real shim stages the batch for daemon replay;
    # the row is read back from there, as tests/test_transcript_digest.sh does.
    env = _env(
        tmp, CLAUDLOBBY_ROOT=root, BOT_ID="tbot", CLAUDLOBBY_FLEET="tfleet", BOT_DIR=bot,
        SESSION_DIGEST_ENABLED="1", SESSION_DIGEST_MIN_TURNS="1",
        SESSION_DIGEST_TIMEOUT=str(MODEL_TIMEOUT_S), CLAUDE_BIN=model_bin, REAL_CLAUDE=CLAUDE,
        PLANE_EMIT_DISABLED="0", PLANE_SOCKET=root / "state" / "plane" / "no.sock",
    )
    payload = json.dumps({"session_id": f"sess-{arm}", "transcript_path": str(tx),
                          "cwd": str(bot), "reason": "clear"})
    r = subprocess.run(["bash", str(DIGEST)], input=payload, cwd=bot, env=env,
                       capture_output=True, text=True, timeout=MODEL_TIMEOUT_S + 90)
    assert r.returncode == 0, f"{arm}: a SessionEnd hook must exit 0:\n{r.stderr}"
    batches = sorted((root / "state" / "plane" / "staged").glob("*.batch"))
    if not batches:
        return {}
    envelope = json.loads(batches[-1].read_text())["events"][-1]
    return {**envelope["payload"], "event_type": envelope["event_type"]}


@pytest.mark.skipif(bool(SKIP), reason=SKIP)
def test_a_real_digest_run_writes_no_needs_auth_entry(tmp_path):
    version = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True,
                             timeout=60, env=_env(tmp_path)).stdout.strip()
    pin = f"{CLAUDE} ({version})"
    bot = _fixture(tmp_path)
    cache = tmp_path / "home" / ".claude" / "mcp-needs-auth-cache.json"
    marks = tmp_path / "marks"

    # TREATMENT, on the pristine fixture: the hook exactly as shipped.
    row = _run_digest(tmp_path, bot, CLAUDE, "treatment")
    data = row.get("data") or {}
    assert data.get("status") == "error" and "no output" in data.get("error", ""), (
        f"treatment: the model call should run and reach the closed port; got {row!r} [{pin}]")
    assert not cache.exists(), (
        f"treatment wrote the host-global needs-auth cache: {cache.read_text()} [{pin}]")
    fired = sorted(p.name for p in marks.iterdir())
    assert fired == [], f"treatment started a server or fired a hook: {fired} [{pin}]"

    # CONTROL: the same hook and fixture, only the three flags stripped.
    wrapper = tmp_path / "strip-isolation-flags"
    wrapper.write_text(STRIP_FLAGS)
    wrapper.chmod(0o755)
    _run_digest(tmp_path, bot, str(wrapper), "control")
    entries = json.loads(cache.read_text()) if cache.exists() else {}
    assert {"plugin:userchan:userchan", "plugin:localchan:localchan"} <= set(entries), (
        f"control: this fixture no longer reproduces #1972 on {pin}, so the treatment's "
        f"clean result proves nothing; cache={entries!r}")
    fired = {p.name for p in marks.iterdir()}
    assert fired == MARKERS, f"control: markers {sorted(fired)} != {sorted(MARKERS)} [{pin}]"
