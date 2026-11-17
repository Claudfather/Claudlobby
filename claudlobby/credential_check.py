"""Run the selected fleet's existing native credential probe once."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess

from .context import native_environment
from .mcp_resolve import required_vars
from .operation_context import resolve_operation_scope
from .runtime_admission import mutation_admission, runtime_release_id, runtime_provenance, bound_runtime_release


class CredentialCheckError(RuntimeError):
    def __init__(self, reason: str, *, effect_attempted: bool = False):
        self.effect_attempted = effect_attempted
        super().__init__(reason)


#: One `--declares` value: a credential's variable name, then the library surface
#: declaring it (`mcp/<name>`, `integration/<name>`). The probe's parser holds the
#: same shape (cli-context.sh); checking it here first names a value it would
#: refuse, where its refusal would read as a probe that may have run.
_DECLARATION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9][A-Za-z0-9._/-]*")


def fleet_declarations(fleet, paths) -> list[str]:
    """``VAR=SURFACE`` for each credential the activated roster declares (#1213).

    The walk is ``mcp_resolve.required_vars``, the one the reconcile and doctor
    doors read through ``credentials.declared_for_fleet``: each bot's MCP
    fragments and integrations, instance names applied. That door skips a bot
    whose walk raises, and a skipped bot would read here as declaring nothing,
    so its credentials would go unprobed with no word said. This refuses
    instead.
    """
    declared: set[str] = set()
    for name in sorted(fleet.bots):
        try:
            needed = required_vars(fleet.bots[name], paths)
        except Exception as exc:  # noqa: BLE001 - any failure leaves them unknown
            raise CredentialCheckError(
                f"the credentials bot {name} declares cannot be read: {exc}") from exc
        declared.update(f"{req.name}={req.origin}" for req in needed)
    for item in sorted(declared):
        if not _DECLARATION.fullmatch(item):
            raise CredentialCheckError(f"a declaration the probe cannot take: {item!r}")
    return sorted(declared)


@dataclass(frozen=True)
class CredentialCheckResult:
    fleet: str
    release_id: str | None
    state_path: Path
    fleet_source_changed: bool = False
    checks: str = "tick_completed"
    health: str = "unobserved"
    source_revision: str | None = None
    config_fingerprint: str | None = None


def check_credentials(*, root: Path, fleet: str | None) -> CredentialCheckResult:
    """Run the probe owner; a completed tick is not a claim that tokens work."""
    with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        destination, origin = resolve_operation_scope(root=root, fleet=fleet)
        if origin is not None and origin.bot_id != destination.fleet.manager:
            raise CredentialCheckError("only the selected fleet manager may check its credentials")
        bound_runtime_release(destination.paths.package, generated=origin is not None)
        native = destination.paths.lib / "creds-check.sh"
        if native != destination.paths.package.runtime_native / "creds-check.sh":
            raise CredentialCheckError("credential probe differs from the selected release")

        # Pending authored edits do not replace the committed roster or alert scope.
        frozen = release.snapshot["fleets"][destination.fleet.name]
        manifest = Path(frozen["manifest"])
        if manifest != destination.paths.fleet_yaml:
            raise CredentialCheckError("fleet source differs from its committed configuration")
        try:
            source_changed = manifest.is_symlink() or manifest.resolve() != manifest
            if not source_changed:
                expected = frozen["fleet"].encode()
                fd = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as source:
                    source_changed = (not stat.S_ISREG(os.fstat(source.fileno()).st_mode)
                                      or source.read(len(expected) + 1) != expected)
        except OSError:
            source_changed = True
        if destination.paths.runtime_bots.resolve() != destination.paths.runtime_bots:
            raise CredentialCheckError("fleet bot runtime is redirected")

        declarations = fleet_declarations(destination.fleet, destination.paths)
        state_path = root / "state/creds-check" / f"{destination.fleet.name}.json"
        log_path = root / "state/logs/creds-check.log"
        env = {**os.environ, **native_environment(destination.paths, current=release),
               "CLAUDLOBBY_FLEET": destination.fleet.name,
               "CLAUDLOBBY_CREDS_LOG": str(log_path),
               "CLAUDLOBBY_CREDS_STATE": str(state_path)}
        if destination.fleet.telegram_group_chat_id:
            env["TELEGRAM_GROUP_CHAT_ID"] = str(destination.fleet.telegram_group_chat_id)
        else:
            env.pop("TELEGRAM_GROUP_CHAT_ID", None)
        command = [str(native), "--admitted-runtime",
                   "--fleet", destination.fleet.name,
                   "--fleet-root", str(destination.paths.fleet_config_dir),
                   "--bots-dir", str(destination.paths.runtime_bots)]
        for bot in sorted(destination.fleet.bots):
            command.extend(("--bot", bot))
        for item in declarations:
            command.extend(("--declares", item))
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True,
                                    check=False, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CredentialCheckError(f"credential probe outcome is unknown; inspect {state_path} "
                                       f"and {log_path}", effect_attempted=True) from exc
        if result.returncode or result.stdout != "tick-complete\n":
            # Probe failures, alert attempts and state writes may already have
            # happened. Never auto-retry a possibly delivered alert.
            raise CredentialCheckError(f"credential probe outcome is unverified; inspect {state_path} "
                                       f"and {log_path}", effect_attempted=True)
        return CredentialCheckResult(destination.fleet.name, runtime_release_id(release), state_path,
                                     fleet_source_changed=source_changed, **runtime_provenance(release))
