"""Lossless-restart age gate + manager detection (lib-common.sh helpers).

These back Mechanism 2 of the fleet update lifecycle:

- ``iso_to_epoch`` / ``session_md_handoff_epoch`` / ``should_resume_session`` —
  the F6 age gate that decides whether ``start-bot.sh`` resumes from a
  ``session.md`` handoff or clean-starts a stale one. It keys off the doc-level
  ``last_updated`` ISO-8601 UTC frontmatter field (robust to file touches the
  way mtime is not), falling back to mtime only for legacy artifacts.
- ``bot_is_manager`` — the F5 guard ``weekly-worker-restart.sh`` uses to skip
  managers (and ``update-claude-code.sh`` to address failure notifications).
  Managers carry ``MANAGER_TMUX == BOT_ID`` plus an inline comment that
  ``bot_conf_get`` does not strip; getting this wrong bounces a manager.

CI runs pytest only, so these bash helpers are exercised here via subprocess —
without this wrapper the logic would be untested in CI.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_COMMON = REPO_ROOT / "lib" / "lib-common.sh"


def _run(snippet: str, *args: str) -> tuple[str, int]:
    """Source lib-common.sh, run a snippet with positional args $2.., return (stdout, rc)."""
    proc = subprocess.run(
        ["bash", "-c", f'. "$1"; {snippet}', "_", str(LIB_COMMON), *args],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip(), proc.returncode


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_md(tmp_path: Path, name: str, last_updated: str | None) -> Path:
    f = tmp_path / name
    if last_updated is None:
        f.write_text("## State\nbranch: x\n")  # legacy: no frontmatter
    else:
        f.write_text(
            f"---\ncwd: /x\nlast_updated: {last_updated}\nschema_version: 2\n---\n"
        )
    return f


class TestIsoToEpoch:
    def test_known_value(self):
        out, rc = _run('iso_to_epoch "$2"', "2026-05-15T14:30:00Z")
        assert rc == 0
        expected = int(datetime(2026, 5, 15, 14, 30, tzinfo=timezone.utc).timestamp())
        assert out == str(expected)

    def test_empty_is_error(self):
        out, rc = _run('iso_to_epoch "$2"', "")
        assert rc != 0
        assert out == ""


class TestShouldResumeSession:
    """F6 age gate: resume from a fresh checkpoint, clean-start a stale one."""

    def test_fresh_resumes(self, tmp_path):
        f = _session_md(
            tmp_path, "fresh.md", _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        )
        _, rc = _run('should_resume_session "$2" "$3"', str(f), "86400")
        assert rc == 0  # resume

    def test_stale_skips(self, tmp_path):
        f = _session_md(
            tmp_path, "stale.md", _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        )
        _, rc = _run('should_resume_session "$2" "$3"', str(f), "86400")
        assert rc == 1  # skip -> clean start

    def test_ancient_skips(self, tmp_path):
        f = _session_md(tmp_path, "ancient.md", "2020-01-01T00:00:00Z")
        _, rc = _run('should_resume_session "$2" "$3"', str(f), "86400")
        assert rc == 1

    def test_missing_skips(self, tmp_path):
        _, rc = _run(
            'should_resume_session "$2" "$3"', str(tmp_path / "nope.md"), "86400"
        )
        assert rc == 1

    def test_legacy_no_field_uses_mtime(self, tmp_path):
        # A freshly written legacy file (no last_updated) falls back to mtime,
        # which is "now" -> fresh -> resume.
        f = _session_md(tmp_path, "legacy.md", None)
        _, rc = _run('should_resume_session "$2" "$3"', str(f), "86400")
        assert rc == 0


class TestHandoffEpoch:
    def test_prefers_last_updated_over_mtime(self, tmp_path):
        # last_updated is 2020; the file mtime is "now" — the field must win.
        f = _session_md(tmp_path, "s.md", "2020-01-01T00:00:00Z")
        out, rc = _run('session_md_handoff_epoch "$2"', str(f))
        assert rc == 0
        assert out == str(int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()))


class TestBotIsManager:
    """F5 guard: MANAGER_TMUX == BOT_ID marks a manager (never auto-restarted)."""

    def _bot(self, tmp_path, name, conf):
        d = tmp_path / name
        d.mkdir()
        (d / "bot.conf").write_text(conf)
        return d

    def test_manager_with_inline_comment(self, tmp_path):
        d = self._bot(
            tmp_path,
            "ari",
            "export BOT_ID=ari\nexport MANAGER_TMUX=ari  # this bot is a manager\n",
        )
        _, rc = _run('bot_is_manager "$2"', str(d))
        assert rc == 0

    def test_worker(self, tmp_path):
        d = self._bot(
            tmp_path, "astrid", "export BOT_ID=astrid\nexport MANAGER_TMUX=ari\n"
        )
        _, rc = _run('bot_is_manager "$2"', str(d))
        assert rc == 1

    def test_worker_double_quoted(self, tmp_path):
        d = self._bot(tmp_path, "val", 'BOT_ID="valbot"\nMANAGER_TMUX="valmgr"\n')
        _, rc = _run('bot_is_manager "$2"', str(d))
        assert rc == 1
