"""Fixed Darwin profile: all platform/network facts are owned fixtures."""
from pathlib import Path
import importlib.util
import json
import os
import sys
import types

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "claudlobby/darwin_runtime.py"


@pytest.fixture(autouse=True)
def restore_owned_fixture_directory_permissions(tmp_path):
    yield
    # Pytest's shared session cleanup predates sealed-component fixtures.
    # Only this test's disposable directories are made removable; keep bytes.
    for directory, directories, files in os.walk(tmp_path, followlinks=False):
        os.chmod(directory, 0o700)


def runtime():
    assert SOURCE.is_file(), "fixed Darwin runtime policy has not been implemented"
    spec = importlib.util.spec_from_file_location("darwin_runtime_fixture", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def request():
    return {"schema_version": 1, "request_id": "11111111-1111-4111-8111-111111111111",
            "expected_uid": 1001, "expected_account": "example"}


def identity(module, tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir(mode=0o700)
    selected = types.SimpleNamespace(uid=os.geteuid(), account="example", home=home, arch="arm64")
    monkeypatch.setattr(module, "_identity", lambda **kwargs: selected)
    monkeypatch.setattr(module, "_parents", lambda *args: None)
    return selected


def test_status_of_absent_profile_performs_no_writes(tmp_path, monkeypatch):
    module = runtime(); selected = identity(module, tmp_path, monkeypatch)
    result = module.status(request())
    assert result["state"] == "absent" and list(selected.home.iterdir()) == []


@pytest.mark.parametrize("change", [{"expected_uid": 0}, {"expected_uid": True},
    {"expected_account": "../other"}, {"request_id": "newest"}, {"home": "/home/user"},
    {"profile_id": "caller-selected"}, {"url": "https://example.com/tool"}])
def test_public_request_cannot_supply_paths_or_profile(change):
    module = runtime()
    with pytest.raises(module.RuntimeRefusal): module.validate_request("prepare", {**request(), **change})


def test_profile_admission_never_uses_path_or_downloads_when_absent(tmp_path, monkeypatch):
    module = runtime(); selected = identity(module, tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "foreign"))
    with pytest.raises(module.RuntimeRefusal) as error:
        module.admit_profile(expected_uid=1001, expected_account="example")
    assert error.value.code == "runtime_absent" and list(selected.home.iterdir()) == []


def test_embedded_entry_policy_is_byte_identical_to_canonical_module():
    module = runtime()
    path = SOURCE.parent / "preinstall/darwin-runtime-v1.sh"
    assert path.is_file(), "fixed pre-Python runtime entry has not been implemented"
    embedded = path.read_text().split("# BEGIN CANONICAL DARWIN RUNTIME PYTHON\n", 1)[1].split("# END CANONICAL DARWIN RUNTIME PYTHON\n", 1)[0]
    assert embedded == SOURCE.read_text()


def fixture_profile(module, tmp_path, monkeypatch):
    import hashlib
    import io
    import tarfile
    selected = identity(module, tmp_path, monkeypatch)
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    digest = lambda raw: hashlib.sha256(raw).hexdigest()
    sources = {
        "python": [("python/bin/python3.12", b"fixture-python-not-executable-code", 0o775, None),
                   ("python/lib/fixture.py", b"answer = 42\n", 0o664, None),
                   ("python/lib/python3.12/site-packages/pip/_vendor/certifi/cacert.pem", b"fixture-ca", 0o664, None)],
        "git": [("bin/git", b"fixture-git", 0o755, None),
                ("libexec/git-core/git", b"fixture-git", 0o755, None),
                ("libexec/git-core/git-status", b"", 0o755, "git"),
                ("libexec/git-core/git-remote-http", b"fixture-http", 0o755, None),
                ("libexec/git-core/git-remote-https", b"", 0o755, "git-remote-http"),
                ("libexec/git-core/NOTICE", b"publisher-notice", 0o644, None),
                ("libexec/git-core/git-lfs", b"optional-must-not-install", 0o755, None)],
        "tmux": [("tmux", b"fixture-tmux", 0o755, None)]}
    artifacts = {}; archives = {}
    for kind, entries in sources.items():
        archive_path = tmp_path / (kind + ".tar.gz")
        inventory = []; tree = {}
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, raw, mode, link in entries:
                member = tarfile.TarInfo(name); member.mode = mode
                member.type = tarfile.SYMTYPE if link else tarfile.REGTYPE
                member.linkname = link or ""; member.size = 0 if link else len(raw)
                archive.addfile(member, None if link else io.BytesIO(raw))
                inventory.append({"name": name, "type": "symlink" if link else "file", "mode": mode,
                                  "bytes": member.size, "link": link or ""})
                relative = name[7:] if kind == "python" else "bin/tmux" if kind == "tmux" else name
                if kind == "git" and name.endswith("git-lfs"):
                    continue
                for parent in Path(relative).parents:
                    if str(parent) != ".": tree[str(parent)] = {"name": str(parent), "type": "directory", "mode": 0o555}
                tree[relative] = {"name": relative, "type": "symlink", "link": link} if link else {
                    "name": relative, "type": "file", "mode": 0o555 if mode & 0o111 else 0o444,
                    "sha256": digest(raw), "bytes": len(raw)}
        raw = archive_path.read_bytes()
        artifacts[kind] = {"url": "https://example.invalid/" + kind, "sha256": digest(raw), "bytes": len(raw),
                           "members": len(inventory), "unpacked_bytes": sum(row["bytes"] for row in inventory),
                           "members_sha256": digest(canonical(sorted(inventory, key=lambda row: row["name"]))),
                           "tree_sha256": digest(canonical(sorted(tree.values(), key=lambda row: row["name"])))}
        archives[kind] = archive_path
    raw = b"fixture-jq"; jq = tmp_path / "jq.bin"; jq.write_bytes(raw); archives["jq"] = jq
    jq_tree = [{"name": "bin", "type": "directory", "mode": 0o555},
               {"name": "bin/jq", "type": "file", "mode": 0o555, "sha256": digest(raw), "bytes": len(raw)}]
    artifacts["jq"] = {"url": "https://example.invalid/jq", "sha256": digest(raw), "bytes": len(raw),
                        "members": 0, "unpacked_bytes": 0, "members_sha256": digest(b"[]"), "tree_sha256": digest(canonical(jq_tree))}
    monkeypatch.setattr(module, "_DESCRIPTOR", {"artifacts": artifacts, "python_executable_sha256": digest(sources["python"][0][1]), "ca_sha256": digest(b"fixture-ca")})
    base = module._base(selected); base.mkdir(parents=True, mode=0o700)
    stage = base / ("stage-" + request()["request_id"]); stage.mkdir(mode=0o700)
    work = stage / "profile"; work.mkdir(mode=0o700)
    archive = stage / "python.tar.gz"; archive.write_bytes(archives["python"].read_bytes()); archive.chmod(0o600)
    module._extract(archive, work / "python", "python", artifacts["python"], selected.uid)
    info = stage.stat(); archive_info = archive.stat()
    attempt = {"schema_version": 1, "binding": {"uid": selected.uid, "account": "example", "home": str(selected.home),
                "arch": "arm64", "profile_id": module.PROFILE_ID, "policy_sha256": module._policy()},
               "request_id": request()["request_id"], "stage_device": info.st_dev, "stage_inode": info.st_ino,
               "phase": "bootstrap", "files": {"python.tar.gz": [archive_info.st_dev, archive_info.st_ino]}}
    module._save(base / "attempt.json", attempt, selected.uid, new=True)
    fetched = []
    def fetch(kind, stage, attempt, base, identity, deadline):
        fetched.append(kind)
        path = stage / (kind + (".bin" if kind == "jq" else ".tar.gz")); path.write_bytes(archives[kind].read_bytes()); path.chmod(0o600)
        info = path.stat(); attempt["files"][path.name] = [info.st_dev, info.st_ino]
        module._save(base / "attempt.json", attempt, identity.uid)
        return path
    monkeypatch.setattr(module, "_download", fetch)
    def publish(source, destination):
        assert not destination.exists() and source.stat().st_mode & 0o777 == 0o700
        source.rename(destination)
    monkeypatch.setattr(module, "_publish", publish)
    return selected, stage, artifacts, fetched


def test_preparation_uses_only_fixed_owned_tools_and_full_tree_admission(tmp_path, monkeypatch):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    result = module.prepare(request(), bootstrap=True)
    assert result["state"] == "ready" and fetched == ["git", "tmux", "jq"]
    profile = module.admit_profile(expected_uid=1001, expected_account="example")
    assert profile.root == selected.home / ".local/share/claudlobby-browser/runtime/profile"
    assert profile.python.read_bytes() == b"fixture-python-not-executable-code"
    assert (profile.root / "python/lib/fixture.py").stat().st_mode & 0o777 == 0o444
    assert profile.python.stat().st_mode & 0o777 == 0o555
    assert not (profile.git_exec_path / "git-lfs").exists()
    assert (profile.git_exec_path / "NOTICE").read_bytes() == b"publisher-notice"
    assert set(path.name for path in profile.bin.iterdir()) == {"python3", "git", "tmux", "jq"}
    env = module.environment(profile)
    assert env["PATH"].split(":")[0] == str(profile.bin) and env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_ASKPASS"] == "/usr/bin/false" and "claude" not in {p.name for p in profile.bin.iterdir()}
    assert module.git_argv(profile, "status") == [str(profile.git), "status"]
    before = (profile.root / "receipt.json").read_bytes()
    assert module.prepare({**request(), "request_id": "22222222-2222-4222-8222-222222222222"})["state"] == "ready"
    assert fetched == ["git", "tmux", "jq"] and (profile.root / "receipt.json").read_bytes() == before


@pytest.mark.parametrize("change", ["stdlib", "receipt", "alias", "foreign", "mode"])
def test_changed_tool_profile_is_never_adopted_or_repaired(tmp_path, monkeypatch, change):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    module.prepare(request(), bootstrap=True); profile = module.admit_profile()
    if change == "stdlib":
        path = profile.root / "python/lib/fixture.py"; path.chmod(0o644); path.write_bytes(b"foreign-code"); path.chmod(0o444)
    elif change == "receipt":
        value = json.loads((profile.root / "receipt.json").read_text()); value["binding"]["policy_sha256"] = "b" * 64
        path = profile.root / "receipt.json"; path.chmod(0o644); path.write_text(json.dumps(value)); path.chmod(0o444)
    elif change == "alias":
        profile.bin.chmod(0o755); (profile.bin / "git").unlink(); (profile.bin / "git").symlink_to("/usr/bin/git"); profile.bin.chmod(0o555)
    elif change == "foreign":
        profile.root.chmod(0o700); (profile.root / "unknown").write_bytes(b"retain"); profile.root.chmod(0o700)
    else: profile.python.chmod(0o775)
    with pytest.raises(module.RuntimeRefusal): module.admit_profile()
    with pytest.raises(module.RuntimeRefusal): module.prepare(request())
    assert fetched == ["git", "tmux", "jq"]


def test_unknown_attempt_is_read_only_and_cannot_be_replayed(tmp_path, monkeypatch):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    before = {p.relative_to(stage).as_posix(): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
    assert module.status(request())["state"] == "unknown"
    with pytest.raises(module.RuntimeRefusal): module.prepare(request())
    assert fetched == []
    assert {p.relative_to(stage).as_posix(): p.read_bytes() for p in stage.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("change", ["unknown_name", "stage_inode", "registered_inode", "symlink"])
def test_discard_preserves_unknown_or_replaced_stage_content(tmp_path, monkeypatch, change):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    if change == "unknown_name": (stage / "personal.txt").write_text("retain")
    elif change == "stage_inode":
        value = module._load(module._base(selected) / "attempt.json", selected.uid); value["stage_inode"] += 1
        module._save(module._base(selected) / "attempt.json", value, selected.uid)
    elif change == "registered_inode":
        path = stage / "python.tar.gz"; raw = path.read_bytes(); path.rename(tmp_path / "retained.tar.gz"); path.write_bytes(raw); path.chmod(0o600)
    else:
        (stage / "python.tar.gz").unlink(); (stage / "python.tar.gz").symlink_to(tmp_path / "foreign")
    with pytest.raises((module.RuntimeRefusal, OSError)): module.discard(request())
    assert stage.exists()


def test_explicit_discard_requires_original_request_and_keeps_stable_lock(tmp_path, monkeypatch):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    with pytest.raises(module.RuntimeRefusal): module.discard({**request(), "request_id": "22222222-2222-4222-8222-222222222222"})
    assert stage.exists()
    result = module.discard(request())
    assert result["state"] == "discarded" and not stage.exists()
    assert (module._base(selected) / "transition.lock").exists()
    assert module.status(request())["state"] == "absent"


@pytest.mark.parametrize("system,machine,version", [("Linux", "arm64", "15.0"), ("Darwin", "x86_64", "15.0"), ("Darwin", "arm64", "14.9")])
def test_unsupported_platform_and_rosetta_refuse_before_account_effects(monkeypatch, system, machine, version):
    module = runtime()
    monkeypatch.setattr(module, "platform", types.SimpleNamespace(system=lambda: system, machine=lambda: machine, mac_ver=lambda: (version, (), "")))
    monkeypatch.setattr(module, "pwd", types.SimpleNamespace(getpwuid=lambda uid: pytest.fail("unsupported platform reached account operation")))
    with pytest.raises(module.RuntimeRefusal): module.admit_profile()


@pytest.mark.parametrize("foreign", [False, True])
def test_owned_partial_extraction_discard_is_finite_and_preserves_foreign_bytes(tmp_path, monkeypatch, foreign):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    base = module._base(selected); attempt = module._load(base / "attempt.json", selected.uid)
    module._download("git", stage, attempt, base, selected, 1000)
    component = stage / "profile/git"; (component / "bin").mkdir(parents=True, mode=0o755); component.chmod(0o755)
    path = component / "bin/git"; path.write_bytes(b"foreign-personal-data" if foreign else b"fixture"); path.chmod(0o755)
    if foreign:
        with pytest.raises(module.RuntimeRefusal): module.discard(request())
        assert path.read_bytes() == b"foreign-personal-data"
    else:
        assert module.discard(request())["state"] == "discarded" and not stage.exists()


def test_inherited_open_description_stays_locked_until_last_holder_closes(tmp_path, monkeypatch):
    import fcntl
    module = runtime(); selected = identity(module, tmp_path, monkeypatch)
    base = module._base(selected); base.mkdir(parents=True, mode=0o700)
    path = base / "transition.lock"; original = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(original, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(module, "_INHERITED_LOCK_FD", original)
    duplicate = module._lock(base, selected.uid)
    separate = os.open(path, os.O_RDWR)
    try:
        os.close(original)
        with pytest.raises(BlockingIOError): fcntl.flock(separate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(duplicate); duplicate = None
        fcntl.flock(separate, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if duplicate is not None: os.close(duplicate)
        os.close(separate)


def test_shell_bootstrap_constants_match_the_canonical_policy():
    import re
    module = runtime()
    shell = (SOURCE.parent / "preinstall/darwin-runtime-v1.sh").read_text()
    expected = {"DARWIN_RUNTIME_PROFILE": module.PROFILE_ID,
                "DARWIN_RUNTIME_PYTHON_SHA": module._DESCRIPTOR["python_executable_sha256"],
                "DARWIN_RUNTIME_POLICY_SHA": module._policy(),
                "DARWIN_RUNTIME_PYTHON_URL": module._DESCRIPTOR["artifacts"]["python"]["url"],
                "DARWIN_RUNTIME_PYTHON_BYTES": str(module._DESCRIPTOR["artifacts"]["python"]["bytes"]),
                "DARWIN_RUNTIME_ARCHIVE_SHA": module._DESCRIPTOR["artifacts"]["python"]["sha256"]}
    for key, value in expected.items():
        assert re.findall(r"^" + key + r"=(.*)$", shell, re.M) == [value]


def test_environment_prevents_import_bytecode_from_changing_the_profile(tmp_path, monkeypatch):
    import subprocess
    from tests.conftest import constructed_env
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    module.prepare(request(), bootstrap=True); profile = module.admit_profile()
    # Run the trusted test interpreter, never the inert vendor fixture.
    result = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import fixture",
                             str(profile.root / "python/lib")], env=constructed_env(**module.environment(profile)),
                            capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not list(profile.root.rglob("*.pyc"))
    module.admit_profile()


def test_selected_git_helpers_include_real_builtin_target(tmp_path, monkeypatch):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    module.prepare(request(), bootstrap=True); profile = module.admit_profile()
    assert (profile.git_exec_path / "git").is_file()
    assert (profile.git_exec_path / "git-status").resolve() == profile.git_exec_path / "git"


def test_selected_link_cannot_resolve_only_in_unselected_archive(tmp_path, monkeypatch):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    original = module._selection
    monkeypatch.setattr(module, "_selection", lambda member, kind: None if member.name == "libexec/git-core/git" else original(member, kind))
    with pytest.raises(module.RuntimeRefusal) as error:
        module.prepare(request(), bootstrap=True)
    assert error.value.code == "selected_archive_link_missing"


def test_isolated_python_cannot_write_bytecode_into_sealed_profile(tmp_path, monkeypatch):
    import subprocess
    from tests.conftest import constructed_env
    assert os.geteuid() != 0, "this acceptance case must exercise ordinary-user permissions"
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    module.prepare(request(), bootstrap=True); profile = module.admit_profile()
    source = profile.root / "python/lib/fixture.py"
    with pytest.raises(PermissionError): source.write_bytes(b"unintended write")
    # -I deliberately ignores the environment's PYTHONDONTWRITEBYTECODE.
    # The trusted test interpreter imports only synthetic source from the prefix.
    result = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import fixture; assert not sys.dont_write_bytecode",
                             str(source.parent)], env=constructed_env(**module.environment(profile)), capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not list(profile.root.rglob("*.pyc"))
    module.admit_profile()


@pytest.mark.parametrize("foreign", [False, True])
def test_discard_only_unseals_fully_admitted_owned_stage(tmp_path, monkeypatch, foreign):
    module = runtime(); selected, stage, artifacts, fetched = fixture_profile(module, tmp_path, monkeypatch)
    def interrupted_publish(source, destination):
        raise OSError("fixture interruption after sealing, before publication")
    monkeypatch.setattr(module, "_publish", interrupted_publish)
    with pytest.raises(OSError): module.prepare(request(), bootstrap=True)
    work = stage / "profile"
    assert work.stat().st_mode & 0o777 == 0o700
    if foreign:
        work.chmod(0o700); (work / "personal.txt").write_bytes(b"retain"); work.chmod(0o700)
        with pytest.raises(module.RuntimeRefusal): module.discard(request())
        assert work.stat().st_mode & 0o777 == 0o700 and (work / "personal.txt").read_bytes() == b"retain"
    else:
        assert module.discard(request())["state"] == "discarded" and not stage.exists()
