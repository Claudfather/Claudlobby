"""The real mkdir mutex, under constructed environments and disposable locks."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

import pytest

REPO = Path(__file__).resolve().parents[1]
BASH = "/bin/bash"  # macOS's actual 3.2, not a PATH-selected newer shell


class Scene:
    def __init__(self, root):
        self.root = root
        for name in ("home", "tmp", "state", "channels", "bin"):
            (root / name).mkdir()
        # Any unexpected outward door is observable and fails closed.
        for name in ("curl", "tmux", "launchctl", "systemctl", "claudlobby"):
            p = root / "bin" / name
            p.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$ROOT/outbound"\nexit 91\n')
            p.chmod(0o755)
        self.env = {
            "PATH": f"{root / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C", "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
            "XDG_CONFIG_HOME": str(root / "home/config"),
            "XDG_DATA_HOME": str(root / "home/data"),
            "XDG_CACHE_HOME": str(root / "home/cache"),
            "CLAUDLOBBY_ROOT": str(root), "ROOT": str(root),
            "PLANE_EMIT_DISABLED": "1", "PLANE_SOCKET": str(root / "state/no.sock"),
            "TELEGRAM_STATE_DIR": str(root / "channels"), "WITH_LOCK_WAIT_S": "0",
        }
        self.prefix = f'. "{REPO}/lib/lib-common.sh"\n_FLOCK_BIN=""\n'
        self.processes = []

    def run(self, script, **env):
        return subprocess.run([BASH, "-c", self.prefix + script],
                              env={**self.env, **env}, text=True, capture_output=True, timeout=12)

    def spawn(self, script, **env):
        p = subprocess.Popen([BASH, "-c", self.prefix + script],
                             env={**self.env, **env}, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(p)
        return p

    def wait(self, name, proc=None):
        path = self.root / name
        deadline = time.monotonic() + 8
        while not path.exists():
            if proc and proc.poll() is not None:
                pytest.fail(f"child exited {proc.returncode}: {proc.communicate()}")
            assert time.monotonic() < deadline, f"barrier never reached: {name}"
            time.sleep(0.01)
        return path

    def finish(self, p):
        out, err = p.communicate(timeout=12)
        assert p.returncode == 0, (out, err, p.returncode)
        return out, err

    def cleanup(self):
        for p in self.processes:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)
        assert not (self.root / "outbound").exists()
        assert not (self.root / "state/plane").exists()


@pytest.fixture
def scene(tmp_path):
    s = Scene(tmp_path)
    yield s
    s.cleanup()


def test_isolation_boundary(scene):
    result = scene.run('printf "%s\\n" "$HOME" "$CLAUDLOBBY_ROOT" "$PLANE_SOCKET"; env')
    assert result.returncode == 0, result.stderr
    assert f"HOME={scene.root / 'home'}" in result.stdout
    assert "PLANE_EMIT_DISABLED=1" in result.stdout
    assert "GITHUB_PAT=" not in result.stdout
    assert "FLEET_NAME=" not in result.stdout


def test_timeout_does_not_invoke_callback(scene):
    (scene.root / "mutex.d").mkdir()  # legacy/ambiguous owner: preserve
    result = scene.run('work() { touch "$ROOT/callback"; }; with_lock "$ROOT/mutex" work')
    assert result.returncode != 0
    assert not (scene.root / "callback").exists()
    assert (scene.root / "mutex.d").is_dir()
    assert "lock" in result.stderr


@pytest.mark.parametrize("status", [0, 17])
def test_callback_failure_preserves_status(scene, status):
    result = scene.run(f'work() {{ printf "entered\\n"; return {status}; }}; with_lock "$ROOT/mutex" work')
    assert result.returncode == status, result.stderr
    assert result.stdout == "entered\n"
    assert not (scene.root / "mutex.d").exists()


def hold(scene, name="holder", **env):
    os.mkfifo(scene.root / f"{name}.release")
    return scene.spawn(f'''work() {{
      touch "$ROOT/{name}.entered"
      read -r line < "$ROOT/{name}.release"
    }}
    with_lock "$ROOT/mutex" work
    ''', **env)


def release(scene, name="holder"):
    with (scene.root / f"{name}.release").open("w") as f:
        f.write("release\n")


def test_live_holder_is_never_broken(scene):
    p = hold(scene)
    scene.wait("holder.entered", p)
    before = (scene.root / "mutex.d/owner").read_bytes()
    result = scene.run('work() { touch "$ROOT/contender"; }; with_lock "$ROOT/mutex" work')
    assert result.returncode != 0
    assert not (scene.root / "contender").exists()
    assert (scene.root / "mutex.d/owner").read_bytes() == before
    release(scene)
    scene.finish(p)
    assert not (scene.root / "mutex.d").exists()


def test_crashed_owner_is_preserved(scene):
    p = hold(scene)
    scene.wait("holder.entered", p)
    owner = (scene.root / "mutex.d/owner").read_bytes()
    assert int(owner.split(b" ", 1)[0]) == p.pid
    p.kill()
    p.communicate(timeout=5)
    result = scene.run('work() { touch "$ROOT/callback"; }; with_lock "$ROOT/mutex" work')
    assert result.returncode == 75, result.stderr
    assert not (scene.root / "callback").exists()
    assert (scene.root / "mutex.d/owner").read_bytes() == owner


def test_old_owner_cannot_release_replacement(scene):
    p = hold(scene)
    scene.wait("holder.entered", p)
    (scene.root / "mutex.d").rename(scene.root / "old.d")
    replacement = scene.root / "mutex.d"
    replacement.mkdir()
    (replacement / "owner").write_text(f"{os.getpid()} replacement-token\n")
    release(scene)
    scene.finish(p)
    assert (replacement / "owner").read_text().endswith(" replacement-token\n")
    assert replacement.is_dir()


def test_subshell_owner_is_not_parent_pid(scene):
    result = scene.run('''( work() { cut -d " " -f1 "$ROOT/mutex.d/owner"; printf "parent=%s\\n" "$$"; };
    with_lock "$ROOT/mutex" work )''')
    assert result.returncode == 0, result.stderr
    owner, parent = result.stdout.splitlines()
    assert owner != parent.removeprefix("parent=")


def test_four_contenders_do_not_overlap(scene):
    contenders = []
    for i in range(4):
        os.mkfifo(scene.root / f"{i}.release")
        contenders.append(scene.spawn(f'''work() {{
          mkdir "$ROOT/critical" || {{ touch "$ROOT/overlap"; return 90; }}
          touch "$ROOT/{i}.entered"
          read -r line < "$ROOT/{i}.release"
          rmdir "$ROOT/critical"
        }}
        touch "$ROOT/{i}.ready"
        while [ ! -e "$ROOT/start" ]; do sleep 0.01; done
        with_lock "$ROOT/mutex" work
        ''', WITH_LOCK_WAIT_S="5"))
    for i, p in enumerate(contenders):
        scene.wait(f"{i}.ready", p)
    (scene.root / "start").touch()
    remaining = set(range(4))
    for _ in range(4):
        deadline = time.monotonic() + 8
        while not any((scene.root / f"{i}.entered").exists() for i in remaining):
            assert time.monotonic() < deadline, [p.poll() for p in contenders]
            assert not (scene.root / "overlap").exists()
            time.sleep(0.01)
        entered = [i for i in remaining if (scene.root / f"{i}.entered").exists()]
        assert len(entered) == 1
        i = entered[0]
        release(scene, str(i))
        scene.finish(contenders[i])
        remaining.remove(i)
    assert not (scene.root / "overlap").exists()
    assert not (scene.root / "mutex.d").exists()


@pytest.mark.parametrize("record", ["", "garbage", "0 token", "999999 token\nextra"])
def test_ambiguous_owner_is_preserved(scene, record):
    lock = scene.root / "mutex.d"
    lock.mkdir()
    (lock / "owner").write_text(record)
    result = scene.run('work() { touch "$ROOT/callback"; }; with_lock "$ROOT/mutex" work')
    assert result.returncode == 75
    assert not (scene.root / "callback").exists()
    assert (lock / "owner").read_text() == record


def test_lock_symlink_is_preserved(scene):
    other = scene.root / "other"
    other.mkdir()
    (other / "owner").write_text("999999 missing\n")
    (scene.root / "mutex.d").symlink_to(other, target_is_directory=True)
    result = scene.run('with_lock "$ROOT/mutex" true')
    assert result.returncode == 75
    assert (scene.root / "mutex.d").is_symlink()
    assert (other / "owner").read_text() == "999999 missing\n"



def test_dead_shell_with_live_callback_child_cannot_be_reclaimed(scene):
    """SIGKILL ends the shell, not its foreground child or its critical work."""
    import signal

    os.mkfifo(scene.root / "child.release")
    owner = scene.spawn('''work() {
      /bin/bash -c 'printf "%s\\n" "$$" > "$ROOT/child.pid";
        touch "$ROOT/child.entered";
        read -r line < "$ROOT/child.release";
        touch "$ROOT/child.finished"' &
      wait "$!"
    }
    with_lock "$ROOT/mutex" work''')
    scene.wait("child.entered", owner)
    child_pid = int((scene.root / "child.pid").read_text())
    before = (scene.root / "mutex.d/owner").read_bytes()
    try:
        owner.kill()
        owner.wait(timeout=5)
        os.kill(child_pid, 0)  # positive control: protected callback still exists
        contender = scene.run('work() { touch "$ROOT/overlap"; }; with_lock "$ROOT/mutex" work')
        assert contender.returncode == 75, contender.stderr
        assert not (scene.root / "overlap").exists()
        assert (scene.root / "mutex.d/owner").read_bytes() == before
        release(scene, "child")
        scene.wait("child.finished")
    finally:
        # The orphan may not be a waitable child; kill only our recorded fixture PID.
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        owner.communicate(timeout=5)
