"""Exact native start/refusal and grant/readiness boundaries, with no services."""

import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest

from claudlobby import activation_runtime as runtime, activation_state as state, runtime_admission as admission
from claudlobby.releases import read_release
from claudlobby.supervision_inventory import Adapter as NativeAdapter
from tests.test_config_plan import proposal
from tests.test_releases import installed
from tests.test_runtime_admission import _starting

NATIVE = Path(__file__).resolve().parents[1] / "lib"


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("ar")  # AF_UNIX path bound


def bash(script, *args):
    process = subprocess.Popen(["/bin/bash", "-c", script, "activation-fixture", *map(str, args)],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=3)
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


@pytest.mark.parametrize("manager", ["Linux", "Darwin"])
def test_native_exact_start_refuses_active_or_foreign_before_effect(tmp_path, manager):
    file = tmp_path / ("worker.service" if manager == "Linux" else "worker.plist")
    file.write_text("candidate bytes\n")
    target = "worker.service" if manager == "Linux" else "gui/501/worker"
    script = r'''
. "$1/supervisor.sh"
_OS="$2"; file="$3"; target="$4"; mode="$5"; trace="$6"; native_state="$7"
printf '%s' initial > "$native_state"
systemctl() {
    printf '%s\n' "$*" >> "$trace"
    case "$2" in
        show)
            phase=$(cat "$native_state"); load=loaded; active=inactive; enabled=disabled; fragment="$file"
            if [ "$mode" = refuse ]; then fragment=/foreign.service
            elif [ "$phase" = initial ]; then load=masked; enabled=masked-runtime; fragment=/dev/null
            elif [ "$phase" = started ]; then active=active; fi
            printf 'Id=%s\nLoadState=%s\nActiveState=%s\nUnitFileState=%s\nFragmentPath=%s\nControlGroup=\n' \
                "$target" "$load" "$active" "$enabled" "$fragment" ;;
        unmask) printf '%s' unmasked > "$native_state" ;;
        start) printf '%s' started > "$native_state" ;;
    esac
}
launchctl() {
    printf '%s\n' "$*" >> "$trace"
    case "$1" in
        manageruid) echo 501 ;; managername) echo Aqua ;;
        list)
            printf 'PID Status Label\n'
            if [ "$mode" = refuse ] || [ "$(cat "$native_state")" = started ]; then
                printf '123 0 worker\n'
            fi ;;
        bootstrap) printf '%s' started > "$native_state" ;;
    esac
}
svc_activation_start "$file" "$target"
'''
    trace, native_state = tmp_path / "calls", tmp_path / "state"
    refused = bash(script, NATIVE, manager, file, target, "refuse", trace, native_state)
    assert refused.returncode == 3
    assert "start " not in trace.read_text() and "bootstrap " not in trace.read_text()
    trace.write_text("")
    started = bash(script, NATIVE, manager, file, target, "allow", trace, native_state)
    assert started.returncode == 0, started.stderr
    assert started.stdout.strip() == "start-requested"
    calls = trace.read_text()
    if manager == "Linux":
        assert "--user unmask --runtime worker.service" in calls
        assert calls.index("unmask") < calls.index("--user start worker.service")
    else:
        assert f"bootstrap gui/501 {file}" in calls
    assert "enable" not in calls and "restart" not in calls and "install" not in calls
    assert file.read_text() == "candidate bytes\n"


def test_real_restart_policy_fence_is_verified_and_ready_uses_same_marker(tmp_path):
    bot = tmp_path / "bots/worker"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text('RC_READY_TIMEOUT_S="001"\nBOT_SERVICE="fixture-worker"\n'
                                  'TELEGRAM_BOT_HANDLE="fixture_handle"\n')
    prefix = '''fixture_tmux() { [ "$*" = '-L fixture-worker has-session -t worker' ]; }
TMUX_BIN=fixture_tmux; export TMUX_BIN
. "$1/supervisor.sh"; shift; "$@"'''
    fence = bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot)
    assert fence.returncode == 0, fence.stderr
    ceiling, token = fence.stdout.strip().split("\t")
    assert ceiling == "121"  # actual rr_bot_ceiling, including zero-padded config
    override = bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "37")
    assert override.returncode == 0 and override.stdout.split("\t", 1)[0] == "37"
    assert bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "0").returncode != 0
    log = bot / "logs/startup.log"
    assert token in log.read_text()
    with log.open("a") as out:
        out.write("BRIDGE_READY fixture\n")
    ready = bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token)
    assert ready.returncode == 0 and ready.stdout.strip() == "bridge-ready"
    missing = bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", "absent-marker")
    assert missing.returncode != 0
    log.unlink()
    log.mkdir()  # bridge_fence_write is best-effort; the new boundary must refuse
    failed = bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot)
    assert failed.returncode != 0


