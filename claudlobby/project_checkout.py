"""Prepare a bot's selected repository; never update or replace existing work.

Callers hold mutation_admission and supply an activated Context. Credentials and
repository scope remain owned by the named GitHub binding and native helper.
Cloning neither trusts project code nor runs its setup scripts or submodules.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from .composer import GITHUB_BINDINGS_FILENAME, compose_github_bindings
from .env_tiers import resolve
from .github_connections import resolve_github_binding


class CheckoutError(ValueError):
    pass


def _directory(path, *, create=False):
    if path.is_symlink() or path.resolve() != path:
        raise CheckoutError("repository directory is redirected")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise CheckoutError("repository directory is unavailable")


def _target(context, bot, repository):
    binding = resolve_github_binding(context.fleet, bot, repository)
    root = context.paths.bot_runtime(bot)
    _directory(root)
    return binding, root, root / "projects" / binding.repo.split("/", 1)[1]


def _environment(context, bot, root, binding):
    _document, digest = compose_github_bindings(context.fleet.bots[bot], context.fleet)
    path = root / GITHUB_BINDINGS_FILENAME
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise CheckoutError("GitHub configuration differs from the active plan; activate the reviewed configuration first")
    # A small child environment prevents operator Git rewrites, helpers and
    # authentication headers from becoming an alternative identity.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
           "LANG": "C.UTF-8"}
    resolved = resolve(context.paths, bot_name=bot)
    declared = context.fleet.bots[bot].env
    for name in dict(binding.credential_refs).values():
        if name.startswith("GIT_") or name in {"PATH", "HOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"}:
            raise CheckoutError("credential reference conflicts with a process control variable")
        value = declared.get(name, resolved[name].value if name in resolved else os.environ.get(name))
        if value is not None:
            env[name] = str(value)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "CLAUDLOBBY_GITHUB_STRICT": "1",
                "CLAUDLOBBY_GITHUB_BINDINGS": str(path),
                "CLAUDLOBBY_GITHUB_BINDING_SHA256": digest,
                "CLAUDLOBBY_GITHUB_REVISION": binding.revision})
    return env


def _git(args, *, env, cwd=None, timeout=120):
    try:
        return subprocess.run(["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckoutError("Git could not finish repository preparation; inspect the checkout before retrying") from exc


def _existing(target, repository, env):
    if not target.exists() and not target.is_symlink():
        return False
    _directory(target)
    git_dir = target / ".git"
    _directory(git_dir)
    config = git_dir / "config"
    if config.is_symlink() or not config.is_file():
        raise CheckoutError("existing checkout has no regular Git configuration")
    result = _git(["config", "--file", str(config), "--no-includes", "--get-all", "remote.origin.url"], env=env)
    allowed = {f"https://github.com/{repository}", f"https://github.com/{repository}.git"}
    if result.returncode or result.stdout.strip().lower() not in allowed:
        raise CheckoutError("existing checkout has a different or ambiguous origin; it was left untouched")
    result = _git(["rev-parse", "--is-inside-work-tree"], env=env, cwd=target)
    if result.returncode or result.stdout.strip() != "true":
        raise CheckoutError("existing directory is not a usable Git checkout; it was left untouched")
    return True


@contextmanager
def _locked(root):
    data = root / "data"
    _directory(data)
    fd = os.open(data / ".repository-prepare.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CheckoutError("this bot is already preparing a repository") from exc
        yield
    finally:
        os.close(fd)


def prepare_checkout(context, bot, repository):
    """Idempotent clone to projects/REPO using one assigned connection."""
    binding, root, target = _target(context, bot, repository)
    repository = binding.repo
    with _locked(root):
        env = _environment(context, bot, root, binding)
        if _existing(target, repository, env):
            return {"bot": bot, "repository": repository, "path": str(target), "state": "existing"}
        _directory(target.parent, create=True)
        # Only our scratch is cleaned. Existing work is never fetched, reset,
        # deleted, trusted or passed to package-manager install commands.
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=target.parent) as scratch:
            scratch = Path(scratch)
            template = scratch / "empty-template"
            template.mkdir()
            checkout = scratch / "checkout"
            helper = context.paths.lib / "git-credential-github-app"
            result = _git(["-c", "credential.helper=", "-c", f"credential.helper=!{shlex.quote(str(helper))}",
                           "-c", "credential.useHttpPath=true", "-c", "http.followRedirects=false",
                           "-c", f"core.hooksPath={os.devnull}", "clone", "--no-recurse-submodules",
                           f"--template={template}", "--", f"https://github.com/{repository}.git", str(checkout)], env=env)
            if result.returncode:
                raise CheckoutError("repository clone failed; check the selected connection and repository access")
            _directory(target.parent)
            if target.exists() or target.is_symlink():
                raise CheckoutError("checkout appeared during preparation; it was left untouched")
            # Recheck the pinned grant before publishing the working copy.
            _environment(context, bot, root, binding)
            os.rename(checkout, target)
        return {"bot": bot, "repository": repository, "path": str(target), "state": "prepared"}
