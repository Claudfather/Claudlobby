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

    for bot in bots:
        bot_dir = fleet_dir / "runtime" / "bots" / bot
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "bot.conf").write_text(
            f"BOT_NAME={bot}\nBOT_SERVICE={service_prefix}.{bot}\n"
        )

    return fleet_dir


def _scaffold_root(tmp_path: Path) -> Path:
    """Create a minimal claudlobby root."""
    root = tmp_path / "claudlobby"
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "eng.md").write_text(
        "---\ntitle: Engineering\ndescription: Software engineering\n---\n# Engineering\nBuild software.\n"
    )
    (root / "lib").mkdir()
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
        target = local / "fleet-b"
        target.mkdir(parents=True)
        (target / "fleet.yaml").write_text(
            "fleet:\n  name: fleet-b\n  service_prefix: com.test\n  bots:\n"
            "    mybot:\n      expertise: [eng]\n      telegram:\n        handle: mybot\n"
        )

        rc = main(["--root", str(root), "move-bot", "mybot", "--to", "fleet-b"])
        assert rc == 0  # dry run succeeds

    def test_error_bot_not_found(self, tmp_path: Path):
        root = _scaffold_root(tmp_path)
        local = root / "local"
        _scaffold_fleet(local, "fleet-a", ["other"])
        _scaffold_fleet(local, "fleet-b", ["ghost"])

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
        # Both have bot.conf so autodetect finds both
        # But fleet-b also has the stanza so it would succeed as target
        # The ambiguity error should fire first
        rc = main(["--root", str(root), "move-bot", "mybot", "--to", "fleet-b"])
        # Should be ambiguous (both fleets have the bot dir)
        # Actually fleet-a source → fleet-b target works since fleet-b has 'mybot' stanza
        # autodetect picks the only one NOT equal to target... let me check
        # No — autodetect doesn't know the target yet. It finds both fleet-a and fleet-b.
        assert rc == 1  # ambiguous

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
        # Initialize git repo with dirty state
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
        _scaffold_fleet(local, "fleet-b", ["mybot"])

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
        _scaffold_fleet(local, "fleet-b", ["mybot"])

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
