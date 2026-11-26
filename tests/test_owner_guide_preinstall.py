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


def advance_source(module, install, python, home):
    """An ordinary source advance: new commit and matching launcher, same preparation receipt."""
    source = install / "src"
    installation = source / "claudlobby/installation.py"
    installation.write_text(installation.read_text() + "# ordinary source advance\n")
    env = constructed_env(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    for args in (["add", "--all"], ["commit", "--quiet", "-m", "advance"]):
        subprocess.run(["/usr/bin/git", "-C", str(source), *args], env=env, check=True,
                       capture_output=True, timeout=10)
    launcher = install / "bin/claudlobby"
    launcher.write_text("#!" + str(python) + "\n" + installation.read_text())
    launcher.chmod(0o755)
    return subprocess.check_output(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], env=env).decode().strip()


def run_prelude(module, tmp_path, home, python, action, value):
    driver = tmp_path / "advance-driver.py"
    driver.write_text("import pathlib,sys,types,platform\n"
        "platform.system=lambda: 'Linux'\n"
        "script,home,python,action=sys.argv[1:]\n"
        "body=pathlib.Path(script).read_text().split('# BEGIN EMBEDDED PYTHON\\n',1)[1].split('# END EMBEDDED PYTHON\\n',1)[0]\n"
        "m=types.ModuleType('fixture');exec(compile(body,script,'exec'),m.__dict__)\n"
        "m.account_profile=lambda uid:pathlib.Path(home)\n"
        "m.independent_python=lambda:pathlib.Path(python)\n"
        "raise SystemExit(m.main(action))\n")
    return subprocess.run([str(python), "-I", "-S", "-B", str(driver), str(SCRIPT), str(home), str(python), action],
        input=module.wire(value) + b'{"retained":"following"}\n',
        env=constructed_env(BOT_UNKNOWN_FIXTURE="preserved", SSH_CONNECTION=SOURCE, EXPECTED_SOURCE=SOURCE,
            EXPECTED_MODE="api --stdio" if action == "api" else "setup --stdio"),
        capture_output=True, timeout=15)


