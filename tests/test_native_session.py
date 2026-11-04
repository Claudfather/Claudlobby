"""Exact native continuation receipts; no Claude process, model, or service is launched.

The SessionStart payload shape is from Claude Code 2.1.318 captures retained in
native-resume-proof/hooks (startup and resume, tools-disabled print probe).
IDs/paths below are synthetic. This is shape evidence, not interactive acceptance.
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
        self.calls, self.reject_caller = [], False

    def call(self, function, *args, **kwargs):
        assert function == "svc_bot_native_session", "continuation must not send a model request"
        self.calls.append((function, args))
        rc = 3 if self.reject_caller and len(args) == 4 else 0
        return subprocess.CompletedProcess([function], rc,
            f"{self.pid}\t{self.generation}\t{'idle' if self.idle else 'unready'}\n", "")


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
