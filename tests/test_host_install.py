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


def test_actual_resume_after_a_python_change_refuses_an_untrusted_interpreter(tmp_path, local_install_source):
    """The fixture Python is the account's own file outside /usr/bin: bootstrap
    itself refuses to resume across changed bytes, after host-install's check."""
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    failed = invoke_fixture(tmp_path, home, local_install_source, "prepare", fail_download=True)
    assert failed.returncode == 4 and json.loads(failed.stdout)["next"] == "inspect"
    family = home / ".local/share/claudlobby-browser"
    _record_older_python(family)
    record = (family / "install/preparation.json").read_bytes()
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 0 and json.loads(status.stdout)["next"] == "resume_same", status.stdout
    resumed = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    result = json.loads(resumed.stdout)
    assert resumed.returncode == 4 and (result["state"], result["code"]) == ("refused", "preparation_refused")
    assert not result["prepared"] and (family / "install/preparation.json").read_bytes() == record


def _fake_folder(path, defect):
    """The trusted folder as lstat reports it, with at most one property faked."""
    if not (defect or "").startswith("folder-"):
        return path
    import stat as stat_module
    class Folder(type(Path("/"))):
        def lstat(self):
            info = super().lstat()
            mode = stat_module.S_IFLNK | 0o777 if defect == "folder-symlink" else info.st_mode
            mode |= {"folder-group-writable": 0o020, "folder-other-writable": 0o002}.get(defect, 0)
            return types.SimpleNamespace(st_mode=mode, st_uid=info.st_uid + (defect == "folder-owner"))
    return Folder(path)


@pytest.mark.parametrize("defect", [None, "same-bytes-untrusted", "owner", "group-writable", "other-writable",
                                    "outside", "running-python", "folder-owner", "folder-group-writable", "folder-other-writable", "folder-symlink"])
def test_bootstrap_resume_admits_changed_python_bytes_only_for_the_trusted_system_python(tmp_path, monkeypatch, defect):
    from claudlobby import bootstrap
    from claudlobby import installation as locations
    monkeypatch.setattr(__import__("platform"), "system", lambda: "Linux")
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    monkeypatch.setattr(bootstrap.pwd, "getpwuid", lambda _uid: types.SimpleNamespace(pw_dir=str(home), pw_name="fixture"))
    family = home / ".local/share/claudlobby-browser"
    install, data = family / "install", family / "data"
    for path in (install, data):
        path.mkdir(parents=True); path.chmod(0o700)
    folder = tmp_path / "system-bin"; folder.mkdir(); folder.chmod(0o755)
    python = folder / "python3.12"  # stands in for the trusted system file
    python.write_bytes(b"synthetic interpreter; never executed"); python.chmod(0o755)
    monkeypatch.setattr(bootstrap, "_SYSTEM_PYTHON_DIRECTORY", _fake_folder(folder, defect))
    monkeypatch.setattr(bootstrap, "_SYSTEM_PYTHON_OWNER",
                        os.getuid() + (defect in ("owner", "same-bytes-untrusted")))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "other-python" if defect == "running-python" else python))
    if defect == "group-writable":
        python.chmod(0o775)
    elif defect == "other-writable":
        python.chmod(0o757)
    elif defect == "outside":
        monkeypatch.setattr(bootstrap, "_SYSTEM_PYTHON_DIRECTORY", tmp_path / "elsewhere")
    recorded = bootstrap._digest(python) if defect == "same-bytes-untrusted" else "e" * 64
    state = {"schema": 1, "purpose": "installation-preparation-only", "installation": str(install),
             "data_root": str(data), "remote": bootstrap._BROWSER_REMOTE, "ref": "a" * 40,
             "interpreter": str(python), "interpreter_sha256": recorded, "revision": None,
             "lock_sha256": None, "environment_owned": False, "complete": False}
    receipt = install / "preparation.json"
    receipt.write_text(json.dumps(state, sort_keys=True)); receipt.chmod(0o600)
    raw = receipt.read_bytes()
    lock = install / "preparation.lock"; lock.write_bytes(b""); lock.chmod(0o600)
    class Admitted(Exception):
        pass
    def admitted(_installation):
        raise Admitted()
    monkeypatch.setattr(locations, "write_installation", admitted)
    descriptor = os.open(lock, os.O_RDWR)
    try:
        def resume():
            bootstrap.prepare_git_installation(install, data, remote=bootstrap._BROWSER_REMOTE, ref="a" * 40,
                interpreter=python, _preparation_lock_fd=descriptor,
                _browser_runner=lambda *_a, **_k: pytest.fail("no child before receipt admission"))
        if defect in (None, "same-bytes-untrusted"):
            with pytest.raises(Admitted):
                resume()
        else:
            with pytest.raises(bootstrap.BundleError, match="different explicit inputs"):
                resume()
    finally:
        os.close(descriptor)
    assert receipt.read_bytes() == raw


# The update actions (phase 2, step 8): update-status, update and update-continue.

UPDATE_DRIVER = '''import sys,types,json,os,pwd,platform
platform.system=lambda: "Linux"
from pathlib import Path
body=Path(sys.argv[1]).read_text().split("# BEGIN EMBEDDED PYTHON\\n",1)[1].split("# END EMBEDDED PYTHON\\n",1)[0]
m=types.ModuleType("isolated_operation");exec(compile(body,sys.argv[1],"exec"),m.__dict__)
home=Path(sys.argv[2]);action=sys.argv[3];updater=json.loads(sys.argv[4]);log=Path(sys.argv[5])
python=Path(sys.executable).resolve()
m.account_profile=lambda uid: home
m.independent_python=lambda: python
pwd.getpwuid=lambda uid: types.SimpleNamespace(pw_dir=str(home))
original=m.Runner
class LocalRunner(original):
 def __call__(self,args,*,env,timeout=300):
  args=[str(a) for a in args]
  launcher=args[:2]==[str(python),"-I"]
  with log.open("a") as stream: stream.write(json.dumps({"args":args,"launcher":launcher,
   "env":env==(m.update_environment(home) if launcher else m.fixed_environment(home))})+"\\n")
  if launcher and args[9] in updater:
   # The stand-in for host update apply|continue: an exit, its output and
   # optionally the record the updater left.
   code,out,*left=updater[args[9]]
   if left: Path(left[0][0]).write_text(left[0][1])
   return m.subprocess.CompletedProcess(args,code,out.encode(),b"")
  assert not launcher or args[9:]==["status","--json"], args
  return super().__call__(args,env=env,timeout=timeout)
m.Runner=LocalRunner
raise SystemExit(m.update_main(action))
'''


