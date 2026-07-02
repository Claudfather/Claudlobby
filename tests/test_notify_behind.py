"""Behavioral tests for lib/notify-behind.sh (the F5 notify-only
source-currency nudge) and the lib-common fleet-signal primitives it rides on.

The real script runs against a throwaway CLAUDLOBBY_ROOT that doubles as the
git checkout under inspection (mirroring the production shared install), with
tg-post.sh stubbed to capture Telegram delivery. Assertions cover the Phase 2
contract: the right behind-count is reported, delivery uses the FLEET NOTICE
framing (not FLEET ALERT), and the working tree is NEVER pulled.
"""

import os
import shutil
import subprocess

from tests.conftest import _write_exec

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "lib", "notify-behind.sh")
LIB_COMMON = os.path.join(REPO_ROOT, "lib", "lib-common.sh")
TG_POST = os.path.join(REPO_ROOT, "lib", "tg-post.sh")

# Identity/signing pinned per-invocation so tests never depend on host git config.
GIT = [
    "git",
    "-c",
    "user.email=test@example.com",
    "-c",
    "user.name=test",
    "-c",
    "commit.gpgsign=false",
]

# Captures chat id, the (expanded) state dir the caller resolved, and the message.
TG_STUB = (
    "#!/bin/bash\n"
    'printf "%s|%s|%s\\n" "$TELEGRAM_GROUP_CHAT_ID" "$TELEGRAM_STATE_DIR" "$1" >> "$TG_CAPTURE"\n'
)

CURL_STUB = (
    "#!/bin/bash\n"
    'echo "curl $*" >> "$CURL_CAPTURE"\n'
    'printf \'{"ok":true,"result":{"message_id":7}}\\n\'\n'
)


