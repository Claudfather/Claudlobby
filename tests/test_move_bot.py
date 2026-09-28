"""Tests for claudlobby move-bot command."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from claudlobby.__main__ import main
from tests.package_fixtures import source_package


def _native_dir(root: Path) -> Path:
    return root.parent / "package" / "native"


@pytest.fixture(autouse=True)
def _isolated_move_runtime(tmp_path: Path, monkeypatch):
    """Use explicit source assets and a private enrollment stub, never services."""
    import claudlobby.composer as composer
    import claudlobby.context as context

    source = source_package()
    native = _native_dir(tmp_path / "claudlobby")
    native.mkdir(parents=True)
    for child in source.native.iterdir():
        if child.name != "spin-up-bot.sh":
            (native / child.name).symlink_to(child, target_is_directory=child.is_dir())
    spin_up = native / "spin-up-bot.sh"
    spin_up.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$1" "$CLAUDLOBBY_ROOT" "$FLEET_ROOT" '
        '"$CLAUDLOBBY_NATIVE_DIR" > "$CLAUDLOBBY_ROOT/enrollment-call"\n'
    )
    spin_up.chmod(0o755)
    package = replace(source, native=native)
    monkeypatch.setattr(context, "get_resources", lambda: package)

    cli = tmp_path / "package" / "bin" / "claudlobby"
    cli.parent.mkdir()
    cli.write_text("#!/bin/bash\nprintf '%s\\n' '--boot'\n")
    cli.chmod(0o755)
    monkeypatch.setattr(context, "selected_cli", lambda: cli)
    monkeypatch.setattr(composer, "selected_cli", lambda: cli)
    composer._brief_cli_probe.cache_clear()

    # Keep config/composition and their read-only query helpers real. Only the
    # direct supervision calls are intercepted; enrollment runs our own stub.
    real_run = subprocess.run
    calls: list[list[str]] = []

    def isolated_run(cmd, *args, **kwargs):
        argv = [str(arg) for arg in cmd]
        calls.append(argv)
        executable = Path(argv[0]).name
        if executable in {"tmux", "systemctl", "launchctl"}:
            rc = 1 if executable == "tmux" and "has-session" in argv else 0
            output = "" if kwargs.get("text") else b""
            return subprocess.CompletedProcess(cmd, rc, output, output)
        if executable == "spin-up-bot.sh":
            assert Path(argv[0]) == spin_up and not spin_up.is_symlink()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", isolated_run)
    yield calls
    composer._brief_cli_probe.cache_clear()


def _scaffold_fleet(
    local_dir: Path,
    fleet_name: str,
    bots: list[str],
    *,
    service_prefix: str = "com.test",
    telegram_group_chat_id: str | None = None,
    create_bot_dirs: bool = True,
    manager: str | None = None,
) -> Path:
    """Create worker fixtures with an explicit, separate fleet owner."""
    fleet_dir = local_dir / fleet_name
    fleet_dir.mkdir(parents=True, exist_ok=True)
    manager = manager if manager is not None else f"{fleet_name}-manager"

    manager_yaml = f"    {manager}:\n      expertise: [eng]\n      channels: []"
    workers_yaml = "\n".join(
        f"    {b}:\n      expertise: [eng]\n      telegram:\n        handle: {b}"
        for b in bots if b != manager
    )
    bots_yaml = "\n".join(part for part in (manager_yaml, workers_yaml) if part)
    tg_line = (
        f"\n  telegram_group_chat_id: '{telegram_group_chat_id}'"
        if telegram_group_chat_id
        else ""
    )
    (fleet_dir / "fleet.yaml").write_text(
        f"fleet:\n  name: {fleet_name}\n  manager: {manager}\n"
        f"  service_prefix: {service_prefix}{tg_line}\n  bots:\n{bots_yaml}\n"
    )
    expertise = fleet_dir / "library" / "expertise"
    expertise.mkdir(parents=True, exist_ok=True)
    (expertise / "eng.md").write_text(
        "---\ntitle: Engineering\ndescription: Software engineering\n---\n# Engineering\nBuild software.\n"
    )

    if create_bot_dirs:
        for bot in dict.fromkeys([manager, *bots]):
            bot_dir = fleet_dir / "runtime" / "bots" / bot
            bot_dir.mkdir(parents=True, exist_ok=True)
            (bot_dir / "bot.conf").write_text(
                f"BOT_NAME={bot}\nBOT_SERVICE={service_prefix}.{bot}\n"
            )

    return fleet_dir


def _scaffold_root(tmp_path: Path) -> Path:
    """Create mutable host data separately from the injected package assets."""
    root = tmp_path / "claudlobby"
    root.mkdir()
    return root


class TestMoveBotDryRun:
    def test_auto_detects_source_fleet(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])  # source: has bot dir + bot.conf

        # Target: has stanza in fleet.yaml but no bot dir with bot.conf
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        rc = main(["--root", str(root), "move-bot", "mybot", "--to", "fleet-b"])
        assert rc == 0  # dry run succeeds

    def test_error_bot_not_found(self, tmp_path: Path):
        """Bot not present in any fleet (no bot dir with bot.conf anywhere)."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["other"])
        # fleet-b has stanza but no bot.conf dir — autodetect won't find 'ghost'
        _scaffold_fleet(local, "fleet-b", ["otherbot"], create_bot_dirs=False)

        rc = main(["--root", str(root), "move-bot", "ghost", "--to", "fleet-b"])
        assert rc == 1

    def test_error_same_fleet(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])

        rc = main(["--root", str(root), "move-bot", "mybot", "--to", "fleet-a"])
        assert rc == 1

    def test_error_not_in_target_fleet_yaml(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["otherbot"])  # mybot not in target

        rc = main(["--root", str(root), "move-bot", "mybot", "--to", "fleet-b"])
        assert rc == 1

    def test_error_target_fleet_missing(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])

        rc = main(["--root", str(root), "move-bot", "mybot", "--to", "nonexistent"])
        assert rc == 1

    def test_from_flag_overrides_autodetect(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"])

        # Explicit --from resolves the ambiguity
        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
            ]
        )
        assert rc == 0

    def test_error_ambiguous_without_from(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"])

        rc = main(["--root", str(root), "move-bot", "mybot", "--to", "fleet-b"])
        assert rc == 1  # ambiguous — both fleets have bot dir

    def test_wip_check_blocks(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"])

        # Create a project with uncommitted changes
        projects = (
            local / "fleet-a" / "runtime" / "bots" / "mybot" / "projects" / "repo"
        )
        projects.mkdir(parents=True)
        subprocess.run(["git", "init", str(projects)], capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=projects,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@t",
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin",
            },
        )
        (projects / "dirty.txt").write_text("uncommitted")

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 1  # blocked by WIP


class TestMoveBotApply:
    def test_declared_manager_move_refuses_until_source_config_is_updated(
        self, tmp_path: Path, caplog, _isolated_move_runtime
    ):
        """Even --force/cleanup cannot silently replace the source manager."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        source = _scaffold_fleet(
            local, "fleet-a", ["mybot", "successor"], manager="mybot"
        )
        target = _scaffold_fleet(
            local, "fleet-b", ["mybot"], create_bot_dirs=False
        )
        source_yaml = source / "fleet.yaml"
        original = source_yaml.read_text()
        source_bot = source / "runtime" / "bots" / "mybot"
        (source_bot / ".env").write_text("SECRET=keep-me\n")
        argv = [
            "--root", str(root), "move-bot", "mybot", "--to", "fleet-b",
            "--from", "fleet-a", "--apply", "--force", "--cleanup-source",
        ]

        assert main(argv) == 1
        assert "update fleet.manager" in caplog.text
        assert source_yaml.read_text() == original
        assert (source_bot / ".env").read_text() == "SECRET=keep-me\n"
        assert not (target / "runtime").exists()
        assert not _isolated_move_runtime
        assert not (root / "enrollment-call").exists()

        # The operator explicitly changes the owner and removes the departing
        # stanza. move-bot must accept that valid config without rewriting it.
        _scaffold_fleet(
            local, "fleet-a", [], manager="successor", create_bot_dirs=False
        )
        updated = source_yaml.read_text()
        assert main(argv) == 0
        assert source_yaml.read_text() == updated
        assert not source_bot.exists()
        assert (source / "runtime" / "bots" / "successor").is_dir()

    def test_copies_env_and_memory(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        # Add .env and memory to source
        src_bot = src_fleet / "runtime" / "bots" / "mybot"
        (src_bot / ".env").write_text("SECRET=abc123\n")
        (src_bot / ".env").chmod(0o600)
        mem_dir = src_bot / "memory"
        mem_dir.mkdir()
        (mem_dir / "note.md").write_text("remember this")

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 0

        target_bot = local / "fleet-b" / "runtime" / "bots" / "mybot"
        assert (target_bot / ".env").read_text() == "SECRET=abc123\n"
        assert (target_bot / "memory" / "note.md").read_text() == "remember this"

    def test_cleanup_removes_source(self, tmp_path: Path, capsys):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        src_bot = src_fleet / "runtime" / "bots" / "mybot"
        assert src_bot.is_dir()

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
                "--cleanup-source",
            ]
        )
        assert rc == 0
        assert not src_bot.exists()
        # With cleanup, there is no orphan — the warning must NOT appear.
        assert "orphaned" not in capsys.readouterr().out

    def test_leaves_source_warns_when_no_cleanup(self, tmp_path: Path, capsys):
        """--apply without --cleanup-source keeps the source dir AND warns that
        it is now an orphan, with the exact path and remediation — so operators
        do not discover stale bot dirs weeks later (the craig/greg incident)."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        src_bot = src_fleet / "runtime" / "bots" / "mybot"

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 0
        # Source dir survives (cleanup is opt-in) ...
        assert src_bot.is_dir()
        # ... and the orphan is made loud, with path + remediation.
        out = capsys.readouterr().out
        assert "orphaned" in out
        assert str(src_bot) in out
        assert "rm -rf" in out
        # The prospective plan-time note is dry-run-only — on --apply the
        # post-apply warning is the single source, not duplicated here.
        assert "will be LEFT" not in out

    def test_dryrun_notes_orphan_when_no_cleanup(self, tmp_path: Path, capsys):
        """Dry-run (no --cleanup-source) previews the orphan up front so the
        operator can add the flag before applying. This note is dry-run-only;
        the apply path relies on the post-apply warning instead."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)
        src_bot = src_fleet / "runtime" / "bots" / "mybot"

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
            ]
        )
        assert rc == 0
        assert src_bot.is_dir()  # dry-run mutates nothing
        out = capsys.readouterr().out
        assert "will be LEFT in place (orphaned)" in out
        assert "Dry run" in out

    def test_enrollment_runs_spin_up_bot(self, tmp_path: Path):
        """spin-up-bot.sh is called and its exit code determines success."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 0  # stub exits 0
        target = local / "fleet-b"
        assert (root / "enrollment-call").read_text().splitlines() == [
            str(target / "runtime" / "bots" / "mybot"),
            str(root),
            str(target),
            str(_native_dir(root)),
        ]

    def test_enrollment_failure_returns_nonzero_and_warns_orphan(
        self, tmp_path: Path, capsys
    ):
        """spin-up-bot.sh failure returns 1 AND states the source disposition —
        the source is already orphaned, and the operator is distracted fixing
        enrollment, which is exactly when a silent orphan gets forgotten (#546)."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)
        src_bot = src_fleet / "runtime" / "bots" / "mybot"

        # Replace stub with one that fails
        stub = _native_dir(root) / "spin-up-bot.sh"
        stub.write_text("#!/bin/bash\necho 'enrollment failed' >&2\nexit 1\n")

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 1
        assert src_bot.is_dir()
        out = capsys.readouterr().out
        assert "INCOMPLETE MIGRATION" in out
        assert "orphaned" in out
        assert str(src_bot) in out
        assert "rm -rf" in out

    def test_enrollment_failure_defers_cleanup(self, tmp_path: Path, capsys):
        """--cleanup-source never removes the source of a half-done migration;
        the deferral is stated instead of silently skipped (#546)."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)
        src_bot = src_fleet / "runtime" / "bots" / "mybot"

        stub = _native_dir(root) / "spin-up-bot.sh"
        stub.write_text("#!/bin/bash\nexit 1\n")

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
                "--cleanup-source",
            ]
        )
        assert rc == 1
        assert src_bot.is_dir()
        out = capsys.readouterr().out
        assert "--cleanup-source deferred" in out
        assert str(src_bot) in out

    def test_access_json_failure_still_warns_orphan(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        """The access.json INCOMPLETE MIGRATION exit happens after step 3 has
        already ended source supervision — the disposition must print there
        too, not only on the spin-up branches (#546)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(
            local,
            "fleet-b",
            ["mybot"],
            create_bot_dirs=False,
            telegram_group_chat_id="-100777",
        )
        src_bot = src_fleet / "runtime" / "bots" / "mybot"

        channel_dir = tmp_path / ".claude" / "channels" / "telegram-mybot"
        channel_dir.mkdir(parents=True)
        (channel_dir / "access.json").write_text("{ not json")

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 1
        assert src_bot.is_dir()
        out = capsys.readouterr().out
        assert "access.json update failed" in out
        assert "orphaned" in out
        assert str(src_bot) in out

    def test_missing_spinup_still_warns_orphan(self, tmp_path: Path, capsys):
        """The not-enrolled branch (spin-up-bot.sh absent) states the orphan
        disposition too (#546)."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)
        src_bot = src_fleet / "runtime" / "bots" / "mybot"

        (_native_dir(root) / "spin-up-bot.sh").unlink()

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 1
        assert src_bot.is_dir()
        out = capsys.readouterr().out
        assert "not enrolled" in out
        assert "orphaned" in out
        assert str(src_bot) in out

    def test_memory_copy_is_atomic(self, tmp_path: Path):
        """Memory copy uses temp-dir-then-rename for rollback safety."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        src_fleet = _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        # Create source memory
        src_bot = src_fleet / "runtime" / "bots" / "mybot"
        mem_dir = src_bot / "memory"
        mem_dir.mkdir()
        (mem_dir / "fact.md").write_text("important fact")

        # Create existing target memory that should be replaced
        target_bot = local / "fleet-b" / "runtime" / "bots" / "mybot"
        target_bot.mkdir(parents=True, exist_ok=True)
        target_mem = target_bot / "memory"
        target_mem.mkdir()
        (target_mem / "old.md").write_text("old data")

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 0
        # New memory replaced old
        assert (target_mem / "fact.md").read_text() == "important fact"
        assert not (target_mem / "old.md").exists()
        # No temp dir left behind
        assert not (target_bot / ".memory_tmp").exists()

    def test_validate_runs_before_mutation(self, tmp_path: Path):
        """Validation failure should return 1 without stopping any service."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])

        # Target fleet references a nonexistent expertise — validation will fail
        target = _scaffold_fleet(
            local, "fleet-b", ["mybot"], create_bot_dirs=False
        )
        target_yaml = target / "fleet.yaml"
        target_yaml.write_text(
            target_yaml.read_text().replace(
                "    mybot:\n      expertise: [eng]",
                "    mybot:\n      expertise: [nonexistent_expertise]",
            )
        )

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
            ]
        )
        assert rc == 1  # validation error, no mutation occurred

    def test_kills_source_tmux_server(self, tmp_path: Path, _isolated_move_runtime):
        """move-bot --apply tears down the source bot's per-bot tmux server so the
        move doesn't strand an orphaned server on the source host."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        rc = main(
            [
                "--root",
                str(root),
                "move-bot",
                "mybot",
                "--to",
                "fleet-b",
                "--from",
                "fleet-a",
                "--apply",
                "--force",
            ]
        )
        assert rc == 0
        assert ["tmux", "-L", "com.test.mybot", "kill-server"] in _isolated_move_runtime
