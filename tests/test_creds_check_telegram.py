"""creds-check: per-bot Telegram token validation (#502).

A channel bot could sit deaf — revoked token, or a token resolving *empty*
through the env tiers (#492) — with zero credential alerts. The check's
mechanics (SSOT token resolution shared with bridge_state, getMe via curl
config file) are documented at check_telegram_tokens in lib/creds-check.sh;
this suite runs the real script end-to-end against a scratch fleet with a
canned-response curl stub.

State keys are fleet-namespaced (telegram_<fleet>_<bot>): multi-fleet hosts
share one state file, and the alert text must say whose bot failed.

Also covers resolve_delivery_token (#542): alert-channel selection must skip a
bot whose own token is dead — the real tg-post.sh runs via the `real_tgpost`
fixture switch and the curl stub records every sendMessage URL in send.log.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from tests.conftest import TG_STUB, _scrubbed_env, _write_exec

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "lib" / "creds-check.sh"

VALID_TOKEN = "111111:validAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WRONGBOT_TOKEN = "222222:wrongbotAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
REVOKED_TOKEN = "333333:revokedAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ALL_TOKENS = (VALID_TOKEN, WRONGBOT_TOKEN, REVOKED_TOKEN)


def _curl_stub(bindir: Path) -> None:
    """Fake curl: appends its argv to argv.log (so tests can prove no token
    ever rides the command line), then reads the URL out of the --config
    file and answers getMe by token."""
    _write_exec(
        bindir / "curl",
        f"""#!/bin/bash
echo "$*" >> "$(dirname "$0")/argv.log"
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
  *sendMessage*) echo "$url" >> "$(dirname "$0")/send.log"; printf '{{"ok":true,"result":{{"message_id":1}}}}' ;;
  *) printf '{{"ok":false,"error_code":404,"description":"Not Found"}}' ;;
