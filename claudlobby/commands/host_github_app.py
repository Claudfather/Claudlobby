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
    if args.public_command.startswith("host.github-app.issuer."):
        return _issuer(args)
    try:
        paths = resolve_paths(root=args.root)
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid host root selector") from exc
    if args.public_command == "host.github-app.setup":
        return _setup(args, paths.root, paths.lib)
    if args.public_command == "host.github-app.token":
        return _token(paths.root, paths.lib)
    raise CommandFailure("invalid_argument", "unsupported GitHub App command")


def _issuer(args) -> CommandOutput:
    import json
    import sqlite3
    from ..github_issuer import GrantStore, GithubMinter, IssuanceDenied, IssuerUnavailable, _private_file, create_app

    try:
        store = GrantStore(args.store)
        consent = None
        if getattr(args, "consent_config", None):
            from ..github_consent import ConsentFlow, GithubConsentProvider
            config = json.loads(_private_file(args.consent_config).read_text())
            consent = ConsentFlow(store, GithubConsentProvider(config))
        if args.issuer_command in ("approve", "invite"):
            grant = json.loads(_private_file(args.grant_file).read_text())
            credential = _private_file(args.credential_file).read_text().strip()
            result = consent.invite(grant, credential) if args.issuer_command == "invite" else store.approve(grant, credential)
        elif args.issuer_command == "cancel":
            from ..github_consent import ConsentFlow
            result = ConsentFlow(store, None).cancel(args.ticket)
        elif args.issuer_command == "revoke":
            result = store.revoke(args.grant_id)
        else:
            import uvicorn
            if not 1 <= args.port <= 65535:
                raise ValueError("invalid port")
            keys = {}
            for declaration in args.app_key:
                app_id, separator, path = declaration.partition("=")
                if not separator or not app_id.isdecimal() or int(app_id) <= 0 or int(app_id) in keys:
                    raise ValueError("invalid App key mapping")
                keys[int(app_id)] = _private_file(path)
            store.initialize()
            if consent and consent.provider.app_id not in keys:
                raise ValueError("consent App has no issuer key")
            webhooks = None
            if args.webhook_secret:
                from ..github_webhooks import GithubWebhooks
                secrets = {}
                for declaration in args.webhook_secret:
                    app_id, separator, path = declaration.partition("=")
                    if not separator or not app_id.isdecimal() or int(app_id) not in keys or int(app_id) in secrets:
                        raise ValueError("invalid webhook secret mapping")
                    secrets[int(app_id)] = path
                webhooks = GithubWebhooks(store, secrets)
            uvicorn.run(create_app(store, GithubMinter(keys), consent, webhooks), host="127.0.0.1", port=args.port,
                        access_log=False, date_header=False)
            result = {"issuer": "stopped"}
    except ImportError as exc:
        raise CommandFailure("unavailable", "Install the plane-ui extra to run the issuer") from exc
    except (IssuanceDenied, ValueError) as exc:
        raise CommandFailure("invalid_argument", "Invalid issuer request; check the grant, expiry and credential files") from exc
    except (IssuerUnavailable, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "Issuer state unavailable; check private files and store") from exc
    return CommandOutput(result, lines=(json.dumps(result, sort_keys=True),))


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
