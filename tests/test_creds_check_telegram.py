"""creds-check: per-bot Telegram token validation (#502).

creds-check validated GitHub/Railway/MCP but never Telegram bot tokens, so
a channel bot could sit deaf (revoked token, or a token resolving *empty*
through the env tiers, #492) with zero credential alerts. The new
check_telegram_tokens resolves each channel bot's token exactly the way
bridge_state does (bot.conf names the var; value from the tiered .env,
shell-sourced so quoting can't mislead) and getMe-validates it.

Runs the real script against a scratch fleet with a canned-response curl
stub (token never hits argv — the check passes the URL via a curl config
file, so the stub reads it from there).
"""

import json
import os
import stat
import subprocess
from pathlib import Path

from tests.conftest import _scrubbed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "lib" / "creds-check.sh"

VALID_TOKEN = "111111:validAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WRONGBOT_TOKEN = "222222:wrongbotAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
REVOKED_TOKEN = "333333:revokedAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _curl_stub(bindir: Path) -> None:
    """Fake curl: reads the URL out of the --config file, answers getMe by
    token; records every URL-bearing config so tests can assert no-argv-leak."""
    _write_exec(
        bindir / "curl",
        f"""#!/bin/bash
cfg=""
prev=""
for a in "$@"; do
  [ "$prev" = "--config" ] && cfg="$a"
  prev="$a"
done
url=""
[ -n "$cfg" ] && url=$(sed -n 's/^url *= *"\\(.*\\)"$/\\1/p' "$cfg")
case "$url" in
  *bot{VALID_TOKEN}/getMe*)    printf '{{"ok":true,"result":{{"username":"bot_one_bot"}}}}' ;;
  *bot{WRONGBOT_TOKEN}/getMe*) printf '{{"ok":true,"result":{{"username":"some_other_bot"}}}}' ;;
  *bot{REVOKED_TOKEN}/getMe*)  printf '{{"ok":false,"error_code":401,"description":"Unauthorized"}}' ;;
  *) printf '{{"ok":false,"error_code":404,"description":"Not Found"}}' ;;
esac
""",
    )


def _fleet(tmp_path: Path) -> dict:
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True)
    (root / "state").mkdir()
    tg_log = root / "tg-posts.log"
    _write_exec(root / "lib" / "tg-post.sh", f'#!/bin/bash\necho "$*" >> "{tg_log}"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _curl_stub(bindir)
    (root / ".env").write_text("")  # no PAT/Railway -> those checks skip

    def bot(name: str, handle: str | None, token_var: str, token: str | None):
        d = root / "local" / "f" / "runtime" / "bots" / name
        (d / "data").mkdir(parents=True)
        conf = [f'export BOT_ID="{name}"', f'export BOT_SERVICE="com.t.f.{name}"']
        if handle:
            conf.append(f'export TELEGRAM_BOT_HANDLE="{handle}"')
            conf.append(f'export TELEGRAM_TOKEN_ENV_NAME="{token_var}"')
        (d / "bot.conf").write_text("\n".join(conf) + "\n")
        if token is not None:
            # Quoted on purpose — the false-404 pitfall this check must dodge.
            (d / ".env").write_text(f'{token_var}="{token}"\n')
        return d

    bot("bot1", "bot_one_bot", "T_BOT1_TOKEN", VALID_TOKEN)
    bot("bot2", "bot_two_bot", "T_BOT2_TOKEN", REVOKED_TOKEN)
    bot("bot3", "bot_three_bot", "T_BOT3_TOKEN", None)  # configured, no value
    bot("bot4", None, "UNUSED", None)  # not a channel bot
    bot("bot5", "bot_five_bot", "T_BOT5_TOKEN", WRONGBOT_TOKEN)

    state = root / "state" / "creds-check-state.json"
    env = _scrubbed_env()
    env.update(
        {
            "PATH": f"{bindir}:{env.get('PATH', os.defpath)}",
            "HOME": str(tmp_path / "home"),
            "CLAUDLOBBY_ROOT": str(root),
            "CLAUDLOBBY_ENV": str(root / ".env"),
            "CLAUDLOBBY_CREDS_LOG": str(root / "creds-check.log"),
            "CLAUDLOBBY_CREDS_STATE": str(state),
        }
    )
    (tmp_path / "home").mkdir()
    return {"root": root, "env": env, "state": state, "tg_log": tg_log}


def _run(f: dict) -> dict:
    r = subprocess.run(
        ["bash", str(SCRIPT), "f"],  # positional fleet arg — the composed-timer contract
        capture_output=True,
        text=True,
        env=f["env"],
        timeout=60,
    )
    assert r.returncode == 0, f"creds-check exited {r.returncode}\n{r.stderr}"
    return json.loads(f["state"].read_text())


def test_valid_token_matching_handle_ok(tmp_path):
    state = _run(_fleet(tmp_path))
    assert state["telegram_bot1"]["status"] == "ok"


def test_revoked_token_fails_with_error_code(tmp_path):
    f = _fleet(tmp_path)
    state = _run(f)
    assert state["telegram_bot2"]["status"] == "fail"
    assert "401" in state["telegram_bot2"]["detail"]
    assert REVOKED_TOKEN not in f["state"].read_text(), "token must never be recorded"


def test_empty_token_fails_not_skips(tmp_path):
    """#492 class: a bot *configured* for Telegram whose token resolves empty
    is an outage, not a skip — it must alert."""
    state = _run(_fleet(tmp_path))
    assert state["telegram_bot3"]["status"] == "fail"
    assert "empty" in state["telegram_bot3"]["detail"].lower()


def test_handle_mismatch_fails(tmp_path):
    """A valid token for the WRONG bot (cross-wired .env) must fail loudly."""
    state = _run(_fleet(tmp_path))
    assert state["telegram_bot5"]["status"] == "fail"
    d = state["telegram_bot5"]["detail"]
    assert "some_other_bot" in d and "bot_five_bot" in d


def test_non_channel_bot_not_checked(tmp_path):
    state = _run(_fleet(tmp_path))
    assert "telegram_bot4" not in state


def test_fail_alerts_via_tg_post_once(tmp_path):
    f = _fleet(tmp_path)
    _run(f)
    posts = f["tg_log"].read_text() if f["tg_log"].exists() else ""
    assert "telegram_bot2 FAIL" in posts
    _run(f)  # second tick: still failing -> edge-alert stays quiet
    assert posts == f["tg_log"].read_text(), "repeat fail must not re-alert"


def test_token_never_on_curl_argv(tmp_path):
    """The stub only learns the URL via --config; if the check ever put the
    token on argv the stub would miss it and answer 404 -> bot1 not ok."""
    state = _run(_fleet(tmp_path))
    assert state["telegram_bot1"]["status"] == "ok"
