"""Behavioral tests for claudlobby/_runtime_scripts/notify-behind.sh (the F5 notify-only
source-currency nudge) and the lib-common fleet-signal primitives it rides on.

The real script runs against a throwaway CLAUDLOBBY_ROOT that doubles as the
git checkout under inspection (mirroring the production shared install), with
tg-post.sh stubbed to capture Telegram delivery. Assertions cover the Phase 2
contract: the right behind-count is reported, delivery uses the FLEET NOTICE
framing (not FLEET ALERT), and the working tree is NEVER pulled.
"""

import json
import os
import shutil
import subprocess

import pytest

from tests.conftest import TG_STUB, _scrubbed_env, _write_exec, read_fleet_events
from tests.fixtures.current_runtime import current_runtime  # noqa: F401
from tests.test_maintenance_jobs import _native_fixture
from tests.test_plane_events_door import _serving

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "claudlobby", "_runtime_scripts", "notify-behind.sh")
LIB_COMMON = os.path.join(REPO_ROOT, "claudlobby", "_runtime_scripts", "lib-common.sh")
TG_POST = os.path.join(REPO_ROOT, "claudlobby", "_runtime_scripts", "tg-post.sh")

# Identity/signing pinned per-invocation so tests never depend on host git config.
GIT = [
    "git",
    "-c",
    "user.email=test@example.com",
    "-c",
    "user.name=test",
    "-c",
    "commit.gpgsign=false",
    # Git 2.47 and later end a commit by detaching `git maintenance run --auto`, which still
    # holds .git/objects/maintenance.lock after the commit returns. That races the test here
    # that deletes origin (#2717).
    "-c",
    "maintenance.auto=false",
]

CURL_STUB = (
    "#!/bin/bash\n"
    'echo "curl $*" >> "$CURL_CAPTURE"\n'
    'printf \'{"ok":true,"result":{"message_id":7}}\\n\'\n'
)


def _git(cwd, *args):
    return subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, name):
    with open(os.path.join(repo, name), "w") as f:
        f.write(name + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name)


# On a Git-runtime host the composed CLI is the installation's stable launcher,
# INSTALL/bin/claudlobby, a script with no interpreter beside it (#2886). This
# stand-in answers the one read notify-behind makes with what Harness.answer
# wrote, in the envelope the real read prints (TestTheGitRuntimeRead runs the
# real one), and records each call.
CLI_STUB = (
    "#!/bin/bash\n"
    'here="${0%/*}/.."\n'
    'printf "%s\\n" "$*" >> "$here/calls"\n'
    'cat "$here/answer.err" >&2 2>/dev/null\n'
    'cat "$here/answer.json" 2>/dev/null\n'
    'exit "$(cat "$here/answer.rc" 2>/dev/null || echo 0)"\n'
)

# What the real read answers when the committed configuration cannot be
# verified, its envelope's error (claudlobby/commands/releases.py).
CONFLICT = {"code": "conflict", "hint": None, "retryable": False,
            "message": "committed Git configuration drift is unavailable"}


def _envelope(*, revision=None, error=None):
    """The one-line JSON envelope `claudlobby --json config diff` prints."""
    data = {"bot": None, "changes": [], "fleet": None, "mode": "current"}
    if revision is not None:
        data.update(source_revision=revision, config_fingerprint="f" * 64)
    return json.dumps({"command": "config.diff", "data": {} if error else data,
                       "error": error, "ok": error is None, "release_id": None,
                       "request_id": None, "schema_version": 1},
                      sort_keys=True, separators=(",", ":"))


