"""Bounded preinstall protocol and real disposable Git/child fixtures."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import types

import pytest

from tests.conftest import constructed_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "claudlobby/preinstall/host-install-v1.sh"
BODY = SCRIPT.read_text().split("# BEGIN EMBEDDED PYTHON\n", 1)[1].split("# END EMBEDDED PYTHON\n", 1)[0]
REVISION = "a" * 40


def payload():
    module = types.ModuleType("host_install_fixture")
    exec(compile(BODY, str(SCRIPT), "exec"), module.__dict__)
    return module


def frame(**changes):
    value = {"schema_version": 1, "source_revision": REVISION, "expected_uid": 1001}
    value.update(changes)
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def parse(raw):
    module = payload()
    reader, writer = os.pipe()
    os.write(writer, raw)
    os.close(writer)
    try:
        return module.request(reader, time.monotonic() + 10)
    finally:
        os.close(reader)


def snapshot(root):
    return {str(p.relative_to(root)): (stat.S_IMODE(p.lstat().st_mode),
            os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in root.rglob("*")}


def profile(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    python = Path(sys.executable).resolve()
    monkeypatch.setattr(module, "account_profile", lambda expected: home)
    monkeypatch.setattr(module, "independent_python", lambda: python)
    monkeypatch.setenv("PATH", "/nonexistent")
    return home, python


def create_paths(module, home):
    module.claim_paths(home)
    paths = module.paths(home)
    for path in paths[1:]:
        module.directory(path, private=True, create=True)
    return paths


def test_request_is_one_canonical_ascii_line_with_eof():
    assert parse(frame()) == {"schema_version": 1, "source_revision": REVISION, "expected_uid": 1001}


@pytest.mark.parametrize("raw", [
    b"", b"{}\n", frame()[:-1], frame() + b"\n", frame() + frame(), b" " + frame(),
    frame().replace(b"\n", b"\r\n"), frame(expected_uid=True), frame(expected_uid=0),
    frame(expected_uid=-1), frame(expected_uid=2**32), frame(source_revision="main"),
    frame(source_revision="A" * 40), frame(extra="not admitted"), b"x" * 513,
    frame().replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
    frame().replace(b'"schema_version":1', b'"schema_version":1.0'), b'\xff\n',
])
def test_malformed_request_refuses_without_echo_or_effect(raw):
    module = payload()
    with pytest.raises(module.Refusal):
        # Use the exact production reader from this module instance.
        reader, writer = os.pipe()
        try:
            os.write(writer, raw); os.close(writer)
            module.request(reader, time.monotonic() + 10)
        finally:
            os.close(reader)


def test_request_missing_eof_has_its_own_finite_budget():
    module = payload(); reader, writer = os.pipe()
    os.write(writer, frame())
    started = time.monotonic()
    try:
        with pytest.raises(module.Refusal):
            module.request(reader, started + 4.05)
        assert time.monotonic() - started < 0.5
    finally:
        os.close(reader); os.close(writer)


def test_shell_unknown_action_is_one_bounded_closed_result(tmp_path):
    result = subprocess.run(["/bin/sh", str(SCRIPT), "arbitrary"], input=frame(),
                            env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stderr == b""
    data = json.loads(result.stdout)
    assert result.stdout.count(b"\n") == 1 and len(result.stdout) <= 8192
    assert set(data) == {"schema_version", "operation", "source_revision", "account_uid",
                        "binding_sha256", "state", "code", "prepared", "activated", "next"}
    assert data["code"] == "invalid_request" and not data["prepared"] and not data["activated"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "oversize"])
def test_plain_record_refuses_redirected_or_unsafe_files(tmp_path, kind):
    module = payload(); path = tmp_path / "receipt"
    path.write_bytes(b"{}") ; path.chmod(0o600)
    if kind == "symlink":
        other = tmp_path / "other"; path.rename(other); path.symlink_to(other)
    elif kind == "hardlink":
        os.link(path, tmp_path / "other")
    elif kind == "mode":
        path.chmod(0o644)
    else:
        path.write_bytes(b"x" * 65537)
    with pytest.raises((module.Refusal, OSError)):
        module.plain_file(path, 65536)


def test_status_absent_never_creates_paths_or_executes_children(tmp_path, monkeypatch):
    module = payload(); home, _python = profile(module, tmp_path, monkeypatch)
    before = snapshot(home)
    class NoChildren(module.Runner):
        def __call__(self, *_args, **_kwargs):
            pytest.fail("absent status executed a child")
    monkeypatch.setattr(module, "Runner", NoChildren)
    result = module.execute("status", json.loads(frame()), time.monotonic() + 15)
    assert result[:4] == ("absent", "none", "inspect", False)
    assert snapshot(home) == before


def test_status_reports_busy_before_reading_partial_receipt_or_staging(tmp_path, monkeypatch):
    module = payload(); home, _python = profile(module, tmp_path, monkeypatch)
    install, data, staging = create_paths(module, home)
    lock = module.lock_open(install / "preparation.lock", True, True)
    (staging / "partial").write_text("retained")
    (install / "preparation.json").write_text("not a receipt")
    before = snapshot(home)
    try:
        with pytest.raises(module.Refusal) as error:
            module.execute("status", json.loads(frame()), time.monotonic() + 15)
        assert (error.value.state, error.value.code) == ("busy", "preparation_in_progress")
        assert snapshot(home) == before
    finally:
        os.close(lock)


def test_status_matching_incomplete_is_read_only_and_resume_requires_caller_approval(tmp_path, monkeypatch):
    module = payload(); home, python = profile(module, tmp_path, monkeypatch)
    install, data, _staging = create_paths(module, home)
    lock = module.lock_open(install / "preparation.lock", True, True); os.close(lock)
    expected = module.binding(install, data, REVISION, python)
    state = {**expected, "revision": None, "lock_sha256": None, "environment_owned": False, "complete": False}
    for path, value in [(install / "preparation.json", state),
                        (install / "installation.json", {"schema": 1, "data_root": str(data)})]:
        path.write_text(json.dumps(value)); path.chmod(0o600)
    before = snapshot(home)
    result = module.execute("status", json.loads(frame()), time.monotonic() + 15)
    assert result[:4] == ("incomplete", "preparation_incomplete", "resume_same", False)
    assert snapshot(home) == before


@pytest.mark.parametrize("foreign", ["local", "state/config.json", "state/update.json", "state/plane/ledger"])
def test_foreign_data_is_preserved_and_never_prepared(tmp_path, monkeypatch, foreign):
    module = payload(); home, _python = profile(module, tmp_path, monkeypatch)
    install, data, _staging = create_paths(module, home)
    descriptor = module.lock_open(install / "preparation.lock", True, True); os.close(descriptor)
    target = data / foreign; target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("PRIVATE-FIXTURE-DO-NOT-ADOPT")
    before = snapshot(home)
    with pytest.raises(module.Refusal):
        module.execute("prepare", json.loads(frame()), time.monotonic() + 20)
    assert snapshot(home) == before


def test_binding_and_receipt_schema_match_bootstrap_owner(tmp_path, monkeypatch):
    from claudlobby import bootstrap
    module = payload()
    assert module.FIELDS == bootstrap._PREPARATION_FIELDS
    home, python = profile(module, tmp_path, monkeypatch)
    install, data, _staging = create_paths(module, home)
    expected = module.binding(install, data, REVISION, python)
    state = {**expected, "revision": REVISION, "lock_sha256": "b" * 64,
             "environment_owned": True, "complete": True}
    path = install / "preparation.json"; path.write_text(json.dumps(state)); path.chmod(0o600)
    assert module.receipt(path, expected) == bootstrap._browser_receipt(path) == state
    path.write_text(json.dumps({**state, "new_journal": "not allowed"}))
    with pytest.raises(module.Refusal): module.receipt(path, expected)
    with pytest.raises(bootstrap.BundleError): bootstrap._browser_receipt(path)


def test_actual_owned_child_timeout_drains_and_keeps_canonical_lock_until_exit(tmp_path):
    module = payload(); lockpath = tmp_path / "preparation.lock"
    lock = module.lock_open(lockpath, True, True)
    ready = tmp_path / "ready"
    runner = module.Runner(time.monotonic() + 4.4, (lock,))
    outcomes = []
    def work():
        try:
            runner([sys.executable, "-I", "-c", "import pathlib,time; pathlib.Path(" + repr(str(ready)) + ").write_text('ready'); time.sleep(30)"], env=constructed_env())
        except module.Refusal:
            outcomes.append("timed-out")
    thread = threading.Thread(target=work); thread.start()
    until = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < until: time.sleep(0.01)
    assert ready.exists()
    os.close(lock)
    with pytest.raises(module.Refusal) as error: module.lock_open(lockpath, False, True)
    assert error.value.state == "busy"
    thread.join(5)
    assert not thread.is_alive() and outcomes == ["timed-out"] and runner.drained and not runner.active
    descriptor = module.lock_open(lockpath, False, True); os.close(descriptor)


def test_child_output_overflow_is_bounded_and_drained(tmp_path):
    module = payload(); module.MAX_CHILD_OUTPUT = 1024
    runner = module.Runner(time.monotonic() + 10)
    with pytest.raises(module.Refusal):
        runner([sys.executable, "-I", "-c", "import sys,time; sys.stdout.write('x'*5000);sys.stdout.flush();time.sleep(30)"], env=constructed_env())
    assert runner.drained and not runner.active


@pytest.mark.parametrize("kind", ["wrong-inode", "read-only", "hardlink", "wrong-mode", "nonempty"])
def test_bootstrap_borrowed_lock_is_verified_not_an_integer_bypass(tmp_path, kind):
    from claudlobby import bootstrap
    canonical = tmp_path / "preparation.lock"
    canonical.write_bytes(b""); canonical.chmod(0o600)
    other = tmp_path / "other"; other.write_bytes(b""); other.chmod(0o600)
    path = other if kind == "wrong-inode" else canonical
    descriptor = os.open(path, os.O_RDONLY if kind == "read-only" else os.O_RDWR)
    if kind == "hardlink": os.link(canonical, tmp_path / "linked")
    if kind == "wrong-mode": canonical.chmod(0o644)
    if kind == "nonempty": canonical.write_bytes(b"foreign")
    try:
        with pytest.raises(bootstrap.BundleError): bootstrap._checked_lock(descriptor, canonical)
        os.fstat(descriptor)  # A borrowed descriptor remains its caller's property.
    finally:
        os.close(descriptor)


def test_bootstrap_isolation_drops_ambient_configuration(tmp_path, monkeypatch):
    from claudlobby import bootstrap
    from claudlobby.installation import Installation
    monkeypatch.setattr(__import__("platform"), "system", lambda: "Linux")
    home = tmp_path / "account"
    monkeypatch.setattr(bootstrap.pwd, "getpwuid", lambda _uid: types.SimpleNamespace(pw_dir=str(home)))
    for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "HTTPS_PROXY",
                "SSL_CERT_FILE", "PIP_INDEX_URL", "PYTHONPATH", "CLAUDLOBBY_DATA_DIR"):
        monkeypatch.setenv(key, "FOREIGN-FIXTURE")
    family = home / ".local/share/claudlobby-browser"
    env = bootstrap._browser_environment(Installation(family / "install", family / "data"))
    assert env == payload().fixed_environment(home)
    assert "FOREIGN-FIXTURE" not in env.values()


def test_existing_preflight_policy_runs_through_injected_runner(tmp_path):
    from claudlobby.installation import Installation, InstallationError
    source = tmp_path / "managed/src"; source.mkdir(parents=True)
    commands = []
    def run(arguments):
        result = subprocess.run(arguments, env=constructed_env(), capture_output=True, timeout=15)
        assert result.returncode == 0
        return result.stdout.strip().decode()
    run(["/usr/bin/git", "init", "--quiet", str(source)])
    (source / "tracked").write_text("fixture")
    run(["/usr/bin/git", "-C", str(source), "add", "."])
    run(["/usr/bin/git", "-C", str(source), "-c", "user.name=Fixture", "-c",
         "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture"])
    revision = run(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"])
    def injected(arguments):
        commands.append(arguments)
        return subprocess.run(arguments, env=constructed_env(), capture_output=True, timeout=15)
    installation = Installation(source.parent, tmp_path / "data")
    assert installation.preflight(expected_revision=revision, _run_git=injected) == revision
    with pytest.raises(InstallationError):
        installation.preflight(expected_revision="b" * 40, _run_git=injected)
    (source / "tracked").write_text("dirty")
    with pytest.raises(InstallationError): installation.preflight(_run_git=injected)
    assert any("status" in command for command in commands)
    assert all(command[:2] == ["git", "--no-optional-locks"] for command in commands)


@pytest.fixture
def local_install_source(tmp_path):
    """Exact reviewed source plus a real tiny wheel; no remote network or host install."""
    import shutil
    from tests.test_git_update import _tiny_wheel
    remote = tmp_path / "public-source-fixture"; remote.mkdir()
    (remote / "claudlobby").mkdir()
    for name in ("bootstrap.py", "installation.py", "git_update.py", "__init__.py"):
        shutil.copyfile(ROOT / "claudlobby" / name, remote / "claudlobby" / name)
    wheels = tmp_path / "wheels"; wheels.mkdir()
    (remote / "requirements-runtime.lock").write_bytes(_tiny_wheel(wheels, "1.0"))
    def git(*arguments):
        result = subprocess.run(["/usr/bin/git", "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "-C", str(remote), *arguments],
            env=constructed_env(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null"),
            capture_output=True, check=True, timeout=15)
        return result.stdout.decode().strip()
    git("init", "--quiet"); git("add", "."); git("commit", "--quiet", "-m", "immutable fixture")
    return remote, wheels, git("rev-parse", "HEAD")


def invoke_fixture(tmp_path, home, local_install_source, action, *, fail_download=False):
    remote, wheels, revision = local_install_source
    driver = tmp_path / "fixture-driver.py"
    # Separate interpreter: the payload must import only its newly verified stage.
    # The two test adapters replace platform admission and the network with owned
    # local fixtures; Git, venv, pip, bootstrap, receipts and stable entries are real.
    driver.write_text('''import sys,types,json,os,pwd,platform
platform.system=lambda: "Linux"
from pathlib import Path
body=Path(sys.argv[1]).read_text().split("# BEGIN EMBEDDED PYTHON\\n",1)[1].split("# END EMBEDDED PYTHON\\n",1)[0]
m=types.ModuleType("isolated_operation");exec(compile(body,sys.argv[1],"exec"),m.__dict__)
home=Path(sys.argv[2]);remote=sys.argv[3];wheels=sys.argv[4];action=sys.argv[5];fail=sys.argv[6]=="1"
python=Path(sys.executable).resolve()
m.account_profile=lambda uid: home
m.independent_python=lambda: python
pwd.getpwuid=lambda uid: types.SimpleNamespace(pw_dir=str(home))
original=m.Runner
class LocalRunner(original):
 def __call__(self,args,*,env,timeout=300):
  args=[str(a) for a in args]
  assert env==m.fixed_environment(home), env
  if action=="status":
   assert args[0]=="/usr/bin/git" and not any(a in args for a in ("fetch","init","checkout")),args
  if "fetch" in args:
   assert m.REMOTE in args
   args[args.index(m.REMOTE)]=remote
  if "pip" in args: assert "--no-cache-dir" in args
  if "download" in args:
   if fail:return m.subprocess.CompletedProcess(args,1,b"",b"fixture interruption")
   args += ["--no-index","--find-links",wheels]
  return super().__call__(args,env=env,timeout=timeout)
m.Runner=LocalRunner
raise SystemExit(m.main(action))
''')
    return subprocess.run([sys._base_executable, "-I", "-B", str(driver), str(SCRIPT), str(home),
        str(remote), str(wheels), action, "1" if fail_download else "0"],
        input=frame(source_revision=revision, expected_uid=os.getuid()),
        env=constructed_env(PATH="/nonexistent", HOME=str(tmp_path / "untrusted-home"),
            GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="url.file:///invalid/.insteadOf",
            GIT_CONFIG_VALUE_0="https://github.com/", HTTPS_PROXY="http://127.0.0.1:1",
            PIP_INDEX_URL="http://127.0.0.1:1"), capture_output=True, timeout=75)


def test_actual_local_git_preparation_and_read_only_standalone_status(tmp_path, local_install_source):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    personal = home / ".claude"; personal.mkdir(); (personal / "settings.json").write_text("PRIVATE-SENTINEL")
    prepared = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    result = json.loads(prepared.stdout)
    assert result["state"] == "prepared" and result["prepared"] and not result["activated"]
    assert prepared.stderr == b"" and result["source_revision"] == local_install_source[2]
    assert prepared.stdout == (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    assert len(prepared.stdout) <= 8192 and {path.name for path in home.iterdir()} == {".claude", ".local"}
    family = home / ".local/share/claudlobby-browser"
    assert json.loads((family / "install/preparation.json").read_text())["complete"]
    assert {p.name for p in (family / "data/state").iterdir()} == {"runtime-use.lock"}
    assert not list((family / "staging").iterdir())
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 0, (status.stdout, status.stderr)
    assert json.loads(status.stdout)["state"] == "recorded_complete" and status.stderr == b""
    assert snapshot(home) == before and (personal / "settings.json").read_text() == "PRIVATE-SENTINEL"
    # Status never starts installed Python, even if site initialization was edited.
    marker = tmp_path / "site-executed"
    site = next((family / "install/env/lib").glob("python*/site-packages"))
    (site / "sitecustomize.py").write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 0 and not marker.exists() and snapshot(home) == before
    # Neither a dirty source nor a named interrupted Git lock authorizes repair.
    tracked = family / "install/src/requirements-runtime.lock"
    original = tracked.read_bytes(); tracked.write_bytes(original + b"# changed\n")
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 4 and json.loads(status.stdout)["code"] == "source_mismatch"
    assert snapshot(home) == before
    tracked.write_bytes(original)
    leftover = family / "install/src/.git/index.lock"; leftover.write_text("retained")
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 4 and snapshot(home) == before and leftover.read_text() == "retained"


def test_actual_interrupted_dependency_preparation_retains_receipt_and_explicit_resume(tmp_path, local_install_source):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    failed = invoke_fixture(tmp_path, home, local_install_source, "prepare", fail_download=True)
    assert failed.returncode == 4 and json.loads(failed.stdout)["next"] == "inspect"
    family = home / ".local/share/claudlobby-browser"
    record = family / "install/preparation.json"
    state = json.loads(record.read_text())
    assert state["revision"] == local_install_source[2] and not state["complete"]
    assert state["environment_owned"] and not (family / "install/bin").exists()
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 0 and json.loads(status.stdout)["next"] == "resume_same"
    assert snapshot(home) == before
    resumed = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert resumed.returncode == 0 and json.loads(resumed.stdout)["state"] == "prepared"


@pytest.mark.parametrize("startup", ["sitecustomize.py", "foreign.pth"])
def test_modified_incomplete_startup_stops_before_managed_execution(tmp_path, local_install_source, startup):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    failed = invoke_fixture(tmp_path, home, local_install_source, "prepare", fail_download=True)
    assert failed.returncode == 4
    family = home / ".local/share/claudlobby-browser"
    site = next((family / "install/env/lib").glob("python*/site-packages"))
    marker = tmp_path / "startup-executed"
    (site / startup).write_text("import pathlib; pathlib.Path(" + repr(str(marker)) + ").touch()\n")
    before = snapshot(home)
    for action in ("status", "prepare"):
        result = invoke_fixture(tmp_path, home, local_install_source, action)
        assert result.returncode == 4 and json.loads(result.stdout)["next"] == "inspect"
        assert not marker.exists() and snapshot(home) == before


@pytest.mark.parametrize("leftover", [".update-retained", ".installation-retained", "bin/.launcher-retained", "cache/foreign", "../staging/prepare-retained"])
def test_named_uncertain_leftovers_stop_and_are_never_cleaned(tmp_path, monkeypatch, leftover):
    module = payload(); home, python = profile(module, tmp_path, monkeypatch)
    install, data, _staging = create_paths(module, home)
    descriptor = module.lock_open(install / "preparation.lock", True, True); os.close(descriptor)
    expected = module.binding(install, data, REVISION, python)
    state = {**expected, "revision": None, "lock_sha256": None, "environment_owned": False, "complete": False}
    for path, value in [(install / "preparation.json", state),
                        (install / "installation.json", {"schema": 1, "data_root": str(data)})]:
        path.write_text(json.dumps(value)); path.chmod(0o600)
    target = install / leftover; target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text("UNKNOWN-OUTCOME-RETAIN")
    before = snapshot(home)
    with pytest.raises(module.Refusal) as error:
        module.execute("status", json.loads(frame()), time.monotonic() + 15)
    assert error.value.next_step == "inspect" and snapshot(home) == before


def test_account_mismatch_refuses_before_account_home_inspection(monkeypatch):
    module = payload()
    monkeypatch.setattr(module.pwd, "getpwuid", lambda _: pytest.fail("account mismatch read a home"))
    with pytest.raises(module.Refusal) as error: module.account_profile(os.getuid() + 1)
    assert error.value.code == "account_changed"


@pytest.mark.parametrize("kind", ["sitecustomize-package", "usercustomize-package", "lib-symlink", "site-symlink"])
def test_incomplete_startup_admission_refuses_package_hooks_and_redirected_libraries(tmp_path, kind):
    from claudlobby import bootstrap
    module = payload()
    environment = tmp_path / "env"; environment.mkdir(mode=0o700)
    lib = environment / "lib"; lib.mkdir(mode=0o700)
    version = lib / ("python" + str(sys.version_info.major) + "." + str(sys.version_info.minor))
    version.mkdir(mode=0o700); site = version / "site-packages"; site.mkdir(mode=0o700)
    if kind.endswith("package"):
        package = site / kind.removesuffix("-package"); package.mkdir(mode=0o700)
        (package / "__init__.py").write_text("raise RuntimeError('must not execute')\n")
    else:
        path = lib if kind == "lib-symlink" else site
        retained = tmp_path / "retained"; path.rename(retained); path.symlink_to(retained)
    before = snapshot(tmp_path)
    assert module.startup_files_safe(environment) is False
    assert bootstrap._browser_startup_files_safe(environment) is False
    assert snapshot(tmp_path) == before


def test_fixed_git_environment_disables_replacement_refs_for_exact_pin(tmp_path, monkeypatch):
    module = payload()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    source = tmp_path / "source"; source.mkdir(mode=0o700)
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    def fixture_git(*args):
        result = subprocess.run(["/usr/bin/git", "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "-C", str(source), *args],
            env=constructed_env(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null"),
            capture_output=True, check=True, timeout=15, umask=0o077)
        return result.stdout.strip().decode()
    fixture_git("init", "--quiet")
    tracked = source / "tracked"; tracked.write_text("exact-pinned-bytes")
    fixture_git("add", "."); fixture_git("commit", "--quiet", "-m", "original")
    original = fixture_git("rev-parse", "HEAD")
    tracked.write_text("replacement-bytes")
    fixture_git("add", "."); fixture_git("commit", "--quiet", "-m", "replacement")
    replacement = fixture_git("rev-parse", "HEAD")
    fixture_git("replace", original, replacement)
    assert fixture_git("show", original + ":tracked") == "replacement-bytes"
    runner = module.Runner(time.monotonic() + 15)
    result = module.git(runner, module.fixed_environment(home), source, "show", original + ":tracked")
    assert result.stdout == b"exact-pinned-bytes" and runner.drained


@pytest.mark.parametrize("missing", ["runtime-lock", "state-directory", "data-root"])
@pytest.mark.parametrize("action", ["status", "prepare"])
def test_completed_preparation_missing_inactive_skeleton_refuses_without_repair(
        tmp_path, local_install_source, missing, action):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    prepared = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    family = home / ".local/share/claudlobby-browser"
    data = family / "data"
    record = family / "install/preparation.json"
    receipt_bytes = record.read_bytes()
    assert json.loads(receipt_bytes)["complete"]
    (data / "state/runtime-use.lock").unlink()
    if missing in {"state-directory", "data-root"}:
        (data / "state").rmdir()
    if missing == "data-root":
        data.rmdir()
    before = snapshot(home)
    inspected = invoke_fixture(tmp_path, home, local_install_source, action)
    result = json.loads(inspected.stdout)
    assert inspected.returncode == 4, (inspected.stdout, inspected.stderr)
    assert result["code"] == "identity_changed" and result["next"] == "inspect"
    assert not result["prepared"] and not result["activated"] and inspected.stderr == b""
    assert snapshot(home) == before and record.read_bytes() == receipt_bytes


def add_setup_state(family):
    """What this product's own setup guide adds beside and inside a completed preparation."""
    for name in ("host-policy", "provider-bin", "provider-profile"):
        (family / name).mkdir(mode=0o700)
    for name, mode in (("host-policy/system.yaml", 0o600), ("provider-bin/claude", 0o500),
                       ("data/.env", 0o600), ("data/state/host-uid", 0o600)):
        (family / name).write_text("FIXTURE-LATER-STATE"); (family / name).chmod(mode)
    (family / "data/state/owner").mkdir(mode=0o700)
    (family / "data/state/owner/owner.sock.lock").touch(mode=0o600)
    (family / "staging/app-temporary").write_text("in flight")