def update_frame(**value):
    return (json.dumps({"schema_version": 1, "expected_uid": os.getuid(), **value}, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def invoke_update(tmp_path, home, action, request, updater=None):
    """One update action against the fixture preparation. The real launcher answers
    `host update status --json`; `updater` stands in for `host update apply|continue`."""
    driver, log = tmp_path / "update-driver.py", tmp_path / "children.jsonl"
    driver.write_text(UPDATE_DRIVER)
    log.write_text("")
    started = time.monotonic()
    result = subprocess.run([sys._base_executable, "-I", "-B", str(driver), str(SCRIPT), str(home), action,
                             json.dumps(updater or {}), str(log)], input=request,
                            env=constructed_env(PATH="/nonexistent", HOME=str(tmp_path / "untrusted-home"),
                                                PLANE_EMIT_DISABLED="1"), capture_output=True, timeout=120)
    print(f"MEASURED {action} {time.monotonic() - started:.2f}s")
    children = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(child["env"] for child in children), children
    assert result.stdout.count(b"\n") == 1 and len(result.stdout) <= 8192, (result.stdout, result.stderr)
    return result, json.loads(result.stdout), children


def _launched(children, *action):
    """The launcher's update commands, after `--root <data> host update`."""
    return [child["args"][9:] for child in children if child["launcher"] and child["args"][9:9 + len(action)] == list(action)]


def _installation(home):
    from claudlobby.installation import Installation
    family = home / ".local/share/claudlobby-browser"
    return Installation(family / "install", family / "data")


def _prepared(tmp_path, local_install_source, *, setup="untouched"):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    prepared = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    family = home / ".local/share/claudlobby-browser"
    if setup != "untouched":
        add_setup_state(family)
    if setup == "started":
        (family / "data/state/config.json").write_text("{}"); (family / "data/state/config.json").chmod(0o600)
    return home, family


def _record(home, revision, kind="update", **changes):
    """A record of `kind` as the updater would read it; the store makes the base."""
    from claudlobby import git_update as updater
    from tests.test_git_update import prepare
    installation = _installation(home)
    with updater.UpdateStore(installation) as store:
        record = prepare(store, revision)
    record.update({"phase": "quiesce", **changes})
    if kind in {"start", "first_team"}:
        record["initialization_scope"] = {"directory": str(home / "native"), "manifests": (
            [] if kind == "start" else [str(home / "authored-team/fleet.yaml")])}
    if kind == "addition":
        record["addition_scope"] = {"manifests": [str(home / "authored-team/fleet.yaml")]}
    record["maintenance"] = record["phase"] in {"git", "environment", "publish", "commit"}
    updater._write(installation.data_root / "state/update.json",
                   json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
    assert updater.read_update(installation) == record
    return record


def _outcome(record, **job):
    return json.dumps({"update": record, "job": job or {"dispatch": "started"}})


STATUS_KEYS = {"schema_version", "operation", "state", "recorded_revision", "installed_revision",
               "receipt_is_ancestor", "binding_sha256", "setup", "interpreter", "update"}
REQUEST_ID = "00000000-0000-4000-8000-000000000001"


def test_the_update_actions_build_their_own_environment(tmp_path, monkeypatch):
    module = payload()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    home = tmp_path / "account"
    family = home / ".local/share/claudlobby-browser"
    env = module.update_environment(home)
    uid = str(os.getuid())
    assert {key: env[key] for key in ("CLAUDLOBBY_ROOT", "CLAUDLOBBY_INSTALLATION", "CLAUDLOBBY_HOST_SYSTEM_YAML",
                                      "CLAUDE_CONFIG_DIR", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")} == {
        "CLAUDLOBBY_ROOT": str(family / "data"), "CLAUDLOBBY_INSTALLATION": str(family / "install"),
        "CLAUDLOBBY_HOST_SYSTEM_YAML": str(family / "host-policy/system.yaml"),
        "CLAUDE_CONFIG_DIR": str(family / "provider-profile"), "XDG_RUNTIME_DIR": "/run/user/" + uid,
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/" + uid + "/bus"}
    assert "PLANE_EMIT_DISABLED" not in env and env["PATH"] == "/usr/bin:/bin" and env["HOME"] == str(home)
    # The guide script pins the same four selectors.
    guide = (ROOT / "claudlobby/preinstall/owner-guide-v1.sh").read_text()
    for key in ("CLAUDLOBBY_ROOT", "CLAUDLOBBY_INSTALLATION", "CLAUDLOBBY_HOST_SYSTEM_YAML", "CLAUDE_CONFIG_DIR"):
        assert f'"{key}": str(family / ' in guide
    # prepare and status keep theirs.
    assert module.fixed_environment(home)["PLANE_EMIT_DISABLED"] == "1"
    assert "CLAUDLOBBY_ROOT" not in module.fixed_environment(home)
    # On a Mac: the managed runtime's map without PLANE_EMIT_DISABLED, and no user bus.
    home.mkdir(mode=0o700)
    _profile, _policy, environment = _darwin_policy_fixture(module, home, monkeypatch)
    environment["PLANE_EMIT_DISABLED"] = "1"
    mac = module.update_environment(home)
    assert "PLANE_EMIT_DISABLED" not in mac and "XDG_RUNTIME_DIR" not in mac and "DBUS_SESSION_BUS_ADDRESS" not in mac
    assert mac["CLAUDLOBBY_HOST_SYSTEM_YAML"] == str(family / "host-policy/system.yaml")
    assert mac["PATH"] == environment["PATH"]


@pytest.mark.parametrize("action, request_value", [
    ("update-status", {}), ("update-status", {"compare_revision": "a" * 40}),
    ("update", {"expected_installed_revision": "a" * 40, "target_revision": "b" * 40}),
    ("update-continue", {"request_id": REQUEST_ID}),
])
def test_each_update_action_reads_exactly_its_own_request(action, request_value):
    module = payload()
    def read(raw, named=action):
        reader, writer = os.pipe()
        os.write(writer, raw); os.close(writer)
        try:
            return module.request(reader, time.monotonic() + 10, named)
        finally:
            os.close(reader)
    assert read(update_frame(**request_value)) == {"schema_version": 1, "expected_uid": os.getuid(), **request_value}
    bad = [update_frame(**request_value, source_revision="a" * 40), frame(),
           update_frame(**request_value)[:-1], update_frame(**request_value).replace(b",", b", ", 1),
           update_frame(**request_value, schema_version=2), update_frame(**request_value, expected_uid="1")]
    if request_value:
        bad.append(update_frame(**{key: "A" * 40 for key in request_value}))
        bad.append(update_frame(**{key: "a" * 39 for key in request_value}))
    if action != "update-status":
        bad.append(update_frame())  # every key the action names is required
    for raw in bad:
        with pytest.raises(module.Refusal) as refused:
            read(raw)
        assert (refused.value.code, refused.value.exit_code) == ("invalid_request", 2)
    # Another action's keys are refused, and prepare and status read only their own.
    for other in {"update-status", "update", "update-continue"} - {action}:
        if request_value:
            with pytest.raises(module.Refusal):
                read(update_frame(**request_value), other)
    with pytest.raises(Exception) as refused:
        parse(update_frame(**request_value))  # its own payload, its own Refusal
    assert (type(refused.value).__name__, refused.value.code) == ("Refusal", "invalid_request")


def _bashes():
    import shutil
    found = {Path("/bin/bash").resolve()}
    other = shutil.which("bash")
    if other:
        found.add(Path(other).resolve())
    return sorted(str(path) for path in found)


@pytest.mark.parametrize("action, operation", [("update-status", "host-update-status-v1"),
                                               ("update", "host-update-v1"),
                                               ("update-continue", "host-update-continue-v1")])
def test_the_shell_answers_each_update_action_in_its_own_names(tmp_path, action, operation):
    text = SCRIPT.read_text()
    # The portable head: the operation is named, then a refusal in the action's own keys.
    head = text.split("exec 3<<'FIXED_OPERATION_PYTHON'\n", 1)[0]
    shells = ["/bin/sh", *_bashes()]
    for shell in shells:
        script = tmp_path / "head.sh"
        script.write_text(head.replace('if [ "$#" -ne 1 ]', 'refuse prerequisite_missing stop unavailable; exit 6\nif [ "$#" -ne 1 ]', 1))
        result = subprocess.run([shell, str(script), action], input=b"", env=constructed_env(),
                                capture_output=True, timeout=5)
        assert result.returncode == 6 and result.stderr == b"", (shell, result)
        # A code outside the action's contract is answered as unavailable.
        assert json.loads(result.stdout) == {"code": "unavailable", "operation": operation,
                                             "schema_version": 1, "state": "unavailable"}
        # Two arguments name no action: the preparation's own refusal.
        result = subprocess.run([shell, str(SCRIPT), action, "extra"], input=b"", env=constructed_env(),
                                capture_output=True, timeout=5)
        assert result.returncode == 2 and json.loads(result.stdout)["operation"] == "host-install-v1"
    # The Mac launcher, under every bash here (3.2 on a Mac, 5 on Linux).
    launcher = text.split("<<'DARWIN_OPERATION_LAUNCHER' 2>/dev/null\n", 1)[1].split("\nDARWIN_OPERATION_LAUNCHER\n", 1)[0]
    launcher = launcher.replace('if ! darwin_existing_python', 'darwin_existing_python() { return 6; }\nif ! darwin_existing_python', 1)
    for shell in _bashes():
        script = tmp_path / "launcher.sh"
        script.write_text(launcher)
        result = subprocess.run([shell, str(script), action], input=b"", env=constructed_env(),
                                capture_output=True, timeout=5)
        assert result.returncode == 6, (shell, result)
        assert json.loads(result.stdout) == {"code": "unavailable", "operation": operation,
                                             "schema_version": 1, "state": "unavailable"}


def test_update_status_on_an_absent_computer(tmp_path, local_install_source):
    home = tmp_path / "account"; home.mkdir(mode=0o700)
    before = snapshot(home)
    result, answer, children = invoke_update(tmp_path, home, "update-status", update_frame())
    assert result.returncode == 0 and set(answer) == STATUS_KEYS and children == []
    assert answer["state"] == "absent" and answer["operation"] == "host-update-status-v1"
    assert all(answer[key] is None for key in STATUS_KEYS - {"schema_version", "operation", "state"})
    for action, value in (("update", {"expected_installed_revision": "a" * 40, "target_revision": "b" * 40}),
                          ("update-continue", {"request_id": REQUEST_ID})):
        result, answer, children = invoke_update(tmp_path, home, action, update_frame(**value))
        assert result.returncode == 4 and answer == {"schema_version": 1, "operation": UPDATE_OPERATIONS[action],
                                                     "state": "refused", "code": "not_prepared"}
        assert children == []
    assert snapshot(home) == before


UPDATE_OPERATIONS = {"update-status": "host-update-status-v1", "update": "host-update-v1",
                     "update-continue": "host-update-continue-v1"}


@pytest.mark.parametrize("setup", ["untouched", "in_use", "started"])
def test_update_status_reads_the_recorded_and_installed_revisions(tmp_path, local_install_source, setup):
    _remote, _wheels, revision = local_install_source
    home, family = _prepared(tmp_path, local_install_source, setup=setup)
    receipt = json.loads((family / "install/preparation.json").read_text())
    before = snapshot(home)
    result, answer, children = invoke_update(tmp_path, home, "update-status", update_frame())
    assert result.returncode == 0, answer
    assert set(answer) == STATUS_KEYS and answer["state"] == "recorded"
    assert (answer["recorded_revision"], answer["installed_revision"]) == (revision, revision)
    assert answer["receipt_is_ancestor"] is True and answer["setup"] == setup and answer["interpreter"] == "same"
    assert answer["binding_sha256"] == _bound({key: receipt[key] for key in (
        "schema", "purpose", "installation", "data_root", "remote", "ref", "interpreter", "interpreter_sha256")})
    assert answer["update"] is None and snapshot(home) == before
    # The record is the updater's own read, through the launcher's update route.
    assert [child["args"][5:] for child in children if child["launcher"]] == [
        ["--root", str(family / "data"), "host", "update", "status", "--json"]]
    for compare, relation in ((revision, "same"), ("f" * 40, "other")):
        result, answer, _children = invoke_update(tmp_path, home, "update-status",
                                                  update_frame(compare_revision=compare))
        assert result.returncode == 0 and answer["relation"] == relation
    assert snapshot(home) == before


def _commit(source, *parents, message="later"):
    def git(*args):
        return subprocess.run(["/usr/bin/git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               "-C", str(source), *args], env=constructed_env(GIT_CONFIG_GLOBAL="/dev/null"),
                              capture_output=True, check=True, timeout=15).stdout.decode().strip()
    return git("commit-tree", "HEAD^{tree}", *[item for parent in parents for item in ("-p", parent)], "-m", message), git


def test_update_status_reports_an_installed_revision_ahead_of_its_receipt(tmp_path, local_install_source):
    _remote, _wheels, revision = local_install_source
    home, family = _prepared(tmp_path, local_install_source, setup="started")
    later, git = _commit(family / "install/src", "HEAD")
    git("update-ref", "--no-deref", "HEAD", later)
    receipt = (family / "install/preparation.json").read_bytes()
    result, answer, _children = invoke_update(tmp_path, home, "update-status", update_frame(compare_revision=revision))
    assert result.returncode == 0, answer
    assert (answer["recorded_revision"], answer["installed_revision"]) == (revision, later)
    assert answer["receipt_is_ancestor"] is True and answer["relation"] == "ahead"
    assert (family / "install/preparation.json").read_bytes() == receipt


RECORDS = {
    "start": ("start", {}, {"kind": "start", "dispatched": False, "failed": False, "maintenance": False}),
    "first_team": ("first_team", {}, {"kind": "first_team", "dispatched": False, "failed": False}),
    "addition": ("addition", {}, {"kind": "addition", "dispatched": False}),
    "maintenance": ("update", {"phase": "environment", "job_dispatch": {"state": "entered", "attempt": 1}},
                    {"kind": "update", "maintenance": True, "dispatched": True, "failed": False}),
    "failed": ("update", {"phase": "environment", "job_dispatch": {"state": "finished", "attempt": 1},
                          "stage_error": {"stage": "publish", "detail": "x"}},
               {"kind": "update", "failed": True}),
    "health_errors": ("start", {"phase": "done", "job_dispatch": {"state": "finished", "attempt": 1},
                                "health_errors": {"b.service": "x", "a.timer": "y"}},
                      {"kind": "start", "phase": "done", "failed": True, "health_errors": ["a.timer", "b.service"]}),
    "done": ("update", {"phase": "done", "job_dispatch": {"state": "finished", "attempt": 1}},
             {"kind": "update", "phase": "done", "failed": False, "health_errors": []}),
}


@pytest.mark.parametrize("name", sorted(RECORDS))
def test_update_status_reports_the_record_and_its_kind(tmp_path, local_install_source, name):
    _remote, _wheels, revision = local_install_source
    home, _family = _prepared(tmp_path, local_install_source, setup="started")
    kind, changes, expected = RECORDS[name]
    record = _record(home, revision, kind, **changes)
    result, answer, _children = invoke_update(tmp_path, home, "update-status", update_frame())
    assert result.returncode == 0, answer
    update = answer["update"]
    assert set(update) == UPDATE_KEYS
    assert update["request_id"] == record["request_id"] and update["target"] == revision
    assert {key: update[key] for key in expected} == expected


# The whole of `update` in a status answer: #3079's fields, then where the update
# is, each read from the record alone.
UPDATE_KEYS = {"request_id", "kind", "phase", "target", "maintenance", "dispatched", "failed", "health_errors",
               "stage", "units", "runner", "pending_seconds", "entered_seconds", "outcome", "waiting",
               "runtime_busy", "unhealthy_units"}
NOW = 1_800_000_000
SUMMARY_BASE = {"request_id": REQUEST_ID, "phase": "quiesce", "target": "a" * 40, "maintenance": False}
UNITS = {"a.service": {"stop": "done"}, "b.service": {"stop": "done", "restore_error": "x"}, "c.service": {}}
PROGRESS = {
    "prepared": ({}, {"stage": None, "units": None, "runner": "none", "pending_seconds": None,
                      "entered_seconds": None, "outcome": None, "waiting": False, "runtime_busy": False,
                      "unhealthy_units": 0, "failed": False}),
    "pending": ({"job_dispatch": {"state": "pending", "attempt": 1}, "job_clock": {"pending_at": NOW - 130}},
                {"runner": "pending", "pending_seconds": 130, "entered_seconds": None, "failed": False}),
    # A job that entered and has said nothing since is running, however long ago.
    "entered": ({"job_dispatch": {"state": "entered", "attempt": 2},
                 "job_clock": {"pending_at": NOW - 9000, "entered_at": NOW - 8000}},
                {"runner": "entered", "pending_seconds": 9000, "entered_seconds": 8000, "outcome": None,
                 "failed": False}),
    "clock_ahead": ({"job_dispatch": {"state": "entered", "attempt": 1}, "job_clock": {"entered_at": NOW + 5}},
                    {"entered_seconds": 0, "pending_seconds": None}),
    "quiesce_units": ({"consumers": UNITS}, {"units": {"done": 1, "total": 3}}),
    "restore_units": ({"phase": "restore", "consumers": {"a.service": {"stop": "done", "restore": "done"},
                                                         "b.service": {"stop": "done"}}},
                      {"units": {"done": 1, "total": 2}}),
    "no_units": ({"phase": "restore", "consumers": {}}, {"units": None}),
    "units_elsewhere": ({"phase": "git", "maintenance": True, "consumers": UNITS}, {"units": None}),
    "stage": ({"phase": "restore", "stage_error": {"stage": "restore", "detail": "unit failed: secret text"}},
              {"stage": "restore", "failed": True}),
    "other_stage": ({"stage_error": {"stage": "something new", "detail": "x"}}, {"stage": "other", "failed": True}),
    "finished": ({"phase": "done", "job_dispatch": {"state": "finished", "attempt": 1},
                  "job_outcome": {"attempt": 1, "at": NOW, "result": "finished"}},
                 {"runner": "finished", "outcome": "finished", "failed": False}),
    "unfinished": ({"phase": "environment", "maintenance": True, "job_dispatch": {"state": "finished", "attempt": 1},
                    "job_outcome": {"attempt": 1, "at": NOW, "result": "unfinished"}},
                   {"runner": "finished", "outcome": "unfinished", "failed": True}),
    # Refused before it entered: the dispatch still says pending, and the update has stopped.
    "refused": ({"job_dispatch": {"state": "pending", "attempt": 1},
                 "job_outcome": {"attempt": 1, "at": NOW, "result": "refused"}},
                {"runner": "pending", "outcome": "refused", "failed": True}),
    # An earlier attempt's outcome does not speak for the dispatch after it.
    "earlier_attempt": ({"job_dispatch": {"state": "pending", "attempt": 2},
                         "job_outcome": {"attempt": 1, "at": NOW, "result": "refused"}},
                        {"outcome": None, "failed": False}),
    "waiting": ({"job_dispatch": {"state": "entered", "attempt": 1},
                 "runtime_wait": {"gate": "cutover", "at": "x", "seconds": 301.0, "holders": [{"unit": "a"}]},
                 "runtime_waits": [{"gate": "drain", "outcome": "taken"}]},
                {"waiting": True, "runtime_busy": False, "failed": False}),
    "runtime_busy": ({"phase": "restore", "job_dispatch": {"state": "finished", "attempt": 1},
                      "runtime_waits": [{"gate": "drain", "outcome": "taken"}, {"gate": "cutover", "outcome": "refused"}]},
                     {"waiting": False, "runtime_busy": True, "failed": True}),
    "busy_before": ({"runtime_waits": [{"outcome": "refused"}, {"outcome": "taken"}]}, {"runtime_busy": False}),
    # An outcome with no dispatch to speak for (the updater writes none) says nothing.
    "no_dispatch_outcome": ({"job_outcome": {"attempt": None, "at": NOW, "result": "refused"}},
                            {"runner": "none", "outcome": None, "failed": False}),
    # Unhashable values where a code belongs read as `other` or nothing, and refuse nothing.
    "unhashable": ({"stage_error": {"stage": ["restore"]}, "job_dispatch": {"state": ["entered"], "attempt": 1},
                    "job_outcome": {"attempt": 1, "at": NOW, "result": ["refused"]}},
                   {"stage": "other", "runner": "none", "outcome": None}),
    "unhealthy": ({"phase": "done", "health_errors": {"b.service": "x", "a.timer": "y"}},
                  {"unhealthy_units": 2, "failed": True}),
    # Keys in a shape this script does not know read as nothing, and refuse nothing.
    "malformed": ({"job_clock": {"pending_at": "soon", "entered_at": True}, "job_outcome": "finished",
                   "runtime_waits": {"outcome": "refused"}, "runtime_wait": "yes",
                   "consumers": ["a.service"], "stage_error": "x"},
                  {"pending_seconds": None, "entered_seconds": None, "outcome": None, "runtime_busy": False,
                   "waiting": False, "units": None, "stage": "other"}),
}


@pytest.mark.parametrize("name", sorted(PROGRESS))
def test_update_status_reports_where_the_update_is_from_the_record_alone(name):
    """Each progress field, from the record and the computer's clock only.

    Nothing here can look at a native job: a job that entered and has not
    finished reads as running, however old, and only the record says stopped.
    """
    module = payload()
    changes, expected = PROGRESS[name]
    update = module.summary({**SUMMARY_BASE, **changes}, now=NOW)
    assert set(update) == UPDATE_KEYS
    assert {key: update[key] for key in expected} == expected
    # A closed set of codes, counts and seconds: no record text, and no unit names beyond #3079's own.
    assert update["stage"] in module.STAGES | {"other", None}
    assert update["runner"] in {"none", "pending", "entered", "finished"}
    assert update["outcome"] in {"finished", "unfinished", "refused", None}
    assert "secret text" not in json.dumps(update)


def test_the_carrier_document_names_exactly_the_update_fields():
    import re
    carrier = (ROOT / "documentation/architecture/owner-ssh-carrier.md").read_text()
    section = carrier.split("## Updating a started computer\n", 1)[1].split("\n## ", 1)[0]
    listed = re.sub(r"\([^)]*\)", "", section.split("in the names a\nclient reads: ", 1)[1].split(", then where", 1)[0])
    table = [line.split("|")[1] for line in section.splitlines() if line.startswith("| `")
             and "`" in line.split("|")[1] and line.split("|")[2].strip() != ""]
    progress = [line for line in table if not line.strip().startswith(("`update-status`", "`update`",
                                                                       "`update-continue`"))]
    named = set(re.findall(r"`([a-z_]+)`", listed)) | {key for line in progress
                                                        for key in re.findall(r"`([a-z_]+)`", line)}
    assert named == UPDATE_KEYS
    for key in payload().STAGES | payload().OUTCOMES:
        assert f"`{key}`" in section, key
    # A record of each shape still yields only those fields, and update/continue's own reads work on it.
    module = payload()
    for changes, _expected in PROGRESS.values():
        update = module.summary({**SUMMARY_BASE, **changes}, now=NOW)
        assert set(update) == UPDATE_KEYS
        module.unfinished(update), module.undispatched_team(update)


def test_the_stage_codes_are_the_updaters_own_stages():
    import re
    entry = (ROOT / "claudlobby/update_entry.py").read_text()
    choices = re.search(r'add_argument\("stage", choices=\[([^\]]*)\]', entry).group(1)
    assert payload().STAGES == set(re.findall(r'"([a-z-]+)"', choices))
    from claudlobby import git_update
    assert payload().OUTCOMES == set(git_update.JOB_RESULTS)


@pytest.mark.parametrize("phase", ["prepare", "quiesce", "git", "environment", "publish", "migrate", "commit",
                                   "restore", "done"])
def test_update_status_reads_progress_while_the_updater_holds_its_lock(tmp_path, local_install_source, phase):
    """While the updater holds update.lock, at any phase, update-status still answers.

    It takes no update lock: a read that waited would run past its budget and
    answer unavailable.
    """
    import fcntl
    _remote, _wheels, revision = local_install_source
    home, family = _prepared(tmp_path, local_install_source, setup="started")
    record = _record(home, revision, "first_team", phase="commit" if phase == "migrate" else phase,
                     job_dispatch={"state": "entered", "attempt": 1}, job_clock={"pending_at": 1, "entered_at": 2},
                     consumers={"a.service": {"stop": "done", "restore": "done"}, "b.service": {"stop": "done"}})
    if phase == "migrate":  # the fixture's maintenance rule leaves migrate out
        record = _rewrite(home, record, phase="migrate", maintenance=True)
    descriptor = os.open(family / "data/state/update.lock", os.O_RDWR | os.O_CREAT, 0o600)
    before = snapshot(home)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, answer, _children = invoke_update(tmp_path, home, "update-status", update_frame())
    finally:
        os.close(descriptor)
    assert result.returncode == 0 and answer["state"] == "recorded", answer
    update = answer["update"]
    assert set(update) == UPDATE_KEYS and update["request_id"] == record["request_id"]
    assert (update["phase"], update["runner"], update["outcome"], update["failed"]) == (phase, "entered", None, False)
    assert type(update["pending_seconds"]) is int and type(update["entered_seconds"]) is int
    assert update["units"] == ({"done": 2, "total": 2} if phase == "quiesce" else {"done": 1, "total": 2}
                               if phase == "restore" else None)
    assert snapshot(home) == before


def test_update_status_after_a_system_python_update_reports_it_and_every_action_goes_on(tmp_path, local_install_source):
    """Step 10: the system Python replaced after preparation. Linux admits it by trust, not bytes."""
    _remote, _wheels, revision = local_install_source
    home, family = _prepared(tmp_path, local_install_source, setup="started")
    _record_older_python(family)
    receipt = (family / "install/preparation.json").read_bytes()
    result, answer, _children = invoke_update(tmp_path, home, "update-status", update_frame())
    assert result.returncode == 0 and answer["interpreter"] == "changed" and answer["state"] == "recorded", answer
    later, _git = _commit(family / "install/src", "HEAD")
    result, answer, children = invoke_update(tmp_path, home, "update", update_frame(
        expected_installed_revision=revision, target_revision=later), {"apply": [0, _outcome(
            {**_record_shape(home, revision), "target": later})]})
    assert result.returncode == 0 and answer["state"] == "updating", answer
    assert _launched(children, "apply") == [["apply", later]]
    record = _record(home, revision)
    result, answer, _children = invoke_update(tmp_path, home, "update-continue", update_frame(
        request_id=record["request_id"]), {"continue": [0, _outcome(record)]})
    assert result.returncode == 0 and answer["state"] == "updating", answer
    assert (family / "install/preparation.json").read_bytes() == receipt
    # prepare's status still answers as before.
    status = invoke_fixture(tmp_path, home, local_install_source, "status")
    assert status.returncode == 0 and json.loads(status.stdout)["state"] == "recorded_in_use"


def _record_shape(home, revision):
    """A record of an ordinary update the stand-in reports, without writing one."""
    record = _record(home, revision)
    (_installation(home).data_root / "state/update.json").unlink()
    return record


def test_update_dispatches_and_exits(tmp_path, local_install_source):
    _remote, _wheels, revision = local_install_source
    home, family = _prepared(tmp_path, local_install_source, setup="started")
    later, _git = _commit(family / "install/src", "HEAD")
    record = {**_record_shape(home, revision), "target": later}
    receipt = (family / "install/preparation.json").read_bytes()
    result, answer, children = invoke_update(tmp_path, home, "update", update_frame(
        expected_installed_revision=revision, target_revision=later), {"apply": [0, _outcome(record)]})
    assert result.returncode == 0, answer
    assert answer == {"schema_version": 1, "operation": "host-update-v1", "state": "updating",
                      "request_id": record["request_id"], "target": later}
    assert _launched(children, "apply") == [["apply", later]]
    assert [child["args"][5:9] for child in children if child["launcher"]] == [
        ["--root", str(family / "data"), "host", "update"]] * 2
    assert (family / "install/preparation.json").read_bytes() == receipt
    # Asked again for the same target in flight: the same command runs again, and
    # the answer is the record the updater reports.
    in_flight = _record(home, revision, phase="git", job_dispatch={"state": "entered", "attempt": 1})
    in_flight = _rewrite(home, in_flight, target=later)
    result, answer, children = invoke_update(tmp_path, home, "update", update_frame(
        expected_installed_revision=revision, target_revision=later),
        {"apply": [0, _outcome(in_flight, dispatch="attached")]})
    assert result.returncode == 0 and answer["request_id"] == in_flight["request_id"], answer
    assert _launched(children, "apply") == [["apply", later]]
    # An updater that answers for another target is not a dispatch of this one.
    result, answer, _children = invoke_update(tmp_path, home, "update", update_frame(
        expected_installed_revision=revision, target_revision=later),
        {"apply": [0, _outcome({**in_flight, "target": "e" * 40})]})
    assert (result.returncode, answer["code"]) == (6, "unavailable"), answer


def _rewrite(home, record, **changes):
    from claudlobby import git_update as updater
    record = {**record, **changes}
    updater._write(_installation(home).data_root / "state/update.json",
                   json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
    return record


@pytest.mark.parametrize("case", ["stale", "current", "not_started", "untouched", "other_target", "health_errors",
                                  "undispatched_other", "preview_same_target", "addition", "not_forward",
                                  "raced_preview", "refused"])
def test_update_refuses_by_name(tmp_path, local_install_source, case):
    _remote, _wheels, revision = local_install_source
    setup = {"not_started": "in_use", "untouched": "untouched"}.get(case, "started")
    home, family = _prepared(tmp_path, local_install_source, setup=setup)
    later, git = _commit(family / "install/src", "HEAD")
    shown, exit_code = revision, 4
    if case == "stale":
        shown, expected = "d" * 40, "installed_changed"
    elif case == "current":
        later, expected = revision, "already_current"
    elif case in {"not_started", "untouched"}:
        expected = "needs_reset"
    elif case == "other_target":
        _rewrite(home, _record(home, revision, phase="git", job_dispatch={"state": "entered", "attempt": 1}),
                 target="e" * 40)
        expected = "update_in_progress"
    elif case == "health_errors":
        _record(home, revision, phase="done", health_errors={"a.service": "x"}); expected = "update_in_progress"
    elif case == "undispatched_other":
        _record(home, revision, "update"); expected = "update_in_progress"
    elif case == "preview_same_target":
        _record(home, revision, "first_team"); later, expected = revision, "update_in_progress"
    elif case == "addition":
        _record(home, revision, "addition"); expected = "update_in_progress"
    elif case == "not_forward":
        later, _git = _commit(family / "install/src", message="unrelated")
        expected = "not_forward"
    elif case == "raced_preview":
        # Nothing on disk when the script read; under its lock the updater found a
        # Preview for this target and refused it (exit 7) before any dispatch.
        expected = "update_in_progress"
    else:
        later, expected, exit_code = "c" * 40, "unavailable", 6
    path = family / "data/state/update.json"
    before = path.read_bytes() if path.exists() else None
    result, answer, children = invoke_update(tmp_path, home, "update", update_frame(
        expected_installed_revision=shown, target_revision=later),
        {"apply": [7 if case == "raced_preview" else 6, ""]})
    assert (result.returncode, answer) == (exit_code, {"schema_version": 1, "operation": "host-update-v1",
                                                       "state": "refused" if exit_code == 4 else "unavailable",
                                                       "code": expected}), answer
    assert len(_launched(children, "apply")) == (1 if case in {"not_forward", "raced_preview", "refused"} else 0)
    assert (path.read_bytes() if path.exists() else None) == before
    assert git("rev-parse", "HEAD") == revision


@pytest.mark.parametrize("case", ["start", "failed_update", "health_errors", "dispatched_team", "dispatched_addition"])
def test_update_continue_serves_what_is_unfinished(tmp_path, local_install_source, case):
    _remote, _wheels, revision = local_install_source
    home, family = _prepared(tmp_path, local_install_source, setup="started")
    finished = {"job_dispatch": {"state": "finished", "attempt": 1}}
    record = {"start": lambda: _record(home, revision, "start"),
              "failed_update": lambda: _record(home, revision, phase="environment", **finished),
              "health_errors": lambda: _record(home, revision, phase="done", health_errors={"a.service": "x"},
                                               **finished),
              "dispatched_team": lambda: _record(home, revision, "first_team", **finished),
              "dispatched_addition": lambda: _record(home, revision, "addition", **finished)}[case]()
    before = (family / "data/state/update.json").read_bytes()
    result, answer, children = invoke_update(tmp_path, home, "update-continue",
                                             update_frame(request_id=record["request_id"]),
                                             {"continue": [0, _outcome(record)]})
    assert result.returncode == 0, answer
    assert answer == {"schema_version": 1, "operation": "host-update-continue-v1", "state": "updating",
                      "request_id": record["request_id"], "target": revision}
    assert _launched(children, "continue") == [["continue", "--expected-request", record["request_id"]]]
    assert (family / "data/state/update.json").read_bytes() == before


@pytest.mark.parametrize("case", ["done", "other_id", "none", "preview", "prepared_team", "addition",
                                  "raced_team", "raced_record", "updater_failed", "old_updater"])
def test_update_continue_refuses_by_name(tmp_path, local_install_source, case):
    _remote, _wheels, revision = local_install_source
    home, family = _prepared(tmp_path, local_install_source, setup="started")
    path = family / "data/state/update.json"
    request, updater, exit_code, expected = REQUEST_ID, {"continue": [6, ""]}, 4, None
    if case == "done":
        _record(home, revision, phase="done"); expected = "nothing_to_continue"
    elif case == "other_id":
        _record(home, revision); request, expected = "00000000-0000-4000-8000-0000000000ff", "no_such_request"
    elif case == "none":
        expected = "no_such_request"
    elif case == "preview":
        _record(home, revision, "first_team"); expected = "not_continuable"
    elif case == "prepared_team":
        _record(home, revision, "first_team", phase="restore"); expected = "not_continuable"
    elif case == "addition":
        _record(home, revision, "addition"); expected = "not_continuable"
    elif case == "raced_team":
        # The script read a start; the updater, held to the id, found a team preparation.
        _record(home, revision, "start"); updater, expected = {"continue": [7, ""]}, "not_continuable"
    elif case == "raced_record":
        # Between the script's read and the updater's, another request replaced it.
        record = _record(home, revision, "start")
        replaced = json.dumps({**record, "request_id": "00000000-0000-4000-8000-0000000000ee"}, sort_keys=True)
        updater, expected = {"continue": [6, "", [str(path), replaced]]}, "no_such_request"
    elif case == "updater_failed":
        _record(home, revision, "start"); expected, exit_code = "unavailable", 6
    else:
        # An updater that does not know the flag stops at its parser.
        _record(home, revision, "start"); updater, expected, exit_code = {"continue": [2, ""]}, "unavailable", 6
    before = path.read_bytes() if path.exists() else None
    result, answer, children = invoke_update(tmp_path, home, "update-continue", update_frame(request_id=request),
                                             updater)
    assert (result.returncode, answer) == (exit_code, {"schema_version": 1, "operation": "host-update-continue-v1",
                                                       "state": "refused" if exit_code == 4 else "unavailable",
                                                       "code": expected}), answer
    continued = _launched(children, "continue")
    assert len(continued) == (1 if case in {"raced_team", "raced_record", "updater_failed", "old_updater"} else 0)
    if case != "raced_record":
        assert (path.read_bytes() if path.exists() else None) == before


CONTRACT = {"update-status": set(),
            "update": {"not_prepared", "installed_changed", "already_current", "update_in_progress", "not_forward",
                       "needs_reset"},
            "update-continue": {"not_prepared", "no_such_request", "nothing_to_continue", "not_continuable"}}


def _every_refusal_code():
    import re
    codes = set(re.findall(r'Refusal\("([a-z_]+)"', BODY)) | set(re.findall(r'unavailable\("([a-z_]+)"\)', BODY))
    # update_refusal and continue_refusal return theirs, raised as Refusal(code).
    return codes | {"inspection_unavailable"} | set().union(*CONTRACT.values())


@pytest.mark.parametrize("action", sorted(CONTRACT))
def test_every_code_an_update_action_can_answer_is_its_contract(action, monkeypatch):
    """Whatever the script raises, an update action answers only its own listed
    refusals, invalid_request (exit 2) or unavailable (exit 6)."""
    module = payload()
    assert module.UPDATE_REFUSALS == CONTRACT
    monkeypatch.setattr(module, "signal", types.SimpleNamespace(
        signal=lambda *_a: None, setitimer=lambda *_a: None, SIGTERM=15, SIGINT=2, SIGALRM=14, ITIMER_REAL=0))
    monkeypatch.setattr(module, "request", lambda *_a: {"schema_version": 1, "expected_uid": os.getuid()})
    seen = set()
    codes = _every_refusal_code()
    assert {"existing_installation", "identity_changed", "source_mismatch", "invalid_request"} <= codes
    for code in sorted(codes):
        for state, exit_code in (("refused", 4), ("unavailable", 6)):
            def execute(*_a, code=code, state=state, exit_code=exit_code):
                raise module.Refusal(code, state, exit_code=exit_code)
            monkeypatch.setattr(module, "update_execute", execute)
            buffer = types.SimpleNamespace(data=b"")
            monkeypatch.setattr(module.sys, "stdout", types.SimpleNamespace(
                buffer=types.SimpleNamespace(write=lambda raw, buffer=buffer: setattr(buffer, "data", raw))))
            returned = module.update_main(action)
            answer = json.loads(buffer.data)
            assert set(answer) == {"schema_version", "operation", "state", "code"}
            if code == "invalid_request":
                assert (returned, answer["state"], answer["code"]) == (2, "refused", "invalid_request")
            elif state == "refused" and code in CONTRACT[action]:
                assert (returned, answer["state"], answer["code"]) == (4, "refused", code)
            else:
                assert (returned, answer["state"], answer["code"]) == (6, "unavailable", "unavailable"), code
            seen.add(answer["code"])
    assert seen == CONTRACT[action] | {"invalid_request", "unavailable"}
    # The carrier document lists the same codes.
    carrier = (ROOT / "documentation/architecture/owner-ssh-carrier.md").read_text()
    section = carrier.split("## Updating a started computer\n", 1)[1].split("\n## ", 1)[0]
    for code in CONTRACT[action] | {"invalid_request", "unavailable"}:
        assert f"`{code}`" in section, code


def test_the_budget_alarm_answers_unavailable(tmp_path):
    """An update action whose budget runs out answers unavailable; the record is left to say the truth."""
    driver = tmp_path / "budget-driver.py"
    driver.write_text('''import sys,types,time
from pathlib import Path
body=Path(sys.argv[1]).read_text().split("# BEGIN EMBEDDED PYTHON\\n",1)[1].split("# END EMBEDDED PYTHON\\n",1)[0]
m=types.ModuleType("isolated_operation");exec(compile(body,sys.argv[1],"exec"),m.__dict__)
m.BUDGETS["update"]=time.monotonic()-m.STARTED+4.2
m.request=lambda *_a: {"schema_version":1,"expected_uid":1001,"expected_installed_revision":"a"*40,"target_revision":"b"*40}
def execute(*_a):
    time.sleep(120)
m.update_execute=execute
raise SystemExit(m.update_main("update"))
''')
    result = subprocess.run([sys._base_executable, "-I", "-B", str(driver), str(SCRIPT)], input=b"",
                            env=constructed_env(), capture_output=True, timeout=60)
    assert result.returncode == 6, result
    assert json.loads(result.stdout) == {"code": "unavailable", "operation": "host-update-v1",
                                         "schema_version": 1, "state": "unavailable"}


def test_update_status_reading_a_corrupt_record_answers_unavailable(tmp_path, local_install_source):
    home, family = _prepared(tmp_path, local_install_source, setup="started")
    path = family / "data/state/update.json"
    path.write_text("{not json"); path.chmod(0o600)
    before = snapshot(home)
    for action, value in (("update-status", {}), ("update", {"expected_installed_revision": "a" * 40,
                                                            "target_revision": "b" * 40}),
                          ("update-continue", {"request_id": REQUEST_ID})):
        result, answer, children = invoke_update(tmp_path, home, action, update_frame(**value))
        assert (result.returncode, answer) == (6, {"schema_version": 1, "operation": UPDATE_OPERATIONS[action],
                                                   "state": "unavailable", "code": "unavailable"}), answer
        assert _launched(children, "apply") == [] and _launched(children, "continue") == []
    assert snapshot(home) == before
