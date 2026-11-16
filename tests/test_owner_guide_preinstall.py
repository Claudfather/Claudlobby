"""Fixed prelude admission: real local Git/exec, no native host actions."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types

import pytest

from tests.conftest import constructed_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "claudlobby/preinstall/owner-guide-v1.sh"
FINISH = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
          "b" * 64, "c" * 40]


def payload():
    assert SCRIPT.exists(), "missing immutable owner-guide prelude"
    body = SCRIPT.read_text().split("# BEGIN EMBEDDED PYTHON\n", 1)[1].split("# END EMBEDDED PYTHON\n", 1)[0]
    module = types.ModuleType("owner_guide_fixture")
    exec(compile(body, str(SCRIPT), "exec"), module.__dict__)
    return module


def frame(**changes):
    value = {"schema_version": 1, "expected_uid": 1001, "source_revision": "a" * 40,
             "binding_sha256": "b" * 64, "finish_setup": None}
    value.update(changes)
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def read(module, raw):
    reader, writer = os.pipe()
    try:
        os.write(writer, raw)
        os.close(writer)
        writer = None
        return module.request(reader, time.monotonic() + .2)
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)


@pytest.mark.parametrize("raw", [
    b"", b"{}\n", frame()[:-1], b" " + frame(), frame().replace(b"\n", b"\r\n"),
    frame(expected_uid=True), frame(expected_uid=0), frame(expected_uid=2**32),
    frame(source_revision="main"), frame(source_revision="A" * 40), frame(binding_sha256="c" * 63),
    frame(extra="never a command"), frame(finish_setup=FINISH[:-1]), frame(finish_setup=[True] * 4),
    frame(finish_setup=[*FINISH[:2], "bad", FINISH[3]]),
    frame().replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
    b"x" * 1024 + b"\n", b"\xff\n",
])
def test_prelude_refuses_noncanonical_or_open_admission(raw):
    module = payload()
    with pytest.raises(module.Refusal) as error:
        read(module, raw)
    assert error.value.code == "invalid_request" and error.value.exit_code == 2


def test_prelude_reads_one_line_without_eof_or_consuming_guide_start():
    module = payload()
    reader, writer = os.pipe()
    following = b'{"type":"start","session_id":"retained-guide-input"}\n'
    try:
        os.write(writer, frame() + following)
        assert module.request(reader, time.monotonic() + .2) == json.loads(frame())
        assert os.read(reader, 1024) == following
    finally:
        os.close(reader)
        os.close(writer)


def test_prelude_partial_line_has_finite_input_budget():
    module = payload()
    reader, writer = os.pipe()
    try:
        os.write(writer, frame()[:-1])
        began = time.monotonic()
        with pytest.raises(module.Refusal):
            module.request(reader, began + .03)
        assert time.monotonic() - began < .5
    finally:
        os.close(reader)
        os.close(writer)


def test_shell_unknown_mode_is_closed_redacted_refusal():
    payload()
    result = subprocess.run(["/bin/sh", str(SCRIPT), "arbitrary-command"], input=frame(),
                            env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stderr == b""
    value = json.loads(result.stdout)
    assert set(value) == {"schema_version", "operation", "state", "code", "retry"}
    assert value == {"schema_version": 1, "operation": "owner-guide-v1", "state": "refused",
                     "code": "invalid_request", "retry": False}
    assert result.stdout == payload().wire(value) and len(result.stdout) < 1024


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, os.readlink(p) if p.is_symlink()
            else p.read_bytes() if p.is_file() else None) for p in root.rglob("*")}


@pytest.fixture
def installed(tmp_path, monkeypatch):
    module = payload()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    family = home / ".local/share/claudlobby-browser"
    for part in (home / ".local", home / ".local/share", family, family / "install",
                 family / "data", family / "staging"):
        part.mkdir(mode=0o700)
    install, data = family / "install", family / "data"
    source = install / "src"
    source.mkdir(mode=0o700)
    (source / "claudlobby").mkdir(mode=0o700)
    # Public fixture, executed only after the real prelude and Git admission.
    body = ("import json,os,sys\n"
            "assert sys.flags.isolated and sys.flags.no_site\n"
            "assert os.environ['BOT_UNKNOWN_FIXTURE']=='preserved'\n"
            "assert sys.argv[1:]==['--root',os.environ['CLAUDLOBBY_ROOT'],'host','owner',*os.environ['EXPECTED_MODE'].split()]\n"
            "os.write(1,b'{\"type\":\"hello\",\"fixture\":true}\\n')\n"
            "if 'EXPECTED_SOURCE' in os.environ: assert os.environ['SSH_CONNECTION']==os.environ['EXPECTED_SOURCE']\n"
            "os.write(1,sys.stdin.buffer.readline())\n")
    (source / "claudlobby/installation.py").write_text(body)
    (source / "requirements-runtime.lock").write_text("# public fixture\n")
    env = constructed_env(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    for args in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "-m", "fixture"]):
        subprocess.run(["/usr/bin/git", "-C", str(source), *args], env=env, check=True,
                       capture_output=True, timeout=10)
    (source / ".git").chmod(0o700)
    revision = subprocess.check_output(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], env=env).decode().strip()
    python = Path(sys._base_executable).resolve()
    environment = install / "env"
    environment.mkdir(mode=0o700)
    (environment / "bin").mkdir(mode=0o700)
    (environment / "bin/python").symlink_to(python)
    (environment / "pyvenv.cfg").write_text("home = " + str(python.parent) +
        "\ninclude-system-site-packages = false\nversion = " + ".".join(map(str, sys.version_info[:3])) + "\n")
    (install / "bin").mkdir(mode=0o700)
    launcher = install / "bin/claudlobby"
    launcher.write_text("#!" + str(python) + "\n" + body)
    launcher.chmod(0o755)
    state = {"schema": 1, "purpose": "installation-preparation-only", "installation": str(install),
        "data_root": str(data), "remote": module.REMOTE, "ref": revision, "interpreter": str(python),
        "interpreter_sha256": module.digest(python), "revision": revision, "lock_sha256": "d" * 64,
        "environment_owned": True, "complete": True}
    for path, value in ((install / "preparation.json", state),
                        (install / "installation.json", {"schema": 1, "data_root": str(data)})):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    (install / "preparation.lock").touch(mode=0o600)
    binding = {key: state[key] for key in module.BINDING_FIELDS}
    value = json.loads(frame(expected_uid=os.getuid(), source_revision=revision,
        binding_sha256=hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()))
    monkeypatch.setattr(module, "account_profile", lambda _uid: home)
    monkeypatch.setattr(module, "independent_python", lambda: python)
    for key in module.GENERATED | {"CLAUDLOBBY_ROOT", "CLAUDLOBBY_INSTALLATION", "CLAUDLOBBY_HOST_SYSTEM_YAML", "CLAUDE_CONFIG_DIR"}:
        monkeypatch.delenv(key, raising=False)
    return module, home, install, value, python


def test_completed_fixed_installation_admission_is_read_only(installed):
    module, home, install, value, python = installed
    before = snapshot(home)
    actual_python, launcher, data, env = module.validate(value, time.monotonic() + 10)
    assert actual_python == python and launcher == install / "bin/claudlobby"
    assert data == install.parent / "data" and env["HOME"] == str(home)
    assert env["CLAUDLOBBY_HOST_SYSTEM_YAML"] == str(install.parent / "host-policy/system.yaml")
    assert snapshot(home) == before


@pytest.mark.parametrize("change", ["binding", "source", "incomplete", "identity", "interpreter", "environment", "launcher", "symlink", "git-lock"])
def test_changed_installation_refuses_without_execution_or_repair(installed, change):
    module, home, install, value, _python = installed
    if change == "binding":
        value["binding_sha256"] = "0" * 64
    elif change == "source":
        (install / "src/claudlobby/installation.py").write_text("raise RuntimeError('never execute')\n")
    elif change == "incomplete":
        path = install / "preparation.json"
        state = json.loads(path.read_text()); state["complete"] = False
        path.write_text(json.dumps(state))
    elif change == "identity":
        (install / "installation.json").write_text("{}")
    elif change == "interpreter":
        path = install / "preparation.json"
        state = json.loads(path.read_text()); state["interpreter_sha256"] = "0" * 64
        path.write_text(json.dumps(state))
    elif change == "environment":
        (install / "env/pyvenv.cfg").write_text("include-system-site-packages = true\n")
    elif change == "launcher":
        (install / "bin/claudlobby").write_text("raise RuntimeError('never execute')\n")
    elif change == "symlink":
        path = install / "preparation.json"
        path.rename(install / "retained.json"); path.symlink_to(install / "retained.json")
    else:
        (install / "src/.git/index.lock").write_text("retained")
    before = snapshot(home)
    with pytest.raises((module.Refusal, OSError, ValueError)):
        module.validate(value, time.monotonic() + 10)
    assert snapshot(home) == before


@pytest.mark.parametrize("selector", ["BOT_ID", "FLEET_ROOT", "CLAUDLOBBY_TIMER_CONTEXT", "CLAUDLOBBY_ROOT", "CLAUDLOBBY_HOST_SYSTEM_YAML", "CLAUDE_CONFIG_DIR"])
def test_inherited_context_refuses_before_python_or_source_inspection(installed, monkeypatch, selector):
    module, home, _install, value, _python = installed
    monkeypatch.setenv(selector, "foreign-private-context")
    monkeypatch.setattr(module, "independent_python", lambda: pytest.fail("context was erased before inspection"))
    with pytest.raises(module.Refusal) as error:
        module.validate(value, time.monotonic() + 10)
    assert error.value.code == "operator_required" and os.environ[selector] == "foreign-private-context"


def test_original_finish_preserves_historical_preparation_without_installer_recovery(installed):
    module, home, install, value, _python = installed
    path = install / "preparation.json"
    state = json.loads(path.read_text())
    state["ref"] = state["revision"] = "e" * 40
    path.write_text(json.dumps(state))
    value["binding_sha256"] = hashlib.sha256(json.dumps({key: state[key] for key in module.BINDING_FIELDS}, sort_keys=True).encode()).hexdigest()
    # Canonical operational state is not the original inactive skeleton.
    (install.parent / "data/state").mkdir(mode=0o700)
    (install.parent / "data/state/update.json").write_text('{"retained":"original receipt fixture"}')
    before = snapshot(home)
    with pytest.raises(module.Refusal):
        module.validate(value, time.monotonic() + 10)
    value["finish_setup"] = FINISH
    assert module.validate(value, time.monotonic() + 10)[1] == install / "bin/claudlobby"
    assert snapshot(home) == before


SOURCE = "100.64.0.42 4242 100.64.0.7 22"


@pytest.mark.parametrize("action,flag", [("setup", "setup --stdio"), ("status", "setup --stdio-status"),
                                         ("api", "api --stdio")])
def test_actual_fixed_exec_preserves_normal_hello_and_following_guide_input(installed, tmp_path, action, flag):
    module, home, _install, value, python = installed
    driver = tmp_path / "public-fixture-driver.py"
    driver.write_text("import pathlib,sys,types,platform\n"
        "platform.system=lambda: 'Linux'\n"
        "script,home,python,action=sys.argv[1:]\n"
        "body=pathlib.Path(script).read_text().split('# BEGIN EMBEDDED PYTHON\\n',1)[1].split('# END EMBEDDED PYTHON\\n',1)[0]\n"
        "m=types.ModuleType('fixture');exec(compile(body,script,'exec'),m.__dict__)\n"
        "m.account_profile=lambda uid:pathlib.Path(home)\n"
        "m.independent_python=lambda:pathlib.Path(python)\n"
        "raise SystemExit(m.main(action))\n")
    following = b'{"retained":"unchanged guide start fixture"}\n'
    before = snapshot(home)
    result = subprocess.run([str(python), "-I", "-S", "-B", str(driver), str(SCRIPT), str(home), str(python), action],
        input=module.wire(value) + following,
        env=constructed_env(BOT_UNKNOWN_FIXTURE="preserved", EXPECTED_MODE=flag,
            **({"SSH_CONNECTION": SOURCE, "EXPECTED_SOURCE": SOURCE} if action == "api" else {}),
            HOME=str(tmp_path / "untrusted-home"), GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="alias.status",
            GIT_CONFIG_VALUE_0="!false"), capture_output=True, timeout=15)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == b'{"type":"hello","fixture":true}\n' + following and result.stderr == b""
    assert snapshot(home) == before


def test_api_mode_refuses_without_a_server_source_before_reading_admission(installed, monkeypatch):
    module, home, *_ = installed
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(module, "request", lambda *_a: pytest.fail("read admission without an SSH source"))
    monkeypatch.setattr(module.os, "execve", lambda *_a: pytest.fail("executed the CLI"))
    reader, writer = os.pipe()
    observer, output = os.pipe()
    saved = os.dup(1)
    before = snapshot(home)
    try:
        os.dup2(output, 1)
        code = module.main("api", reader)
    finally:
        os.dup2(saved, 1)
        for fd in (saved, reader, writer, output):
            os.close(fd)
    try:
        value = json.loads(os.read(observer, 4096))
    finally:
        os.close(observer)
    assert code == 4 and snapshot(home) == before
    assert value == {"schema_version": 1, "operation": "owner-api-v1", "state": "refused",
                     "code": "ssh_source_missing", "retry": False}


@pytest.mark.parametrize("arguments,operation", [(["api", "extra"], "owner-guide-v1"), (["api --stdio"], "owner-guide-v1"),
                                                 (["API"], "owner-guide-v1")])
def test_shell_api_mode_is_one_exact_word(arguments, operation):
    result = subprocess.run(["/bin/sh", str(SCRIPT), *arguments], input=frame(),
                            env=constructed_env(SSH_CONNECTION=SOURCE), capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stderr == b""
    assert json.loads(result.stdout) == {"schema_version": 1, "operation": operation, "state": "refused",
                                         "code": "invalid_request", "retry": False}


def test_generated_selector_list_stays_with_canonical_owner():
    from claudlobby.commands.host_owner import _GENERATED
    assert payload().GENERATED == set(_GENERATED)


def test_darwin_guide_uses_closed_runtime_tools_and_refuses_python_drift(tmp_path, monkeypatch):
    from tests.test_host_install import _darwin_policy_fixture
    module = payload(); home = tmp_path / "account"; home.mkdir(mode=0o700)
    profile, _policy, environment = _darwin_policy_fixture(module, home, monkeypatch)
    monkeypatch.setattr(module.sys, "executable", str(profile.python))
    monkeypatch.setattr(module.sys, "prefix", module.sys.base_prefix)
    assert module.account_profile(os.geteuid()) == home
    assert module.independent_python() == profile.python
    monkeypatch.setenv("GIT_EXEC_PATH", "FOREIGN-HELPER")
    monkeypatch.setenv("SSL_CERT_FILE", "FOREIGN-CA")
    assert module.git_environment(home) == environment
    monkeypatch.setattr(module.sys, "executable", str(home / "other-python"))
    with pytest.raises(module.Refusal) as error: module.independent_python()
    assert error.value.code == "prerequisite_unusable"


def test_shared_darwin_loader_cleanup_preserves_inherited_operator_evidence():
    shell = SCRIPT.read_text().split("# BEGIN DARWIN EXISTING PYTHON SHELL\n", 1)[1].split(
        "# END DARWIN EXISTING PYTHON SHELL\n", 1)[0]
    definitions = shell.split("darwin_native() {", 1)[0]
    # Fixed shell fixture only: no runtime tool or native platform operation.
    script = definitions + """
DYLD_FIXTURE_ONLY=loader-sentinel
LD_FIXTURE_ONLY=loader-sentinel
BOT_ID=bot-evidence
CLAUDLOBBY_ROOT=root-evidence
BASH_ENV=never-source
ENV=never-source
darwin_clean_loader_environment || exit 20
[ -z "${DYLD_FIXTURE_ONLY+x}${LD_FIXTURE_ONLY+x}${BASH_ENV+x}${ENV+x}" ] || exit 21
[ "$BOT_ID" = bot-evidence ] && [ "$CLAUDLOBBY_ROOT" = root-evidence ] || exit 22
readonly LD_REFUSE_FIXTURE=retained
darwin_clean_loader_environment >/dev/null 2>&1 && exit 23
exit 0
"""
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script],
                            env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stdout == b"" and result.stderr == b""