class Harness:
    """origin repo + a clone acting as CLAUDLOBBY_ROOT (like the shared install).

    ``behind`` commits land in origin after the clone, so the root is exactly
    that many commits behind origin/main, and so is the revision the host runs:
    the commit the root was cloned at, as the stand-in CLI reports it (``runs``
    reports another, as an update does). A bot.conf declaring the Telegram
    chat id (and a sourced-shape literal-$HOME state dir) is planted at
    ``bots_at`` (relative to root) and tg-post.sh is stubbed to capture what
    would have been sent.
    """

    def __init__(self, tmp_path, behind=0, bots_at="runtime/bots",
                 tag_at_seed=None, tag_after=None, *, scratch_plane_env):
        self.scratch_plane_env = scratch_plane_env
        # The installation's launcher, with nothing beside it (#2886).
        self.installation = tmp_path / "installation"
        (self.installation / "bin").mkdir(parents=True)
        self.cli = str(self.installation / "bin" / "claudlobby")
        _write_exec(self.cli, CLI_STUB)
        self.origin = str(tmp_path / "origin")
        self.root = str(tmp_path / "root")
        self.capture = str(tmp_path / "tg-capture")

        os.makedirs(self.origin)
        _git(self.origin, "init", "-b", "main")
        _commit(self.origin, "seed")
        # A tag at seed is inherited by the clone, so it sits at the root's
        # HEAD: the release-gap shape (on the newest release, main has moved).
        if tag_at_seed:
            _git(self.origin, "tag", tag_at_seed)
        subprocess.run(
            GIT + ["clone", self.origin, self.root], check=True, capture_output=True
        )
        self.runs(self.head())
        for i in range(behind):
            _commit(self.origin, f"ahead-{i}")
        # A tag landing AFTER the clone is one the root is behind: the
        # behind-a-cut-release shape. notify-behind fetches --tags to see it.
        if tag_after:
            _git(self.origin, "tag", tag_after)

        self.native = _native_fixture(tmp_path, "notify-behind.sh")
        bot_dir = os.path.join(self.root, bots_at, "tbot")
        os.makedirs(bot_dir, exist_ok=True)
        with open(os.path.join(bot_dir, "bot.conf"), "w") as f:
            f.write('export TELEGRAM_GROUP_CHAT_ID="-100123"\n')
            f.write(
                'export TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-tbot"\n'
            )

    def env(self, *, socket=None):
        # a host job: no fleet, so its receipts land on the plane under _host
        return _scrubbed_env(TG_CAPTURE=self.capture, CLAUDLOBBY_CLI=self.cli,
                            **self.scratch_plane_env(self.root, socket=socket, initialize=True))

    def run(self, *, serve=False):
        if serve:
            with _serving(self.root, self.scratch_plane_env) as socket:
                return self._run(socket=socket)
        return self._run()

    def _run(self, *, socket=None):
        return subprocess.run(
            ["bash", str(self.native / "notify-behind.sh")],
            env=self.env(socket=socket), capture_output=True, text=True
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

    def runs(self, revision):
        """The host runs REVISION, as its Git runtime reports it."""
        self.answer(_envelope(revision=revision))

    def answer(self, stdout, rc=0, stderr=""):
        """What the stand-in CLI prints and exits with."""
        (self.installation / "answer.json").write_text(stdout + "\n" if stdout else "")
        (self.installation / "answer.err").write_text(stderr)
        (self.installation / "answer.rc").write_text(f"{rc}\n")

    def calls(self):
        p = self.installation / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def origin_rev(self, ref="HEAD"):
        return _git(self.origin, "rev-parse", ref)

    def captured(self):
        if not os.path.exists(self.capture):
            return []
        with open(self.capture) as f:
            return [line.strip() for line in f if line.strip()]

    def messages(self):
        return [line.split("|", 2)[2] for line in self.captured()]

    def log(self):
        p = os.path.join(self.root, "state", "notify-behind.log")
        return open(p).read() if os.path.exists(p) else ""

    def events(self):
        return read_fleet_events(self.root)


def test_harness_commits_start_no_git_maintenance(tmp_path, monkeypatch):
    """A detached `git maintenance` would race the test that deletes origin (#2717)."""
    trace = tmp_path / "trace2.jsonl"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _commit(repo, "seed")
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [event for event in events if event.get("event") == "start" and "commit" in event["argv"]], \
        "the trace must see the commit, or its silence below proves nothing"
    maintenance = [event["argv"] for event in events if "maintenance" in event.get("argv", [])]
    assert not maintenance, maintenance


class TestNotifyBehind:
    def test_behind_nudges_with_count_and_notice_framing(self, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
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
        assert "host update apply" in msg and "git -C " not in msg

    def test_never_pulls(self, tmp_path, *, scratch_plane_env):
        # The core F5=c contract: notify-only. HEAD must be untouched and the
        # origin's new work must NOT appear in the tree after a run.
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        before = h.head()
        r = h.run()
        assert r.returncode == 0, r.stderr
        assert h.head() == before
        assert not os.path.exists(os.path.join(h.root, "ahead-0"))

    def test_in_sync_is_silent(self, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=0, scratch_plane_env=scratch_plane_env)
        r = h.run(serve=True)
        assert r.returncode == 0, r.stderr
        assert h.captured() == []
        assert "source_behind" not in h.events()

    def test_behind_writes_notice_event(self, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=1, scratch_plane_env=scratch_plane_env)
        r = h.run(serve=True)
        assert r.returncode == 0, r.stderr
        events = h.events()
        assert '"type":"source_behind"' in events
        assert '"source":"notice"' in events
        # a host job's receipt is anchored on the HOST (the retired file said
        # "fleet"): never misattributed to a bot
        assert '"bot":"host"' in events

    def test_fetch_failure_is_quiet_but_evidenced(self, tmp_path, *, scratch_plane_env):
        # Offline host: no nudge, no alert spam, exit 0 — but a durable
        # script_error breadcrumb lands in state/events for later diagnosis.
        h = Harness(tmp_path, behind=1, scratch_plane_env=scratch_plane_env)
        shutil.rmtree(h.origin)
        r = h.run(serve=True)
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

    def test_multifleet_fallback_delivers(self, tmp_path, *, scratch_plane_env):
        # Host jobs run fleet-less: resolve_bots_dir "" points at root-mode
        # runtime/bots, which is EMPTY on a multi-fleet host. The nudge must
        # fall back to scanning local/*/runtime/bots or it is dead on exactly
        # the hosts the tier targets.
        h = Harness(tmp_path, behind=3, bots_at="local/eng/runtime/bots", scratch_plane_env=scratch_plane_env)
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
    def _emit(self, tmp_path, fn, event_type, msg, *, scratch_plane_env):
        h = Harness(tmp_path, behind=0, scratch_plane_env=scratch_plane_env)
        bots_dir = os.path.join(h.root, "runtime", "bots")
        with _serving(h.root, scratch_plane_env) as socket:
            r = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'. "{h.native}/lib-common.sh" && {fn} "{bots_dir}" "{event_type}" "{msg}"',
                ],
                env=h.env(socket=socket),
                capture_output=True,
                text=True,
            )
        assert r.returncode == 0, r.stderr
        return h

    def test_failure_alert_framing_preserved(self, tmp_path, *, scratch_plane_env):
        # emit_failure_alert predates the notice variant; the shared body must
        # keep its wire format byte-identical for existing callers.
        h = self._emit(tmp_path, "emit_failure_alert", "boom_type", "it broke", scratch_plane_env=scratch_plane_env)
        lines = h.captured()
        assert len(lines) == 1
        assert "FLEET ALERT [boom_type]: it broke" in lines[0]
        events = h.events()
        assert '"type":"boom_type"' in events
        assert '"source":"alert"' in events

    def test_notice_framing(self, tmp_path, *, scratch_plane_env):
        h = self._emit(tmp_path, "emit_fleet_notice", "heads_up", "fyi", scratch_plane_env=scratch_plane_env)
        lines = h.captured()
        assert len(lines) == 1
        assert "FLEET NOTICE [heads_up]: fyi" in lines[0]
        assert '"source":"notice"' in h.events()


