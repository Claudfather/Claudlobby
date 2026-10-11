"""Nonsecret repository setup inputs; selection is never an authorization grant."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .github_connections import canonical_github_repo, parse_github_policy
from .plane.workspace_assertion import _origin, _unique_object

PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}


def repository_input(value):
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("use a GitHub repository URL or owner/repo")
    value = value.strip()
    if value.startswith("https://"):
        url = urlsplit(value)
        if url.netloc != "github.com" or url.query or url.fragment:
            raise ValueError("use an exact github.com repository URL")
        value = url.path.removeprefix("/").removesuffix("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("use a GitHub repository URL or owner/repo")
    return canonical_github_repo(value)


def selection(value):
    if (not isinstance(value, dict) or set(value) != {"version", "issuer_origin", "app_id",
            "installation_id", "repository", "repository_id", "permissions"}
            or type(value["version"]) is not int or value["version"] != 1
            or not _origin(value["issuer_origin"], False)
            or any(type(value[k]) is not int or not 0 < value[k] < 2**53
                   for k in ("app_id", "installation_id", "repository_id"))
            or repository_input(value["repository"]) != value["repository"]
            or value["permissions"] != PERMISSIONS):
        raise ValueError("invalid repository selection")
    return json.loads(json.dumps(value))


def read_selection(path, issuer_origin):
    # This is untrusted configuration, displayed for explicit local approval.
    # Provider consent later rechecks these identities; the file grants nothing.
    with Path(path).expanduser().open("rb") as stream:
        content = stream.read(16385)
    if len(content) > 16384:
        raise ValueError("repository selection is oversized")
    selected = selection(json.loads(content, object_pairs_hook=_unique_object))
    if not _origin(issuer_origin, False) or selected["issuer_origin"] != issuer_origin:
        raise ValueError("selection does not match the locally trusted issuer origin")
    policy = {"connections": {"managed": {"name": "Managed GitHub", "mode": "managed",
        "app_id": selected["app_id"], "installation_id": selected["installation_id"],
        "permissions": selected["permissions"], "repos": [selected["repository"]],
        "credential_refs": {"issuer_url_env": "TEAM_GITHUB_ISSUER_URL", "issuer_token_env": "TEAM_GITHUB_ISSUER_TOKEN"}}},
        "attachments": {"team": {"connection": "managed"}}}
    parse_github_policy(policy)
    return selected["repository"], json.dumps(policy, sort_keys=True, separators=(",", ":")), {
        "issuer_origin": selected["issuer_origin"], "repository_id": selected["repository_id"]}
