"""TELEGRAM_TOKEN_ENV_NAME is read only when it is a variable name.

bot.conf names the variable that holds a bot's Telegram token
(``TELEGRAM_TOKEN_ENV_NAME``), and three reads look the token up through that
name: start-bot.sh's own (bash ``${!name}``), the session shell's (in the
block start-bot.sh appends to the bot's ``.tmux-env``, which zsh sources on
macOS and bash on Linux), and ``resolve_bot_telegram_token`` in lib-common.sh,
which ``bridge_state`` and creds-check call. Each runs one
``case`` first. A value that holds anything but ASCII letters, digits and
underscores, or starts with a digit, reads no token, as an unset name does, and
logs one line that does not repeat it: a token written in place of a name is
not a variable name either, and the line must not print it.

The names here are plain values with no shell syntax in them: a hyphen, a
space, a leading digit, and the fake token itself.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from claudlobby.known_values import SHELL_IDENT_RE
from tests.conftest import constructed_env
from tests.test_creds_check_telegram import VALID_TOKEN, _fleet

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "claudlobby" / "_runtime_scripts"
START_BOT = SCRIPTS / "start-bot.sh"
LIB_COMMON = SCRIPTS / "lib-common.sh"

#: The repository's fake Telegram token, never a real one.
TOKEN = "8888888:AAAAAAAAAAAAAAAAAAAA"
#: A token a .env tier exported itself, which a bad name must leave as it is.
TIER_TOKEN = "7777777:BBBBBBBBBBBBBBBBBBBB"
GOOD_NAME = "MY_TOKEN"
BAD_NAMES = {
    "hyphen": "MY-TOKEN",
    "space": "MY TOKEN",
    "leading-digit": "1TOKEN",
    "the-token-itself": TOKEN,
}
BAD = pytest.mark.parametrize("name", list(BAD_NAMES.values()), ids=list(BAD_NAMES))

#: The case all three reads run, as written in each.
CASE_PATTERN = "[0123456789]*|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_]*)"
NOTICE = "TELEGRAM_TOKEN_ENV_NAME is not a variable name"

#: start-bot.sh's own read: from the comment that opens it through the unset
#: that ends it. Both lines are older than the check, so a run of these tests on
#: a tree without it reads that tree's own read.
START_BOT_READ = (
    "# Resolve the Telegram token from the env-var name declared in fleet.yaml.",
    '[ -z "${TELEGRAM_BOT_TOKEN:-}" ] && unset TELEGRAM_BOT_TOKEN',
)
#: What start-bot.sh writes into the env file for the session shell's read, from
#: the comment that opens it through the heredoc's end.
SESSION_READ = ("# Resolve the Telegram token via the indirection var set in bot.conf. .env tiers", "TGTOKEOF")


def _block(first: str, last: str) -> str:
    """start-bot.sh from the line FIRST through the next line LAST, as written."""
    lines = START_BOT.read_text().splitlines()
    start = lines.index(first)
    return "\n".join(lines[start : lines.index(last, start) + 1]) + "\n"


def _token(stdout: str) -> str | None:
    """The TELEGRAM_BOT_TOKEN a run printed last, or None when it was unset."""
    value = [line for line in stdout.splitlines() if line.startswith("token=")][-1][len("token=") :]
    return None if value == "<unset>" else value


def _notices(text: str) -> list[str]:
    return [line for line in text.splitlines() if NOTICE in line]


def _env(name: str | None, tier_token: str | None = None, **extra) -> dict[str, str]:
    values = {GOOD_NAME: TOKEN, **extra}
    if name is not None:
        values["TELEGRAM_TOKEN_ENV_NAME"] = name
    if tier_token is not None:
        values["TELEGRAM_BOT_TOKEN"] = tier_token
    return constructed_env(**values)


# --- start-bot.sh's own read -------------------------------------------------


def _start_bot_read(tmp_path: Path, name: str | None, tier_token: str | None = None):
    """Run start-bot.sh's own read as start-bot.sh runs it: under
    `set -euo pipefail`, after lib-common.sh. Returns (rc, token, stderr, the
    lines it left in the bot's startup.log)."""
    bot = tmp_path / "bot"
    log = bot / "logs" / "startup.log"
    log.unlink(missing_ok=True)
    bot.mkdir(exist_ok=True)
    script = (
        'set -euo pipefail\n. "$1"\nBOT_DIR="$2"\n'
        + _block(*START_BOT_READ)
        + 'printf "token=%s\\n" "${TELEGRAM_BOT_TOKEN-<unset>}"\n'
    )
    r = subprocess.run(
        ["bash", "-c", script, "_", str(LIB_COMMON), str(bot)],
        env=_env(name, tier_token),
        capture_output=True,
        text=True,
        timeout=60,
    )
    token = _token(r.stdout) if r.returncode == 0 else None
    return r.returncode, token, r.stderr, log.read_text().splitlines() if log.exists() else []


@BAD
def test_start_bot_reads_no_token_through_a_name_that_is_not_a_variable_name(tmp_path, name):
    rc, token, err, log = _start_bot_read(tmp_path, name)
    assert rc == 0, f"start-bot.sh's read stopped the script: {err}"
    assert token is None
    assert name not in err
    assert len(log) == 1 and NOTICE in log[0] and "start-bot.sh" in log[0], log
    assert name not in log[0]
    # As an unset name does: no token of its own, and a tier's own token kept.
    assert _start_bot_read(tmp_path, None)[:2] == (0, None)
    assert _start_bot_read(tmp_path, name, TIER_TOKEN)[:2] == (0, TIER_TOKEN)
    assert _start_bot_read(tmp_path, None, TIER_TOKEN)[:2] == (0, TIER_TOKEN)


def test_start_bot_still_reads_a_variable_name(tmp_path):
    rc, token, err, log = _start_bot_read(tmp_path, GOOD_NAME)
    assert (rc, token, log) == (0, TOKEN, [])
    assert NOTICE not in err


# --- the session shell's read ------------------------------------------------


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_the_session_shell_reads_no_token_through_a_name_that_is_not_a_variable_name(tmp_path, shell):
    """The block start-bot.sh appends to the env file, written by start-bot.sh's
    own lines and sourced in each shell a session runs: bash on Linux, zsh on
    macOS."""
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed here; the session shell on macOS is zsh, and the macOS lane runs this")
    bot = tmp_path / "bot"
    env_file = tmp_path / "tmux-env"
    env_file.write_text("")
    written = subprocess.run(
        ["bash", "-c", '. "$1"\n' + _block(*SESSION_READ), "_", str(LIB_COMMON)],
        env=constructed_env(BOT_DIR=bot, BOT_ENV_FILE=env_file),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert written.returncode == 0, written.stderr
    log = bot / "logs" / "startup.log"

    def source(name, tier_token=None):
        log.unlink(missing_ok=True)
        r = subprocess.run(
            [shell, "-c", '. "$1"; printf "token=%s\\n" "${TELEGRAM_BOT_TOKEN-<unset>}"', "_", str(env_file)],
            env=_env(name, tier_token),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 0, (name, r.stderr)
        return _token(r.stdout), r.stderr, log.read_text().splitlines() if log.exists() else []

    for case, name in BAD_NAMES.items():
        token, err, lines = source(name)
        assert token is None, case
        assert name not in err, case
        assert len(lines) == 1 and NOTICE in lines[0] and "session shell" in lines[0], (case, lines)
        assert name not in lines[0], case
        # As an unset name does: a tier's own token kept.
        assert source(name, TIER_TOKEN)[0] == TIER_TOKEN, case
    assert source(None)[0] is None
    assert source(None, TIER_TOKEN)[0] == TIER_TOKEN
    token, err, lines = source(GOOD_NAME)
    assert (token, lines) == (TOKEN, [])
    assert NOTICE not in err

    # A log it cannot open costs the line, and prints nothing in its place.
    log.parent.rmdir()
    token, err, lines = source(BAD_NAMES["hyphen"])
    assert (token, lines) == (None, [])
    assert "no such file" not in err.lower() and NOTICE not in err


# --- resolve_bot_telegram_token and its callers --------------------------------


def _channel_bot(tmp_path: Path, name: str | None) -> Path:
    """A channel bot whose bot.conf names NAME, with a GOOD_NAME token in its .env."""
    root = tmp_path / "root"
    bot = root / "bots" / "b1"
    bot.mkdir(parents=True, exist_ok=True)
    conf = [f'export CLAUDLOBBY_ROOT="{root}"', "export FLEET_NAME=test-fleet", f'BOT_DIR="{bot}"', "export TELEGRAM_BOT_HANDLE=b1"]
    if name is not None:
        conf.append(f'export TELEGRAM_TOKEN_ENV_NAME="{name}"')
    (bot / "bot.conf").write_text("\n".join(conf) + "\n")
    (bot / ".env").write_text(f"{GOOD_NAME}={TOKEN}\n")
    return bot


def _lib_call(tmp_path: Path, call: str, bot: Path) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "-c", f'. "$1"; {call} "$2"', "_", str(LIB_COMMON), str(bot)],
        env=constructed_env(HOME=home),
        capture_output=True,
        text=True,
        timeout=60,
    )


@BAD
def test_the_resolver_reads_no_token_through_a_name_that_is_not_a_variable_name(tmp_path, name):
    r = _lib_call(tmp_path, "resolve_bot_telegram_token", _channel_bot(tmp_path, name))
    assert (r.returncode, r.stdout) == (0, "")
    assert len(_notices(r.stderr)) == 1, r.stderr
    assert name not in r.stderr
    # As an unset name does.
    assert _lib_call(tmp_path, "resolve_bot_telegram_token", _channel_bot(tmp_path, None)).stdout == ""


def test_the_resolver_still_reads_a_variable_name(tmp_path):
    r = _lib_call(tmp_path, "resolve_bot_telegram_token", _channel_bot(tmp_path, GOOD_NAME))
    assert (r.returncode, r.stdout) == (0, TOKEN)
    assert _notices(r.stderr) == []


# The resolver asked for its cause: `_lib_call` puts the bot dir last.
CAUSE = '_cause() { resolve_bot_telegram_token "$1" --cause; }; _cause'


@BAD
def test_the_resolver_names_the_cause_for_a_name_that_is_not_a_variable_name(tmp_path, name):
    """With --cause the resolver prints `not_a_name` in place of a token, for
    the daily check's reason (#2975), and nothing that repeats the name."""
    r = _lib_call(tmp_path, CAUSE, _channel_bot(tmp_path, name))
    assert (r.returncode, r.stdout) == (0, "not_a_name")
    assert name not in r.stdout + r.stderr


@pytest.mark.parametrize("name", [GOOD_NAME, None], ids=["variable-name", "unset"])
def test_the_resolver_names_no_cause_for_a_variable_name_or_none(tmp_path, name):
    """--cause prints nothing for a variable name, never the token it names,
    and nothing for no name."""
    r = _lib_call(tmp_path, CAUSE, _channel_bot(tmp_path, name))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


@BAD
def test_bridge_state_reads_a_name_that_is_not_a_variable_name_as_no_token(tmp_path, name):
    r = _lib_call(tmp_path, "bridge_state", _channel_bot(tmp_path, name))
    assert (r.stdout, r.returncode) == ("no_token", 1)
    assert len(_notices(r.stderr)) == 1 and name not in r.stderr, r.stderr
    unset = _lib_call(tmp_path, "bridge_state", _channel_bot(tmp_path, None))
    assert (unset.stdout, unset.returncode) == ("no_token", 1)


def test_the_daily_check_says_a_name_that_is_not_a_variable_name_reads_no_token(tmp_path):
    """Each bad name's row fails, as the row of a bot that names no variable
    does, and its reason says the name is not a variable name (#2975); no name
    reaches anything the run writes."""
    roster = [(f"bad{i}", f"bad_{i}_bot", name, None) for i, name in enumerate(BAD_NAMES.values(), 1)]
    roster += [("bot1", "bot_one_bot", "T_BOT1_TOKEN", VALID_TOKEN), ("unnamed", "unnamed_bot", "", None)]
    f = _fleet(tmp_path, roster=roster)
    r = subprocess.run(
        ["bash", str(f["native"] / "creds-check.sh"), "f"], capture_output=True, text=True, env=f["env"], timeout=120
    )
    assert r.returncode == 0, r.stderr
    state = json.loads(f["state"].read_text())
    assert state["telegram_f_bot1"]["status"] == "ok"
    unnamed = state["telegram_f_unnamed"]
    assert unnamed["status"] == "fail" and NOTICE not in unnamed["detail"], unnamed
    for i in range(1, len(BAD_NAMES) + 1):
        row = state[f"telegram_f_bad{i}"]
        assert row["status"] == "fail" and NOTICE in row["detail"], row
    assert len(_notices(r.stderr)) >= len(BAD_NAMES), r.stderr
    written = [r.stdout, r.stderr, f["state"].read_text()]
    written += [p.read_text() for p in (f["root"] / "creds-check.log", f["tg_log"]) if p.exists()]
    for case, name in BAD_NAMES.items():
        assert not any(name in text for text in written), case


# --- one case ------------------------------------------------------------------


def test_the_three_reads_run_one_case_that_refuses_what_the_loader_refuses():
    found = {
        path.name: [line.strip() for line in path.read_text().splitlines() if line.strip().startswith("[0123456789]*|")]
        for path in (START_BOT, LIB_COMMON)
    }
    assert found == {"start-bot.sh": [CASE_PATTERN, CASE_PATTERN], "lib-common.sh": [CASE_PATTERN]}
    # The pattern refuses a non-empty name exactly when the loader's
    # SHELL_IDENT_RE does (each read skips an empty one before its case, as unset).
    alternatives = CASE_PATTERN[:-1].split("|")
    chars = string.printable + "é"
    for name in [*chars, *(f"A{c}" for c in chars), *(f"{c}A" for c in chars), "TELEGRAM_TOKEN_X", *BAD_NAMES.values()]:
        refused = any(fnmatch.fnmatchcase(name, alternative) for alternative in alternatives)
        assert refused == (SHELL_IDENT_RE.match(name) is None), repr(name)