def test_status_reports_its_own_completed_preparation_in_use_once_setup_has_started(tmp_path, local_install_source):
    import fcntl
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    prepared = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    family = home / ".local/share/claudlobby-browser"
    add_setup_state(family)
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    result = json.loads(status.stdout)
    assert status.returncode == 0, (status.stdout, status.stderr)
    assert (result["state"], result["code"], result["next"]) == ("recorded_in_use", "none", "continue_onboarding")
    assert result["prepared"] and not result["activated"] and status.stderr == b""
    assert result["source_revision"] == local_install_source[2] and snapshot(home) == before
    # A running app holds the runtime lock; status reads around it.
    held = os.open(family / "data/state/runtime-use.lock", os.O_RDWR)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = invoke_fixture(tmp_path, home, local_install_source, "status")
        assert status.returncode == 0 and json.loads(status.stdout)["state"] == "recorded_in_use"
    finally:
        os.close(held)
    # The preparation itself is still verified exactly.
    tracked = family / "install/src/requirements-runtime.lock"
    original = tracked.read_bytes(); tracked.write_bytes(original + b"# changed\n")
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 4 and json.loads(status.stdout)["code"] == "source_mismatch"
    tracked.write_bytes(original)
    # Prepare never adopts or replaces what setup has added.
    before = snapshot(home)
    again = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    result = json.loads(again.stdout)
    assert again.returncode == 4 and result["code"] == "existing_installation" and not result["prepared"]
    assert snapshot(home) == before


