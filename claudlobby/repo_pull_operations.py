"""Selected, operator-requested project pulls through the packaged native owner."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from .active_config import resolve_active_context
from .context import native_environment
from .runtime_admission import mutation_admission, runtime_release_id, runtime_provenance, bound_runtime_release


class RepositoryPullError(RuntimeError):
    def __init__(self, reason: str, *, effect_attempted: bool = False):
        self.effect_attempted = effect_attempted
        super().__init__(reason)


@dataclass(frozen=True)
class RepositoryPullResult:
    fleet: str
    bot: str
    release_id: str | None
    repositories: tuple[tuple[str, str], ...]
    source_revision: str | None = None
    config_fingerprint: str | None = None


def _reported(stdout: bytes) -> tuple[tuple[str, str], ...]:
    fields = stdout.split(b"\0")
    if not fields or fields[0] != b"git-pull-all-v1" or fields[-1] != b"" or len(fields) % 2 != 0:
        raise RepositoryPullError("native repository results are incomplete", effect_attempted=True)
    rows = []
    seen = set()
    for name_bytes, status_bytes in zip(fields[1:-1:2], fields[2:-1:2]):
        name = os.fsdecode(name_bytes)
        status = status_bytes.decode("ascii", errors="replace")
        if (not name or name in (".", "..") or "/" in name or name in seen
                or status not in {"updated", "unchanged", "skipped_dirty", "skipped_blocked",
                                  "skipped_redirected", "failed"}):
            raise RepositoryPullError("native repository results are invalid", effect_attempted=True)
        seen.add(name)
        rows.append((name, status))
    return tuple(rows)


def pull_repositories(root: Path, fleet: str, bot: str) -> RepositoryPullResult:
    """Pull one declared bot's immediate project repos; never choose a generic path."""
    with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        context = resolve_active_context(root=root, fleet=fleet, bot=bot)
        bound_runtime_release(context.paths.package, generated=False)
        if bot not in context.fleet.bots:
            raise RepositoryPullError("bot is not declared in the selected fleet")
        bot_dir = context.paths.bot_runtime(bot)
        projects = bot_dir / "projects"
        if (not bot_dir.is_dir() or bot_dir.resolve() != bot_dir
                or not projects.is_dir() or projects.resolve() != projects):
            raise RepositoryPullError("selected bot projects directory is absent or redirected")
        env = {**os.environ, **native_environment(context.paths, current=release),
               "FLEET_NAME": context.fleet.name}
        script = context.paths.package.runtime_native / "git-pull-all.sh"
        if not script.is_file() or script.resolve() != script:
            raise RepositoryPullError("selected repository updater is absent or redirected")
        for key in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE", "CLAUDLOBBY_FLEET"):
            env.pop(key, None)
        try:
            completed = subprocess.run([str(script), str(projects), "--status-nul"], env=env,
                                       capture_output=True, timeout=3600, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RepositoryPullError("repository pull outcome is unknown; inspect the bot log before retrying",
                                      effect_attempted=True) from exc
        rows = _reported(completed.stdout)
        failed = any(status == "failed" for _, status in rows)
        if completed.returncode not in (0, 1) or (completed.returncode == 1) != failed:
            raise RepositoryPullError("native repository result disagrees with its exit status",
                                      effect_attempted=True)
        return RepositoryPullResult(context.fleet.name, bot, runtime_release_id(release), rows, **runtime_provenance(release))
