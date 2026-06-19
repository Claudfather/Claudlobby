"""Tests for claudlobby move-bot command."""

from __future__ import annotations

from pathlib import Path

from claudlobby.__main__ import main


def _scaffold_fleet(
    local_dir: Path,
    fleet_name: str,
    bots: list[str],
    *,
    service_prefix: str = "com.test",
    telegram_group_chat_id: str | None = None,
    create_bot_dirs: bool = True,
) -> Path:
    """Create a minimal fleet overlay with bot dirs."""
    fleet_dir = local_dir / fleet_name
    fleet_dir.mkdir(parents=True, exist_ok=True)

    bots_yaml = "\n".join(
        f"    {b}:\n      expertise: [eng]\n      telegram:\n        handle: {b}"
        for b in bots
    )
    tg_line = (
        f"\n  telegram_group_chat_id: '{telegram_group_chat_id}'"
        if telegram_group_chat_id
        else ""
    )
    (fleet_dir / "fleet.yaml").write_text(
        f"fleet:\n  name: {fleet_name}\n  service_prefix: {service_prefix}{tg_line}\n  bots:\n{bots_yaml}\n"
    )

    if create_bot_dirs:
        for bot in bots:
            bot_dir = fleet_dir / "runtime" / "bots" / bot
            bot_dir.mkdir(parents=True, exist_ok=True)
            (bot_dir / "bot.conf").write_text(
                f"BOT_NAME={bot}\nBOT_SERVICE={service_prefix}.{bot}\n"
            )

    return fleet_dir


def _scaffold_root(tmp_path: Path) -> Path:
    """Create a minimal claudlobby root with a working spin-up-bot.sh stub."""
    root = tmp_path / "claudlobby"
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "eng.md").write_text(
        "---\ntitle: Engineering\ndescription: Software engineering\n---\n# Engineering\nBuild software.\n"
    )
    lib_dir = root / "lib"
    lib_dir.mkdir()
    # Create a stub spin-up-bot.sh that exits 0 (tests enrollment path)
    stub = lib_dir / "spin-up-bot.sh"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    (root / "voices").mkdir()
    (root / "templates").mkdir()
    # Minimal template
    (root / "templates" / "claude.md.j2").write_text("# {{ bot.name }}\n")
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
        import subprocess

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

    def test_cleanup_removes_source(self, tmp_path: Path):
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

    def test_enrollment_failure_returns_nonzero(self, tmp_path: Path):
        """spin-up-bot.sh failure should cause move-bot to return 1."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        # Replace stub with one that fails
        stub = root / "lib" / "spin-up-bot.sh"
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
        target = local / "fleet-b"
        target.mkdir(parents=True, exist_ok=True)
        (target / "fleet.yaml").write_text(
            "fleet:\n  name: fleet-b\n"
            "  service_prefix: com.test\n  bots:\n"
            "    mybot:\n"
            "      expertise: [nonexistent_expertise]\n"
            "      telegram:\n        handle: mybot\n"
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

    def test_kills_source_tmux_server(self, tmp_path: Path, monkeypatch):
        """move-bot --apply tears down the source bot's per-bot tmux server so the
        move doesn't strand an orphaned server on the source host."""
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["mybot"])
        _scaffold_fleet(local, "fleet-b", ["mybot"], create_bot_dirs=False)

        import claudlobby.commands.move_bot as move_bot_mod

        calls: list[list[str]] = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        monkeypatch.setattr(move_bot_mod.subprocess, "run", fake_run)

        # --force skips the active-session pre-flight (the mock would otherwise
        # report a live session and abort before teardown).
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
        assert ["tmux", "-L", "com.test.mybot", "kill-server"] in calls