def test_actual_api_prelude_admits_current_source_after_advance_without_finish_tuple(installed, tmp_path):
    module, home, install, value, python = installed
    current = advance_source(module, install, python, home)
    assert current != value["source_revision"]
    # The preparation receipt (and so the binding) keeps the historical ref.
    assert json.loads((install / "preparation.json").read_text())["ref"] == value["source_revision"]
    value["source_revision"], value["finish_setup"] = current, None
    before = snapshot(home)
    result = run_prelude(module, tmp_path, home, python, "api", value)
    # The fixed CLI was reached with its exact api route and received the following input.
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == b'{"type":"hello","fixture":true}\n{"retained":"following"}\n'
    assert snapshot(home) == before
    # Guide mode keeps its finish rule: no finish tuple after an advance is refused.
    refused = run_prelude(module, tmp_path, home, python, "setup", value)
    assert refused.returncode != 0 and json.loads(refused.stdout)["state"] == "refused"
    # Binding and exact installed-source verification are retained in api mode.
    stale = dict(value, source_revision=json.loads((install / "preparation.json").read_text())["ref"])
    assert run_prelude(module, tmp_path, home, python, "api", stale).returncode != 0
    forged = dict(value, binding_sha256="0" * 64)
    assert run_prelude(module, tmp_path, home, python, "api", forged).returncode != 0


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


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_replaced_python_bytes_admit_only_the_linux_system_python(installed, monkeypatch, tmp_path, system):
    """A security update replaces Ubuntu's /usr/bin Python; a Mac runtime stays pinned."""
    module, home, install, value, _python = installed
    python = tmp_path / "system-bin/python3.12"  # stands in for the admitted file
    python.parent.mkdir()
    python.write_bytes(b"synthetic interpreter before a security update; never executed")
    python.chmod(0o755)
    (install / "env/bin/python").unlink(); (install / "env/bin/python").symlink_to(python)
    config = install / "env/pyvenv.cfg"
    config.write_text("home = " + str(python.parent) + "\n" + config.read_text().split("\n", 1)[1])
    launcher = install / "bin/claudlobby"
    launcher.write_bytes(b"#!" + str(python).encode() + b"\n" + launcher.read_bytes().split(b"\n", 1)[1])
    path = install / "preparation.json"
    state = json.loads(path.read_text())
    state.update(interpreter=str(python), interpreter_sha256=module.digest(python))
    path.write_text(json.dumps(state)); path.chmod(0o600)
    value["binding_sha256"] = hashlib.sha256(json.dumps(
        {key: state[key] for key in module.BINDING_FIELDS}, sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(module, "independent_python", lambda: python)
    assert module.validate(value, time.monotonic() + 10)[0] == python
    python.write_bytes(b"synthetic interpreter after a security update; never executed")
    assert module.digest(python) != state["interpreter_sha256"]
    monkeypatch.setattr(module.platform, "system", lambda: system)
    if system == "Darwin":
        monkeypatch.setattr(module, "git_environment", lambda _home: pytest.fail("changed bytes admitted"))
    receipt, before = path.read_bytes(), snapshot(home)
    if system == "Linux":
        assert module.validate(value, time.monotonic() + 10)[0] == python
    else:
        with pytest.raises(module.Refusal) as error:
            module.validate(value, time.monotonic() + 10)
        assert error.value.code == "installation_changed"
    assert path.read_bytes() == receipt and snapshot(home) == before


@pytest.mark.parametrize("defect", [None, "owner", "group-writable", "other-writable", "outside",
                                    "folder-owner", "folder-group-writable", "folder-other-writable", "folder-symlink"])
def test_linux_system_python_is_admitted_only_while_trusted(monkeypatch, tmp_path, defect):
    module = payload()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.sys, "prefix", module.sys.base_prefix)
    executable = Path("/usr/bin/true")  # a root-owned stand-in; never executed
    if defect == "outside":
        executable = tmp_path / "python3"; executable.write_bytes(b"outside"); executable.chmod(0o755)
    monkeypatch.setattr(module.sys, "executable", str(executable))
    class Observed(type(Path("/"))):
        def stat(self, **kwargs):
            info = super().stat(**kwargs)
            mode, uid = info.st_mode, (1001 if defect == "owner" else info.st_uid)
            mode |= {"group-writable": 0o020, "other-writable": 0o002}.get(defect, 0)
            return types.SimpleNamespace(st_mode=mode, st_uid=uid)
        def lstat(self):
            info = super().lstat()
            if str(self) != "/usr/bin":
                return info
            mode = __import__("stat").S_IFLNK | 0o777 if defect == "folder-symlink" else info.st_mode
            mode |= {"folder-group-writable": 0o020, "folder-other-writable": 0o002}.get(defect, 0)
            return types.SimpleNamespace(st_mode=mode, st_uid=1001 if defect == "folder-owner" else info.st_uid)
    monkeypatch.setattr(module, "Path", Observed)
    if defect is None:
        assert module.independent_python() == executable.resolve()
        return
    with pytest.raises(module.Refusal) as error:
        module.independent_python()
    assert (error.value.code, error.value.exit_code) == ("prerequisite_unusable", 6)


def test_linux_recorded_python_digest_is_never_compared(installed):
    """Only the recorded digest changes, and the binding the website holds agrees."""
    module, home, install, value, python = installed
    path = install / "preparation.json"
    state = json.loads(path.read_text()); state["interpreter_sha256"] = "e" * 64
    path.write_text(json.dumps(state)); path.chmod(0o600)
    value["binding_sha256"] = hashlib.sha256(json.dumps(
        {key: state[key] for key in module.BINDING_FIELDS}, sort_keys=True).encode()).hexdigest()
    assert module.platform.system() == "Linux"  # the fixture's platform
    assert module.validate(value, time.monotonic() + 10)[0] == python


RECORD = ["33333333-3333-4333-8333-333333333333", "f" * 40]


@pytest.mark.parametrize("known", [None, RECORD])
def test_prelude_computer_request_takes_the_one_extra_key(known):
    module = payload()
    value = json.loads(frame(update_record=known))
    reader, writer = os.pipe()
    try:
        os.write(writer, frame(update_record=known))
        assert module.request(reader, time.monotonic() + .2, "computer") == value
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize("raw", [
    frame(), frame(update_record=RECORD[:1]), frame(update_record=[*RECORD, RECORD[1]]),
    frame(update_record="33333333-3333-4333-8333-333333333333"), frame(update_record=[RECORD[0], "F" * 40]),
    frame(update_record=["not-an-id", RECORD[1]]), frame(update_record=[RECORD[0], 7]),
    frame(update_record={"request_id": RECORD[0], "target": RECORD[1]}),
    frame(update_record=None, extra="never a command"),
])
def test_prelude_computer_request_refuses_anything_else(raw):
    module = payload()
    reader, writer = os.pipe()
    try:
        os.write(writer, raw)
        with pytest.raises(module.Refusal) as error:
            module.request(reader, time.monotonic() + .2, "computer")
    finally:
        os.close(reader)
        os.close(writer)
    assert error.value.code == "invalid_request" and error.value.exit_code == 2


@pytest.mark.parametrize("action", ["setup", "status", "api"])
def test_prelude_todays_requests_refuse_the_computer_key(action):
    module = payload()
    for known in (None, RECORD):
        reader, writer = os.pipe()
        try:
            os.write(writer, frame(update_record=known))
            with pytest.raises(module.Refusal) as error:
                module.request(reader, time.monotonic() + .2, action)
        finally:
            os.close(reader)
            os.close(writer)
        assert error.value.code == "invalid_request"


def run_mode(module, tmp_path, home, python, action, value, mode):
    driver = tmp_path / "mode-driver.py"
    driver.write_text("import pathlib,sys,types,platform\n"
        "platform.system=lambda: 'Linux'\n"
        "script,home,python,action=sys.argv[1:]\n"
        "body=pathlib.Path(script).read_text().split('# BEGIN EMBEDDED PYTHON\\n',1)[1].split('# END EMBEDDED PYTHON\\n',1)[0]\n"
        "m=types.ModuleType('fixture');exec(compile(body,script,'exec'),m.__dict__)\n"
        "m.account_profile=lambda uid:pathlib.Path(home)\n"
        "m.independent_python=lambda:pathlib.Path(python)\n"
        "raise SystemExit(m.main(action))\n")
    return subprocess.run([str(python), "-I", "-S", "-B", str(driver), str(SCRIPT), str(home), str(python), action],
        input=module.wire(value) + b'{"retained":"following"}\n',
        env=constructed_env(BOT_UNKNOWN_FIXTURE="preserved", EXPECTED_MODE=mode),
        capture_output=True, timeout=15)


def refusal(module, code, operation="owner-guide-computer-v1"):
    return module.wire({"schema_version": 1, "operation": operation, "state": "refused", "code": code, "retry": False})


def test_computer_maps_to_its_route_at_the_receipts_revision(installed, tmp_path):
    module, home, _install, value, python = installed
    before = snapshot(home)
    result = run_mode(module, tmp_path, home, python, "computer", dict(value, update_record=None),
                      "setup --stdio-computer")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == b'{"type":"hello","fixture":true}\n{"retained":"following"}\n'
    assert snapshot(home) == before


def test_computer_is_admitted_by_the_script_at_a_later_installed_revision_and_setup_is_not(installed, tmp_path):
    """The script skips only the receipt's ref for the computer run; Python checks the record it names."""
    module, home, install, value, python = installed
    current = advance_source(module, install, python, home)
    named = dict(value, source_revision=current, update_record=["33333333-3333-4333-8333-333333333333", current])
    before = snapshot(home)
    result = run_mode(module, tmp_path, home, python, "computer", named, "setup --stdio-computer")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == b'{"type":"hello","fixture":true}\n{"retained":"following"}\n'
    setup = dict(named)
    del setup["update_record"]
    assert run_mode(module, tmp_path, home, python, "setup", setup, "setup --stdio").stdout == refusal(
        module, "installation_changed", "owner-guide-v1")
    # Binding and the receipt's shape are still checked for the computer run.
    forged = dict(named, binding_sha256="0" * 64)
    assert run_mode(module, tmp_path, home, python, "computer", forged, "-").stdout == refusal(
        module, "installation_changed")
    assert snapshot(home) == before


@pytest.mark.parametrize("receipt", ["installed", "requested"])
def test_computer_names_a_revision_that_is_not_installed_as_revision_differs(installed, tmp_path, receipt):
    """Installed X, request Y: the script answers revision_differs, whatever the receipt's ref says."""
    module, home, install, value, python = installed
    requested = "e" * 40
    if receipt == "requested":
        path = install / "preparation.json"
        state = json.loads(path.read_text())
        state["ref"] = state["revision"] = requested
        path.write_text(json.dumps(state))
        value["binding_sha256"] = hashlib.sha256(json.dumps(
            {key: state[key] for key in module.BINDING_FIELDS}, sort_keys=True).encode()).hexdigest()
    before = snapshot(home)
    request = dict(value, source_revision=requested)
    for known in (None, ["33333333-3333-4333-8333-333333333333", requested]):
        result = run_mode(module, tmp_path, home, python, "computer", dict(request, update_record=known), "-")
        assert (result.returncode, result.stdout) == (4, refusal(module, "revision_differs"))
    # Today's guide keeps its own refusal names for the same computer.
    today = run_mode(module, tmp_path, home, python, "setup", request, "-")
    assert today.stdout == refusal(module, "installation_changed" if receipt == "installed" else "source_mismatch",
                                   "owner-guide-v1")
    assert snapshot(home) == before


def test_computer_refusal_names_its_own_operation():
    module = payload()
    reader, writer = os.pipe()
    observer, output = os.pipe()
    saved = os.dup(1)
    try:
        os.write(writer, frame())  # no update_record
        os.close(writer)
        writer = None
        os.dup2(output, 1)
        code = module.main("computer", reader)
    finally:
        os.dup2(saved, 1)
        for fd in (saved, reader, output, writer):
            if fd is not None:
                os.close(fd)
    try:
        assert os.read(observer, 4096) == refusal(module, "invalid_request")
    finally:
        os.close(observer)
    assert code == 2


@pytest.mark.parametrize("arguments", [["computer", "extra"], ["COMPUTER"], ["computer --stdio-computer"]])
def test_shell_computer_mode_is_one_exact_word(arguments):
    result = subprocess.run(["/bin/sh", str(SCRIPT), *arguments], input=frame(update_record=None),
                            env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stderr == b""
    assert json.loads(result.stdout) == {"schema_version": 1, "operation": "owner-guide-v1", "state": "refused",
                                         "code": "invalid_request", "retry": False}


# The website sends the guide as one command argument (`sh -c '<guide>'`), and Linux allows 128 KiB
# (MAX_ARG_STRLEN) per argument; the installer already runs over the same path at about 115 KiB. 96 KiB
# keeps a wide margin under that ceiling and room to change the guide. The website's build keeps its
# own guard; raise it there before a pin carries a guide over 64 KiB.
GUIDE_BYTE_BOUND = 98304


def test_the_guide_script_stays_within_its_byte_bound():
    """The guide script's hard bound, 96 KiB; no existing check is trimmed to meet it."""
    assert SCRIPT.stat().st_size <= GUIDE_BYTE_BOUND


# The computer run's link intent (C2): link_claude on the first line, only as true, only for the computer run.
@pytest.mark.parametrize("known", [None, RECORD])
def test_prelude_computer_request_takes_the_link_key(known):
    module = payload()
    raw = frame(update_record=known, link_claude=True)
    reader, writer = os.pipe()
    try:
        os.write(writer, raw)
        assert module.request(reader, time.monotonic() + .2, "computer") == json.loads(raw)
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize("action, raw", [
    *[("computer", frame(update_record=None, link_claude=value)) for value in (False, None, 1, "true", [True])],
    ("computer", frame(link_claude=True)),  # the computer run's record key is still required
    *[(action, frame(link_claude=True)) for action in ("setup", "status", "api")],
    *[(action, frame(update_record=None, link_claude=True)) for action in ("setup", "status", "api")],
])
def test_prelude_link_key_refuses_anything_else(action, raw):
    module = payload()
    reader, writer = os.pipe()
    try:
        os.write(writer, raw)
        with pytest.raises(module.Refusal) as error:
            module.request(reader, time.monotonic() + .2, action)
    finally:
        os.close(reader)
        os.close(writer)
    assert error.value.code == "invalid_request" and error.value.exit_code == 2


def test_guide_bound_with_link_key():
    """The guide that admits the link key still fits its byte bound."""
    module = payload()
    reader, writer = os.pipe()
    try:
        os.write(writer, frame(update_record=None, link_claude=True))
        assert module.request(reader, time.monotonic() + .2, "computer")["link_claude"] is True
    finally:
        os.close(reader)
        os.close(writer)
    assert SCRIPT.stat().st_size <= GUIDE_BYTE_BOUND


def test_computer_with_the_link_key_takes_the_same_route_and_revision_check(installed, tmp_path):
    """The link intent changes nothing in the script: the computer route, the active source's revision
    (a later installed revision is admitted while the receipt keeps its first), and revision_differs."""
    module, home, install, value, python = installed
    current = advance_source(module, install, python, home)
    named = dict(value, source_revision=current, update_record=["33333333-3333-4333-8333-333333333333", current],
                 link_claude=True)
    before = snapshot(home)
    result = run_mode(module, tmp_path, home, python, "computer", named, "setup --stdio-computer")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == b'{"type":"hello","fixture":true}\n{"retained":"following"}\n'
    stale = dict(named, source_revision=value["source_revision"])  # the receipt's, no longer the active source
    result = run_mode(module, tmp_path, home, python, "computer", stale, "-")
    assert (result.returncode, result.stdout) == (4, refusal(module, "revision_differs"))
    assert snapshot(home) == before