@pytest.mark.parametrize(("extra_conf", "accepted", "rejected", "readiness"), [
    ("", "READY — non-channel bot, no poller to await",
     "BRIDGE_READY — Telegram poller up", "session-ready"),
    ('TELEGRAM_BOT_HANDLE="fixture_handle"\nEXPECT_NO_TOKEN=1\n',
     "BRIDGE_SKIP — no token by design (EXPECT_NO_TOKEN); canary/throwaway, no alert",
     "READY — non-channel bot, no poller to await", "session-ready"),
    ('TELEGRAM_BOT_HANDLE="fixture_handle"\nEXPECT_NO_TOKEN=1\n',
     "BRIDGE_READY — Telegram poller up",
     "READY — non-channel bot, no poller to await", "bridge-ready"),
    ('TELEGRAM_BOT_HANDLE="fixture_handle"\n',
     "BRIDGE_READY — Telegram poller up",
     "BRIDGE_SKIP — no token by design (EXPECT_NO_TOKEN); canary/throwaway, no alert",
     "bridge-ready"),
])
def test_native_bot_readiness_accepts_only_configured_post_fence_outcome(
    tmp_path, extra_conf, accepted, rejected, readiness
):
    bot = tmp_path / "bots/worker"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text('BOT_SERVICE="fixture-worker"\n' + extra_conf)
    log = bot / "logs/startup.log"
    log.parent.mkdir()
    log.write_text(accepted + "\n")  # an old boot cannot satisfy the new fence
    prefix = '''fixture_tmux() { [ "$*" = '-L fixture-worker has-session -t worker' ]; }
TMUX_BIN=fixture_tmux; export TMUX_BIN
. "$1/supervisor.sh"; shift; "$@"'''
    fence = bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "1")
    assert fence.returncode == 0, fence.stderr
    _, token = fence.stdout.strip().split("\t")
    assert bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token).returncode != 0
    with log.open("a") as out:
        out.write(rejected + "\n")
    assert bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token).returncode != 0
    with log.open("a") as out:
        out.write(accepted + "\n")
    ready = bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token)
    assert ready.returncode == 0 and ready.stdout.strip() == readiness
    no_session = bash(prefix.replace(
        "fixture_tmux() { [ \"$*\" = '-L fixture-worker has-session -t worker' ]; }",
        "fixture_tmux() { return 1; }",
    ), NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token)
    assert no_session.returncode != 0