def _incomplete_record(family):
    record = family / "install/preparation.json"
    value = json.loads(record.read_text())
    value.update(revision=None, lock_sha256=None, environment_owned=False, complete=False)
    record.write_text(json.dumps(value))


def _replace(path, make):
    import shutil
    shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    make(path)


UNKNOWN_BESIDE_STARTED_SETUP = {
    "family-name": (lambda f: (f / "other").mkdir(mode=0o700), "existing_installation"),
    "family-symlink": (lambda f: _replace(f / "provider-profile", lambda p: p.symlink_to(f.parent)), "existing_installation"),
    "family-dangling": (lambda f: _replace(f / "provider-profile", lambda p: p.symlink_to(f / "absent")), "existing_installation"),
    "family-file": (lambda f: _replace(f / "provider-bin", lambda p: p.write_text("not a folder")), "existing_installation"),
    "family-shared": (lambda f: (f / "host-policy").chmod(0o755), "existing_installation"),
    "no-record": (lambda f: (f / "install/preparation.json").unlink(), "existing_installation"),
    "incomplete-record": (_incomplete_record, "existing_installation"),
    "lock-missing": (lambda f: (f / "data/state/runtime-use.lock").unlink(), "identity_changed"),
    "lock-symlink": (lambda f: _replace(f / "data/state/runtime-use.lock", lambda p: p.symlink_to(f / "data/.env")), "existing_installation"),
    "lock-linked": (lambda f: os.link(f / "data/state/runtime-use.lock", f / "data/state/second-name"), "existing_installation"),
    "lock-shared": (lambda f: (f / "data/state/runtime-use.lock").chmod(0o644), "existing_installation"),
    "lock-written": (lambda f: (f / "data/state/runtime-use.lock").write_text("foreign"), "existing_installation"),
    "state-symlink": (lambda f: ((f / "data/state").rename(f / "data/moved"), (f / "data/state").symlink_to(f / "data/moved")), "existing_installation"),
    "state-missing": (lambda f: _replace(f / "data/state", lambda p: None), "identity_changed"),
    "data-missing": (lambda f: _replace(f / "data", lambda p: None), "identity_changed"),
    "launcher-changed": (lambda f: (f / "install/bin/claudlobby").write_text("#!/bin/false\n"), "identity_changed"),
    "environment-missing": (lambda f: _replace(f / "install/env", lambda p: None), "identity_changed"),
}


