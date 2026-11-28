"""Start again: the installer's removal of a never-started preparation.

It deletes files on a person's computer, so most of these tests are refusals: each one
checks that the whole home folder, a socket and every sentinel outside the family folder
come out byte-identical. The family trees are built by hand from the receipt's own binding
so many cases stay cheap; one test runs a real preparation, the guide's records, Start
again at a later revision and a real preparation after it.
"""
import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from tests.conftest import constructed_env
from tests.test_host_install import (  # noqa: F401  (local_install_source is a fixture)
    REVISION, SCRIPT, _commit, _darwin_policy_fixture, add_setup_state, frame, invoke_fixture,
    local_install_source, payload, profile, snapshot)

OLD, NEW = "b" * 40, "c" * 40
FAMILY = ".local/share/claudlobby-browser"
VERBS = ("start-again-status", "start-again")


class Crash(BaseException):
    """A process that dies: nothing after the raising call runs except what a kill leaves."""


@pytest.fixture(autouse=True)
def private_umask():
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def put(path, content=b"x", mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return path


def bound_socket(path):
    # A socket path is limited to about a hundred bytes; bind by a short relative name.
    here = os.getcwd()
    os.chdir(path.parent)
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path.name)
        listener.close()
    finally:
        os.chdir(here)
    assert stat.S_ISSOCK(path.lstat().st_mode)


def prepared(module, home, python, *, ref=OLD, complete=True, source=True):
    family = home / FAMILY
    for folder in (home / ".local", home / ".local/share", family):
        folder.mkdir(exist_ok=True)
        folder.chmod(0o700)
    install, data = family / "install", family / "data"
    for folder in (install / "bin", install / "cache/bootstrap-wheels", data / "state", family / "staging"):
        folder.mkdir(parents=True, exist_ok=True)
    put(install / "preparation.lock", b"")
    record = {**module.binding(install, data, ref, python), "revision": ref if complete else None,
              "lock_sha256": "d" * 64 if complete else None, "environment_owned": complete, "complete": complete}
    put(install / "preparation.json", json.dumps(record, sort_keys=True).encode())
    put(install / "installation.json", json.dumps({"schema": 1, "data_root": str(data)}, sort_keys=True).encode())
    if source:
        put(install / "src/.git/HEAD", (ref + "\n").encode(), 0o644)
        put(install / "src/claudlobby/installation.py", b"source", 0o644)
        (install / "env/bin").mkdir(parents=True)
        (install / "env/bin/python").symlink_to(python)
        put(install / "env/pyvenv.cfg", b"home = x\n", 0o644)
    put(install / "bin/claudlobby", b"#!x\n", 0o755)
    put(install / "bin/claudlobby-update", b"#!x\n", 0o755)
    put(install / "cache/bootstrap-requirements.lock")
    put(install / "cache/bootstrap-wheels/example-1.0-py3-none-any.whl")
    put(data / "state/runtime-use.lock", b"")
    return family


# Every name the guide's first steps write beside a preparation, and each crash leftover.
GUIDE_FILES = ("data/.env", "data/state/host-uid", "data/state/update.lock", "data/state/setup-preview.lock",
               "data/state/provider-signin.lock", "data/state/owner/owner.sock.lock",
               "data/state/plane/owner-access.db", "data/state/plane/owner-access.db-wal",
               "data/state/plane/owner-access.db-shm", "data/state/plane/owner-access.db-journal",
               "host-policy/system.yaml", "provider-bin/claude")
LEFTOVER_FILES = ("install/.update-x1", "install/.installation-x1", "install/bin/.launcher-x1",
                  "install/cache/.update-x1", "data/state/.host-uid.12.abc.tmp", "data/state/plane/.owner-access-x1",
                  "host-policy/.owner-policy-x1", "provider-bin/.claude-stage-x1")


def guide(family, *, leftovers=True):
    for name in GUIDE_FILES + (LEFTOVER_FILES if leftovers else ()):
        put(family / name, b"FIXTURE")
    for folder in ("data/state/owner", "data/state/plane", "data/state/provider-signin", "host-policy",
                   "provider-bin", "provider-profile"):
        (family / folder).mkdir(parents=True, exist_ok=True)
        (family / folder).chmod(0o700)
    (family / "provider-bin/claude").chmod(0o500)
    bound_socket(family / "data/state/owner/owner.sock")
    attempt = family / "data/state/provider-signin" / ("claude-signin-" + str(uuid.uuid4()))
    attempt.mkdir(mode=0o700)
    put(attempt / "claude", b"binary", 0o500)
    # Claude's own profile folder and the staging folder are owned trees: any names.
    put(family / "provider-profile/.claude.json", b"{}")
    put(family / "provider-profile/projects/deep/notes.txt", b"n")
    put(family / "staging/prepare-x1/probe/pyvenv.cfg", b"p")
    (family / "staging/prepare-x1/probe/python").symlink_to("/usr/bin/python3")
    put(family / "staging/app-temporary", b"in flight")
    # Content in lock files that a lock opener with a size rule would refuse.
    put(family / "data/state/owner/owner.sock.lock", json.dumps({"version": 1, "parent": [1, 2], "socket": [3, 4]}).encode())
    put(family / "data/state/provider-signin.lock", (str(uuid.uuid4()) + "\n").encode())


def outside_world(home, tmp_path):
    put(home / ".claude/settings.json", b"PRIVATE-SENTINEL")
    put(home / "notes.txt", b"mine")
    (home / "claudfather-projects/first-team").mkdir(parents=True)
    (home / ".config/systemd/user").mkdir(parents=True)
    put(home / ".config/systemd/user/notes.txt", b"not a unit")
    put(home / "Library/LaunchAgents/com.example.other.plist", b"someone else's")
    outer = tmp_path / "outside"
    put(outer / "sentinel.txt", b"OUTSIDE-SENTINEL")
    return outer


