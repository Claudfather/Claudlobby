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
import stat
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "lib", "notify-behind.sh")
LIB_COMMON = os.path.join(REPO_ROOT, "lib", "lib-common.sh")

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

TG_STUB = (
    '#!/bin/bash\nprintf "%s|%s\\n" "$TELEGRAM_GROUP_CHAT_ID" "$1" >> "$TG_CAPTURE"\n'
)


def _git(cwd, *args):
    subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True)


def _commit(repo, name):
    with open(os.path.join(repo, name), "w") as f:
        f.write(name + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name)


def _write_exec(path, content):
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class Harness:
    """origin repo + a clone acting as CLAUDLOBBY_ROOT (like the shared install).

    ``behind`` commits land in origin after the clone, so the root is exactly
    that many commits behind origin/main. A bot.conf declaring the Telegram
    chat id is planted at ``bots_at`` (relative to root) and tg-post.sh is
    stubbed to capture what would have been sent.
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

    def env(self):
        # Scrub bot-session vars that would short-circuit chat resolution
        # (FLEET_PULSE_ESCALATION_CHAT_ID et al) or repoint the root.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("TELEGRAM", "CLAUDLOBBY", "FLEET"))
        }
        env["CLAUDLOBBY_ROOT"] = self.root
        env["TG_CAPTURE"] = self.capture
        return env

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
        chat_id, msg = lines[0].split("|", 1)
        assert chat_id == "-100123"
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
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("TELEGRAM", "CLAUDLOBBY", "FLEET"))
        }
        env["CLAUDLOBBY_ROOT"] = str(root)
        r = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)
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


CURL_STUB = (
    "#!/bin/bash\n"
    'echo "curl $*" >> "$CURL_CAPTURE"\n'
    'printf \'{"ok":true,"result":{"message_id":7}}\\n\'\n'
)

TG_POST = os.path.join(REPO_ROOT, "lib", "tg-post.sh")


class TestTgPostStateDirResolution:
    """tg-post.sh must survive the two ways a real host breaks its token path:
    bot.conf values read raw carry a literal $HOME, and per-bot channel dirs
    may never have been provisioned with an .env."""

    def _env(self, tmp_path, state_dir_value):
        home = tmp_path / "home"
        default_chan = home / ".claude" / "channels" / "telegram"
        default_chan.mkdir(parents=True)
        (default_chan / ".env").write_text("TELEGRAM_BOT_TOKEN=default-token\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_exec(str(bin_dir / "curl"), CURL_STUB)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("TELEGRAM", "CLAUDLOBBY", "FLEET"))
        }
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["TMPDIR"] = str(tmp_path)
        env["CURL_CAPTURE"] = str(tmp_path / "curl-capture")
        env["TELEGRAM_GROUP_CHAT_ID"] = "-100123"
        env["TELEGRAM_STATE_DIR"] = state_dir_value
        return env

    def _run(self, env):
        return subprocess.run(
            ["bash", TG_POST, "hello"], env=env, capture_output=True, text=True
        )

    def test_literal_home_prefix_expands(self, tmp_path):
        env = self._env(tmp_path, "$HOME/.claude/channels/telegram-mybot")
        chan = tmp_path / "home" / ".claude" / "channels" / "telegram-mybot"
        chan.mkdir(parents=True)
        (chan / ".env").write_text("TELEGRAM_BOT_TOKEN=bot-token\n")
        r = self._run(env)
        assert r.returncode == 0, r.stderr
        assert "hello" in (tmp_path / "curl-capture").read_text()

    def test_unprovisioned_dir_falls_back_to_default_channel(self, tmp_path):
        env = self._env(tmp_path, "$HOME/.claude/channels/telegram-ghost")
        r = self._run(env)
        assert r.returncode == 0, r.stderr
        assert "hello" in (tmp_path / "curl-capture").read_text()

    def test_no_token_anywhere_still_fails(self, tmp_path):
        env = self._env(tmp_path, "$HOME/.claude/channels/telegram-ghost")
        os.remove(os.path.join(env["HOME"], ".claude", "channels", "telegram", ".env"))
        r = self._run(env)
        assert r.returncode == 1
        assert "no TELEGRAM_BOT_TOKEN" in r.stderr


class TestFleetSignalPrimitives:
    def test_failure_alert_framing_preserved(self, tmp_path):
        # emit_failure_alert predates the notice variant; extracting the shared
        # body must keep its wire format byte-identical for existing callers.
        h = Harness(tmp_path, behind=0)
        bots_dir = os.path.join(h.root, "runtime", "bots")
        r = subprocess.run(
            [
                "bash",
                "-c",
                f'. "{LIB_COMMON}" && emit_failure_alert "{bots_dir}" "boom_type" "it broke"',
            ],
            env=h.env(),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        lines = h.captured()
        assert len(lines) == 1
        assert "FLEET ALERT [boom_type]: it broke" in lines[0]
        events = h.events()
        assert '"type":"boom_type"' in events
        assert '"source":"alert"' in events

    def test_notice_framing(self, tmp_path):
        h = Harness(tmp_path, behind=0)
        bots_dir = os.path.join(h.root, "runtime", "bots")
        r = subprocess.run(
            [
                "bash",
                "-c",
                f'. "{LIB_COMMON}" && emit_fleet_notice "{bots_dir}" "heads_up" "fyi"',
            ],
            env=h.env(),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        lines = h.captured()
        assert len(lines) == 1
        assert "FLEET NOTICE [heads_up]: fyi" in lines[0]
        assert '"source":"notice"' in h.events()