@pytest.mark.parametrize("unknown", sorted(UNKNOWN_BESIDE_STARTED_SETUP))
def test_status_refuses_unknown_state_beside_a_started_setup(tmp_path, local_install_source, unknown):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    prepared = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    family = home / ".local/share/claudlobby-browser"
    add_setup_state(family)
    change, code = UNKNOWN_BESIDE_STARTED_SETUP[unknown]
    change(family)
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    result = json.loads(status.stdout)
    assert status.returncode == 4, (status.stdout, status.stderr)
    assert result["code"] == code and result["next"] == "inspect" and not result["prepared"]
    assert status.stderr == b"" and snapshot(home) == before


@pytest.mark.parametrize("later", ["staging/app-temporary", "data/.env", "data/state/host-uid"])
def test_any_later_state_under_the_private_roots_means_in_use(tmp_path, local_install_source, later):
    # The guide runs with its temporary files in staging, so it can leave one
    # there before it has created any folder of its own.
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    prepared = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    family = home / ".local/share/claudlobby-browser"
    (family / later).write_text("FIXTURE-LATER-STATE")
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 0, (status.stdout, status.stderr)
    assert json.loads(status.stdout)["state"] == "recorded_in_use" and snapshot(home) == before


def test_untouched_completed_preparation_keeps_its_strict_status(tmp_path, local_install_source):
    import fcntl
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    prepared = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    state = home / ".local/share/claudlobby-browser/data/state"
    # With nothing added since, a held runtime lock still means busy.
    held = os.open(state / "runtime-use.lock", os.O_RDWR)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = invoke_fixture(tmp_path, home, local_install_source, "status")
        result = json.loads(status.stdout)
        assert status.returncode == 4 and (result["state"], result["code"]) == ("busy", "preparation_in_progress")
    finally:
        os.close(held)
    # A state path that is not a directory refuses by name, and is never listed.
    (state / "runtime-use.lock").unlink(); state.rmdir(); state.write_text("not a folder")
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    result = json.loads(status.stdout)
    assert status.returncode == 4 and result["code"] == "existing_installation" and snapshot(home) == before


