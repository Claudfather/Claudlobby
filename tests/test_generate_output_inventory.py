"""Measured cmd_generate output inventory; no claim about every configuration.

The recorder sees Python filesystem operation intentions (including failed or
unchanged writes), while snapshots check results. Subprocesses are limited to
the real private env-tier query and private read-only Git provenance probe.
Registry invocation is observed with emission disabled, not counted as a
durable append. Nothing in this module replaces a generation writer.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys

import pytest
import yaml

from claudlobby import composer, config
from claudlobby.commands import core
from claudlobby.plane import registry_emit

SOURCE = Path(__file__).resolve().parents[1]


def _put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class Operations:
    """Observe CPython audit events; refuse mutation outside the private estate."""

    def __init__(self, estate):
        self.estate = estate.resolve()
        self.active = False
        self.events = set()
        self.fds = {}
        self.refusals = []

    def refuse(self, reason):
        self.refusals.append(reason)
        raise AssertionError(reason)

    def path(self, value, dir_fd=-1):
        if isinstance(value, int):
            if value not in self.fds:
                self.refuse("write through an untracked descriptor")
            return self.fds[value]
        path = Path(os.fsdecode(value))
        if not path.is_absolute():
            path = (self.fds[dir_fd] if dir_fd not in (-1, None) else Path.cwd()) / path
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(self.estate):
            self.refuse(f"outside private estate: {path}")
        # The owning path and followed destination must both be private. These
        # fixtures contain only private mount/skill targets, including stales.
        if not path.resolve().is_relative_to(self.estate):
            self.refuse(f"outside link: {path}")
        return path

    def note(self, operation, value, dir_fd=-1, detail=""):
        path = self.path(value, dir_fd)
        self.events.add((operation, path.relative_to(self.estate).as_posix(), detail))

    def audit(self, event, args):
        if not self.active:
            return
        if event == "open":
            path, _mode, flags = args
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                self.note("append" if flags & os.O_APPEND else "write", path)
        elif event in ("os.mkdir", "os.chmod"):
            path, mode, dir_fd = args
            self.note(event[3:], path, dir_fd, oct(mode) if event == "os.chmod" else "")
        elif event in ("os.remove", "os.rmdir"):
            self.note(event[3:], args[0], args[1])
        elif event == "os.symlink":
            target, path, dir_fd = args
            destination = self.path(path, dir_fd)
            self.path(destination.parent / os.fsdecode(target))
            self.note("symlink", path, dir_fd)
        elif event == "os.rename":
            old, new, old_fd, new_fd = args
            self.note("rename-from", old, old_fd)
            self.note("rename-to", new, new_fd)
        elif event in ("os.truncate", "os.chown", "os.utime"):
            self.note(event[3:], args[0])
        elif event == "os.link":
            self.path(args[0], args[2])
            self.note("hardlink", args[1], args[3])
        elif event == "subprocess.Popen":
            _, command, _, child = args
            root = self.estate / "root"
            tier = (isinstance(command, list) and command[:2] ==
                    ["bash", str(root / "lib/env-tiers.sh")])
            git = command == ["git", "-C", str(root / "local/sample"),
                              "rev-parse", "--is-inside-work-tree"]
            child = os.environ if child is None else child
            if (not (tier or git) or child.get("HOME") != str(self.estate / "home")
                    or child.get("TMPDIR") != str(self.estate / "tmp")):
                self.refuse(f"unexpected subprocess: {command}")
        elif event in ("socket.connect", "socket.bind", "socket.getaddrinfo",
                       "os.system", "os.posix_spawn", "os.exec", "os.fork",
                       "os.forkpty", "pty.spawn"):
            self.refuse(f"unexpected external effect: {event}")

    @contextmanager
    def recording(self, monkeypatch):
        real_open, real_close = os.open, os.close

        def tracked_open(path, flags, *args, **kwargs):
            resolved = self.path(path, kwargs.get("dir_fd", -1))
            fd = real_open(path, flags, *args, **kwargs)
            self.fds[fd] = resolved
            return fd

        def tracked_close(fd):
            try:
                return real_close(fd)
            finally:
                self.fds.pop(fd, None)

        # CPython audit hooks cannot be removed; the inactive flag makes this
        # hook inert after this single bounded generation call.
        sys.addaudithook(self.audit)
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", tracked_open)
            patch.setattr(os, "close", tracked_close)
            self.active = True
            try:
                yield self
            finally:
                self.active = False


def _snapshot(root):
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            value = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            value = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            value = None
        result[path.relative_to(root).as_posix()] = (stat.S_IFMT(info.st_mode),
                                                   stat.S_IMODE(info.st_mode), value)
    return result


@pytest.fixture
def estate(tmp_path, monkeypatch):
    base = (tmp_path / "estate").resolve()
    root, home = base / "root", base / "home"
    root.mkdir(parents=True)
    home.mkdir()
    bins = base / "bin"
    bins.mkdir()
    for name in ("bash", "dirname", "uname", "git", "mktemp", "rm"):
        executable = shutil.which(name)
        assert executable, name
        (bins / name).symlink_to(executable)
    env = {"PATH": str(bins), "HOME": str(home), "LANG": "C.UTF-8",
           "PLANE_EMIT_DISABLED": "1", "PLANE_EMIT_ENABLED": "0",
           "CLAUDLOBBY_ROOT": str(root), "CLAUDLOBBY_PLANE_DIR": str(base / "plane"),
           "PLANE_SOCKET": str(base / "sockets/absent.sock"),
           "TELEGRAM_STATE_DIR": str(base / "channel"),
           "TELEGRAM_CHANNEL_STATE_DIR": str(base / "channel"),
           "TMPDIR": str(base / "tmp"), "TMUX_TMPDIR": str(base / "sockets"),
           "XDG_CONFIG_HOME": str(base / "xdg"), "XDG_CACHE_HOME": str(base / "cache"),
           "CLAUDLOBBY_HOST_SYSTEM_YAML": str(base / "absent-host-override.yaml"),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for name in ("tmp", "sockets", "channel", "xdg", "cache"):
        (base / name).mkdir()
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(base)
    for name in ("env-tiers.sh", "lib-common.sh", "supervisor.sh", "mcp-package-grammar.py"):
        (root / "lib").mkdir(exist_ok=True)
        shutil.copy2(SOURCE / "lib" / name, root / "lib" / name)
    _put(root / "lib/noop.sh", "#!/bin/sh\nexit 0\n")
    _put(home / ".env", "export INVENTORY_HOST_TIER=private-host\n")
    _put(root / ".env", "export INVENTORY_ROOT_TIER=private-root\n")
    _put(root / "templates/claude.md.j2", (SOURCE / "templates/claude.md.j2").read_text())
    _put(root / "library/expertise/fixture.md", "# Fixture\n\nSynthetic inventory bot.\n")
    _put(root / "library/skills/fixture/SKILL.md", "---\nname: fixture\n---\nRead synthetic data.\n")
    _put(root / "library/tools/probe/tool.yaml", "type: script\nenv: [PROBE_TOKEN]\n")
    _put(root / "library/tools/probe/probe.sh.j2", "#!/bin/sh\nprintf '%s\\n' fixture\n")
    _put(root / "library/mcp/fixture.json", json.dumps({
        "fixture": {"command": "fixture-mcp", "env": {"TOKEN": "${FIXTURE_TOKEN}"}},
        "_env_contract": {"FIXTURE_TOKEN": {"secret": True, "default_tier": "fleet"},
                          "PERSONAL_TOKEN": {"secret": True, "default_tier": "bot"}},
    }))
    (home / "mount-target").mkdir()
    _put(home / "mount-target/user.txt", "mutable mount target\n")
    system = root / "fixture-system.yaml"
    system.write_text(yaml.safe_dump({
        "defaults": {"jobs": {"inventory-tick": {"interval": 600, "script": "$CLAUDLOBBY_ROOT/lib/noop.sh"}}},
        "host": {"jobs": {
            "inventory-host": {"interval": 600, "script": "$CLAUDLOBBY_ROOT/lib/noop.sh"},
            "inventory-service": {"unit": "service", "enroll": True, "script": "$CLAUDLOBBY_ROOT/lib/noop.sh"},
            "inventory-off": {"interval": 600, "enroll": False, "script": "$CLAUDLOBBY_ROOT/lib/noop.sh"},
        }},
    }))
    # Supply package configuration through its real YAML reader, with a private
    # cache. All subsequent selection, validation and writers remain real.
    monkeypatch.setattr(config, "_resolve_system_yaml", lambda _: system)
    monkeypatch.setattr(config._load_system_defaults, "__defaults__", ({},))
    bots = {}
    for name in ("alpha", "beta"):
        bots[name] = {"expertise": ["fixture"], "channels": [], "skills": ["fixture"],
                      "tools": ["probe"], "mcp": ["fixture"],
                      "telegram": {"handle": f"inventory_{name}_bot", "token_env": f"TG_{name.upper()}"},
                      "mounts": {"docs": str(home / "mount-target")}}
    bots["alpha"]["github_app"] = {"slug": "fixture-app", "bot_user_id": 123, "orgs": ["FixtureOrg"]}
    manifest = {"fleet": {"name": "sample", "service_prefix": "com.inventory",
                          "telegram_group_chat_id": "-1001", "human_telegram_id": "12345",
                          "system_defaults": {"hooks": False, "protocols": False,
                                              "guardrails": False, "observability": False},
                          "plugins": {"include_defaults": False}, "bots": bots}}
    fleet_dir = root / "local/sample"
    _put(fleet_dir / "fleet.yaml", yaml.safe_dump(manifest, sort_keys=False))
    _put(root / "local/group/sibling/fleet.yaml", "fleet:\n  name: sibling\n  bots: {sibling: {expertise: [fixture]}}\n")
    real_run = subprocess.run
    query_failures = []

    def private_run(args, *a, **kw):
        tier = args[:2] == ["bash", str(root / "lib/env-tiers.sh")]
        git = args == ["git", "-C", str(fleet_dir), "rev-parse", "--is-inside-work-tree"]
        if not (tier or git):
            query_failures.append(f"unexpected subprocess: {args}")
            raise AssertionError(query_failures[-1])
        child = kw.get("env", os.environ)
        assert child["HOME"] == str(home)
        assert child.get("CLAUDLOBBY_ROOT") == str(root)
        # read_tiers deliberately constructs a small child environment; add
        # only scratch isolation because sourcing lib-common creates a temp
        # directory even for this read-only query. The real query still runs.
        kw["env"] = {**child, "TMPDIR": str(base / "tmp")}
        result = real_run(args, *a, **kw)
        if tier:
            rows = [line.split("\t") for line in result.stdout.splitlines()]
            valid = (result.returncode == 0 and len(rows) == 4
                     and all(len(row) == 3 for row in rows)
                     and [row[0] for row in rows] == ["host", "root", "fleet", "bot"]
                     and all(not row[1] or Path(row[1]).resolve().is_relative_to(base) for row in rows))
            if not valid:
                query_failures.append(f"private env query failed: {result.stderr}")
                raise AssertionError(query_failures[-1])
        return result

    monkeypatch.setattr(subprocess, "run", private_run)
    scans = []
    real_scan = registry_emit.run_generate_scan

    def scan(paths, fleet):
        scans.append((str(paths.root), fleet.name))
        result = real_scan(paths, fleet)
        assert result is None  # the explicit disabled-emission boundary
        return result

    monkeypatch.setattr(registry_emit, "run_generate_scan", scan)
    yield base, root, home, scans
    assert not query_failures  # even if a best-effort caller swallowed a refusal


def _generate(root, selected):
    return core.cmd_generate(argparse.Namespace(root=str(root), fleet="sample",
                                               bot=selected, strict=False))


def _seed_stale(root, home):
    fleet = root / "local/sample"
    for name in ("alpha", "beta"):
        bot = fleet / "runtime/bots" / name
        for relative in ("memory/user.md", "data/events/user.json", "projects/user.txt",
                         "logs/user.log", ".claude/skills/user.txt", "mounts/user.txt"):
            _put(bot / relative, f"preserve {name}/{relative}\n")
        _put(bot / ".claude/skills/retired/nested.txt", "old package\n")
        (bot / "mounts/retired").symlink_to(home / "mount-target")
        _put(bot / "tools/retired.sh", "old generated tool\n")
        (bot / "tools/probe.sh").chmod(0o644)
        _put(bot / ".env", "export PERSONAL_TOKEN='operator-value'\n")
        (bot / ".env").chmod(0o644)
        channel = home / f".claude/channels/telegram-inventory_{name}_bot/access.json"
        _put(channel, json.dumps({"dmPolicy": "open", "allowFrom": ["6789"],
             "pending": {"request": {"from": "6789"}},
             "groups": {"-1001": {"requireMention": False, "allowFrom": ["6789"]},
                        "-9999": {"requireMention": False, "allowFrom": ["8888"]}}}))
    for relative in (".gitconfig", ".gitconfig-github-app-id"):
        _put(fleet / "runtime/bots/beta" / relative, "stale app config\n")
    _put(fleet / ".env", "export FIXTURE_TOKEN='operator-fleet-value'\n")
    (fleet / ".env").chmod(0o644)
    _put(fleet / "shared/knowledge/user.md", "preserve shared knowledge\n")
    for ext in ("service", "timer", "plist"):
        _put(fleet / f"runtime/fleet/timers/com.inventory.briefing-retired.{ext}", "stale\n")
        _put(fleet / f"runtime/fleet/timers/com.inventory.manager-checkin.{ext}", "stale\n")
        _put(root / f"runtime/_host/timers/claudlobby-inventory-off.{ext}", "stale\n")
    _put(root / "runtime/_host/timers/DORMANT", "obsolete manifest\n")


def _expected_operations(selected, stale):
    """Finite, hand-reviewed paths: adding a writer does not expand this set.

    The inventory intentionally includes mkdir/unlink attempts that may leave
    no changed artifact. Frequencies and temporal ordering are not measured.
    """
    events = set()
    fleet = "root/local/sample"
    host = "root/runtime/_host"

    def add(operation, *paths, detail=""):
        events.update((operation, path, detail) for path in paths)

    for name in ([selected] if selected else ["alpha", "beta"]):
        bot = f"{fleet}/runtime/bots/{name}"
        channel = f"home/.claude/channels/telegram-inventory_{name}_bot"
        add("mkdir", bot, channel, *(f"{bot}/{leaf}" for leaf in (
            ".claude", ".claude/skills", "data", "data/events", "logs",
            "memory", "mounts", "projects", "tools")))
        add("write", f"{channel}/access.json", *(f"{bot}/{leaf}" for leaf in (
            ".claude/settings.local.json", ".mcp.json", "CLAUDE.md", "bot.conf",
            f"com.inventory.{name}.plist", f"com.inventory.{name}.service",
            "tools/probe.sh")))
        add("chmod", f"{bot}/tools/probe.sh", detail="0o755")
        add("symlink", f"{bot}/.claude/skills/fixture")
        if not stale:
            add("symlink", f"{bot}/mounts/docs")
        if name == "alpha":
            add("write", f"{bot}/.gitconfig", f"{bot}/.gitconfig-github-app-id",
                f"{bot}/tools/gh")
            add("chmod", f"{bot}/tools/gh", detail="0o755")
        else:
            add("remove", f"{bot}/.gitconfig", f"{bot}/.gitconfig-github-app-id")
        if stale:
            add("remove", *(f"{bot}/{leaf}" for leaf in (
                ".claude/skills/fixture", ".claude/skills/retired/nested.txt",
                "mounts/retired", "tools/retired.sh")))
            add("rmdir", f"{bot}/.claude/skills/retired")

    if not selected:
        add("mkdir", f"{fleet}/runtime", f"{fleet}/runtime/bots",
            *(f"{fleet}/shared/{leaf}" for leaf in (
                "decisions", "knowledge", "planning/active", "planning/completed", "runbooks")))
        add("write", f"{fleet}/runtime/composed.json")
        for env in (f"{fleet}/.env", *(f"{fleet}/runtime/bots/{bot}/.env" for bot in ("alpha", "beta"))):
            add("write", env)
            add("chmod", env, detail="0o600")
        if not stale:
            add("mkdir", f"{fleet}/shared", f"{fleet}/shared/planning")

    # The command generates fleet/host timer and guard-list outputs even for
    # --bot. Host resident services also create the host log's parent state/.
    timer = f"{fleet}/runtime/fleet/timers"
    add("mkdir", timer, host, f"{host}/timers", "root/state")
    if not stale:
        add("mkdir", "home/.claude", "home/.claude/channels", "root/runtime",
            f"{fleet}/runtime", f"{fleet}/runtime/bots", f"{fleet}/runtime/fleet")
    add("write", f"{host}/bot-handles", f"{host}/mention-allowlist")
    for ext in ("plist", "service", "timer"):
        add("write", f"{timer}/com.inventory.inventory-tick.{ext}",
            f"{host}/timers/claudlobby-inventory-host.{ext}")
        if ext != "timer":
            add("write", f"{host}/timers/claudlobby-inventory-service.{ext}")
        if stale:
            add("remove", f"{timer}/com.inventory.briefing-retired.{ext}",
                f"{timer}/com.inventory.manager-checkin.{ext}",
                f"{host}/timers/claudlobby-inventory-off.{ext}")
    for name in ("DORMANT", "BRIEFING_EXPECTED"):
        add("write", f"{timer}/{name}.tmp")
        add("rename-from", f"{timer}/{name}.tmp")
        add("rename-to", f"{timer}/{name}")
    if stale:
        add("remove", f"{host}/timers/DORMANT")
    return events


def _assert_inventory(actual, expected):
    unexpected, missing = actual - expected, expected - actual
    assert not (unexpected or missing), (
        f"unexpected output operations: {sorted(unexpected)}; "
        f"missing output operations: {sorted(missing)}"
    )


def _assert_results(base, before, expected, selected, stale):
    """Check final existence, explicit modes, links and mutable preservation."""
    after = _snapshot(base)
    create = {p for op, p, _ in expected if op in ("write", "mkdir", "symlink", "rename-to")}
    remove = {p for op, p, _ in expected if op in ("remove", "rmdir", "rename-from")}
    final_create = create - {p for op, p, _ in expected if op == "rename-from"}
    final_remove = remove - final_create
    assert set(after) - set(before) == final_create - set(before)
    assert set(before) - set(after) == final_remove & set(before)
    assert final_create <= set(after)
    assert not final_remove & set(after)
    mutable = {p for op, p, _ in expected if op != "mkdir"}
    assert {p: value for p, value in before.items() if p not in mutable} == {
        p: after.get(p) for p in before if p not in mutable
    }
    for op, path, detail in expected:
        if op == "chmod":
            assert after[path][1] == int(detail, 8), path
        if op == "mkdir":
            assert after[path][0] == stat.S_IFDIR, path
        if op == "symlink":
            target = (base / "root/library/skills/fixture" if path.endswith("/fixture")
                      else base / "home/mount-target")
            assert after[path][0] == stat.S_IFLNK and after[path][2] == str(target)
            # Symlink mode itself is platform-owned.
    if stale:
        fleet = base / "root/local/sample"
        for env, value in [(fleet / ".env", "operator-fleet-value"), *(
            (fleet / f"runtime/bots/{bot}/.env", "operator-value") for bot in ("alpha", "beta")
        )]:
            assert value in env.read_text()
        for name in ([selected] if selected else ["alpha", "beta"]):
            channel = base / f"home/.claude/channels/telegram-inventory_{name}_bot/access.json"
            access = json.loads(channel.read_text())
            assert access["pending"] == {"request": {"from": "6789"}}
            assert access["groups"]["-9999"] == {"requireMention": False, "allowFrom": ["8888"]}
            assert "6789" in access["allowFrom"]
            assert access["groups"]["-1001"]["requireMention"] is True
            assert access["groups"]["-1001"]["allowFrom"] == ["6789"]


@pytest.mark.parametrize("selected", [None, "alpha"], ids=["full", "bot"])
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_generate_output_inventory(estate, monkeypatch, selected, stale):
    base, root, home, scans = estate
    if stale:
        with Operations(base).recording(monkeypatch):
            assert _generate(root, None) == 0
        _seed_stale(root, home)
        scans.clear()
    before = _snapshot(base)
    recorder = Operations(base)
    with recorder.recording(monkeypatch):
        assert _generate(root, selected) == 0
    assert scans == [(str(root), "sample")]
    assert not (base / "plane").exists()
    assert not recorder.refusals  # catch any boundary refusal swallowed by a best-effort writer
    expected = _expected_operations(selected, stale)
    _assert_inventory(recorder.events, expected)
    _assert_results(base, before, expected, selected, stale)


@pytest.mark.parametrize("relative", [
    "root/local/sample/runtime/bots/alpha/unclassified.txt",
    "root/local/sample/runtime/fleet/timers/unclassified.txt",
    "root/runtime/_host/unclassified.txt",
    "home/.claude/channels/telegram-inventory_alpha_bot/unclassified.txt",
    "root/local/sample/shared/knowledge/unclassified.txt",
    "root/unclassified.txt",
])
def test_inventory_rejects_new_writer_output(estate, monkeypatch, relative):
    """Inject after a real writer; no renderer or expected set is replaced."""
    base, root, _, _ = estate
    original = composer.compose_host_bot_handles

    def extended_writer(*args, **kwargs):
        result = original(*args, **kwargs)
        (base / relative).write_text("new output requiring inventory review\n")
        return result

    monkeypatch.setattr(composer, "compose_host_bot_handles", extended_writer)
    recorder = Operations(base)
    with recorder.recording(monkeypatch):
        assert _generate(root, None) == 0
    with pytest.raises(AssertionError, match="unexpected output operations") as exc:
        _assert_inventory(recorder.events, _expected_operations(None, False))
    assert relative in str(exc.value)
    assert not recorder.refusals


@pytest.mark.parametrize("operation", ["append", "chmod", "remove", "mkdir", "symlink"])
def test_inventory_rejects_new_operation(estate, monkeypatch, operation):
    base, root, home, _ = estate
    original = composer.compose_host_bot_handles
    target = home / "mount-target/user.txt"

    def extended_writer(*args, **kwargs):
        result = original(*args, **kwargs)
        if operation == "append":
            with target.open("a") as stream:
                stream.write("unexpected append\n")
        elif operation == "chmod":
            target.chmod(0o600)
        elif operation == "remove":
            target.unlink()
        elif operation == "mkdir":
            (home / "unclassified").mkdir()
        else:
            (home / "unclassified").symlink_to(target)
        return result

    monkeypatch.setattr(composer, "compose_host_bot_handles", extended_writer)
    recorder = Operations(base)
    with recorder.recording(monkeypatch):
        assert _generate(root, None) == 0
    with pytest.raises(AssertionError, match="unexpected output operations") as exc:
        _assert_inventory(recorder.events, _expected_operations(None, False))
    assert operation in str(exc.value)
    assert not recorder.refusals


@pytest.mark.parametrize("effect", ["write", "remove", "chmod", "escaped-link",
                                    "link-target", "subprocess", "socket"])
def test_recording_boundary_refuses_before_effect(estate, monkeypatch, effect):
    base, _, home, _ = estate
    outside = base.parent / "outside-estate"
    outside.write_text("untouched\n")
    before = outside.read_bytes(), outside.stat().st_mode
    link = home / "escaped"
    if effect == "escaped-link":
        link.symlink_to(outside)
    recorder = Operations(base)
    with recorder.recording(monkeypatch), pytest.raises(AssertionError):
        if effect == "write":
            outside.write_text("must not happen")
        elif effect == "remove":
            outside.unlink()
        elif effect == "chmod":
            outside.chmod(0o777)
        elif effect == "escaped-link":
            link.write_text("must not happen")
        elif effect == "link-target":
            link.symlink_to(outside)
        elif effect == "subprocess":
            subprocess.Popen(["launchctl", "version"])
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect(("127.0.0.1", 9))
    assert recorder.refusals
    assert (outside.read_bytes(), outside.stat().st_mode) == before
    if effect == "link-target":
        assert not link.is_symlink()
