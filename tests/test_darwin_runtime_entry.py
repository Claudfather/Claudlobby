"""Pre-Python framing/selector behavior; never run a native/vendor tool."""
from pathlib import Path
import json
import subprocess
import sys

import pytest
from tests.conftest import constructed_env

ENTRY = Path(__file__).resolve().parents[1] / "claudlobby/preinstall/darwin-runtime-v1.sh"


@pytest.mark.parametrize("args,frame,code", [(["shell"], b"{}\n", "invalid_operation"),
    (["prepare", "extra"], b"{}\n", "invalid_request"), (["prepare"], b"{}\n", "invalid_request"),
    (["status"], b"{}\n{}\n", "invalid_request")])
def test_framing_refuses_before_any_native_operation(args, frame, code):
    result = subprocess.run(["/bin/bash", str(ENTRY), *args], input=frame,
                            env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 6 and json.loads(result.stdout)["code"] == code and not result.stderr


@pytest.mark.parametrize("selector", ["BOT_SERVICE", "CLAUDLOBBY_ROOT"])
def test_inherited_installation_selectors_remain_rejected(selector):
    result = subprocess.run(["/bin/bash", str(ENTRY), "status"], input=b"{}\n",
                            env=constructed_env(**{selector: "fixture"}), capture_output=True, timeout=5)
    assert result.returncode == 6 and json.loads(result.stdout)["code"] == "inherited_context_refused"


def test_loader_cleanup_clears_names_without_erasing_context_evidence():
    source = ENTRY.read_text().split("# BEGIN DARWIN EXISTING PYTHON SHELL\n", 1)[1].split("# END DARWIN EXISTING PYTHON SHELL\n", 1)[0]
    script = source + "\nDYLD_INSERT_LIBRARIES=fixture; LD_PRELOAD=fixture; BOT_SERVICE=preserve; CLAUDLOBBY_ROOT=preserve\ndarwin_clean_loader_environment\n[ -z \"${DYLD_INSERT_LIBRARIES+x}\" ] && [ -z \"${LD_PRELOAD+x}\" ] && [ \"$BOT_SERVICE\" = preserve ] && [ \"$CLAUDLOBBY_ROOT\" = preserve ]\n"
    result = subprocess.run(["/bin/bash", "-c", script], input=b"untouched\n", env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 0 and not result.stdout and not result.stderr


def shell_functions():
    return ENTRY.read_text().split('for inherited in ', 1)[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS BSD stat metadata formats")
@pytest.mark.parametrize("mutation", [None, "wrong_request", "stage_inode", "archive_inode", "trailing_record", "foreign", "partial_extraction", "symlink"])
def test_native_early_discard_requires_exact_archive_only_record(tmp_path, mutation):
    import os
    import shlex
    from tests.test_darwin_runtime import runtime, request
    module = runtime(); uid = os.getuid(); home = tmp_path / "home"; home.mkdir(mode=0o700)
    base = home / ".local/share/claudlobby-browser/runtime"; base.mkdir(parents=True, mode=0o700)
    # Every ancestor is owned private data; no actual account runtime is used.
    for path in [home / ".local", home / ".local/share", base.parent]: path.chmod(0o700)
    request_id = request()["request_id"]; stage = base / ("stage-" + request_id); stage.mkdir(mode=0o700)
    archive = stage / "python.tar.gz"; archive.write_bytes(b"partial curl body"); archive.chmod(0o600)
    selected = type("Identity", (), {"uid": uid, "account": "example", "home": home})()
    attempt = {"schema_version": 1, "binding": module._binding(selected), "request_id": request_id,
               "stage_device": stage.stat().st_dev, "stage_inode": stage.stat().st_ino, "phase": "bootstrap",
               "files": {"python.tar.gz": [archive.stat().st_dev, archive.stat().st_ino]}}
    if mutation == "wrong_request": request_id = "22222222-2222-4222-8222-222222222222"
    elif mutation == "stage_inode": attempt["stage_inode"] += 1
    elif mutation == "archive_inode": attempt["files"]["python.tar.gz"][1] += 1
    elif mutation == "foreign": (stage / "personal.txt").write_bytes(b"retain")
    elif mutation == "partial_extraction": (stage / "profile").mkdir(mode=0o700)
    elif mutation == "symlink": archive.unlink(); archive.symlink_to(tmp_path / "foreign")
    module._save(base / "attempt.json", attempt, uid, new=True)
    if mutation == "trailing_record":
        with (base / "attempt.json").open("ab") as output: output.write(b"\n")
    lock = base / "transition.lock"; lock.touch(mode=0o600)
    # Native admission/locking are separate qualification gates; this exercises
    # the exact shell record/namespace checks and disposable-file deletion only.
    script = shell_functions() + '\ndarwin_native() { :; }\ndarwin_lock() { lock="$DARWIN_RUNTIME_BASE/transition.lock"; }\n'
    values = {"DARWIN_RUNTIME_HOME": home, "DARWIN_RUNTIME_BASE": base, "uid": uid, "account": "example", "request_id": request_id}
    script += "\n".join(key + "=" + shlex.quote(str(value)) for key, value in values.items()) + "\ndarwin_discard_early\n"
    result = subprocess.run(["/bin/bash", "-c", script], input=b"", env=constructed_env(), capture_output=True, timeout=5)
    if mutation is None:
        assert result.returncode == 0 and json.loads(result.stdout)["result"]["state"] == "discarded"
        assert not stage.exists() and not (base / "attempt.json").exists() and lock.exists()
    else:
        assert result.returncode == 6 and json.loads(result.stdout)["state"] == "unavailable"
        assert stage.exists() and (base / "attempt.json").exists()


@pytest.mark.parametrize("suffix", [b"{}\n", b"x", b"\n"])
def test_canonical_first_frame_rejects_detectable_extra_data(suffix):
    from tests.test_darwin_runtime import request
    raw = json.dumps(request(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    result = subprocess.run(["/bin/bash", str(ENTRY), "status"], input=raw + suffix,
                            env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 6 and json.loads(result.stdout)["code"] == "invalid_request"


@pytest.mark.skipif(sys.platform != "darwin", reason="depends on macOS /bin/bash 3.2 timed-read behavior")
def test_framing_accepts_one_record_without_claiming_eof(tmp_path):
    from tests.test_darwin_runtime import request
    # Replace the native selector with a pure shell observer. Hold the pipe open
    # beyond its second-read timeout; the accepted contract is one record.
    prefix = ENTRY.read_text().split('mode=existing\n', 1)[0]
    script = prefix + 'IFS= read -r ignored && exit 99\nprintf \'{"input_closed":true}\\n\'\n'
    child = subprocess.Popen(["/bin/bash", "-c", script, "--", "status"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=constructed_env())
    try:
        child.stdin.write(json.dumps(request(), sort_keys=True, separators=(",", ":")).encode() + b"\n"); child.stdin.flush()
        child.wait(timeout=4)
        assert child.returncode == 0 and json.loads(child.stdout.read())["input_closed"]
    finally:
        child.stdin.close()
        if child.poll() is None: child.kill(); child.wait()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS BSD fdesc stat metadata")
@pytest.mark.parametrize("lock_rc,expected", [(0, None), (75, "preparation_busy"), (64, "unsupported_native_lock")])
def test_lock_compares_fdesc_metadata_without_false_device_equality(tmp_path, lock_rc, expected):
    import os
    import shlex
    base = tmp_path / "runtime"; base.mkdir(mode=0o700)
    lock = base / "transition.lock"; lock.touch(mode=0o600)
    source = shell_functions().replace('/usr/bin/lockf -s -t 0 9', 'fixture_lock')
    script = source + '\ndarwin_native() { :; }\nfixture_lock() { return ' + str(lock_rc) + '; }\n'
    script += 'uid=' + str(os.getuid()) + '\nDARWIN_RUNTIME_BASE=' + shlex.quote(str(base)) + '\ndarwin_lock\nprintf \'{"locked":true}\\n\'\n'
    result = subprocess.run(["/bin/bash", "-c", script], input=b"", env=constructed_env(), capture_output=True, timeout=5)
    if expected is None:
        assert result.returncode == 0 and json.loads(result.stdout)["locked"]
    else:
        assert result.returncode == 6 and json.loads(result.stdout)["code"] == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS BSD stat metadata formats")
@pytest.mark.parametrize("state,expected", [("absent", "absent"), ("attempt", "original_attempt_retained"), ("profile", "runtime_not_admitted"), ("symlink", "unsafe_directory")])
def test_pre_python_status_distinguishes_absence_attempt_and_damaged_profile(tmp_path, state, expected):
    import os
    import shlex
    from tests.test_darwin_runtime import request
    home = tmp_path / "home"; home.mkdir(mode=0o700)
    base = home / ".local/share/claudlobby-browser/runtime"; base.mkdir(parents=True, mode=0o700)
    if state == "attempt": (base / "attempt.json").write_bytes(b"retained")
    elif state == "profile": (base / "profile").mkdir(mode=0o700)
    elif state == "symlink": base.rmdir(); base.symlink_to(tmp_path / "foreign")
    source = ENTRY.read_text().split('for inherited in ', 1)
    overrides = '\ndarwin_existing_python() { return 6; }\ndarwin_identity() { DARWIN_RUNTIME_HOME=' + shlex.quote(str(home)) + '; DARWIN_RUNTIME_BASE=' + shlex.quote(str(base)) + '; DARWIN_RUNTIME_ROOT="$DARWIN_RUNTIME_BASE/profile"; }\n'
    script = source[0] + overrides + 'for inherited in ' + source[1]
    value = {**request(), "expected_uid": os.getuid()}
    result = subprocess.run(["/bin/bash", "-c", script, "--", "status"],
                            input=json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n",
                            env=constructed_env(), capture_output=True, timeout=5)
    data = json.loads(result.stdout)
    if state == "absent": assert result.returncode == 0 and data["result"]["state"] == expected
    else: assert result.returncode == 6 and data["code"] == expected


def test_missing_installed_python_reaches_caller_fallback(tmp_path):
    import os
    import shlex
    home = tmp_path / "private-home"
    home.mkdir(mode=0o700)
    profile = home / ".local/share/claudlobby-browser/runtime/profile"
    uid = os.getuid()
    script = shell_functions() + "\n"
    # Isolate only native account selection; exercise real existing-Python and
    # path-stat selector functions against a disposable absent interpreter.
    script += "darwin_identity() { DARWIN_RUNTIME_UID=\"$1\"; DARWIN_RUNTIME_ROOT=" + shlex.quote(str(profile)) + "; }\n"
    script += "if darwin_existing_python " + shlex.quote(str(uid)) + " example; then printf '%s\\n' '{\"fallback_reached\":false}'; else printf '%s\\n' '{\"fallback_reached\":true}'; fi\n"
    result = subprocess.run(["/bin/bash", "-c", script], env=constructed_env(),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"fallback_reached": True}