def test_status_never_reports_absent_beside_setup_state(tmp_path, monkeypatch):
    module = payload(); home, _python = profile(module, tmp_path, monkeypatch)
    family = home / ".local/share/claudlobby-browser"
    family.mkdir(mode=0o700, parents=True)
    (family / "provider-bin").mkdir(mode=0o700)
    before = snapshot(home)
    with pytest.raises(module.Refusal) as error:
        module.execute("status", json.loads(frame()), time.monotonic() + 15)
    assert (error.value.code, error.value.next_step) == ("existing_installation", "inspect")
    assert snapshot(home) == before


def test_darwin_policy_embeddings_and_existing_python_selector_are_exact():
    canonical = (ROOT / "claudlobby/darwin_runtime.py").read_text()
    entry = (ROOT / "claudlobby/preinstall/darwin-runtime-v1.sh").read_text()
    begin, end = "# BEGIN CANONICAL DARWIN RUNTIME PYTHON\n", "# END CANONICAL DARWIN RUNTIME PYTHON\n"
    for path in (SCRIPT, ROOT / "claudlobby/preinstall/owner-guide-v1.sh", ROOT / "claudlobby/installation.py"):
        assert path.read_text().split(begin, 1)[1].split(end, 1)[0] == canonical
    begin, end = "# BEGIN DARWIN EXISTING PYTHON SHELL\n", "# END DARWIN EXISTING PYTHON SHELL\n"
    selector = entry.split(begin, 1)[1].split(end, 1)[0]
    for path in (SCRIPT, ROOT / "claudlobby/preinstall/owner-guide-v1.sh"):
        assert path.read_text().split(begin, 1)[1].split(end, 1)[0] == selector


