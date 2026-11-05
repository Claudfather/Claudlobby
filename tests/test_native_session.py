"""Exact native continuation receipts; no Claude process, model, or service is launched.

The SessionStart payload shape is from Claude Code 2.1.318 captures retained in
native-resume-proof/hooks (startup and resume, tools-disabled print probe).
IDs/paths below are synthetic. This is shape evidence, not interactive acceptance.
Provider PID records are grounded in native-session-adoption-20261104's
sanitized-darwin-shape.json (Claude 2.1.318) and sanitized-linux-shape.json
(Claude 2.1.316). The Linux capture matched kernel start ticks, machine ID and
process PID namespace; it was read-only evidence, not update acceptance.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from claudlobby import bot_operations, native_session as native
from claudlobby.isolation import transcript_slug


SID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
LINUX_DOMAIN = "linux:00000000000000000000000000000000:pid:[4026531836]"
LINUX_START = "12345678"


def session_start(bot, account, sid, *, source="startup"):
    # Exact native startup keys; capture_pid/capture_ppid were probe additions.
    return {"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(bot),
            "transcript_path": str(account / "projects" / transcript_slug(bot) / (sid + ".jsonl")),
            "source": source}


def retain_native_session(bot, label, tmpdir, account, adapter):
    """Create an owned launch and genuine matching hook through production owners."""
    account.mkdir(parents=True, exist_ok=True)
    mode, sid = native.launch(bot, label, tmpdir, account)
    transcript = Path(native.read(bot)["transcript"])
    transcript.parent.mkdir(parents=True, exist_ok=True)
    if not transcript.exists():
        # Opaque file bytes: transcript content parsing is not this owner's contract.
        transcript.write_bytes(b"retained native context\n")
    native.capture(bot, label, tmpdir, account, adapter,
                   session_start(bot, account, sid, source="resume" if mode == "resume" else "startup"),
                   caller=os.getpid())
    return native.read(bot)


class Observation:
    def __init__(self, root):
        self.package = SimpleNamespace(installation=SimpleNamespace(data_root=root))
        self.pid, self.generation, self.idle = 4242, 1700000000, True
        self.kernel_start = "Darwin:Wed Nov  4 12:20:00 2026"
        self.calls, self.reject_caller = [], False

    def call(self, function, *args, **kwargs):
        assert function == "svc_bot_native_session", "continuation must not send a model request"
        self.calls.append((function, args))
        rc = 3 if self.reject_caller and len(args) == 4 else 0
        start = "\t" + self.kernel_start if args[3:] == ("", "start") else ""
        return subprocess.CompletedProcess([function], rc,
            f"{self.pid}\t{self.generation}\t{'idle' if self.idle else 'unready'}{start}\n", "")


@pytest.fixture
def bound(tmp_path, monkeypatch):
    bot, account, tmpdir = tmp_path / "bot", tmp_path / "account", tmp_path / "tmux"
    (bot / "data").mkdir(parents=True)
    adapter = Observation(tmp_path)
    context = SimpleNamespace(paths=object())
    monkeypatch.setattr(native, "_context", lambda *_: (context, account))
    monkeypatch.setattr(native, "require_provider", lambda _: None)
    receipt = retain_native_session(bot, "example.worker", tmpdir, account, adapter)
    return SimpleNamespace(bot=bot, account=account, tmpdir=tmpdir, adapter=adapter, receipt=receipt,
                           label="example.worker")


def provider_record(bot, account, adapter, sid):
    """Author the captured provider shape at its exact PID filename."""
    path = account / "sessions" / (str(adapter.pid) + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"pid": adapter.pid, "sessionId": sid, "cwd": str(bot),
        "startedAt": 1793790000000, "procStart": "Wed Nov  4 12:20:00 2026",
        "version": "2.1.318", "kind": "interactive", "entrypoint": "cli", "pidDomain": "darwin"}
    if adapter.kernel_start.startswith("Linux:"):
        _, ticks, domain = adapter.kernel_start.split(":", 2)
        value.update(procStart=ticks, pidDomain=domain, version="2.1.316")
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def older(bound):
    b = bound
    (b.bot / "data" / native.NAME).unlink()
    (b.bot / "data/.spawn").touch()
    b.provider = provider_record(b.bot, b.account, b.adapter, b.receipt["session_id"])
    b.adapter.calls.clear()
    return b


@pytest.fixture
def older_linux(older):
    b = older
    b.adapter.kernel_start = "Linux:" + LINUX_START + ":" + LINUX_DOMAIN
    b.provider = provider_record(b.bot, b.account, b.adapter, b.receipt["session_id"])
    return b


def test_exact_session_continues_despite_newer_decoy_and_requires_matching_capture(bound):
    b = bound
    decoy = Path(b.receipt["transcript"]).with_name(OTHER + ".jsonl")
    decoy.write_bytes(b"newer unrelated headless session\n")
    proof = native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="update-one")
    assert proof["session"]["session_id"] == b.receipt["session_id"]
    assert json.loads(json.dumps(proof)) == proof
    assert native.launch(b.bot, b.label, b.tmpdir, b.account) == ("resume", b.receipt["session_id"])
    with pytest.raises(native.NativeSessionError, match="not confirmed"):
        native.confirm(b.bot, expected=proof["session"])
    pending = (b.bot / "data" / native.NAME).read_bytes()
    with pytest.raises(native.NativeSessionError, match="retained launch identity"):
        native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter,
                       session_start(b.bot, b.account, OTHER, source="resume"), caller=123)
    assert (b.bot / "data" / native.NAME).read_bytes() == pending
    b.adapter.pid += 1
    payload = session_start(b.bot, b.account, b.receipt["session_id"], source="resume")
    # Native resume includes optional metrics and still has no permission_mode.
    payload.update(context_tokens=3, estimated_cache_write_usd=0.0,
                   prompt_cache_likely_expired=False, seconds_since_last_response=1)
    native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter, payload, caller=os.getpid())
    confirmed = native.confirm(b.bot, expected=proof["session"])
    assert confirmed["process"] != proof["session"]["process"]
    assert confirmed["session_id"] == b.receipt["session_id"]
    assert all(name == "svc_bot_native_session" for name, _ in b.adapter.calls)
    assert all(args[3:] != ("", "start") for _, args in b.adapter.calls)


def test_clear_rotates_identity_only_inside_the_recorded_process(bound):
    b = bound
    payload = session_start(b.bot, b.account, OTHER, source="clear")
    native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter, payload, caller=123)
    assert native.read(b.bot)["session_id"] == OTHER
    assert b.adapter.calls[-1][1][-1] == "123"
    before = (b.bot / "data" / native.NAME).read_bytes()
    b.adapter.pid += 1
    with pytest.raises(native.NativeSessionError, match="recorded native process"):
        native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter,
                       session_start(b.bot, b.account, SID, source="clear"), caller=123)
    assert (b.bot / "data" / native.NAME).read_bytes() == before


def test_headless_or_foreign_hook_cannot_replace_retained_identity(bound):
    b = bound
    before = (b.bot / "data" / native.NAME).read_bytes()
    b.adapter.reject_caller = True
    with pytest.raises(native.NativeSessionError, match="observation"):
        native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter,
                       session_start(b.bot, b.account, b.receipt["session_id"]), caller=999)
    assert (b.bot / "data" / native.NAME).read_bytes() == before
    b.adapter.reject_caller = False
    with pytest.raises(native.NativeSessionError):
        native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter,
                       session_start(b.bot, b.account, OTHER), caller=123)
    assert (b.bot / "data" / native.NAME).read_bytes() == before


@pytest.mark.parametrize("source", ["compact", "clear"])
@pytest.mark.parametrize("where", ["projects/example-checkout", "memory"])
def test_session_working_below_the_bot_directory_is_captured_and_resumed(bound, monkeypatch, capsys, where, source):
    # SessionStart names the session's current directory, which follows its shell
    # into a checkout. The composed hook command drives this entry with the payload.
    import io
    import sys
    from claudlobby import resources, supervision_inventory
    b = bound
    monkeypatch.setattr(resources, "get_resources", lambda: b.adapter.package)
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda _package: b.adapter)
    sid = OTHER if source == "clear" else b.receipt["session_id"]
    (b.bot / where).mkdir(parents=True)
    payload = {**session_start(b.bot, b.account, sid, source=source), "cwd": str(b.bot / where)}
    Path(payload["transcript_path"]).write_bytes(b"context after the session moved into a subdirectory\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    args = SimpleNamespace(root=b.bot.parent, directory=b.bot, label=b.label, tmpdir=b.tmpdir, action="capture")
    assert native.entry(args) == 0, capsys.readouterr().err
    receipt = native.read(b.bot)
    assert receipt["session_id"] == sid and receipt["cwd"] == str(b.bot)
    assert native.launch(b.bot, b.label, b.tmpdir, b.account) == ("resume", sid)


@pytest.mark.parametrize("where", ["parent", "another bot", "name-prefix sibling", "unrelated",
                                   "dot-dot escape", "symlink out", "relative", "missing", "nul byte"])
def test_session_outside_the_bot_directory_is_refused_before_observation(bound, tmp_path, where):
    b = bound
    elsewhere, another = tmp_path / "elsewhere", b.bot.parent / "another-bot"
    for directory in (elsewhere, another, b.bot / "projects", Path(str(b.bot) + "-two")):
        directory.mkdir(exist_ok=True)
    (b.bot / "projects" / "link").symlink_to(elsewhere, target_is_directory=True)
    cwd = {"parent": str(b.bot.parent), "another bot": str(another),
           "name-prefix sibling": str(b.bot) + "-two", "unrelated": str(elsewhere),
           "dot-dot escape": str(b.bot / "projects" / ".." / ".." / "another-bot"),
           "symlink out": str(b.bot / "projects" / "link"), "relative": "projects",
           "missing": None, "nul byte": str(b.bot / "projects") + "\0"}[where]
    payload = {**session_start(b.bot, b.account, b.receipt["session_id"], source="compact"), "cwd": cwd}
    if cwd is None:
        del payload["cwd"]
    before, calls = (b.bot / "data" / native.NAME).read_bytes(), len(b.adapter.calls)
    with pytest.raises(native.NativeSessionError, match="unsupported native SessionStart identity"):
        native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter, payload, caller=123)
    assert (b.bot / "data" / native.NAME).read_bytes() == before
    assert len(b.adapter.calls) == calls


@pytest.mark.parametrize("drift", ["account", "label", "tmpdir", "transcript"])
def test_continuation_refuses_binding_drift_without_overwriting_receipt(bound, drift):
    b = bound
    before = (b.bot / "data" / native.NAME).read_bytes()
    if drift == "transcript":
        changed = deepcopy(native.read(b.bot)); changed["transcript"] = str(b.account / "foreign.jsonl")
        native._write(b.bot, changed, expected=native.read(b.bot))
        before = (b.bot / "data" / native.NAME).read_bytes()
    with pytest.raises(native.NativeSessionError):
        native.launch(b.bot, "foreign" if drift == "label" else b.label,
                      b.tmpdir / "other" if drift == "tmpdir" else b.tmpdir,
                      b.account / "other" if drift == "account" else b.account)
    assert (b.bot / "data" / native.NAME).read_bytes() == before


@pytest.mark.parametrize("mutation", ["prefix", "truncate", "replacement"])
def test_stopped_proof_allows_appended_session_end_but_refuses_lost_prefix(bound, mutation):
    b = bound
    proof = native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    transcript = Path(b.receipt["transcript"])
    transcript.write_bytes(transcript.read_bytes() + b"appended SessionEnd bytes\n")
    bot_operations.verify_checkpoint_after_stop(b.bot, proof)
    if mutation == "prefix":
        data = transcript.read_bytes(); transcript.write_bytes(b"X" + data[1:])
    elif mutation == "truncate":
        transcript.write_bytes(b"short\n")
    else:
        replacement = transcript.with_suffix(".replacement")
        replacement.write_bytes(transcript.read_bytes()); replacement.replace(transcript)
    with pytest.raises(native.NativeSessionError):
        bot_operations.verify_checkpoint_after_stop(b.bot, proof)


def test_prior_spawn_without_receipt_requires_explicit_adoption(tmp_path):
    bot = tmp_path / "bot"; (bot / "data").mkdir(parents=True)
    (bot / "data/.spawn").touch()
    with pytest.raises(native.NativeSessionError, match="adoption"):
        native.launch(bot, "example.worker", tmp_path / "tmux", tmp_path / "account")
    assert not (bot / "data" / native.NAME).exists()


def test_absent_receipt_adopts_exact_pane_pid_despite_newer_decoy_and_optional_handoff(older):
    b = older
    provider_bytes = b.provider.read_bytes()
    decoy = b.provider.with_name(str(b.adapter.pid + 1) + ".json")
    decoy.write_text(json.dumps({**json.loads(provider_bytes), "pid": b.adapter.pid + 1, "sessionId": OTHER}))
    os.utime(decoy, ns=(decoy.stat().st_atime_ns, b.provider.stat().st_mtime_ns + 1_000_000_000))
    transcript_decoy = Path(b.receipt["transcript"]).with_name(OTHER + ".jsonl")
    transcript_decoy.write_bytes(b"newer unrelated session\n")
    os.utime(transcript_decoy, ns=(transcript_decoy.stat().st_atime_ns,
                                 Path(b.receipt["transcript"]).stat().st_mtime_ns + 1_000_000_000))
    optional = b.bot / "data/.handoff-request"
    optional.write_bytes(b'{"request_id":"original-unknown","state":"pending"}\n')
    original_optional = optional.read_bytes(), optional.stat().st_ino

    proof = bot_operations.save_planned_checkpoint(b.bot, b.label, str(b.tmpdir), b.adapter,
                                                  operation_id="original-update")
    assert proof["kind"] == "native-session-v1" and proof["operation_id"] == "original-update"
    assert proof["session"]["session_id"] == b.receipt["session_id"]
    assert proof["session"]["process"] == f"{b.adapter.generation}:{b.adapter.pid}"
    assert proof["session"]["provider_start"] == b.adapter.kernel_start
    assert native.read(b.bot) == proof["session"] and not proof["session"]["pending"]
    assert all(args[3:] == ("", "start") for _, args in b.adapter.calls)
    bot_operations.verify_checkpoint_after_stop(b.bot, proof)
    assert native.launch(b.bot, b.label, b.tmpdir, b.account) == ("resume", b.receipt["session_id"])
    assert native.read(b.bot)["provider_start"] == b.adapter.kernel_start
    b.adapter.pid += 2
    native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter,
                   session_start(b.bot, b.account, b.receipt["session_id"], source="resume"), caller=123)
    assert native.confirm(b.bot, expected=proof["session"])["session_id"] == b.receipt["session_id"]
    assert "provider_start" not in native.read(b.bot)
    assert (optional.read_bytes(), optional.stat().st_ino) == original_optional
    assert b.provider.read_bytes() == provider_bytes
    assert all(name == "svc_bot_native_session" for name, _ in b.adapter.calls)


def test_linux_adoption_matches_exact_pid_start_machine_and_namespace_then_resumes(older_linux):
    b = older_linux
    decoy = b.provider.with_name(str(b.adapter.pid + 1) + ".json")
    decoy.write_text(json.dumps({**json.loads(b.provider.read_bytes()), "pid": b.adapter.pid + 1, "sessionId": OTHER}))
    proof = native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original-linux")
    assert proof["session"]["session_id"] == b.receipt["session_id"]
    assert proof["session"]["provider_start"] == b.adapter.kernel_start
    assert native.read(b.bot) == proof["session"]
    native.verify_live(b.bot, proof, b.adapter)
    native.verify(b.bot, proof)
    assert native.launch(b.bot, b.label, b.tmpdir, b.account) == ("resume", b.receipt["session_id"])
    assert native.read(b.bot)["provider_start"] == b.adapter.kernel_start
    b.adapter.pid += 2
    native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter,
                   session_start(b.bot, b.account, b.receipt["session_id"], source="resume"), caller=123)
    assert "provider_start" not in native.confirm(b.bot, expected=proof["session"])
    assert all(name == "svc_bot_native_session" for name, _ in b.adapter.calls)


@pytest.mark.parametrize("drift", ["machine-id", "pid-namespace", "start-ticks"])
def test_linux_adoption_refuses_mismatched_provider_kernel_binding_without_receipt(older_linux, drift):
    b = older_linux
    value = json.loads(b.provider.read_bytes())
    if drift == "machine-id":
        value["pidDomain"] = "linux:" + "1" * 32 + ":pid:[4026531836]"
    elif drift == "pid-namespace":
        value["pidDomain"] = "linux:" + "0" * 32 + ":pid:[4026531837]"
    else:
        value["procStart"] = str(int(LINUX_START) + 1)
    b.provider.write_text(json.dumps(value))
    before = b.provider.read_bytes()
    with pytest.raises(native.NativeSessionError):
        native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original-linux")
    assert not (b.bot / "data" / native.NAME).exists()
    assert b.provider.read_bytes() == before and (b.bot / "data/.spawn").exists()
    assert all(name == "svc_bot_native_session" for name, _ in b.adapter.calls)


@pytest.mark.parametrize("drift", ["stale-start", "cwd", "account", "pid"])
def test_absent_receipt_refuses_provider_drift_without_publishing_or_waiting(older, monkeypatch, drift):
    b = older
    value = json.loads(b.provider.read_bytes())
    if drift == "stale-start":
        value["procStart"] = "Tue Nov  3 12:20:00 2026"
    elif drift == "cwd":
        value["cwd"] = str(b.bot.parent / "other-bot")
    elif drift == "account":
        other = b.account.parent / "other-account/sessions"; other.mkdir(parents=True)
        b.provider.rename(other / b.provider.name)
    elif drift == "pid":
        value["pid"] += 1
    if drift != "account":
        b.provider.write_text(json.dumps(value))
    monkeypatch.setattr(bot_operations.time, "sleep", lambda _: pytest.fail("permanent identity errors must not wait"))
    with pytest.raises(bot_operations.BotLifecycleError):
        bot_operations.save_planned_checkpoint(b.bot, b.label, str(b.tmpdir), b.adapter, operation_id="original")
    assert not (b.bot / "data" / native.NAME).exists()
    assert (b.bot / "data/.spawn").exists()
    assert all(name == "svc_bot_native_session" for name, _ in b.adapter.calls)


@pytest.mark.parametrize("invalid", ["malformed-receipt", "wrong-account-receipt", "symlink-provider", "oversize-provider"])
def test_adoption_never_replaces_invalid_existing_evidence(older, invalid):
    b = older
    receipt = b.bot / "data" / native.NAME
    if invalid == "malformed-receipt":
        receipt.write_bytes(b"{malformed original receipt\n")
    elif invalid == "wrong-account-receipt":
        receipt.write_text(json.dumps({**b.receipt, "account": str(b.account.parent / "foreign")}))
    elif invalid == "symlink-provider":
        target = b.provider.with_suffix(".original")
        b.provider.rename(target); b.provider.symlink_to(target)
    else:
        b.provider.write_bytes(b" " * (native.TAIL_BYTES + 1))
    before = receipt.read_bytes() if receipt.exists() else None
    with pytest.raises((native.NativeSessionError, OSError)):
        native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    assert (receipt.read_bytes() if receipt.exists() else None) == before
    if before is not None:
        assert b.adapter.calls == []


def test_adoption_rereads_provider_clear_identity_before_publication(older, monkeypatch):
    b = older
    original_observe, observations = native.observe, []
    def clear_during_second_observation(*args, **kwargs):
        observed = original_observe(*args, **kwargs)
        observations.append(observed)
        if len(observations) == 2:
            provider_record(b.bot, b.account, b.adapter, OTHER)
            Path(b.receipt["transcript"]).with_name(OTHER + ".jsonl").write_bytes(b"new clear context\n")
        return observed
    monkeypatch.setattr(native, "observe", clear_during_second_observation)
    with pytest.raises(native.NativeSessionError, match="session changed"):
        native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    assert not (b.bot / "data" / native.NAME).exists()
    assert json.loads(b.provider.read_bytes())["sessionId"] == OTHER


def test_adopted_legacy_process_tracks_clear_until_new_hook_confirms_resume(older):
    b = older
    old = native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    provider_record(b.bot, b.account, b.adapter, OTHER)
    Path(b.receipt["transcript"]).with_name(OTHER + ".jsonl").write_bytes(b"context after legacy clear\n")
    refreshed = native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    assert refreshed["session"]["session_id"] == OTHER
    assert refreshed["session"]["provider_start"] == old["session"]["provider_start"]
    with pytest.raises(native.NativeSessionError):
        native.verify(b.bot, old)
    assert native.launch(b.bot, b.label, b.tmpdir, b.account) == ("resume", OTHER)
    assert native.read(b.bot)["provider_start"] == b.adapter.kernel_start
    b.adapter.pid += 1
    native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter,
                   session_start(b.bot, b.account, OTHER, source="resume"), caller=123)
    assert "provider_start" not in native.confirm(b.bot, expected=refreshed["session"])
    b.provider.unlink()
    b.adapter.calls.clear()
    hooked = native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="later")
    assert hooked["session"]["session_id"] == OTHER
    assert all(args[3:] == () for _, args in b.adapter.calls)


@pytest.mark.parametrize("drift", ["process", "account"])
def test_adopted_legacy_receipt_refuses_changed_process_or_account(older, monkeypatch, drift):
    b = older
    native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    before = (b.bot / "data" / native.NAME).read_bytes()
    if drift == "process":
        b.adapter.pid += 1
        provider_record(b.bot, b.account, b.adapter, OTHER)
    else:
        account = b.account.parent / "foreign-account"
        provider_record(b.bot, account, b.adapter, OTHER)
        monkeypatch.setattr(native, "_context", lambda *_: (object(), account))
    with pytest.raises(native.NativeSessionError):
        native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    assert (b.bot / "data" / native.NAME).read_bytes() == before


def test_absent_receipt_waits_for_idle_then_adopts_in_same_operation(older, monkeypatch):
    b = older
    b.adapter.idle = False
    waits = []
    def become_idle(seconds):
        assert native.read(b.bot) is None
        waits.append(seconds); b.adapter.idle = True
    monkeypatch.setattr(bot_operations.time, "sleep", become_idle)
    proof = bot_operations.save_planned_checkpoint(b.bot, b.label, str(b.tmpdir), b.adapter,
                                                  operation_id="original")
    assert len(waits) == 1 and proof["operation_id"] == "original"
    assert proof["session"]["session_id"] == b.receipt["session_id"]
    assert all(name == "svc_bot_native_session" for name, _ in b.adapter.calls)


def test_native_observer_reports_actual_kernel_start_without_starting_processes(tmp_path):
    import sys
    from tests.conftest import constructed_env

    bot, tmpdir = tmp_path / "worker", tmp_path / "tmux"
    bot.mkdir(); tmpdir.mkdir()
    (bot / "bot.conf").write_text(f'BOT_SERVICE="example.worker"\nTMUX_TMPDIR="{tmpdir}"\n')
    scripts = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts"
    # Only tmux queries are substituted; the kernel observation reads this
    # already-running pytest process. No session, service or provider starts.
    snippet = r'''
fixture_tmux() {
    case "$3" in
        list-panes) printf '%s\n' "$FIXTURE_PID" ;;
        display-message) printf '1700000000\n' ;;
        capture-pane) printf '\n❯ \n' ;;
        *) return 3 ;;
    esac
}
export TMUX_BIN=fixture_tmux
. "$1/supervisor.sh"
svc_bot_native_session "$2" example.worker "$3" '' start
'''
    env = constructed_env(FIXTURE_PID=str(os.getpid()), LC_ALL="C", TZ="Pacific/Honolulu",
                          CLAUDLOBBY_NATIVE_PYTHON=sys.executable)
    result = subprocess.run(["/bin/bash", "-c", snippet, "observer-fixture", str(scripts), str(bot), str(tmpdir)],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    pid, generation, _idle, kernel_start = result.stdout.strip().split("\t")
    if os.uname().sysname == "Linux":
        process = Path("/proc") / str(os.getpid())
        ticks = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
        expected = ticks + ":linux:" + Path("/etc/machine-id").read_text().strip() + ":" + os.readlink(process / "ns/pid")
    else:
        expected = subprocess.check_output(["ps", "-p", str(os.getpid()), "-o", "lstart="],
                                          env={**env, "TZ": "UTC"}, text=True).strip()
    assert pid == str(os.getpid()) and generation == "1700000000"
    assert kernel_start == os.uname().sysname + ":" + expected


def test_busy_then_idle_completes_one_checkpoint_without_model_send(bound, monkeypatch):
    b = bound
    b.adapter.idle = False
    sleeps = []
    def idle_after_wait(seconds):
        sleeps.append(seconds); b.adapter.idle = True
    monkeypatch.setattr(bot_operations.time, "sleep", idle_after_wait)
    proof = bot_operations.save_planned_checkpoint(b.bot, b.label, b.tmpdir, b.adapter,
                                                 operation_id="original")
    assert sleeps and proof["operation_id"] == "original"
    assert proof["session"]["session_id"] == b.receipt["session_id"]
    assert all(name == "svc_bot_native_session" for name, _ in b.adapter.calls)


def test_permanent_identity_error_does_not_wait_or_replace_receipt(bound, monkeypatch):
    b = bound
    before = (b.bot / "data" / native.NAME).read_bytes()
    b.adapter.pid += 1
    monkeypatch.setattr(bot_operations.time, "sleep", lambda _: pytest.fail("identity drift must not wait"))
    with pytest.raises(bot_operations.BotLifecycleError, match="recorded process"):
        bot_operations.save_planned_checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    assert (b.bot / "data" / native.NAME).read_bytes() == before


def test_checkpoint_does_not_overwrite_clear_captured_during_final_observation(bound, monkeypatch):
    b = bound
    original_observe = native.observe
    observations = []
    newer = []
    def clear_after_observation(*args, **kwargs):
        current = original_observe(*args, **kwargs)
        observations.append(current)
        if len(observations) == 2:
            payload = session_start(b.bot, b.account, OTHER, source="clear")
            Path(payload["transcript_path"]).write_bytes(b"new clear session context\n")
            native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter, payload, caller=123)
            newer.append((b.bot / "data" / native.NAME).read_bytes())
        return current
    monkeypatch.setattr(native, "observe", clear_after_observation)
    with pytest.raises(native.NativeSessionError):
        native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter, operation_id="original")
    assert newer and (b.bot / "data" / native.NAME).read_bytes() == newer[0]
    assert native.read(b.bot)["session_id"] == OTHER


@pytest.mark.parametrize("version,accepted", [("2.1.318", True), ("2.1.256", False), (None, False)])
def test_native_resume_measures_authored_provider_on_composed_path(tmp_path, monkeypatch, version, accepted):
    import shlex
    from claudlobby import composer
    from tests.conftest import _write_exec
    from tests.test_claude_version import _paths, _version_tool_path
    canonical = _version_tool_path(tmp_path)
    provider, decoy = tmp_path / "configured-provider", tmp_path / "ambient-provider"
    executed, wrong = tmp_path / "configured-executed", tmp_path / "ambient-executed"
    _write_exec(provider, "#!/bin/bash\nprintf '%s' \"$*\" > " + shlex.quote(str(executed)) +
                "\nprintf '%s\\n' '" + (version + " (Claude Code)" if version else "broken provider") + "'\n")
    _write_exec(decoy, "#!/bin/bash\ntouch " + shlex.quote(str(wrong)) + "\necho '9.9.9 (Claude Code)'\n")
    context = SimpleNamespace(paths=_paths(tmp_path), bot_id="worker",
        fleet=SimpleNamespace(bots={"worker": SimpleNamespace(env={"CLAUDE_BIN": str(provider)})}))
    monkeypatch.setattr(composer, "_scheduler_tool_path", lambda paths: canonical)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_BIN", str(decoy))
    if accepted:
        native.require_provider(context)
    else:
        with pytest.raises(native.NativeSessionError, match="2.1.256" if version else "broken provider"):
            native.require_provider(context)
    assert executed.read_text() == "--version"
    assert not wrong.exists()
    assert os.environ["PATH"] == "/usr/bin:/bin" and os.environ["CLAUDE_BIN"] == str(decoy)


def test_clear_capture_keeps_new_identity_when_checkpoint_is_published_first(bound, monkeypatch):
    b = bound
    original_write = native._write
    interleaved = []
    def checkpoint_before_capture_write(bot, value, **kwargs):
        if value["session_id"] == OTHER and not interleaved:
            interleaved.append(native.checkpoint(b.bot, b.label, b.tmpdir, b.adapter,
                                                operation_id="original"))
        return original_write(bot, value, **kwargs)
    monkeypatch.setattr(native, "_write", checkpoint_before_capture_write)
    payload = session_start(b.bot, b.account, OTHER, source="clear")
    Path(payload["transcript_path"]).write_bytes(b"new clear context\n")
    native.capture(b.bot, b.label, b.tmpdir, b.account, b.adapter, payload, caller=123)
    current = native.read(b.bot)
    assert interleaved and current["session_id"] == OTHER and current["checkpoint"] is None
    with pytest.raises(native.NativeSessionError):
        native.verify(b.bot, interleaved[0])


@pytest.mark.parametrize(("state", "expected_rc"), [
    ("pending", 75), ("confirmed", 0), ("missing", 6), ("malformed", 6),
    ("wrong-account", 6), ("wrong-process", 6),
])
def test_confirm_entry_distinguishes_pending_hook_from_fatal_identity_errors(bound, monkeypatch, state, expected_rc):
    from claudlobby import resources, supervision_inventory
    b = bound
    monkeypatch.setattr(resources, "get_resources", lambda: b.adapter.package)
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda _package: b.adapter)
    path = b.bot / "data" / native.NAME
    if state == "missing":
        path.unlink()
    elif state == "malformed":
        path.write_bytes(b"invalid original receipt")
    elif state != "confirmed":
        receipt = native.read(b.bot)
        changed = {**receipt, "pending": state in {"pending", "wrong-account"}}
        if state == "wrong-account": changed["account"] = str(b.bot / "another-account")
        if state == "wrong-process": changed["process"] = "1:2"
        native._write(b.bot, changed, expected=receipt)
    before = path.read_bytes() if path.exists() else None
    args = SimpleNamespace(root=b.bot.parent, directory=b.bot, label=b.label, tmpdir=b.tmpdir, action="confirm")
    assert native.entry(args) == expected_rc
    assert (path.read_bytes() if path.exists() else None) == before


def test_pending_confirmation_checks_expected_uuid_before_waiting(bound):
    b = bound
    native.launch(b.bot, b.label, b.tmpdir, b.account)
    with pytest.raises(native.NativeSessionError, match="differs from the original checkpoint") as error:
        native.confirm(b.bot, expected={**b.receipt, "session_id": OTHER})
    assert not isinstance(error.value, native.NativeSessionPending)
