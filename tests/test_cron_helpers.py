"""Tests for cron migration helpers in claudlobby/__main__.py."""
from __future__ import annotations

from pathlib import Path

from claudlobby.__main__ import (
    _BotCronCtx,
    _resolve_cron_path,
    _rewrite_cron_line,
    _verify_cron_paths,
)


def _make_ctx(tmp_path: Path, bot_name: str = "eng-1",
              legacy_prefix: str = "/home/user/bots/eng-1") -> _BotCronCtx:
    """Build a _BotCronCtx with real directories under tmp_path."""
    data_dir = tmp_path / "runtime" / "bots" / bot_name / "data"
    data_dir.mkdir(parents=True)
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(exist_ok=True)
    return _BotCronCtx(
        legacy_prefix=legacy_prefix,
        bot_name=bot_name,
        data_dir=data_dir,
        search_dirs=[
            lib_dir,
            lib_dir / "personal",
            data_dir / "scripts",
            data_dir,
        ],
    )


# ---------------------------------------------------------------------------
# _resolve_cron_path
# ---------------------------------------------------------------------------


class TestResolveCronPath:
    def test_non_matching_prefix_unchanged(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        assert _resolve_cron_path("/other/path/script.sh", ctx) == "/other/path/script.sh"

    def test_bare_prefix_returns_data_dir(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        result = _resolve_cron_path("/home/user/bots/eng-1", ctx)
        assert result == str(ctx.data_dir)

    def test_found_in_lib(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        # Create the script in lib/
        script = tmp_path / "lib" / "my-script.sh"
        script.write_text("#!/bin/bash\n")
        result = _resolve_cron_path("/home/user/bots/eng-1/my-script.sh", ctx)
        assert result == str(script)

    def test_found_in_data(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        script = ctx.data_dir / "cleanup.sh"
        script.write_text("#!/bin/bash\n")
        result = _resolve_cron_path("/home/user/bots/eng-1/cleanup.sh", ctx)
        assert result == str(script)

    def test_basename_fallback(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        # File exists only at basename in lib/
        script = tmp_path / "lib" / "rotate.sh"
        script.write_text("#!/bin/bash\n")
        result = _resolve_cron_path("/home/user/bots/eng-1/scripts/rotate.sh", ctx)
        assert result == str(script)

    def test_fallback_to_data_prefix(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        # Nothing exists — should fall back to data_dir/rel
        result = _resolve_cron_path("/home/user/bots/eng-1/missing.sh", ctx)
        assert result == str(ctx.data_dir / "missing.sh")


# ---------------------------------------------------------------------------
# _rewrite_cron_line
# ---------------------------------------------------------------------------


class TestRewriteCronLine:
    def test_no_match_returns_unchanged(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        line = "0 * * * * /usr/bin/echo hello"
        result, bot = _rewrite_cron_line(line, [ctx])
        assert result == line
        assert bot is None

    def test_rewrites_matching_path(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        script = tmp_path / "lib" / "sweep.sh"
        script.write_text("#!/bin/bash\n")
        line = f"0 3 * * * /home/user/bots/eng-1/sweep.sh --flag"
        result, bot = _rewrite_cron_line(line, [ctx])
        assert str(script) in result
        assert bot == "eng-1"
        assert "--flag" in result

    def test_rewrites_multiple_paths_in_line(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        s1 = tmp_path / "lib" / "a.sh"
        s1.write_text("")
        s2 = ctx.data_dir / "b.sh"
        s2.write_text("")
        line = f"*/5 * * * * /home/user/bots/eng-1/a.sh && /home/user/bots/eng-1/b.sh"
        result, bot = _rewrite_cron_line(line, [ctx])
        assert str(s1) in result
        assert str(s2) in result
        assert bot == "eng-1"

    def test_longest_prefix_first(self, tmp_path):
        """When two bots share a prefix, the longer one matches first."""
        ctx_short = _make_ctx(tmp_path, "eng", "/home/user/bots/eng")
        ctx_long = _make_ctx(tmp_path, "eng-1", "/home/user/bots/eng-1")
        script = ctx_long.data_dir / "task.sh"
        script.write_text("")
        line = "0 * * * * /home/user/bots/eng-1/task.sh"
        # Sorted longest-first as _build_cron_contexts does
        ctxs = sorted([ctx_short, ctx_long], key=lambda c: len(c.legacy_prefix), reverse=True)
        result, bot = _rewrite_cron_line(line, ctxs)
        assert bot == "eng-1"


# ---------------------------------------------------------------------------
# _verify_cron_paths
# ---------------------------------------------------------------------------


class TestVerifyCronPaths:
    def test_system_paths_skipped(self, tmp_path):
        rewrites = [(1, "old", "/usr/bin/bash /bin/echo hello", "eng-1")]
        broken = _verify_cron_paths(rewrites, tmp_path)
        assert broken == []

    def test_missing_script_flagged(self, tmp_path):
        missing = str(tmp_path / "runtime" / "bots" / "eng-1" / "gone.sh")
        rewrites = [(1, "old", f"0 * * * * {missing}", "eng-1")]
        broken = _verify_cron_paths(rewrites, tmp_path)
        assert len(broken) == 1
        assert broken[0][2] == missing

    def test_existing_path_passes(self, tmp_path):
        script = tmp_path / "lib" / "ok.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("")
        rewrites = [(1, "old", f"0 * * * * {script}", "eng-1")]
        broken = _verify_cron_paths(rewrites, tmp_path)
        assert broken == []