def _darwin_policy_fixture(module, home, monkeypatch):
    profile = types.SimpleNamespace(uid=os.geteuid(), account="fixture", home=home,
        root=home / ".local/share/claudlobby-browser/runtime/profile",
        python=home / "fixed-python", git=home / "fixed-git",
        ca_bundle=home / ".local/share/claudlobby-browser/runtime/profile/ca.pem")
    class RuntimeRefusal(Exception):
        def __init__(self, code): self.code = code
    def admit(**values):
        assert values == {"expected_uid": os.geteuid(), "expected_account": "fixture"}
        return profile
    environment = {"PATH": str(profile.root / "bin") + ":/usr/bin:/bin", "HOME": str(home),
                   "GIT_EXEC_PATH": str(profile.root / "git/libexec/git-core"),
                   "GIT_TEMPLATE_DIR": str(profile.root / "git/share/git-core/templates"),
                   "GIT_SSL_CAINFO": str(profile.root / "ca.pem"),
                   "GIT_ASKPASS": "/usr/bin/false", "SSL_CERT_FILE": str(profile.root / "ca.pem")}
    policy = types.SimpleNamespace(admit_profile=admit, environment=lambda actual: environment.copy(),
                                   RuntimeRefusal=RuntimeRefusal)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_dir=str(home), pw_name="fixture"))
    monkeypatch.setattr(module, "_darwin_runtime", lambda: policy)
    return profile, policy, environment