def beyond(home):
    return {key: value for key, value in snapshot(home).items() if not key.startswith(FAMILY)}


def world(tmp_path, monkeypatch, *, system="Linux", guided=True, **kwargs):
    module = payload()
    if system == "Linux":
        home, python = profile(module, tmp_path, monkeypatch)
    else:
        home = tmp_path / "account"
        home.mkdir(mode=0o700)
        _profile, _policy, _environment = _darwin_policy_fixture(module, home, monkeypatch)
        python = Path(sys.executable).resolve()
        monkeypatch.setattr(module, "independent_python", lambda: python)
        monkeypatch.setenv("PATH", "/nonexistent")
    family = prepared(module, home, python, **kwargs)
    if guided:
        guide(family)
    outer = outside_world(home, tmp_path)
    return module, home, family, outer


def call(module, verb, revision=NEW, uid=None):
    value = {"schema_version": 1, "source_revision": revision, "expected_uid": os.geteuid() if uid is None else uid}
    return module.start_again_execute(verb, value, time.monotonic() + 60)


def refused(module, home, verb, code, *, state="refused", revision=NEW):
    before = snapshot(home)
    with pytest.raises(module.Refusal) as error:
        call(module, verb, revision)
    assert (error.value.code, error.value.state) == (code, state), (verb, error.value.code)
    assert snapshot(home) == before


def refuses_both(module, home, code, **kwargs):
    for verb in VERBS:
        refused(module, home, verb, code, **kwargs)


def removed_cleanly(module, home, family, outer, *, kept=()):
    before = beyond(home)
    sentinel = (outer / "sentinel.txt").read_bytes()
    assert call(module, "start-again-status") == {"state": "eligible", "next": "start_again"}
    assert beyond(home) == before
    assert call(module, "start-again") == {"state": "removed", "next": "prepare"}
    assert sorted(path.name for path in family.iterdir()) == sorted(kept)
    assert beyond(home) == before and (outer / "sentinel.txt").read_bytes() == sentinel
    assert call(module, "start-again") == {"state": "absent", "next": "prepare"}