def _scrubbed_env(**overrides):
    """os.environ minus the bot-session vars that would short-circuit chat
    resolution (FLEET_PULSE_ESCALATION_CHAT_ID et al) or repoint the root."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TELEGRAM", "CLAUDLOBBY", "FLEET"))
    }
    env.update(overrides)
    return env


def _git(cwd, *args):
    subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True)


def _commit(repo, name):
    with open(os.path.join(repo, name), "w") as f:
        f.write(name + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name)


class Harness:
    """origin repo + a clone acting as CLAUDLOBBY_ROOT (like the shared install).

    ``behind`` commits land in origin after the clone, so the root is exactly
    that many commits behind origin/main. A bot.conf declaring the Telegram
    chat id (and a sourced-shape literal-$HOME state dir) is planted at
    ``bots_at`` (relative to root) and tg-post.sh is stubbed to capture what
    would have been sent.
    """

    def __init__(self, tmp_path, behind=0, bots_at="runtime/bots"):
        self.origin = str(tmp_path / "origin")
        self.root = str(tmp_path / "root")
        self.capture = str(tmp_path / "tg-capture")

        os.makedirs(self.origin)
        _git(self.origin, "init", "-b", "main")
        _commit(self.origin, "seed")
        subprocess.run(
            GIT + ["clone", self.origin, self.root], check=True, capture_output=True
        )
        for i in range(behind):
            _commit(self.origin, f"ahead-{i}")

        os.makedirs(os.path.join(self.root, "lib"), exist_ok=True)
        _write_exec(os.path.join(self.root, "lib", "tg-post.sh"), TG_STUB)
        bot_dir = os.path.join(self.root, bots_at, "tbot")
        os.makedirs(bot_dir, exist_ok=True)
        with open(os.path.join(bot_dir, "bot.conf"), "w") as f:
            f.write('export TELEGRAM_GROUP_CHAT_ID="-100123"\n')
            f.write(
                'export TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-tbot"\n'
            )

    def env(self):
        return _scrubbed_env(CLAUDLOBBY_ROOT=self.root, TG_CAPTURE=self.capture)

    def run(self, script=SCRIPT):
        return subprocess.run(
            ["bash", script], env=self.env(), capture_output=True, text=True
        )

    def head(self):
        out = subprocess.run(
            GIT + ["rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def captured(self):
        if not os.path.exists(self.capture):
            return []
        with open(self.capture) as f:
            return [line.strip() for line in f if line.strip()]

    def events(self):
        events_dir = os.path.join(self.root, "state", "events")
        if not os.path.isdir(events_dir):
            return ""
        chunks = []
        for name in sorted(os.listdir(events_dir)):
            with open(os.path.join(events_dir, name)) as f:
                chunks.append(f.read())
        return "".join(chunks)


class TestNotifyBehind:
    def test_behind_nudges_with_count_and_notice_framing(self, tmp_path):
        h = Harness(tmp_path, behind=2)
        r = h.run()
        assert r.returncode == 0, r.stderr
        lines = h.captured()
        assert len(lines) == 1
        chat_id, state_dir, msg = lines[0].split("|", 2)
        assert chat_id == "-100123"
        # The bot.conf state dir is sourced-shape ($HOME literal); the signal
        # path must hand tg-post an expanded, usable path.
        assert state_dir == os.environ["HOME"] + "/.claude/channels/telegram-tbot"
        # A nudge, not an incident: FLEET NOTICE framing, never FLEET ALERT.
        assert "FLEET NOTICE [source_behind]:" in msg
        assert "FLEET ALERT" not in msg
        assert "2 commit" in msg

    def test_never_pulls(self, tmp_path):
        # The core F5=c contract: notify-only. HEAD must be untouched and the
        # origin's new work must NOT appear in the tree after a run.
        h = Harness(tmp_path, behind=2)
        before = h.head()
        r = h.run()
        assert r.returncode == 0, r.stderr
        assert h.head() == before
        assert not os.path.exists(os.path.join(h.root, "ahead-0"))

    def test_in_sync_is_silent(self, tmp_path):
        h = Harness(tmp_path, behind=0)
        r = h.run()
        assert r.returncode == 0, r.stderr
        assert h.captured() == []
        assert "source_behind" not in h.events()

    def test_behind_writes_notice_event(self, tmp_path):
        h = Harness(tmp_path, behind=1)
        r = h.run()
        assert r.returncode == 0, r.stderr
        events = h.events()
        assert '"type":"source_behind"' in events
        assert '"source":"notice"' in events
        assert '"bot":"fleet"' in events

    def test_fetch_failure_is_quiet_but_evidenced(self, tmp_path):
        # Offline host: no nudge, no alert spam, exit 0 — but a durable
        # script_error breadcrumb lands in state/events for later diagnosis.
        h = Harness(tmp_path, behind=1)
        shutil.rmtree(h.origin)
        r = h.run()
        assert r.returncode == 0, r.stderr
        assert h.captured() == []
        assert '"type":"script_error"' in h.events()
        assert "source_behind" not in h.events()

    def test_non_git_root_skips(self, tmp_path):
        root = tmp_path / "plain"
        (root / "lib").mkdir(parents=True)
        r = subprocess.run(
            ["bash", SCRIPT],
            env=_scrubbed_env(CLAUDLOBBY_ROOT=str(root)),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr

    def test_multifleet_fallback_delivers(self, tmp_path):
        # Host jobs run fleet-less: resolve_bots_dir "" points at root-mode
        # runtime/bots, which is EMPTY on a multi-fleet host. The nudge must
        # fall back to scanning local/*/runtime/bots or it is dead on exactly
        # the hosts the tier targets.
        h = Harness(tmp_path, behind=3, bots_at="local/eng/runtime/bots")
        os.makedirs(os.path.join(h.root, "runtime", "bots"), exist_ok=True)
        r = h.run()
        assert r.returncode == 0, r.stderr
        lines = h.captured()
        assert len(lines) == 1
        assert "3 commit" in lines[0]


class TestTgPostStateDirResolution:
    """tg-post.sh must not go mute when the per-bot channel dir was never
    provisioned with an .env — it falls back to the default channel token."""

    def _env(self, tmp_path, state_dir_value):
        home = tmp_path / "home"
        default_chan = home / ".claude" / "channels" / "telegram"
        default_chan.mkdir(parents=True)
        (default_chan / ".env").write_text("TELEGRAM_BOT_TOKEN=default-token\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_exec(str(bin_dir / "curl"), CURL_STUB)
        return _scrubbed_env(
            HOME=str(home),
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            TMPDIR=str(tmp_path),
            CURL_CAPTURE=str(tmp_path / "curl-capture"),
            TELEGRAM_GROUP_CHAT_ID="-100123",
            TELEGRAM_STATE_DIR=state_dir_value.replace("{home}", str(home)),
        )

    def _run(self, env):
        return subprocess.run(
            ["bash", TG_POST, "hello"], env=env, capture_output=True, text=True
        )

    def test_provisioned_dir_is_used(self, tmp_path):
        env = self._env(tmp_path, "{home}/.claude/channels/telegram-mybot")
        chan = tmp_path / "home" / ".claude" / "channels" / "telegram-mybot"
        chan.mkdir(parents=True)
        (chan / ".env").write_text("TELEGRAM_BOT_TOKEN=bot-token\n")
        r = self._run(env)
        assert r.returncode == 0, r.stderr
        assert "hello" in (tmp_path / "curl-capture").read_text()
        assert "falling back" not in r.stderr

    def test_unprovisioned_dir_falls_back_to_default_channel(self, tmp_path):
        env = self._env(tmp_path, "{home}/.claude/channels/telegram-ghost")
        r = self._run(env)
        assert r.returncode == 0, r.stderr
        assert "hello" in (tmp_path / "curl-capture").read_text()
        # Identity switches are observable, not silent.
        assert "falling back" in r.stderr

    def test_no_token_anywhere_still_fails(self, tmp_path):
        env = self._env(tmp_path, "{home}/.claude/channels/telegram-ghost")
        os.remove(os.path.join(env["HOME"], ".claude", "channels", "telegram", ".env"))
        r = self._run(env)
        assert r.returncode == 1
        assert "no TELEGRAM_BOT_TOKEN" in r.stderr

    def test_env_token_wins_over_env_files(self, tmp_path):
        # Bot sessions carry TELEGRAM_BOT_TOKEN in the environment
        # (start-bot.sh's TELEGRAM_TOKEN_ENV_NAME indirection) — tg-post must
        # use it without needing any channel-dir .env on disk.
        env = self._env(tmp_path, "{home}/.claude/channels/telegram-ghost")
        os.remove(os.path.join(env["HOME"], ".claude", "channels", "telegram", ".env"))
        env["TELEGRAM_BOT_TOKEN"] = "env-token"
        r = self._run(env)
        assert r.returncode == 0, r.stderr
        assert "hello" in (tmp_path / "curl-capture").read_text()


class TestBotConfGetPath:
    """bot.conf is written to be sourced; raw readers get literal $HOME /
    $CLAUDLOBBY_ROOT prefixes. bot_conf_get_path expands them at the seam."""

    def _get_path(self, tmp_path, conf_value):
        bot = tmp_path / "bots" / "tbot"
        bot.mkdir(parents=True)
        (bot / "bot.conf").write_text(f'export SOME_PATH="{conf_value}"\n')
        r = subprocess.run(
            [
                "bash",
                "-c",
                f'. "{LIB_COMMON}" && bot_conf_get_path "{bot}" SOME_PATH ""',
            ],
            env=_scrubbed_env(CLAUDLOBBY_ROOT=str(tmp_path)),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        return r.stdout

    def test_expands_leading_home(self, tmp_path):
        got = self._get_path(tmp_path, "$HOME/.claude/channels/telegram-x")
        assert got == os.environ["HOME"] + "/.claude/channels/telegram-x"

    def test_expands_leading_claudlobby_root(self, tmp_path):
        got = self._get_path(tmp_path, "$CLAUDLOBBY_ROOT/state/x")
        assert got == f"{tmp_path}/state/x"

    def test_plain_value_passes_through(self, tmp_path):
        assert self._get_path(tmp_path, "/abs/path") == "/abs/path"


class TestFleetSignalPrimitives:
    def _emit(self, tmp_path, fn, event_type, msg):
        h = Harness(tmp_path, behind=0)
        bots_dir = os.path.join(h.root, "runtime", "bots")
        r = subprocess.run(
            [
                "bash",
                "-c",
                f'. "{LIB_COMMON}" && {fn} "{bots_dir}" "{event_type}" "{msg}"',
            ],
            env=h.env(),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        return h

    def test_failure_alert_framing_preserved(self, tmp_path):
        # emit_failure_alert predates the notice variant; the shared body must
        # keep its wire format byte-identical for existing callers.
        h = self._emit(tmp_path, "emit_failure_alert", "boom_type", "it broke")
        lines = h.captured()
        assert len(lines) == 1
        assert "FLEET ALERT [boom_type]: it broke" in lines[0]
        events = h.events()
        assert '"type":"boom_type"' in events
        assert '"source":"alert"' in events

    def test_notice_framing(self, tmp_path):
        h = self._emit(tmp_path, "emit_fleet_notice", "heads_up", "fyi")
        lines = h.captured()
        assert len(lines) == 1
        assert "FLEET NOTICE [heads_up]: fyi" in lines[0]
        assert '"source":"notice"' in h.events()