def test_darwin_installer_uses_admitted_python_git_ca_and_exact_runtime_family(tmp_path, monkeypatch):
    module = payload()
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    profile, policy, environment = _darwin_policy_fixture(module, home, monkeypatch)
    assert module.account_profile(os.geteuid()) == home
    monkeypatch.setattr(module.sys, "executable", str(profile.python))
    monkeypatch.setattr(module.sys, "prefix", module.sys.base_prefix)
    assert module.independent_python() == profile.python
    env = module.fixed_environment(home)
    assert all(env[key] == value for key, value in environment.items())
    calls = []
    module.git(lambda args, **kw: calls.append((args, kw)) or subprocess.CompletedProcess(args, 0),
               env, tmp_path / "uncreated-source", "status")
    assert calls[0][0][0] == str(profile.git)
    assert "core.askPass=/usr/bin/false" in calls[0][0]
    install, data, staging = create_paths(module, home)
    runtime = profile.root.parent; runtime.mkdir(mode=0o700)
    assert module.paths(home) == (install, data, staging)
    (runtime.parent / "unknown").mkdir()
    with pytest.raises(module.Refusal):
        module.paths(home)
    assert (runtime.parent / "unknown").exists()
    monkeypatch.setattr(module.sys, "executable", str(home / "other-python"))
    with pytest.raises(module.Refusal): module.independent_python()


def test_darwin_runtime_refusal_precedes_installer_paths_and_children(tmp_path, monkeypatch):
    module = payload(); home = tmp_path / "account"; home.mkdir(mode=0o700)
    _profile, policy, _environment = _darwin_policy_fixture(module, home, monkeypatch)
    def refuse(**_values): raise policy.RuntimeRefusal("unsupported_architecture")
    policy.admit_profile = refuse
    before = snapshot(home)
    monkeypatch.setattr(module, "claim_paths", lambda *_args: pytest.fail("created paths"))
    for action in ("prepare", "status"):
        with pytest.raises(module.Refusal) as error:
            module.execute(action, {"expected_uid": os.geteuid(), "source_revision": REVISION}, time.monotonic() + 15)
        assert error.value.code == "unsupported_architecture"
    assert snapshot(home) == before


