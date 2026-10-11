"""Explicit GitHub connection policy; no credentials are read or minted here."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import BotConfig, FleetConfig

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Repository permissions accepted by this local contract. Organization/account
# permissions cannot be narrowed with a repository token request.
REPOSITORY_PERMISSIONS = frozenset(
    {
        "actions",
        "administration",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "environments",
        "issues",
        "metadata",
        "packages",
        "pages",
        "pull_requests",
        "repository_hooks",
        "repository_projects",
        "secret_scanning_alerts",
        "security_events",
        "statuses",
        "vulnerability_alerts",
        "workflows",
    }
)
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


LEGACY_GITHUB_EQUIPMENT = frozenset({"github", "github-app"})


def uses_named_github(fleet: FleetConfig | None, bot: BotConfig) -> bool:
    """The fleet declaration opts every bot in, including unassigned bots."""
    return bool(
        getattr(fleet, "github_connections_enabled", False)
        or getattr(bot, "github_assignments", {})
    )


class GithubBindingError(ValueError):
    """A declared route cannot authorize the requested repository."""


@dataclass(frozen=True)
class GithubConnection:
    connection_id: str
    name: str
    mode: str
    app_id: int
    installation_id: int
    permissions: tuple[tuple[str, str], ...]
    credential_refs: tuple[tuple[str, str], ...]
    repos: tuple[str, ...] = ()
    enabled: bool = True
    slug: str | None = None
    bot_user_id: int | None = None


@dataclass(frozen=True)
class GithubAttachment:
    attachment_id: str
    connection: str
    enabled: bool = True


@dataclass(frozen=True)
class GithubAssignment:
    assignment_id: str
    attachment: str
    project: str
    repos: tuple[str, ...] = ()
    permissions: tuple[tuple[str, str], ...] | None = None
    enabled: bool = True


@dataclass(frozen=True)
class GithubBinding:
    connection_id: str
    attachment_id: str
    assignment_id: str
    bot_id: str
    project: str
    repo: str
    mode: str
    app_id: int
    installation_id: int
    permissions: tuple[tuple[str, str], ...]
    credential_refs: tuple[tuple[str, str], ...]
    revision: str
    slug: str | None = None
    bot_user_id: int | None = None


def _mapping(raw, where):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a mapping")
    return raw


def _record(raw, allowed, where):
    raw = _mapping(raw, where)
    if set(raw) - allowed:
        raise ValueError(f"{where}: unknown keys {sorted(set(raw) - allowed)}")
    return raw


def _identifier(value, where):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{where} must be a stable identifier")
    return value


def _enabled(raw, where):
    value = raw.get("enabled", True)
    if not isinstance(value, bool):
        raise ValueError(f"{where}.enabled must be a boolean")
    return value


def canonical_github_repo(value: str) -> str:
    """Only registry-style owner/repo names, case insensitive as GitHub is."""
    if (
        not isinstance(value, str)
        or not _REPO.fullmatch(value)
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise GithubBindingError(
            f"invalid canonical GitHub repository {value!r}; use owner/repo"
        )
    return value.lower()


def _repos(raw, where):
    if not isinstance(raw, list) or any(not isinstance(r, str) for r in raw):
        raise ValueError(f"{where}.repos must be a list of owner/repo names")
    return tuple(sorted({canonical_github_repo(r) for r in raw}))


def _permissions(raw, where):
    raw = _mapping(raw, where)
    if not raw:
        raise ValueError(f"{where} must declare at least one permission")
    for key, value in raw.items():
        if not isinstance(key, str) or key not in REPOSITORY_PERMISSIONS:
            raise ValueError(f"{where}: unsupported repository permission {key!r}")
        if (
            not isinstance(value, str)
            or value not in {"read", "write"}
            or (key == "metadata" and value != "read")
        ):
            raise ValueError(
                f"{where}: permission {key!r} must be read or write (metadata is read only)"
            )
    return tuple(sorted(raw.items()))


def parse_github_policy(raw):
    """Parse fleet.github; omission leaves the legacy mode entirely unchanged."""
    # Other fleet.github fields (for example mention_allowlist) predate this
    # contract and remain owned by their existing readers.
    raw = _mapping(raw, "fleet.github")
    connections = {}
    for key, value in _mapping(
        raw.get("connections"), "fleet.github.connections"
    ).items():
        where = f"GitHub connection {key!r}"
        _identifier(key, where)
        value = _record(
            value,
            {
                "name",
                "mode",
                "app_id",
                "installation_id",
                "permissions",
                "credential_refs",
                "repos",
                "enabled",
                "slug",
                "bot_user_id",
            },
            where,
        )
        mode = value.get("mode")
        if not isinstance(mode, str) or mode not in {"managed", "independent"}:
            raise ValueError(f"{where}.mode must be managed or independent")
        for identity in ("app_id", "installation_id"):
            if type(value.get(identity)) is not int or value[identity] <= 0:
                raise ValueError(f"{where}.{identity} must be a positive integer")
        refs = _mapping(value.get("credential_refs"), f"{where}.credential_refs")
        expected = (
            {"private_key_path_env"}
            if mode == "independent"
            else {"issuer_url_env", "issuer_token_env"}
        )
        if set(refs) != expected or any(
            not isinstance(v, str) or not _ENV.fullmatch(v) for v in refs.values()
        ):
            raise ValueError(
                f"{where}.credential_refs must name environment variables for {sorted(expected)}"
            )
        # Import at parse time: config imports this module for its dataclasses.
        # A pasted GitHub token can itself be a valid shell identifier, so the
        # existing config guard is needed before refs reach generated files.
        from .config import _GITHUB_TOKEN_PREFIXES

        if any(v.lower().startswith(_GITHUB_TOKEN_PREFIXES) for v in refs.values()):
            raise ValueError(
                f"{where}.credential_refs looks like a TOKEN VALUE, not an env var name; put the value in .env"
            )
        name = value.get("name", key)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}.name must be nonempty text")
        slug, bot_user_id = value.get("slug"), value.get("bot_user_id")
        if slug is not None and (
            not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug)
        ):
            raise ValueError(f"{where}.slug must be a GitHub App slug")
        if bot_user_id is not None and (
            type(bot_user_id) is not int or bot_user_id <= 0
        ):
            raise ValueError(f"{where}.bot_user_id must be a positive integer")
        if (slug is None) != (bot_user_id is None):
            raise ValueError(f"{where}: slug and bot_user_id must be supplied together")
        connections[key] = GithubConnection(
            key,
            name,
            mode,
            value["app_id"],
            value["installation_id"],
            _permissions(value.get("permissions"), f"{where}.permissions"),
            tuple(sorted(refs.items())),
            _repos(value.get("repos", []), where),
            _enabled(value, where),
            slug,
            bot_user_id,
        )
    attachments = {}
    for key, value in _mapping(
        raw.get("attachments"), "fleet.github.attachments"
    ).items():
        where = f"GitHub attachment {key!r}"
        _identifier(key, where)
        value = _record(value, {"connection", "enabled"}, where)
        attachments[key] = GithubAttachment(
            key,
            _identifier(value.get("connection"), f"{where}.connection"),
            _enabled(value, where),
        )
    return connections, attachments


def parse_github_assignments(raw, *, where):
    assignments = {}
    for key, value in _mapping(raw, f"{where}.github_assignments").items():
        label = f"{where}: GitHub assignment {key!r}"
        _identifier(key, label)
        value = _record(
            value, {"attachment", "project", "repos", "permissions", "enabled"}, label
        )
        assignments[key] = GithubAssignment(
            key,
            _identifier(value.get("attachment"), f"{label}.attachment"),
            _identifier(value.get("project"), f"{label}.project"),
            _repos(value.get("repos", []), label),
            _permissions(value["permissions"], f"{label}.permissions")
            if "permissions" in value
            else None,
            _enabled(value, label),
        )
    return assignments


def _assignment_repos(fleet, assignment):
    project = fleet.projects.get(assignment.project)
    if project is None:
        raise GithubBindingError(
            f"assignment {assignment.assignment_id!r}: unregistered project {assignment.project!r}"
        )
    registered = {canonical_github_repo(r) for r in project.repos}
    selected = set(assignment.repos) if assignment.repos else registered
    if not selected or not selected <= registered:
        raise GithubBindingError(
            f"assignment {assignment.assignment_id!r}: repositories must be a nonempty subset of registered project {assignment.project!r}"
        )
    return selected


def _route(fleet, assignment):
    attachment = fleet.github_attachments.get(assignment.attachment)
    if attachment is None:
        raise GithubBindingError(
            f"assignment {assignment.assignment_id!r}: missing attachment {assignment.attachment!r}"
        )
    connection = fleet.github_connections.get(attachment.connection)
    if connection is None:
        raise GithubBindingError(
            f"attachment {attachment.attachment_id!r}: missing connection {attachment.connection!r}"
        )
    return attachment, connection


def _effective_permissions(assignment, connection):
    permissions = (
        assignment.permissions
        if assignment.permissions is not None
        else connection.permissions
    )
    ceiling = dict(connection.permissions)
    for permission, level in permissions:
        if permission not in ceiling or (
            level == "write" and ceiling[permission] != "write"
        ):
            raise GithubBindingError(
                f"assignment {assignment.assignment_id!r}: permission {permission!r} exceeds connection policy"
            )
    return permissions


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def resolve_github_binding(
    fleet: FleetConfig, bot_name: str, repo: str
) -> GithubBinding:
    repo = canonical_github_repo(repo)
    bot = fleet.bots.get(bot_name)
    if bot is None:
        raise GithubBindingError(f"unknown bot {bot_name!r}")
    candidates = [
        a
        for a in bot.github_assignments.values()
        if repo in _assignment_repos(fleet, a)
    ]
    if len(candidates) != 1:
        raise GithubBindingError(
            f"bot {bot_name!r}, repo {repo!r}: {'missing' if not candidates else 'overlapping'} GitHub assignment"
        )
    assignment = candidates[0]
    attachment, connection = _route(fleet, assignment)
    if not (assignment.enabled and attachment.enabled and connection.enabled):
        raise GithubBindingError(
            f"bot {bot_name!r}, repo {repo!r}: disabled GitHub binding"
        )
    selected_repos = _assignment_repos(fleet, assignment)
    if connection.repos and not selected_repos <= set(connection.repos):
        raise GithubBindingError(
            f"assignment {assignment.assignment_id!r}: repository exceeds connection scope"
        )
    permissions = _effective_permissions(assignment, connection)
    policy = asdict(connection)
    policy.pop("name")  # display names never change authority
    revision = _digest(
        {
            "connection": policy,
            "attachment": asdict(attachment),
            "assignment": asdict(assignment),
            "registered_repos": sorted(selected_repos),
            "bot_id": bot.bot_id,
            "repo": repo,
        }
    )
    return GithubBinding(
        connection.connection_id,
        attachment.attachment_id,
        assignment.assignment_id,
        bot.bot_id,
        assignment.project,
        repo,
        connection.mode,
        connection.app_id,
        connection.installation_id,
        permissions,
        connection.credential_refs,
        revision,
        connection.slug,
        connection.bot_user_id,
    )


def github_bindings_for_bot(
    fleet: FleetConfig, bot_name: str
) -> tuple[GithubBinding, ...]:
    bot = fleet.bots.get(bot_name)
    if bot is None:
        raise GithubBindingError(f"unknown bot {bot_name!r}")
    repos = set()
    declared = set()
    for assignment in bot.github_assignments.values():
        selected = _assignment_repos(fleet, assignment)
        attachment, connection = _route(fleet, assignment)
        _effective_permissions(assignment, connection)
        if connection.repos and not selected <= set(connection.repos):
            raise GithubBindingError(
                f"assignment {assignment.assignment_id!r}: repository exceeds connection scope"
            )
        if declared & selected:
            raise GithubBindingError(
                f"bot {bot_name!r}: overlapping GitHub assignments for {sorted(declared & selected)}"
            )
        declared.update(selected)
        # Disabled policy is retained and checked, but carries no authority.
        # This lets a connection/attachment revoke all its bots in one plan.
        if assignment.enabled and attachment.enabled and connection.enabled:
            repos.update(selected)
    return tuple(resolve_github_binding(fleet, bot_name, r) for r in sorted(repos))


def github_policy_errors(fleet: FleetConfig) -> tuple[str, ...]:
    """Validate all references, including disabled retained declarations."""
    errors = []
    for attachment in fleet.github_attachments.values():
        if attachment.connection not in fleet.github_connections:
            errors.append(
                f"attachment {attachment.attachment_id!r}: missing connection {attachment.connection!r}"
            )
    for bot in fleet.bots.values():
        if (fleet.github_connections_enabled or bot.github_assignments) and (
            bot.github_app or bot.git_credentials
        ):
            errors.append(
                f"bot {bot.bot_id!r}: named GitHub routing cannot coexist with legacy github_app or git_credentials"
            )
        seen = set()
        for assignment in bot.github_assignments.values():
            try:
                repos = _assignment_repos(fleet, assignment)
                _, connection = _route(fleet, assignment)
                _effective_permissions(assignment, connection)
                if connection.repos and not repos <= set(connection.repos):
                    raise GithubBindingError(
                        f"assignment {assignment.assignment_id!r}: repository exceeds connection scope"
                    )
                if seen & repos:
                    raise GithubBindingError(
                        f"bot {bot.bot_id!r}: overlapping GitHub assignments for {sorted(seen & repos)}"
                    )
                seen.update(repos)
            except GithubBindingError as exc:
                errors.append(str(exc))
    return tuple(errors)


def validate_github_connection_identities(
    previous: dict[str, GithubConnection], current: dict[str, GithubConnection]
) -> None:
    """Compare retained connection records within one fleet's history."""
    for key in previous.keys() & current.keys():
        before, after = previous[key], current[key]
        if (before.mode, before.app_id, before.installation_id) != (
            after.mode,
            after.app_id,
            after.installation_id,
        ):
            raise GithubBindingError(
                f"connection {key!r}: App/installation identity is immutable; create a new connection ID"
            )


def validate_github_connection_history(
    previous: FleetConfig, current: FleetConfig
) -> None:
    """An activation/history caller must supply the same fleet's accepted snapshot."""
    if previous.name != current.name:
        return
    validate_github_connection_identities(
        previous.github_connections, current.github_connections
    )