@pytest.mark.parametrize(("fail_ready", "ready_kind"), [
    (False, "bridge-ready"), (False, "session-ready"), (False, "unknown-ready"),
    (True, "bridge-ready"),
])
def test_start_keeps_exact_grant_through_readiness_and_leaves_completion_to_owner(
    proposal, fail_ready, ready_kind
):
    builder, _, settings = proposal
    plan = builder.seal()
    release = read_release(builder.root, plan.release_id)
    env = {"CLAUDLOBBY_ROOT": str(builder.root), "CLAUDLOBBY_RELEASE_ID": release.release_id}
    env["CLAUDLOBBY_CLI"] = str(release.cli_path)
    env["CLAUDLOBBY_NATIVE_DIR"] = str(release.native_path)
    env["CLAUDLOBBY_ARTIFACT_ID"] = release.inputs.artifact_id
    argv = admission.wrap_unit_argv(env, unit="worker", phase="bots", mode="exec",
                                    argv=[str(release.native_path / "start-bot.sh"), str(settings.parent)])
    unit = admission.parse_unit_argv(argv)
    file = builder.root / "worker.plist"
    target = "gui/501/worker"
    file.write_text("frozen published fixture\n")
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    calls = []

    class ReachedExec(Exception):
        pass

    class Adapter:
        package = SimpleNamespace(native=release.native_path)

        def call(self, function, *args, timeout=30):
            calls.append(function)
            assert (builder.root / "state/activation-start.sock").exists()
            with pytest.raises(state.ActivationError, match="holds the lock"):
                with state.locked_activation(builder.root):
                    pytest.fail("EX was released before readiness")
            if function == "svc_activation_bot_fence":
                output = "210\tRR_FENCE_fixture\n"
            elif function == "svc_activation_start":
                identity = admission.RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)

                def execer(*_):
                    raise ReachedExec

                with pytest.raises(ReachedExec):
                    admission.run_unit(argv, identity=identity, environment=env, execer=execer)
                output = "start-requested\n"
            elif function == "svc_activation_bot_ready":
                assert timeout == 220
                if fail_ready:
                    return subprocess.CompletedProcess([function], 1, "", "fixture readiness failure")
                output = ready_kind + "\n"
            elif function == "svc_activation_snapshot":
                output = "unchanged loaded inactive\n"  # successful launchd spawner exits
            else:
                pytest.fail(f"unexpected call after failed readiness: {function}")
            return subprocess.CompletedProcess([function], 0, output, "")

    with state.locked_activation(builder.root) as store:
        _starting(store, plan)
        with pytest.raises(runtime.RuntimeEvidenceError, match="published bytes changed"):
            runtime.start_unit(store, "candidate", installed_file=file, target=target,
                               unit=unit, sha256="0" * 64, adapter=Adapter())
        assert calls == []
        if fail_ready or ready_kind == "unknown-ready":
            with pytest.raises(runtime.RuntimeEvidenceError, match=(
                "native refusal" if fail_ready else "bot readiness evidence unavailable")):
                runtime.start_unit(store, "candidate", installed_file=file, target=target,
                                   unit=unit, sha256=digest, adapter=Adapter())
        else:
            result = runtime.start_unit(store, "candidate", installed_file=file, target=target,
                                        unit=unit, sha256=digest, adapter=Adapter())
            assert result.details["native"] == "unchanged loaded inactive"
            assert result.details["readiness"]["kind"] == ready_kind
            assert len(result.digest) == 64
        assert state.read_activation(builder.root, "candidate").body["pending"] == "bots_started"
        assert not (builder.root / "state/activation-start.sock").exists()
    assert calls == ["svc_activation_bot_fence", "svc_activation_start", "svc_activation_bot_ready"] + (
        [] if fail_ready or ready_kind == "unknown-ready" else ["svc_activation_snapshot"])


def test_adapter_timeout_reaps_its_poll_group_without_delayed_effect(tmp_path):
    late = tmp_path / "late"
    with pytest.raises(subprocess.TimeoutExpired):
        NativeAdapter._run(["/bin/bash", "-c", '( /bin/sleep 0.2; echo late > "$1" ) & wait',
                            "private-timeout", str(late)], timeout=0.05, env=dict(os.environ),
                           capture_output=True, text=True)
    time.sleep(0.3)
    assert not late.exists()


def test_quiet_known_socket_and_pid_are_required_not_inferred_from_inactive(tmp_path):
    class Adapter:
        def call(self, *args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "inactive\tno-cgroup-witness\n", "")

    path = tmp_path / "tmux.sock"
    args = dict(installed_file=tmp_path / "worker.service", target="worker.service", socket_path=path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        with pytest.raises(runtime.RuntimeEvidenceError, match="still accepts"):
            runtime.assert_quiescent(Adapter(), **args)
    with pytest.raises(runtime.RuntimeEvidenceError, match="known process"):
        runtime.assert_quiescent(Adapter(), **args, known_pids=(os.getpid(),))
    observed = runtime.assert_quiescent(Adapter(), **args)
    assert observed.details["socket_state"] == "refused"
    assert observed.details["coverage"] == "named-unit-and-supplied-witnesses-only"
    assert path.exists()  # observation never unlinks a socket