esac
""",
    )


def _fleet(
    tmp_path: Path,
    real_tgpost: bool = False,
    roster: list[tuple[str, str | None, str, str | None]] | None = None,
) -> dict:
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True)
    (root / "state").mkdir()
    tg_log = root / "tg-posts.log"
    if real_tgpost:
        # Real delivery path: creds-check resolves + exports the delivery
        # token, the real tg-post.sh posts under it, the curl stub records
        # the sendMessage URL (which embeds the token) in send.log.
        for helper in ("tg-post.sh", "lib-common.sh"):
            shutil.copy(REPO_ROOT / "lib" / helper, root / "lib" / helper)
    else:
        _write_exec(
            root / "lib" / "tg-post.sh", f'#!/bin/bash\necho "$*" >> "{tg_log}"\n'
        )
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

    if roster is None:
        bot("bot1", "bot_one_bot", "T_BOT1_TOKEN", VALID_TOKEN)
        bot("bot2", "bot_two_bot", "T_BOT2_TOKEN", REVOKED_TOKEN)
        bot("bot3", "bot_three_bot", "T_BOT3_TOKEN", None)  # configured, no value
        bot("bot4", None, "UNUSED", None)  # not a channel bot
        bot("bot5", "bot_five_bot", "T_BOT5_TOKEN", WRONGBOT_TOKEN)
        # Residue dir: a departed bot whose runtime dir (with bot.conf + handle)
        # survives on disk but is NOT declared in fleet.yaml — the stale-dir class
        # the declared-bots filter exists to skip (no getMe, no false alert).
        bot("ghost", "ghost_bot", "T_GHOST_TOKEN", VALID_TOKEN)
        declared = ("bot1", "bot2", "bot3", "bot4", "bot5")
    else:
        # Delivery-selection tests need roster control: resolve_delivery_token
        # walks the bots dir in GLOB order (not fleet.yaml order), so bot
        # names decide who is probed first.
        for spec in roster:
            bot(*spec)
        declared = tuple(name for name, *_ in roster)

    # Declared-bots SSOT the filter reads (parse_fleet_bots schema: bots: at
    # 2-space indent, bot keys at 4-space indent). ghost is absent.
    (root / "local" / "f" / "fleet.yaml").write_text(
        "fleet:\n  name: f\n  bots:\n"
        + "".join(f"    {b}:\n      expertise: [x]\n" for b in declared)
    )

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
    if real_tgpost:
        # The real tg-post.sh aborts without a chat id — bind the placeholder
        # to the one mode that posts for real.
        env["TELEGRAM_GROUP_CHAT_ID"] = "-1001234567890"
    (tmp_path / "home").mkdir()
    return {
        "root": root,
        "env": env,
        "state": state,
        "tg_log": tg_log,
        "bindir": bindir,
    }


def _run(f: dict) -> dict:
    # Positional fleet arg — the composed-timer contract.
    r = subprocess.run(
        ["bash", str(SCRIPT), "f"],
        capture_output=True,
        text=True,
        env=f["env"],
        timeout=60,
    )
    assert r.returncode == 0, f"creds-check exited {r.returncode}\n{r.stderr}"
    return json.loads(f["state"].read_text())


def test_valid_token_matching_handle_ok(tmp_path):
    state = _run(_fleet(tmp_path))
    assert state["telegram_f_bot1"]["status"] == "ok"


def test_revoked_token_fails_with_error_code(tmp_path):
    f = _fleet(tmp_path)
    state = _run(f)
    assert state["telegram_f_bot2"]["status"] == "fail"
    assert "401" in state["telegram_f_bot2"]["detail"]
    assert REVOKED_TOKEN not in f["state"].read_text(), "token must never be recorded"


def test_empty_token_fails_not_skips(tmp_path):
    """#492 class: a bot *configured* for Telegram whose token resolves empty
    is an outage, not a skip — it must alert."""
    state = _run(_fleet(tmp_path))
    assert state["telegram_f_bot3"]["status"] == "fail"
    assert "empty" in state["telegram_f_bot3"]["detail"].lower()


def test_handle_mismatch_fails(tmp_path):
    """A valid token for the WRONG bot (cross-wired .env) must fail loudly."""
    state = _run(_fleet(tmp_path))
    assert state["telegram_f_bot5"]["status"] == "fail"
    d = state["telegram_f_bot5"]["detail"]
    assert "some_other_bot" in d and "bot_five_bot" in d


def test_non_channel_bot_not_checked(tmp_path):
    state = _run(_fleet(tmp_path))
    assert "telegram_f_bot4" not in state


def test_undeclared_residue_dir_not_checked(tmp_path):
    """A departed bot's leftover runtime dir (valid token and all) must not
    be getMe-checked or alerted — declared-bots filter, same as fleet-pulse."""
    f = _fleet(tmp_path)
    state = _run(f)
    assert "telegram_f_ghost" not in state
    posts = f["tg_log"].read_text() if f["tg_log"].exists() else ""
    assert "ghost" not in posts


def test_fail_alerts_via_tg_post_once(tmp_path):
    f = _fleet(tmp_path)
    _run(f)
    posts = f["tg_log"].read_text() if f["tg_log"].exists() else ""
    assert "telegram_f_bot2 FAIL" in posts
    _run(f)  # second tick: still failing -> edge-alert stays quiet
    assert posts == f["tg_log"].read_text(), "repeat fail must not re-alert"


def test_delivery_token_skips_dead_bot_picks_live(tmp_path):
    """#542 invariant: a bot whose OWN token is dead cannot become the alert
    channel. The revoked bot sorts first in the bots-dir glob, so
    resolve_delivery_token must probe and SKIP it, exporting the later live
    bot's token — the dead bot's own FAIL alert is the post that proves which
    token carried the delivery."""
    f = _fleet(
        tmp_path,
        real_tgpost=True,
        roster=[
            ("abot", "a_bot", "T_ABOT_TOKEN", REVOKED_TOKEN),
            ("bbot", "bot_one_bot", "T_BBOT_TOKEN", VALID_TOKEN),
        ],
    )
    _run(f)
    send_log = f["bindir"] / "send.log"
    assert send_log.exists(), "abot's FAIL alert must reach sendMessage"
    sends = send_log.read_text()
    # The env is scrubbed and no channel-dir fallback exists in the fixture,
    # so VALID_TOKEN reaching sendMessage proves the export end-to-end.
    assert VALID_TOKEN in sends, "live bot's token must carry the alert"
    assert REVOKED_TOKEN not in sends, "dead-token bot became the alert channel"
    # Plain-text contract: tg-post must never force a parse_mode.
    assert "parse_mode" not in sends
    assert "parse_mode" not in (f["bindir"] / "argv.log").read_text()


def test_no_live_channel_exports_no_delivery_token(tmp_path):
    """All-dead fleet: no token may be exported and nothing posted — a silent
    no-send beats a confident post under a dead token (the #542 failure
    class this selection exists to prevent)."""
    f = _fleet(
        tmp_path,
        real_tgpost=True,
        roster=[
            ("abot", "a_bot", "T_ABOT_TOKEN", REVOKED_TOKEN),
            ("bbot", "b_bot", "T_BBOT_TOKEN", None),
        ],
    )
    _run(f)
    assert not (f["bindir"] / "send.log").exists(), (
        "dead-only fleet must not reach sendMessage"
    )
    # The export-fired breadcrumb is the guard's ONLY observable: an exported
    # EMPTY token is indistinguishable downstream (tg-post's ${VAR:-fallback}
    # treats empty as unset), so this absence is what catches a dropped
    # [ -n "$_dtok" ] guard. Wording is a tested contract, pinned at the log
    # call in lib/creds-check.sh.
    log_text = (f["root"] / "creds-check.log").read_text()
    assert "alert delivery token resolved" not in log_text


def test_token_never_on_curl_argv(tmp_path):
    """Two independent proofs: (a) the stub only learns the URL via --config,
    so argv-passed tokens would 404 and break the ok assertion; (b) the stub
    records every argv it receives — no token may appear in any of them."""
    f = _fleet(tmp_path)
    state = _run(f)
    assert state["telegram_f_bot1"]["status"] == "ok"
    argv_log = f["bindir"] / "argv.log"
    assert argv_log.exists(), "stub must have been invoked"
    argv = argv_log.read_text()
    for tok in ALL_TOKENS:
        assert tok not in argv, "token leaked onto curl argv"


def test_composed_env_alldead_exports_scanned_state_dir(tmp_path):
    """#572/#588 state-dir gap: creds-check must export the scanned live channel
    dir as TELEGRAM_STATE_DIR, not just the chat id.

    The exact worst case creds-check exists to alert on. The composed timer env
    carries the fleet TELEGRAM_GROUP_CHAT_ID but NOT TELEGRAM_STATE_DIR, and
    every fleet token is dead so resolve_delivery_token exports none — so
    tg-post cannot short-circuit on TELEGRAM_BOT_TOKEN and must read its
    delivery token from TELEGRAM_STATE_DIR/.env. resolve_alert_target resolves
    the live dir by scanning a declaring bot; without the matching export
    (present for the chat id, missing for the state dir before #588) tg-post
    falls to its dead default channel and the every-credential-dead alert never
    reaches the fleet's real channel. This proves creds-check hands the scanned
    dir to tg-post."""
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True)
    (root / "state").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _curl_stub(bindir)
    (root / ".env").write_text("")  # no PAT/Railway -> those checks skip

    # One declared channel bot whose OWN token is revoked: resolve_delivery_token
    # probes and skips it, so no live delivery token is exported (the
    # all-credentials-dead condition). bot.conf declares both the chat id (so the
    # fleet scan finds the bot) and the state dir — the value under test, which
    # the resolver must export so the alert routes to this live channel dir.
    bot = root / "local" / "f" / "runtime" / "bots" / "chanbot"
    bot.mkdir(parents=True)
    channel = bot / "channel"  # the dir the scan resolves; its path is the assertion
    (bot / "bot.conf").write_text(
        'export BOT_ID="chanbot"\n'
        'export BOT_SERVICE="com.t.f.chanbot"\n'
        'export TELEGRAM_BOT_HANDLE="chan_bot"\n'
        'export TELEGRAM_TOKEN_ENV_NAME="T_CHAN_TOKEN"\n'
        'export TELEGRAM_GROUP_CHAT_ID="-100SCANCHAT"\n'
        "export TELEGRAM_STATE_DIR="
        '"$CLAUDLOBBY_ROOT/local/f/runtime/bots/chanbot/channel"\n'
    )
    (bot / ".env").write_text(f'T_CHAN_TOKEN="{REVOKED_TOKEN}"\n')  # own token dead
    (root / "local" / "f" / "fleet.yaml").write_text(
        "fleet:\n  name: f\n  bots:\n    chanbot:\n      expertise: [x]\n"
    )

    # tg-post stub records the chat id + the (expanded) state dir creds-check
    # exported + the message — the shared fleet-signal observation point.
    _write_exec(root / "lib" / "tg-post.sh", TG_STUB)
    capture = root / "tg-capture.log"
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
            "TG_CAPTURE": str(capture),
            # Composed timer env: fleet chat id present, state dir ABSENT.
            "TELEGRAM_GROUP_CHAT_ID": "-100COMPOSEDENV",
            # _scrubbed_env only strips TELEGRAM/CLAUDLOBBY/FLEET, so a host token
            # would leak in and fail the unrelated github/railway/mcp checks
            # against the curl stub. Empty every var they read (github falls back
            # through three names) so only the telegram check fires.
            "GITHUB_PERSONAL_ACCESS_TOKEN": "",
            "GITHUB_TOKEN": "",
            "GITHUB_PAT": "",
            "RAILWAY_API_TOKEN": "",
            "MCP_PROBE_URL": "",
        }
    )
    (tmp_path / "home").mkdir()

    _run({"env": env, "state": state})

    # Precondition: all fleet tokens dead -> no delivery token exported, so the
    # state-dir gap actually bites (tg-post can't short-circuit on a token).
    log = (root / "creds-check.log").read_text()
    assert "alert delivery token resolved" not in log, "a live token would mask the gap"

    # The other checks skip, so chanbot's revoked-token FAIL is the one alert;
    # the stub recorded the chat id + the state dir creds-check handed to
    # tg-post. A dropped export shows here as an empty/default state dir.
    assert capture.exists(), "chanbot FAIL must reach tg-post"
    lines = capture.read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly chanbot's FAIL alert, got {lines}"
    chat_id, state_dir, msg = lines[0].split("|", 2)
    assert "telegram_f_chanbot FAIL" in msg  # the every-credential-dead scenario
    assert chat_id == "-100COMPOSEDENV", "composed-env chat id must win"
    assert state_dir == str(channel), (
        f"tg-post received state_dir={state_dir!r}; expected the scanned live "
        f"channel dir {str(channel)!r}. Empty/default means _alert_state_dir "
        f"was never exported (the #588 gap)."
    )
