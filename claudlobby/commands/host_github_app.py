"""Public host entry points for the packaged GitHub App setup and mint owners."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from ..command_result import CommandFailure, CommandOutput


def _operator_shell() -> None:
    if any(name in os.environ for name in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE")):
        raise CommandFailure("conflict", "GitHub App host commands require an operator shell")


def dispatch(args) -> CommandOutput:
    from ..context import resolve_paths
    from ..paths import InvalidPathSelector

    _operator_shell()
    if args.seed or args.fleet:
        raise CommandFailure("invalid_argument", "GitHub App host commands do not select a fleet")
    try:
        paths = resolve_paths(root=args.root)
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid host root selector") from exc
    if args.public_command == "host.github-app.setup":
        return _setup(args, paths.root, paths.lib)
    if args.public_command == "host.github-app.token":
        return _token(paths.root, paths.lib)
    raise CommandFailure("invalid_argument", "unsupported GitHub App command")


def _invoke(script: Path, argv: list[str], root: Path, *, timeout: int) -> subprocess.CompletedProcess[str]:
    # The script owns env/config precedence. In particular, never ask ambient
    # git credential helpers to fill a host token.
    env = {**os.environ, "CLAUDLOBBY_ROOT": str(root)}
    try:
        return subprocess.run([str(script), *argv], env=env, text=True,
                              capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        # A timed-out setup may already have written its config; no retry.
        raise CommandFailure("unavailable", "GitHub App native outcome is unknown; inspect host config") from exc


def _setup(args, root: Path, native: Path) -> CommandOutput:
    argv = ["--app-id", args.app_id, "--installation-id", args.installation_id,
            "--private-key", args.private_key, "--slug", args.slug]
    if args.config_path:
        argv.extend(("--config-path", args.config_path))
    if args.no_write_config:
        argv.append("--no-write-config")
    result = _invoke(native / "setup-github-app.sh", argv, root, timeout=180)
    if result.returncode:
        # The script's stderr can contain provider output or a key path. Keep
        # its useful fixed diagnostics without reflecting untrusted bytes.
        if "Most likely causes" in result.stderr:
            message = "GitHub rejected the App JWT; check key, App ID, revocation and clock skew"
        elif "does not parse as a valid RSA private key" in result.stderr:
            message = "GitHub App private key is not valid RSA"
        elif "cannot read private key" in result.stderr:
            message = "GitHub App private key cannot be read"
        else:
            message = "GitHub App setup did not complete; inspect host config before retrying"
        raise CommandFailure("conflict", message,
                             hint="See documentation/runbooks/github-app-setup.md")
    return CommandOutput({"configured": not args.no_write_config,
                          "validation": "completed", "wiring": result.stdout},
                         lines=(result.stdout.rstrip("\n"),))


def _token(root: Path, native: Path) -> CommandOutput:
    result = _invoke(native / "mint-github-token.sh", [], root, timeout=60)
    token = result.stdout
    if result.returncode or not token.startswith("ghs_") or "\n" in token:
        raise CommandFailure("unavailable", "GitHub App mint failed; inspect native diagnostics")
    # The token command is the one deliberately secret-bearing public door.
    return CommandOutput({"token": token}, lines=(token,))