# tg-post's real rejected-send shape: HTTP 200 with {"ok":false}, which the
# script parses into exit 3 plus a diagnostic on stderr. The success stub
# (TG_STUB) cannot exercise the branch that matters here.
TG_STUB_REJECTED = (
    "#!/bin/bash\n"
    'printf "%s|%s|%s\\n" "$TELEGRAM_GROUP_CHAT_ID" "$TELEGRAM_STATE_DIR" "$1" >> "$TG_CAPTURE"\n'
    'echo "tg-post: send REJECTED — message NOT delivered'
    ' (ok=false; error: Unauthorized)" >&2\n'
    "exit 3\n"
)


class TestCurrencyOutcomeIsLogged:
    """The log must record what DELIVERY actually did, not that an attempt was
    about to be made.

    The defect these pin: notify-behind wrote "notice raised" to the log and
    only then called notify_currency, whose verdict it never read. The two
    artifacts then disagreed — the log a human audits said success while the
    journal carried ALERT-DELIVERY-FAILED — and the only artifact that would
    reveal a dead channel was the one asserting there was nothing wrong.

    Three outcomes, deliberately distinguished, because collapsing any two
    re-creates the bug: delivered, raised-but-undelivered, and suppressed by
    the debounce (which raised nothing at all).
    """

    def _reject(self, h):
        _write_exec(h.native / "tg-post.sh", TG_STUB_REJECTED)

    def test_delivered_send_is_logged_as_delivered(self, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        r = h.run()
        assert r.returncode == 0, r.stderr
        assert len(h.captured()) == 1, "precondition: the send was attempted"
        assert "notice DELIVERED" in h.log()

    def test_rejected_send_is_not_logged_as_delivered(self, tmp_path, *, scratch_plane_env):
        # The headline defect. A dead token must never leave a log line a human
        # would audit as a correctly-escalated notice.
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        self._reject(h)
        r = h.run()
        assert r.returncode == 0, r.stderr
        log = h.log()
        assert "NOT DELIVERED" in log
        assert "notice DELIVERED" not in log
        # The old text is the specific lie; it must not survive anywhere.
        assert "notice raised" not in log

    def test_rejected_send_still_reports_the_distance(self, tmp_path, *, scratch_plane_env):
        # The behind-count is factual regardless of delivery; fixing the
        # outcome clause must not cost the operator the measurement.
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        self._reject(h)
        assert h.run().returncode == 0
        assert "BEHIND" in h.log() and "2" in h.log()

    def test_debounced_tick_is_not_logged_as_raised(self, tmp_path, *, scratch_plane_env):
        # Second run inside the renotify window raises nothing at all. Logging
        # it as a raised notice inflates the apparent escalation count with
        # notices that were never sent.
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        assert h.run().returncode == 0
        first = h.log()
        assert h.run().returncode == 0
        second = h.log()[len(first):]
        assert len(h.captured()) == 1, "precondition: the second tick sent nothing"
        assert "SUPPRESSED" in second
        assert "DELIVERED" not in second

    def test_release_gap_site_reports_its_outcome(self, tmp_path, *, scratch_plane_env):
        # The third site (source_release_gap) carried the same defect and is
        # reached only when a tag sits at HEAD while main has moved.
        h = Harness(tmp_path, behind=2, tag_at_seed="v1.0.0", scratch_plane_env=scratch_plane_env)
        self._reject(h)
        assert h.run().returncode == 0
        log = h.log()
        assert "release-gap notice" in log
        assert "NOT DELIVERED" in log
        assert "notice raised" not in log

    def test_behind_tag_site_reports_its_outcome(self, tmp_path, *, scratch_plane_env):
        # The second site: behind a cut release.
        h = Harness(tmp_path, behind=2, tag_after="v2.0.0", scratch_plane_env=scratch_plane_env)
        self._reject(h)
        assert h.run().returncode == 0
        log = h.log()
        assert "BEHIND TAG v2.0.0" in log
        assert "NOT DELIVERED" in log
        assert "notice raised" not in log


class TestCurrencyOutcomeDoesNotLeak:
    """_ALERT_DELIVERED is a shell global set inside _emit_fleet_signal, so it
    survives across loop iterations. notify_currency must therefore report
    per-call, never whatever the previous repo left behind — otherwise a
    suppressed notice inherits the prior repo's success and the fix reproduces
    the original lie one layer down.
    """

    def _probe(self, tmp_path, script, *, scratch_plane_env):
        h = Harness(tmp_path, behind=0, scratch_plane_env=scratch_plane_env)
        bots_dir = os.path.join(h.root, "runtime", "bots")
        state_dir = os.path.join(h.root, "state", "currency")
        os.makedirs(state_dir, exist_ok=True)
        body = (
            f'. "{h.native}/lib-common.sh"\n'
            f'BOTS_DIR="{bots_dir}"\nSTATE_DIR="{state_dir}"\n' + script
        )
        r = subprocess.run(
            ["bash", "-c", body], env=h.env(), capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def test_delivered_then_suppressed_reports_suppressed(self, tmp_path, *, scratch_plane_env):
        out = self._probe(
            tmp_path,
            'notify_currency repoA source_behind 1 "msg A"\n'
            'echo "first=$_CURRENCY_OUTCOME"\n'
            # Same repo+event+distinct: the debounce marker suppresses this one.
            'notify_currency repoA source_behind 1 "msg A"\n'
            'echo "second=$_CURRENCY_OUTCOME"\n',
        scratch_plane_env=scratch_plane_env)
        assert "first=delivered" in out
        assert "second=suppressed" in out

    def test_fired_flag_is_reset_per_call(self, tmp_path, *, scratch_plane_env):
        out = self._probe(
            tmp_path,
            'notify_currency repoA source_behind 1 "msg A"\n'
            'notify_currency repoA source_behind 1 "msg A"\n'
            'echo "fired=$_DEBOUNCE_FIRED"\n',
        scratch_plane_env=scratch_plane_env)
        assert "fired=0" in out


class TestUndeliveredNoticeIsRetried:
    """#900: the debounce marker is what buys silence, so a notice the channel
    rejected must not write it. On 2026-09-11 one did: the marker landed two
    seconds before the send failed, and the next three daily runs matched it
    and sent nothing while the root sat 92 commits behind."""

    def test_rejected_notice_leaves_no_marker_and_the_next_run_sends_again(self, tmp_path, scratch_plane_env):
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        tg_post = h.native / "tg-post.sh"
        marker = os.path.join(h.root, "state", "currency", "root.source_behind")
        _write_exec(tg_post, TG_STUB_REJECTED)
        assert h.run().returncode == 0
        assert not os.path.exists(marker), "a rejected send wrote the debounce marker"
        assert h.run().returncode == 0
        assert len(h.captured()) == 2, "the run after a rejected send did not retry it"
        # Once a send lands, the debounce holds again. The marker appearing
        # here is also what shows the absence above was checked at the right path.
        _write_exec(tg_post, TG_STUB)
        assert h.run().returncode == 0
        assert os.path.exists(marker), "a delivered send left no marker"
        assert h.run().returncode == 0
        assert len(h.captured()) == 3, "a delivered notice was sent again inside the window"

    def test_a_notice_with_no_telegram_target_is_raised_once(self, tmp_path, scratch_plane_env):
        # No Telegram target at all (a new install) never reaches tg-post and
        # records exit 2. No later run can deliver it, so it counts as sent;
        # otherwise every run nudges the manager again (#1825 review).
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        with open(os.path.join(h.root, "runtime", "bots", "tbot", "bot.conf"), "w") as f:
            f.write('export TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-tbot"\n')
        assert h.run(serve=True).returncode == 0
        assert '"exit":2' in h.events(), "precondition: no Telegram target resolved"
        assert h.run(serve=True).returncode == 0
        assert h.events().count('"type":"source_behind"') == 1, "a notice with no target was raised again"


class TestCountsFromTheRunningRevision:
    """#2322, #2886: the data root counts from the source revision the host
    runs, never from the root checkout's HEAD. Nothing runs from that checkout
    and nothing pulls it, so its HEAD measured how old an unused checkout was:
    315 commits on one host, where the code it ran lacked 10. On a Git-runtime
    host the revision is the one its runtime reports (`config diff`'s
    data.source_revision); the sealed-release reader before it found no
    interpreter beside the installation's launcher and counted nothing."""

    def _lacks(self, h, ref):
        return int(_git(h.root, "rev-list", "--count", f"{ref}..origin/main"))

    def test_a_stale_root_with_a_current_revision_raises_no_source_behind(
            self, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=3, scratch_plane_env=scratch_plane_env)
        h.runs(h.origin_rev())
        r = h.run()
        assert r.returncode == 0, r.stderr
        assert self._lacks(h, "HEAD") == 3, "control: the root checkout is stale"
        assert h.captured() == []
        log = h.log()
        assert f"RUNNING {h.origin_rev()}" in log and "IN SYNC with origin/main" in log, log

    def test_a_revision_behind_main_counts_exactly_the_commits_it_lacks(
            self, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=5, scratch_plane_env=scratch_plane_env)
        source = h.origin_rev("HEAD~3")
        h.runs(source)
        r = h.run()
        assert r.returncode == 0, r.stderr
        assert (self._lacks(h, source), self._lacks(h, "HEAD")) == (3, 5)
        (msg,) = h.messages()
        assert "FLEET NOTICE [source_behind]:" in msg, msg
        assert "is 3 commit(s) behind origin/main" in msg, msg
        assert f"runs {source[:8]} (" in msg, msg
        assert "5 commit" not in msg, msg
        # The one read it made: the Git runtime's own, with neither a bot nor a fleet.
        assert h.calls() == [f"--root {os.path.realpath(h.root)} --json config diff"]

    @pytest.mark.parametrize("tag", [None, "v2.0.0"])
    def test_the_notice_names_the_git_update_of_the_commit_it_counted_to(
            self, tag, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=2, tag_after=tag, scratch_plane_env=scratch_plane_env)
        if tag:
            _commit(h.origin, "after-the-tag")  # main moves past the release it counts to
        r = h.run()
        assert r.returncode == 0, r.stderr
        (msg,) = h.messages()
        target = h.origin_rev(tag or "HEAD")
        assert f"behind {'release ' + tag if tag else 'origin/main'}" in msg, msg
        assert ("apply with: as the operator, claudlobby --root "
                f"{os.path.realpath(h.root)} host update apply {target}, then host update status") in msg, msg
        assert "host activate" not in msg and "seal" not in msg and "config plan" not in msg, msg

    def test_the_first_run_after_an_update_clears_the_notice(
            self, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        marker = os.path.join(h.root, "state", "currency", "root.source_behind")
        assert h.run().returncode == 0
        assert len(h.captured()) == 1 and os.path.exists(marker), "precondition: notified"
        first = h.log()
        h.runs(h.origin_rev())  # an update to main
        r = h.run()
        assert r.returncode == 0, r.stderr
        second = h.log()[len(first):]
        assert f"RUNNING {h.origin_rev()}" in second and "IN SYNC with origin/main" in second, second
        assert not os.path.exists(marker)
        assert len(h.captured()) == 1
        # Cleared, not merely quiet: the first notice's distance is news again,
        # which a marker left from it would hold back for a week.
        _commit(h.origin, "after-update-0")
        _commit(h.origin, "after-update-1")
        assert h.run().returncode == 0
        assert len(h.captured()) == 2
        assert "is 2 commit(s) behind origin/main" in h.messages()[-1]

    @pytest.mark.parametrize("case, said", [
        ("refused", "cannot be read (claudlobby config diff refused (exit 4): conflict: "
                    "committed Git configuration drift is unavailable)"),
        ("launcher refused", "cannot be read (claudlobby config diff printed no result (exit 6): "
                             "claudlobby: maintenance is active; use host update status or recovery)"),
        ("no revision", "cannot be read (claudlobby config diff answered with no source revision)"),
        ("no CLI", "cannot be read (claudlobby config diff did not run: claudlobby: CLAUDLOBBY_CLI must "
                   "name the composed absolute executable"),
        ("revision not in history", "it runs cccccccc, a commit this checkout does not have"),
    ])
    def test_a_revision_it_cannot_read_is_said_and_nothing_is_counted(
            self, case, said, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env)
        if case == "refused":
            h.answer(_envelope(error=CONFLICT), rc=4)
        elif case == "launcher refused":
            h.answer("", rc=6, stderr="claudlobby: maintenance is active; use host update status or recovery\n")
        elif case == "no revision":
            h.answer(_envelope())
        elif case == "no CLI":
            h.cli = str(tmp_path / "missing" / "claudlobby")
        else:
            h.runs("c" * 40)
        serve = case == "refused"  # one case proves the type lands
        r = h.run(serve=serve)
        assert r.returncode == 0, r.stderr
        (msg,) = h.messages()
        assert "FLEET NOTICE [source_unknown]:" in msg, msg
        assert said in msg, msg
        assert f"not counted — inspect claudlobby --root {os.path.realpath(h.root)} host update status" in msg, msg
        assert "commit(s) behind" not in msg and "2 commit" not in msg, msg
        if serve:
            events = h.events()
            assert '"type":"source_unknown"' in events and "source_behind" not in events

    def test_a_revision_read_again_clears_source_unknown(self, tmp_path, *, scratch_plane_env):
        # #2384: the clear of source_unknown, pinned. Left in place, its marker
        # would hold back the next refusal's notice for the re-notify window.
        h = Harness(tmp_path, behind=0, scratch_plane_env=scratch_plane_env)
        marker = os.path.join(h.root, "state", "currency", "root.source_unknown")
        running = h.head()
        h.answer(_envelope(error=CONFLICT), rc=4)
        assert h.run().returncode == 0
        assert len(h.captured()) == 1 and os.path.exists(marker), "precondition: notified"
        h.runs(running)
        assert h.run().returncode == 0
        assert not os.path.exists(marker), "a revision read again left the source_unknown marker"
        assert len(h.captured()) == 1, "in sync: nothing to send"
        h.answer(_envelope(error=CONFLICT), rc=4)
        assert h.run().returncode == 0
        assert len(h.captured()) == 2, "a refusal after the clear was held back"

    @pytest.mark.parametrize("tag", [{"tag_after": "v2.0.0"}, {"tag_at_seed": "v1.0.0"}],
                             ids=["behind-a-tag", "release-gap"])
    def test_the_tag_counts_start_from_the_running_revision_too(
            self, tag, tmp_path, *, scratch_plane_env):
        h = Harness(tmp_path, behind=2, scratch_plane_env=scratch_plane_env, **tag)
        h.runs(h.origin_rev())
        r = h.run()
        assert r.returncode == 0, r.stderr
        assert h.captured() == []
        assert "IN SYNC with origin/main and release v" in h.log()


class TestTheGitRuntimeRead:
    """The read end to end (#2886): the real CLI's `config diff` against a
    committed Git runtime (tests/fixtures/current_runtime.py), reached through
    INSTALL/bin/claudlobby with no interpreter beside it, the layout the issue
    reported. Here the launcher execs the test CLI, so the installation's
    checkout, its committed configuration and the read are production's; the
    stand-in CLI of the tests above prints the envelope this read prints.

    The data root is a checkout as well, as on a host converted from sealed
    releases: origin holds the installation's history and the commits beyond
    the revision it runs, and the root's files are left as the runtime wrote
    them."""

    def _host(self, current_runtime, test_cli, tmp_path, *, behind):
        root, installation, _, _ = current_runtime
        revision = installation.preflight()
        branch = _git(installation.source, "symbolic-ref", "--short", "HEAD")
        origin = tmp_path / "origin"
        _git(tmp_path, "clone", "--quiet", str(installation.source), str(origin))
        for i in range(behind):
            _commit(str(origin), f"ahead-{i}")
        _git(root, "init", "--quiet")
        _git(root, "config", "maintenance.auto", "false")
        _git(root, "remote", "add", "origin", str(origin))
        _git(root, "fetch", "--quiet", "origin")
        _git(root, "update-ref", f"refs/heads/{branch}", revision)
        _git(root, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        _git(root, "remote", "set-head", "origin", branch)
        _write_exec(installation.cli, f'#!/bin/sh\nexec "{test_cli}" "$@"\n')
        assert not os.path.exists(installation.bin / "python"), "precondition: no interpreter beside it"
        native = _native_fixture(tmp_path, "notify-behind.sh")
        bot = root / "runtime" / "bots" / "tbot"
        bot.mkdir(parents=True, exist_ok=True)
        (bot / "bot.conf").write_text('export TELEGRAM_GROUP_CHAT_ID="-100123"\n'
                                      'export TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-tbot"\n')
        return root, installation, revision, branch, origin, native

    def _run(self, root, installation, native, tmp_path, scratch_plane_env):
        capture = tmp_path / "tg-capture"
        env = _scrubbed_env(TG_CAPTURE=str(capture), CLAUDLOBBY_CLI=str(installation.cli),
                            CLAUDLOBBY_INSTALLATION=str(installation.directory),
                            **scratch_plane_env(root))
        r = subprocess.run(["bash", str(native / "notify-behind.sh")], env=env,
                           capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stderr
        sent = capture.read_text().splitlines() if capture.exists() else []
        log = (root / "state" / "notify-behind.log").read_text()
        return [line.split("|", 2)[2] for line in sent if line.strip()], log

    def test_a_git_runtime_root_counts_the_commits_its_revision_lacks(
            self, current_runtime, test_cli, tmp_path, *, scratch_plane_env):
        root, installation, revision, branch, origin, native = self._host(
            current_runtime, test_cli, tmp_path, behind=3)
        messages, log = self._run(root, installation, native, tmp_path, scratch_plane_env)
        assert f"RUNNING {revision}" in log, log
        (msg,) = messages
        assert "FLEET NOTICE [source_behind]:" in msg, msg
        assert f"runs {revision[:8]} (" in msg and f"is 3 commit(s) behind origin/{branch}" in msg, msg
        assert f"host update apply {_git(origin, 'rev-parse', 'HEAD')}, then host update status" in msg, msg

    def test_an_installation_its_configuration_does_not_match_is_said_and_not_counted(
            self, current_runtime, test_cli, tmp_path, *, scratch_plane_env):
        root, installation, revision, branch, origin, native = self._host(
            current_runtime, test_cli, tmp_path, behind=3)
        _commit(str(installation.source), "moved-by-hand")  # HEAD is no longer what was committed
        messages, log = self._run(root, installation, native, tmp_path, scratch_plane_env)
        (msg,) = messages
        assert "FLEET NOTICE [source_unknown]:" in msg, msg
        assert ("the source revision this host runs cannot be read (claudlobby config diff refused "
                "(exit 4): conflict: committed Git configuration drift is unavailable)") in msg, msg
        assert "host update status" in msg and "commit(s) behind" not in msg, msg
        assert "RUNNING REVISION UNKNOWN" in log, log