def _bound(expected):
    return hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()


def test_status_after_a_system_python_update_keeps_the_recorded_binding(tmp_path, monkeypatch):
    """A security update replaces Ubuntu's /usr/bin Python between two reads."""
    module = payload(); home, _python = profile(module, tmp_path, monkeypatch)
    python = tmp_path / "system-bin/python3.12"  # stands in for the admitted file
    python.parent.mkdir()
    python.write_bytes(b"synthetic interpreter before a security update; never executed")
    python.chmod(0o755)
    monkeypatch.setattr(module, "independent_python", lambda: python)
    install, data, _staging = create_paths(module, home)
    lock = module.lock_open(install / "preparation.lock", True, True); os.close(lock)
    expected = module.binding(install, data, REVISION, python)
    state = {**expected, "revision": None, "lock_sha256": None, "environment_owned": False, "complete": False}
    for path, value in [(install / "preparation.json", state),
                        (install / "installation.json", {"schema": 1, "data_root": str(data)})]:
        path.write_text(json.dumps(value)); path.chmod(0o600)
    python.write_bytes(b"synthetic interpreter after a security update; never executed")
    assert module.digest(python) != expected["interpreter_sha256"]
    receipt, before = (install / "preparation.json").read_bytes(), snapshot(home)
    result = module.execute("status", json.loads(frame()), time.monotonic() + 15)
    assert result[:4] == ("incomplete", "preparation_incomplete", "resume_same", False)
    assert result[6] == _bound(expected)
    assert (install / "preparation.json").read_bytes() == receipt and snapshot(home) == before


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_receipt_compares_python_bytes_only_for_the_managed_mac_runtime(tmp_path, monkeypatch, system):
    module = payload(); home, python = profile(module, tmp_path, monkeypatch)
    install, data, _staging = create_paths(module, home)
    expected = module.binding(install, data, REVISION, python)
    state = {**expected, "interpreter_sha256": "e" * 64, "revision": REVISION, "lock_sha256": "b" * 64,
             "environment_owned": True, "complete": True}
    path = install / "preparation.json"; path.write_text(json.dumps(state)); path.chmod(0o600)
    monkeypatch.setattr(module.platform, "system", lambda: system)
    if system == "Linux":
        assert module.receipt(path, expected) == state
    else:
        with pytest.raises(module.Refusal) as error:
            module.receipt(path, expected)
        assert (error.value.code, error.value.next_step) == ("identity_changed", "inspect")
    for change in ({"interpreter": str(tmp_path / "other-python")}, {"interpreter_sha256": "E" * 64},
                   {"interpreter_sha256": None}):
        path.write_text(json.dumps({**state, **change}))
        with pytest.raises(module.Refusal) as error:
            module.receipt(path, expected)
        assert error.value.code == "identity_changed"


@pytest.mark.parametrize("defect", [None, "owner", "group-writable", "other-writable", "outside"])
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
    monkeypatch.setattr(module, "Path", Observed)
    if defect is None:
        assert module.independent_python() == executable.resolve()
        return
    with pytest.raises(module.Refusal) as error:
        module.independent_python()
    assert (error.value.code, error.value.state, error.value.exit_code) == ("prerequisite_unusable", "unavailable", 6)


def _record_older_python(family):
    """The digest a preparation recorded before this Python was updated."""
    record = family / "install/preparation.json"
    value = json.loads(record.read_text())
    value["interpreter_sha256"] = hashlib.sha256(b"the interpreter before its security update").hexdigest()
    record.write_text(json.dumps(value, sort_keys=True)); record.chmod(0o600)
    return value


def test_actual_status_in_use_after_a_system_python_update(tmp_path, local_install_source):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    prepared = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    family = home / ".local/share/claudlobby-browser"
    value = _record_older_python(family)
    add_setup_state(family)
    record = (family / "install/preparation.json").read_bytes()
    before = snapshot(home)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    result = json.loads(status.stdout)
    assert status.returncode == 0, (status.stdout, status.stderr)
    assert (result["state"], result["code"], result["next"]) == ("recorded_in_use", "none", "continue_onboarding")
    assert result["binding_sha256"] == _bound({key: value[key] for key in (
        "schema", "purpose", "installation", "data_root", "remote", "ref", "interpreter", "interpreter_sha256")})
    assert (family / "install/preparation.json").read_bytes() == record and snapshot(home) == before


def test_actual_resume_after_a_system_python_update_keeps_the_recorded_digest(tmp_path, local_install_source):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    failed = invoke_fixture(tmp_path, home, local_install_source, "prepare", fail_download=True)
    assert failed.returncode == 4 and json.loads(failed.stdout)["next"] == "inspect"
    family = home / ".local/share/claudlobby-browser"
    value = _record_older_python(family)
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 0 and json.loads(status.stdout)["next"] == "resume_same", status.stdout
    resumed = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    result = json.loads(resumed.stdout)
    assert resumed.returncode == 0 and result["state"] == "prepared", (resumed.stdout, resumed.stderr)
    state = json.loads((family / "install/preparation.json").read_text())
    assert state["complete"] and state["interpreter_sha256"] == value["interpreter_sha256"]
    assert result["binding_sha256"] == _bound({key: value[key] for key in (
        "schema", "purpose", "installation", "data_root", "remote", "ref", "interpreter", "interpreter_sha256")})