def test_a_full_never_started_computer_is_removed_and_everything_else_is_untouched(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    assert (home / "claudfather-projects/first-team").is_dir()
    removed_cleanly(module, home, family, outer)
    assert (home / "claudfather-projects/first-team").is_dir() and not list((home / "claudfather-projects/first-team").iterdir())
    # The installer's own status then reads a computer where nothing was prepared.
    value = {"schema_version": 1, "source_revision": NEW, "expected_uid": os.geteuid()}
    assert module.execute("status", value, time.monotonic() + 15)[0] == "absent"


def test_a_preparation_with_no_guide_records_is_removed(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch, guided=False)
    removed_cleanly(module, home, family, outer)


def test_the_mac_keeps_its_runtime_and_the_same_checks_apply(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch, system="Darwin")
    put(family / "runtime/profile/python/bin/python3.12", b"managed python", 0o555)
    put(family / "runtime/profile/receipt.json", b"{}")
    keep = snapshot(family / "runtime")
    removed_cleanly(module, home, family, outer, kept=("runtime",))
    assert snapshot(family / "runtime") == keep
    # A Mac with only the runtime has nothing to start again.
    assert call(module, "start-again-status") == {"state": "absent", "next": "prepare"}


def test_the_mac_refuses_a_runtime_that_is_not_the_admitted_profile(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch, system="Darwin")
    put(family / "runtime/profile/receipt.json", b"{}")
    policy_root = home / "elsewhere"
    monkeypatch.setattr(module, "admitted_darwin_profile", lambda uid=None: type("P", (), {"root": policy_root, "home": home})())
    refuses_both(module, home, "account_changed")


@pytest.mark.parametrize("name", ["Library/LaunchAgents/com.example.other.plist", ".config/systemd/user/notes.txt"])
def test_a_mac_folder_for_units_is_only_read(tmp_path, monkeypatch, name):
    module, home, family, outer = world(tmp_path, monkeypatch, system="Darwin")
    content = (home / name).read_bytes()
    removed_cleanly(module, home, family, outer)
    assert (home / name).read_bytes() == content


def test_a_wrong_account_is_refused_first(tmp_path, monkeypatch):
    module = payload()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    for verb in VERBS:
        with pytest.raises(module.Refusal) as error:
            call(module, verb, uid=os.geteuid() + 1)
        assert (error.value.code, error.value.state) == ("account_changed", "refused")


def test_no_family_folder_and_an_empty_one_are_absent(tmp_path, monkeypatch):
    module = payload()
    home, _python = profile(module, tmp_path, monkeypatch)
    assert call(module, "start-again-status") == {"state": "absent", "next": "prepare"}
    assert not (home / ".local").exists()
    (home / ".local/share/claudlobby-browser").mkdir(parents=True)
    (home / ".local/share/claudlobby-browser").chmod(0o700)
    assert call(module, "start-again") == {"state": "absent", "next": "prepare"}


# What a started team leaves; the names are written out here, not read from the installer.
STARTED = {
    "data/runtime": "dir", "data/local": "dir",
    "data/state/config.json": "file", "data/state/selected-release.json": "file",
    "data/state/migrations": "dir", "data/state/activations": "dir", "data/state/update.json": "file",
    "data/state/update-work": "dir", "data/state/releases": "dir", "data/state/config-plans": "dir",
    "data/state/restored-backup.json": "file", "data/state/restored-backup": "dir",
    "data/state/restored-backup-1": "dir",
    "data/state/setup-finish-3f1c2b1e-5d3a-4c2e-9d11-2c8a8c7a9e10.lock": "file",
    "data/state/plane/plane.db": "file", "data/state/plane/plane.db-wal": "file",
    "install/bin/com.claudlobby.update.0123456789abcdef.service": "file",
    "install/bin/com.claudlobby.update.0123456789abcdef.plist": "file",
}


def make(path, kind):
    if kind == "dir":
        path.mkdir(parents=True)
        path.chmod(0o700)
    else:
        put(path, b"FIXTURE")


@pytest.mark.parametrize("name", sorted(STARTED))
def test_each_sign_a_team_was_started_refuses_with_nothing_removed(tmp_path, monkeypatch, name):
    module, home, family, outer = world(tmp_path, monkeypatch)
    make(family / name, STARTED[name])
    refuses_both(module, home, "started")


@pytest.mark.parametrize("unit", ["claudlobby-plane-owner.service", "claudlobby-x.timer", "other.service",
                                  "x.socket", "claudlobby.target.wants"])
def test_any_linux_unit_means_started(tmp_path, monkeypatch, unit):
    module, home, family, outer = world(tmp_path, monkeypatch)
    make(home / ".config/systemd/user" / unit, "dir" if unit.endswith(".wants") else "file")
    refuses_both(module, home, "started")


@pytest.mark.parametrize("agent", ["com.claudlobby.plane-owner.plist", "claudlobby-x.plist"])
def test_a_launch_agent_of_ours_means_started_on_a_mac(tmp_path, monkeypatch, agent):
    module, home, family, outer = world(tmp_path, monkeypatch, system="Darwin")
    put(home / "Library/LaunchAgents" / agent, b"x")
    refuses_both(module, home, "started")


def test_started_is_answered_before_an_unknown_name_is(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    put(family / "data/state/stranger", b"?")
    put(family / "data/state/config.json", b"{}")
    refuses_both(module, home, "started")


UNKNOWN = ["stranger", "install/stranger", "install/bin/stranger", "install/cache/stranger",
           "install/cache/bootstrap-wheels/stranger.txt", "data/stranger", "data/state/stranger",
           "data/state/owner/stranger", "data/state/plane/stranger", "data/state/provider-signin/stranger",
           "host-policy/stranger", "provider-bin/stranger", "install/.update", "data/state/.host-uid.x",
           "data/state/provider-signin/claude-signin-not-an-id"]


@pytest.mark.parametrize("name", UNKNOWN)
def test_an_unknown_name_is_refused_everywhere_it_can_appear(tmp_path, monkeypatch, name):
    module, home, family, outer = world(tmp_path, monkeypatch)
    put(family / name, b"?")
    refuses_both(module, home, "unknown_content")


def test_a_runtime_folder_is_unknown_on_linux(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "runtime").mkdir()
    (family / "runtime").chmod(0o700)
    refuses_both(module, home, "unknown_content")


def test_an_unknown_name_inside_a_signin_attempt_is_refused(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    attempt = next((family / "data/state/provider-signin").iterdir())
    put(attempt / "stranger", b"?")
    refuses_both(module, home, "unknown_content")


@pytest.mark.parametrize("name", ["install", "data", "data/state", "host-policy", "provider-bin",
                                  "provider-profile", "staging", "data/state/owner"])
def test_a_link_in_place_of_a_folder_is_refused_and_never_followed(tmp_path, monkeypatch, name):
    module, home, family, outer = world(tmp_path, monkeypatch)
    target = family / name
    target.rename(tmp_path / ("held-" + name.replace("/", "-")))
    target.symlink_to(outer)
    refuses_both(module, home, "unknown_content" if name != "install" else "identity_changed")
    assert (outer / "sentinel.txt").read_bytes() == b"OUTSIDE-SENTINEL"


def test_a_link_in_place_of_a_file_is_refused(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "host-policy/system.yaml").unlink()
    (family / "host-policy/system.yaml").symlink_to(outer / "sentinel.txt")
    refuses_both(module, home, "unknown_content")


def test_links_inside_an_owned_tree_are_removed_and_never_followed(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "provider-profile/out-folder").symlink_to(outer)
    (family / "provider-profile/out-file").symlink_to(outer / "sentinel.txt")
    (family / "install/src/out-folder").symlink_to(outer)
    (family / "staging/dangling").symlink_to(tmp_path / "does-not-exist")
    removed_cleanly(module, home, family, outer)
    assert (outer / "sentinel.txt").read_bytes() == b"OUTSIDE-SENTINEL"


@pytest.mark.parametrize("name, mode", [("data", 0o755), ("data/state", 0o750), ("install", 0o755),
                                        ("host-policy", 0o777), ("data/state/plane", 0o755),
                                        ("provider-profile", 0o755), ("staging", 0o755),
                                        ("provider-profile/projects", 0o500)])
def test_a_folder_with_the_wrong_mode_is_refused(tmp_path, monkeypatch, name, mode):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / name).chmod(mode)
    try:
        refuses_both(module, home, "unknown_content")
    finally:
        (family / name).chmod(0o700)


def test_the_family_folder_with_the_wrong_mode_is_refused(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    family.chmod(0o755)
    refuses_both(module, home, "unknown_content")


@pytest.mark.parametrize("name, mode", [("data/state/runtime-use.lock", 0o644), ("data/state/update.lock", 0o666),
                                        ("data/state/owner/owner.sock.lock", 0o640),
                                        ("install/preparation.lock", 0o644)])
def test_a_lock_file_with_the_wrong_mode_is_refused(tmp_path, monkeypatch, name, mode):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / name).chmod(mode)
    refuses_both(module, home, "unknown_content" if "preparation" not in name else "unknown_content")


def test_a_lock_file_with_another_name_for_the_same_inode_is_refused(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    os.link(family / "data/state/update.lock", tmp_path / "second-name")
    refuses_both(module, home, "unknown_content")


def test_a_file_this_account_does_not_own_is_refused(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    real = os.geteuid()
    seen = {"tree": False}
    original = module._owned_tree
    def flipping(root, device):
        seen["tree"] = True
        return original(root, device)
    monkeypatch.setattr(module, "_owned_tree", flipping)
    monkeypatch.setattr(os, "geteuid", lambda: real + 1 if seen["tree"] else real)
    before = snapshot(home)
    with pytest.raises(module.Refusal) as error:
        call(module, "start-again", uid=real)
    assert error.value.code == "unknown_content" and seen["tree"]
    assert snapshot(home) == before


def test_a_node_this_account_does_not_own_in_a_named_folder_is_refused(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    real = os.geteuid()
    seen = {"layout": False}
    original = module._layout
    def flipping(path, name, device):
        if name == "host-policy":
            seen["layout"] = True
        return original(path, name, device)
    monkeypatch.setattr(module, "_layout", flipping)
    monkeypatch.setattr(os, "geteuid", lambda: real + 1 if seen["layout"] else real)
    before = snapshot(home)
    with pytest.raises(module.Refusal) as error:
        call(module, "start-again", uid=real)
    assert error.value.code == "unknown_content" and seen["layout"]
    assert snapshot(home) == before


def other_device(monkeypatch, inode):
    """Report one node, found by inode, as on another device: what a mount inside the tree looks like."""
    def altered(info):
        if info.st_ino != inode:
            return info
        fields = list(info)
        fields[2] = info.st_dev + 1
        return os.stat_result(fields)
    for name in ("stat", "lstat", "fstat"):
        real = getattr(os, name)
        monkeypatch.setattr(os, name, lambda *a, _real=real, **k: altered(_real(*a, **k)))
    scandir = os.scandir
    class Entry:
        def __init__(self, entry): self.entry = entry
        def __getattr__(self, name): return getattr(self.entry, name)
        def stat(self, **kwargs): return altered(self.entry.stat(**kwargs))
    class Scan:
        def __init__(self, path): self.scan = scandir(path)
        def __enter__(self): self.scan.__enter__(); return iter([Entry(entry) for entry in list(self.scan)])
        def __exit__(self, *exc): return self.scan.__exit__(*exc)
    monkeypatch.setattr(os, "scandir", lambda path=".": Scan(path) if not isinstance(path, int) else scandir(path))


@pytest.mark.parametrize("name", ["install/src/claudlobby", "provider-profile/projects/deep", "data/state/plane",
                                  "staging/prepare-x1"])
def test_a_node_on_another_device_is_refused_before_anything_moves(tmp_path, monkeypatch, name):
    module, home, family, outer = world(tmp_path, monkeypatch)
    other_device(monkeypatch, (family / name).lstat().st_ino)
    refuses_both(module, home, "unknown_content")


def test_the_delete_never_descends_into_another_device(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    root = tmp_path / "purge"
    for name in ("a/one", "mount/kept-one", "mount/deeper/kept-two", "z/two"):
        put(root / name, b"x")
    descriptor = os.open(tmp_path, os.O_RDONLY)
    try:
        device = root.lstat().st_dev
        other_device(monkeypatch, (root / "mount").lstat().st_ino)
        with pytest.raises(module.Refusal) as error:
            module._purge(descriptor, "purge", device)
    finally:
        os.close(descriptor)
    assert error.value.code == "unknown_content"
    assert (root / "mount/kept-one").exists() and (root / "mount/deeper/kept-two").exists()


def test_the_delete_removes_links_without_following_them(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    root = tmp_path / "purge"
    put(root / "file", b"x")
    (root / "folder-link").symlink_to(outer)
    (root / "file-link").symlink_to(outer / "sentinel.txt")
    descriptor = os.open(tmp_path, os.O_RDONLY)
    try:
        module._purge(descriptor, "purge", root.lstat().st_dev)
    finally:
        os.close(descriptor)
    assert not root.exists() and (outer / "sentinel.txt").read_bytes() == b"OUTSIDE-SENTINEL"


def rewrite(family, name, **changes):
    path = family / name
    value = json.loads(path.read_text())
    value.update(changes)
    path.write_text(json.dumps(value, sort_keys=True))


@pytest.mark.parametrize("field, value", [
    ("schema", 2), ("purpose", "other"), ("installation", "/elsewhere/install"), ("data_root", "/elsewhere/data"),
    ("remote", "https://example.test/x.git"), ("interpreter", "/elsewhere/python"),
    ("revision", NEW), ("complete", "yes"), ("environment_owned", 1), ("lock_sha256", "short"),
    ("ref", "not-a-commit"), ("ref", None), ("interpreter_sha256", "nothex")])
def test_a_receipt_field_that_is_not_ours_refuses_as_identity_changed(tmp_path, monkeypatch, field, value):
    module, home, family, outer = world(tmp_path, monkeypatch)
    rewrite(family, "install/preparation.json", **{field: value})
    refuses_both(module, home, "identity_changed")


def test_an_extra_or_missing_receipt_key_refuses(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    rewrite(family, "install/preparation.json", extra=1)
    refuses_both(module, home, "identity_changed")
    record = json.loads((family / "install/preparation.json").read_text())
    del record["extra"], record["lock_sha256"]
    (family / "install/preparation.json").write_text(json.dumps(record))
    refuses_both(module, home, "identity_changed")


@pytest.mark.parametrize("damage", ["missing", "link", "mode", "garbage", "hardlink"])
def test_a_receipt_that_cannot_be_read_as_ours_refuses(tmp_path, monkeypatch, damage):
    module, home, family, outer = world(tmp_path, monkeypatch)
    path = family / "install/preparation.json"
    if damage == "missing":
        path.unlink()
    elif damage == "link":
        content = path.read_bytes(); path.unlink(); path.symlink_to(put(outer / "r.json", content))
    elif damage == "mode":
        path.chmod(0o644)
    elif damage == "hardlink":
        os.link(path, tmp_path / "second-name")
    else:
        path.write_text("{not json")
    refuses_both(module, home, "identity_changed")


def test_the_receipt_at_the_requested_revision_has_nothing_to_start_again(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    for verb in VERBS:
        refused(module, home, verb, "already_current", revision=OLD)


def test_installation_json_that_names_another_data_root_refuses(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    rewrite(family, "install/installation.json", data_root="/elsewhere")
    refuses_both(module, home, "identity_changed")


@pytest.mark.parametrize("head", [NEW, "ref: refs/heads/main", "", "g" * 40])
def test_a_checkout_that_moved_off_the_receipt_commit_refuses(tmp_path, monkeypatch, head):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "install/src/.git/HEAD").write_text(head + "\n")
    refuses_both(module, home, "identity_changed")


def test_a_completed_receipt_with_no_checkout_refuses(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    import shutil
    shutil.rmtree(family / "install/src")
    refuses_both(module, home, "identity_changed")


def test_a_completed_receipt_without_the_runtime_lock_refuses(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "data/state/runtime-use.lock").unlink()
    refuses_both(module, home, "identity_changed")


@pytest.mark.parametrize("shape", ["no source", "no environment", "empty source", "bare git", "branch head",
                                   "leftover staging", "no data"])
def test_an_install_that_stopped_part_way_is_removed(tmp_path, monkeypatch, shape):
    import shutil
    module, home, family, outer = world(tmp_path, monkeypatch, complete=False, guided=False, source=shape != "no source")
    install = family / "install"
    if shape == "empty source":
        shutil.rmtree(install / "src"); (install / "src").mkdir(); (install / "src").chmod(0o700)
    if shape == "no environment" and (install / "env").exists():
        shutil.rmtree(install / "env")
    if shape == "bare git":
        shutil.rmtree(install / "src/.git"); (install / "src/.git").mkdir(); (install / "src/.git").chmod(0o700)
    if shape == "branch head":
        (install / "src/.git/HEAD").write_text("ref: refs/heads/master\n")
    if shape == "leftover staging":
        put(family / "staging/prepare-a1/probe/pyvenv.cfg", b"p")
        (family / "staging/prepare-a1/probe/python").symlink_to("/usr/bin/python3")
    if shape == "no data":
        shutil.rmtree(family / "data")
    removed_cleanly(module, home, family, outer)


def test_an_install_that_stopped_part_way_with_a_checkout_at_another_commit_refuses(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch, complete=False, guided=False)
    (family / "install/src/.git/HEAD").write_text(NEW + "\n")
    refuses_both(module, home, "identity_changed")


@pytest.mark.parametrize("name", ["data/state/owner/owner.sock.lock", "data/state/provider-signin.lock",
                                  "data/state/update.lock", "data/state/setup-preview.lock",
                                  "data/state/runtime-use.lock"])
def test_a_lock_file_that_holds_bytes_is_not_a_reason_to_refuse(tmp_path, monkeypatch, name):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / name).write_bytes(b'{"version":1,"parent":[1,2],"socket":[3,4]}')
    removed_cleanly(module, home, family, outer)


def test_every_crash_leftover_and_guide_name_is_removed(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    for name in GUIDE_FILES + LEFTOVER_FILES:
        assert (family / name).exists(), name
    removed_cleanly(module, home, family, outer)


def test_the_allow_list_and_the_guide_fixture_name_the_same_files(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    # Every literal name the installer allows appears in the fixture; the fixture's names
    # are all accepted (the full removal above); and nothing else is listed.
    literal = {(layout, name) for layout, (names, _patterns) in module.LAYOUT.items() for name in names}
    assert literal == {
        ("install", "preparation.lock"), ("install", "preparation.json"), ("install", "installation.json"),
        ("install", "src"), ("install", "env"), ("install", "bin"), ("install", "cache"),
        ("bin", "claudlobby"), ("bin", "claudlobby-update"),
        ("cache", "bootstrap-wheels"), ("cache", "bootstrap-requirements.lock"),
        ("data", ".env"), ("data", "state"),
        ("state", "runtime-use.lock"), ("state", "update.lock"), ("state", "setup-preview.lock"),
        ("state", "provider-signin.lock"), ("state", "host-uid"), ("state", "owner"), ("state", "plane"),
        ("state", "provider-signin"), ("owner", "owner.sock"), ("owner", "owner.sock.lock"),
        ("plane", "owner-access.db"), ("plane", "owner-access.db-wal"), ("plane", "owner-access.db-shm"),
        ("plane", "owner-access.db-journal"), ("attempt", "claude"),
        ("host-policy", "system.yaml"), ("provider-bin", "claude")}
    present = {(layout, name) for layout, name in literal
               if any(path.name == name for path in family.rglob(name))}
    assert present == literal
    assert set(module.COMPONENTS) == {"install", "data", "staging", "host-policy", "provider-bin", "provider-profile"}
    assert module.START_AGAIN_ORDER[-1] == "install" and set(module.START_AGAIN_ORDER) == set(module.COMPONENTS)


@pytest.mark.parametrize("state", ["a file", "a folder with a file", "a link"])
def test_a_first_team_folder_with_anything_in_it_is_refused_and_kept(tmp_path, monkeypatch, state):
    module, home, family, outer = world(tmp_path, monkeypatch)
    project = home / "claudfather-projects/first-team"
    if state == "a file":
        project.rmdir(); put(project, b"x")
    elif state == "a link":
        project.rmdir(); project.symlink_to(outer)
    else:
        put(project / "README.md", b"my own notes")
    refuses_both(module, home, "first_team_not_empty")
    assert not project.is_dir() or (project / "README.md").exists() or state != "a folder with a file"


def test_a_missing_first_team_folder_is_fine(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (home / "claudfather-projects/first-team").rmdir()
    (home / "claudfather-projects").rmdir()
    removed_cleanly(module, home, family, outer)


def hold(path, mode):
    descriptor = os.open(path, os.O_RDWR)
    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    return descriptor


@pytest.mark.parametrize("name", ["data/state/runtime-use.lock", "data/state/update.lock",
                                  "data/state/owner/owner.sock.lock", "data/state/setup-preview.lock",
                                  "data/state/provider-signin.lock"])
def test_a_held_lock_means_in_use_and_nothing_is_removed(tmp_path, monkeypatch, name):
    module, home, family, outer = world(tmp_path, monkeypatch)
    held = hold(family / name, fcntl.LOCK_EX)
    try:
        refuses_both(module, home, "in_use")
    finally:
        os.close(held)
    removed_cleanly(module, home, family, outer)


def test_a_running_launcher_holds_the_runtime_lock_shared_and_start_again_refuses(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    held = hold(family / "data/state/runtime-use.lock", fcntl.LOCK_SH)
    try:
        before = snapshot(home)
        # The status answer reads shared probes: it cannot see a shared hold, and says so
        # in the installer's header. Start again takes the lock exclusively and does see it.
        assert call(module, "start-again-status") == {"state": "eligible", "next": "start_again"}
        refused(module, home, "start-again", "in_use")
        assert snapshot(home) == before
    finally:
        os.close(held)


@pytest.mark.parametrize("mode", [fcntl.LOCK_EX, fcntl.LOCK_SH])
def test_a_held_preparation_lock_is_busy(tmp_path, monkeypatch, mode):
    module, home, family, outer = world(tmp_path, monkeypatch)
    held = hold(family / "install/preparation.lock", mode)
    try:
        if mode == fcntl.LOCK_EX:
            refuses_both(module, home, "preparation_in_progress", state="busy")
        else:
            # A status that is reading holds it shared too; only the removal needs it alone.
            assert call(module, "start-again-status") == {"state": "eligible", "next": "start_again"}
            refused(module, home, "start-again", "preparation_in_progress", state="busy")
    finally:
        os.close(held)


def test_the_status_verb_takes_no_lock_exclusively_and_writes_nothing(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "data/state/update.lock").unlink()
    operations = []
    real = fcntl.flock
    monkeypatch.setattr(module.fcntl, "flock", lambda fd, op: operations.append(op & ~fcntl.LOCK_NB) or real(fd, op))
    before = snapshot(home)
    assert call(module, "start-again-status")["state"] == "eligible"
    assert snapshot(home) == before and not (family / "data/state/update.lock").exists()
    assert operations and set(operations) == {fcntl.LOCK_SH}


def test_the_removal_takes_every_lock_exclusively_in_order(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    names = ["install/preparation.lock", "data/state/runtime-use.lock", "data/state/update.lock",
             "data/state/owner/owner.sock.lock", "data/state/setup-preview.lock", "data/state/provider-signin.lock"]
    inodes = [(family / name).lstat().st_ino for name in names]
    held = []
    real = fcntl.flock
    monkeypatch.setattr(module.fcntl, "flock", lambda fd, op: held.append((os.fstat(fd).st_ino, op & ~fcntl.LOCK_NB)) or real(fd, op))
    call(module, "start-again")
    locks = [item for item in held if item[0] in inodes]
    assert [inode for inode, _op in locks] == inodes and all(op == fcntl.LOCK_EX for _inode, op in locks)


def test_the_locks_are_taken_before_any_deny_or_content_check(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    put(family / "data/state/config.json", b"{}")
    held = hold(family / "data/state/runtime-use.lock", fcntl.LOCK_EX)
    try:
        # A running computer is answered in_use, not started: the locks come first.
        refuses_both(module, home, "in_use")
    finally:
        os.close(held)
    refuses_both(module, home, "started")


class Renames:
    def __init__(self, monkeypatch, *, crash_after=None, block=None):
        self.calls, self.crash_after, self.block = [], crash_after, block
        self.real = os.rename
        monkeypatch.setattr(os, "rename", self)

    def __call__(self, source, target, **kwargs):
        if self.block is not None and threading.current_thread().name == "A":
            self.block()
        if self.crash_after is not None and len(self.calls) >= self.crash_after:
            raise Crash()
        self.calls.append(source)
        return self.real(source, target, **kwargs)


ORDER = ["data", "staging", "provider-profile", "provider-bin", "host-policy", "install"]


def test_the_folders_move_in_the_planned_order_install_last(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    renames = Renames(monkeypatch)
    call(module, "start-again")
    assert renames.calls == ORDER


@pytest.mark.parametrize("after", range(0, 6))
def test_a_crash_after_each_rename_is_finished_by_a_second_press(tmp_path, monkeypatch, after):
    module, home, family, outer = world(tmp_path, monkeypatch)
    before = beyond(home)
    with monkeypatch.context() as patch:
        Renames(patch, crash_after=after)
        with pytest.raises(Crash):
            call(module, "start-again")
    moved = ORDER[:after]
    assert sorted(name for name in os.listdir(family) if name != "start-again") == sorted(
        name for name in ORDER if name not in moved)
    assert (family / "start-again").is_dir() if after else True
    if after:
        # While it is unfinished the installer's own verbs refuse the unknown name, and
        # install stays in place until every other folder has moved.
        value = {"schema_version": 1, "source_revision": NEW, "expected_uid": os.geteuid()}
        with pytest.raises(module.Refusal):
            module.execute("status", value, time.monotonic() + 15)
        assert "install" in os.listdir(family)
        assert call(module, "start-again-status") == {"state": "eligible", "next": "start_again"}
        assert call(module, "start-again") == {"state": "removed", "next": "prepare"}
    else:
        assert call(module, "start-again") == {"state": "removed", "next": "prepare"}
    assert os.listdir(family) == [] and beyond(home) == before
    assert call(module, "start-again") == {"state": "absent", "next": "prepare"}


def test_the_crash_after_install_moved_is_not_answered_absent(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    before = beyond(home)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", lambda *a, **k: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            call(module, "start-again")
    # Only start-again/ remains in the family folder, holding install/ and the rest. It is
    # the one place that still shows a computer was prepared, so absent would be wrong.
    assert os.listdir(family) == ["start-again"] and set(os.listdir(family / "start-again")) == set(ORDER)
    value = {"schema_version": 1, "source_revision": NEW, "expected_uid": os.geteuid()}
    with pytest.raises(module.Refusal) as error:
        module.execute("status", value, time.monotonic() + 15)
    assert error.value.code == "existing_installation"
    assert call(module, "start-again-status") == {"state": "eligible", "next": "start_again"}
    assert call(module, "start-again") == {"state": "removed", "next": "prepare"}
    assert os.listdir(family) == [] and beyond(home) == before


@pytest.mark.parametrize("after", [1, 3, 9, 20])
def test_a_crash_inside_the_delete_is_finished_by_a_second_press(tmp_path, monkeypatch, after):
    module, home, family, outer = world(tmp_path, monkeypatch)
    before = beyond(home)
    real = {name: getattr(os, name) for name in ("unlink", "rmdir")}
    counter = {"calls": 0}
    def dying(name):
        def inner(*args, **kwargs):
            counter["calls"] += 1
            if counter["calls"] > after:
                raise Crash()
            return real[name](*args, **kwargs)
        return inner
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", dying("unlink"))
        patch.setattr(os, "rmdir", dying("rmdir"))
        with pytest.raises(Crash):
            call(module, "start-again")
    assert os.listdir(family) == ["start-again"] and set(os.listdir(family / "start-again")) <= set(ORDER)
    assert call(module, "start-again-status") == {"state": "eligible", "next": "start_again"}
    assert call(module, "start-again") == {"state": "removed", "next": "prepare"}
    assert os.listdir(family) == [] and beyond(home) == before


def test_a_second_press_with_install_still_in_place_runs_every_check_again(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        Renames(patch, crash_after=3)
        with pytest.raises(Crash):
            call(module, "start-again")
    # Something that shows a team was started appears beside the folders not yet moved.
    put(family / "install/bin/com.claudlobby.update.0123456789abcdef.service", b"x")
    before = snapshot(home)
    with pytest.raises(module.Refusal) as error:
        call(module, "start-again")
    assert error.value.code == "started" and snapshot(home) == before
    (family / "install/bin/com.claudlobby.update.0123456789abcdef.service").unlink()
    assert call(module, "start-again")["state"] == "removed"


def test_an_unfinished_start_again_folder_with_anything_else_in_it_refuses(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        Renames(patch, crash_after=2)
        with pytest.raises(Crash):
            call(module, "start-again")
    put(family / "start-again/stranger", b"?")
    refuses_both(module, home, "unknown_content")
    (family / "start-again/stranger").unlink()
    (family / "start-again/data").rename(family / "start-again/data-moved")
    refuses_both(module, home, "unknown_content")


def test_a_folder_in_both_places_after_a_crash_refuses(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        Renames(patch, crash_after=2)
        with pytest.raises(Crash):
            call(module, "start-again")
    (family / "data").mkdir(mode=0o700)
    refuses_both(module, home, "unknown_content")


def test_two_runs_at_once_one_removes_and_the_other_is_busy(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    before = beyond(home)
    reached, release = threading.Event(), threading.Event()
    results = {}
    def block():
        reached.set()
        assert release.wait(30)
    Renames(monkeypatch, block=block)
    def first():
        try:
            results["A"] = call(module, "start-again")
        except BaseException as error:  # noqa: BLE001
            results["A"] = error
    thread = threading.Thread(target=first, name="A")
    thread.start()
    try:
        assert reached.wait(30)
        for verb in VERBS:
            with pytest.raises(module.Refusal) as error:
                call(module, verb)
            assert (error.value.code, error.value.state) == ("preparation_in_progress", "busy")
    finally:
        release.set()
        thread.join(60)
    assert results["A"] == {"state": "removed", "next": "prepare"}
    assert os.listdir(family) == [] and beyond(home) == before


def test_two_runs_that_both_pass_the_checks_cannot_both_create_the_holding_folder(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "start-again").mkdir(mode=0o700)
    held = os.open(family / "start-again", os.O_RDONLY)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        refuses_both(module, home, "preparation_in_progress", state="busy")
    finally:
        os.close(held)
    assert call(module, "start-again") == {"state": "removed", "next": "prepare"}


def test_a_missing_update_lock_is_never_made(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "data/state/update.lock").unlink()
    put(family / "data/state/config.json", b"{}")
    refuses_both(module, home, "started")
    (family / "data/state/config.json").unlink()
    removed_cleanly(module, home, family, outer)


def test_a_started_team_whose_checkout_moved_past_the_receipt_is_started_not_identity_changed(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (family / "install/src/.git/HEAD").write_text(NEW + "\n")
    refuses_both(module, home, "identity_changed")
    put(family / "data/state/config.json", b"{}")
    refuses_both(module, home, "started")


def unfinished(module, tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        Renames(patch, crash_after=2)
        with pytest.raises(Crash):
            call(module, "start-again")
    assert sorted(os.listdir(family / "start-again")) == ["data", "staging"]
    return module, home, family, outer


def test_what_already_moved_into_start_again_is_checked_again(tmp_path, monkeypatch):
    module, home, family, outer = unfinished(None, tmp_path, monkeypatch)
    put(family / "start-again/data/state/stranger", b"?")
    refuses_both(module, home, "unknown_content")
    (family / "start-again/data/state/stranger").unlink()
    bound_socket(family / "start-again/staging/sock")
    refuses_both(module, home, "unknown_content")
    (family / "start-again/staging/sock").unlink()
    assert call(module, "start-again")["state"] == "removed"


def test_a_moved_folder_on_another_device_is_refused(tmp_path, monkeypatch):
    module, home, family, outer = unfinished(None, tmp_path, monkeypatch)
    other_device(monkeypatch, (family / "start-again/data/state/plane").lstat().st_ino)
    refuses_both(module, home, "unknown_content")


# The installer's own process: its request, its one answer line and its exit codes.
START_DRIVER = '''import sys,types,json,os,pwd,platform
platform.system=lambda: "Linux"
from pathlib import Path
body=Path(sys.argv[1]).read_text().split("# BEGIN EMBEDDED PYTHON\\n",1)[1].split("# END EMBEDDED PYTHON\\n",1)[0]
m=types.ModuleType("isolated_operation");exec(compile(body,sys.argv[1],"exec"),m.__dict__)
home=Path(sys.argv[2]);python=Path(sys.executable).resolve()
m.account_profile=lambda uid: home
m.independent_python=lambda: python
pwd.getpwuid=lambda uid: types.SimpleNamespace(pw_dir=str(home))
raise SystemExit(m.update_main(sys.argv[3]))
'''


def invoke_start(tmp_path, home, verb, request):
    driver = tmp_path / "start-driver.py"
    driver.write_text(START_DRIVER)
    result = subprocess.run([sys._base_executable, "-I", "-B", str(driver), str(SCRIPT), str(home), verb],
                            input=request, env=constructed_env(PATH="/nonexistent", HOME=str(tmp_path / "untrusted-home")),
                            capture_output=True, timeout=120)
    assert result.stdout.count(b"\n") == 1 and len(result.stdout) <= 8192 and result.stderr == b"", result
    answer = json.loads(result.stdout)
    assert result.stdout == (json.dumps(answer, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    return result.returncode, answer


def test_the_process_answers_one_canonical_line_with_the_exit_codes_of_the_other_verbs(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    request = frame(source_revision=NEW, expected_uid=os.geteuid())
    operation = {"start-again-status": "host-start-again-status-v1", "start-again": "host-start-again-v1"}
    assert invoke_start(tmp_path, home, "start-again-status", request) == (0, {
        "schema_version": 1, "operation": operation["start-again-status"], "state": "eligible", "next": "start_again"})
    put(family / "data/state/config.json", b"{}")
    assert invoke_start(tmp_path, home, "start-again", request) == (4, {
        "schema_version": 1, "operation": operation["start-again"], "state": "refused", "code": "started"})
    held = hold(family / "install/preparation.lock", fcntl.LOCK_EX)
    try:
        assert invoke_start(tmp_path, home, "start-again", request) == (4, {
            "schema_version": 1, "operation": operation["start-again"], "state": "busy", "code": "preparation_in_progress"})
    finally:
        os.close(held)
    (family / "data/state/config.json").unlink()
    assert invoke_start(tmp_path, home, "start-again", frame(source_revision=OLD, expected_uid=os.geteuid())) == (4, {
        "schema_version": 1, "operation": operation["start-again"], "state": "refused", "code": "already_current"})
    assert invoke_start(tmp_path, home, "start-again", b"{not a request}\n") == (2, {
        "schema_version": 1, "operation": operation["start-again"], "state": "refused", "code": "invalid_request"})
    assert invoke_start(tmp_path, home, "start-again", request) == (0, {
        "schema_version": 1, "operation": operation["start-again"], "state": "removed", "next": "prepare"})
    assert invoke_start(tmp_path, home, "start-again", request) == (0, {
        "schema_version": 1, "operation": operation["start-again"], "state": "absent", "next": "prepare"})


def test_an_unexpected_failure_is_answered_unavailable_and_exit_6(tmp_path, monkeypatch):
    module, home, family, outer = world(tmp_path, monkeypatch)
    (home / ".config/systemd/user").chmod(0o000)
    try:
        code, answer = invoke_start(tmp_path, home, "start-again-status", frame(source_revision=NEW, expected_uid=os.geteuid()))
    finally:
        (home / ".config/systemd/user").chmod(0o700)
    assert (code, answer["state"], answer["code"]) == (6, "unavailable", "unavailable")


def test_the_shell_refuses_a_second_argument_with_the_preparations_own_names(tmp_path):
    for verb in VERBS:
        result = subprocess.run(["/bin/sh", str(SCRIPT), verb, "extra"], input=b"", env=constructed_env(),
                                capture_output=True, timeout=5)
        assert result.returncode == 2 and json.loads(result.stdout)["operation"] == "host-install-v1"


def test_a_real_preparation_is_started_again_at_a_later_revision_and_prepared_again(tmp_path, local_install_source):
    remote, wheels, first = local_install_source
    second, git = _commit(remote, first)
    git("reset", "--soft", second)
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    prepared_first = invoke_fixture(tmp_path, home, local_install_source, "prepare")
    assert prepared_first.returncode == 0, (prepared_first.stdout, prepared_first.stderr)
    family = home / FAMILY
    add_setup_state(family)
    outer = outside_world(home, tmp_path)
    # The new revision cannot be prepared over the old one.
    refused_prepare = invoke_fixture(tmp_path, home, (remote, wheels, second), "prepare")
    assert refused_prepare.returncode == 4 and json.loads(refused_prepare.stdout)["code"] in {"identity_changed", "existing_installation"}
    before = beyond(home)
    request = frame(source_revision=second, expected_uid=os.geteuid())
    assert invoke_start(tmp_path, home, "start-again-status", request)[1]["state"] == "eligible"
    assert invoke_start(tmp_path, home, "start-again", request) == (0, {
        "schema_version": 1, "operation": "host-start-again-v1", "state": "removed", "next": "prepare"})
    assert os.listdir(family) == [] and beyond(home) == before
    assert (outer / "sentinel.txt").read_bytes() == b"OUTSIDE-SENTINEL"
    again = invoke_fixture(tmp_path, home, (remote, wheels, second), "prepare")
    assert again.returncode == 0, (again.stdout, again.stderr)
    assert json.loads(again.stdout)["state"] == "prepared"
    assert json.loads((family / "install/preparation.json").read_text())["ref"] == second
