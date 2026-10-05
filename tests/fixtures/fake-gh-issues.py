#!/usr/bin/env python3
"""A stand-in for `gh`, for the issue intake tests.

It answers the reads the intake makes from a JSON state file ($FAKE_GH_STATE):

- `gh issue list --repo R --state S --limit N --json FIELDS [--label L]`
- `gh issue view N --repo R --json FIELDS`
- `gh api repos/R/collaborators/LOGIN/permission`
- `gh api --paginate repos/R/issues/N/events?per_page=100`
- `gh api graphql -f query=... -F owner=O -F name=N -F number=K`

Each call's arguments are appended to $FAKE_GH_LOG, one call per line. Response
shapes mirror live captures from api.github.com and gh 2.92 (identifiers
replaced): the permission read carries `user.permissions`, a failed read prints
its error body on stdout and exits 1 as gh does, and a bot's login is spelled
`app/NAME` by `gh issue list`, `NAME[bot]` by REST and `NAME` by GraphQL. Any
other call, or a flag this stand-in does not model, exits 2, so a reader that
dropped a filter or read only the first page of events cannot pass here.

State file:
  issues: [{number, title, body, author, author_name, bot, labels, createdAt, state}]
  roles:  {LOGIN: admin|maintain|write|triage|read|none}   (absent = HTTP 404)
  events: {"N": [{event, actor, created_at, label?, rename_from?, rename_to?}]}
  edits:  {"N": {lastEditedAt, editor, editor_bot}, or a list of them}; GraphQL
          answers with the newest one's time and editor, as GitHub does
  fail:   {issue_list: bool, issue_view: bool, permission: [LOGIN], events: [N], graphql: [N]}
  raw_issue_list: text printed verbatim for `issue list` (a malformed reply)
"""

import json
import os
import re
import sys
from urllib.parse import unquote

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
with open(os.environ["FAKE_GH_STATE"], encoding="utf-8") as fh:
    state = json.load(fh)
fail = state.get("fail") or {}

PERMS = {
    "admin": dict(admin=True, maintain=True, push=True, triage=True, pull=True),
    "maintain": dict(admin=False, maintain=True, push=True, triage=True, pull=True),
    "write": dict(admin=False, maintain=False, push=True, triage=True, pull=True),
    "triage": dict(admin=False, maintain=False, push=False, triage=True, pull=True),
    "read": dict(admin=False, maintain=False, push=False, triage=False, pull=True),
    "none": dict(admin=False, maintain=False, push=False, triage=False, pull=False),
}
LEGACY = {
    "admin": "admin",
    "maintain": "write",
    "write": "write",
    "triage": "read",
    "read": "read",
    "none": "none",
}


def refuse(why: str) -> None:
    print(f"fake gh: {why}: {args}", file=sys.stderr)
    sys.exit(2)


def http_error(message: str, status: int, doc: str) -> None:
    print(
        json.dumps(
            {"message": message, "documentation_url": doc, "status": str(status)}
        )
    )
    print(f"gh: {message} (HTTP {status})", file=sys.stderr)
    sys.exit(1)


def rest_user(login: str) -> dict:
    return {
        "login": login,
        "id": 1,
        "node_id": "U_kgDOAAAAAQ",
        "type": "Bot" if login.endswith("[bot]") else "User",
        "site_admin": False,
        "user_view_type": "public",
    }


def flags(rest: list[str], known: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(rest):
        if rest[i] not in known or i + 1 >= len(rest):
            refuse(f"unmodelled flag {rest[i]!r}")
        out[rest[i]] = rest[i + 1]
        i += 2
    return out


def issue_list(rest: list[str]) -> None:
    opts = flags(rest, {"--repo", "--state", "--limit", "--json", "--label"})
    if "--json" not in opts or "--repo" not in opts:
        refuse("issue list without --repo and --json")
    if "raw_issue_list" in state:  # a reply gh might give that is not the documented shape
        print(state["raw_issue_list"])
        return
    if fail.get("issue_list"):
        print(
            "HTTP 401: Bad credentials (https://api.github.com/graphql)",
            file=sys.stderr,
        )
        sys.exit(1)
    want = opts.get("--state", "open").upper()
    rows = [
        i
        for i in state.get("issues", [])
        if want == "ALL" or i.get("state", "OPEN") == want
    ]
    if "--label" in opts:
        rows = [i for i in rows if opts["--label"] in i.get("labels", [])]
    rows.sort(key=lambda i: i["createdAt"], reverse=True)
    rows = rows[: int(opts.get("--limit", "30"))]
    out = []
    for i in rows:
        login = (
            f"app/{i['author'].removesuffix('[bot]')}" if i.get("bot") else i["author"]
        )
        full = {
            "number": i["number"],
            "title": i["title"],
            "body": i.get("body", ""),
            "author": {
                "id": "U_kgDOAAAAAQ",
                "is_bot": bool(i.get("bot")),
                "login": login,
                "name": i.get("author_name", ""),
            },
            "labels": [
                {"id": "LA_kwDOAAAAAQ", "name": n, "description": "", "color": "ededed"}
                for n in i.get("labels", [])
            ],
            "createdAt": i["createdAt"],
            "state": i.get("state", "OPEN"),
            "url": f"https://github.com/{opts['--repo']}/issues/{i['number']}",
        }
        fields = opts["--json"].split(",")
        unknown = [f for f in fields if f not in full]
        if unknown:
            refuse(f"unmodelled --json field(s) {unknown}")
        out.append({f: full[f] for f in fields})
    print(json.dumps(out))


def permission(login: str) -> None:
    doc = (
        "https://docs.github.com/rest/collaborators/collaborators"
        "#get-repository-permissions-for-a-user"
    )
    if login in (fail.get("permission") or []):
        http_error("Resource not accessible by personal access token", 403, doc)
    role = (state.get("roles") or {}).get(login)
    if role is None:
        http_error(f"{login} is not a user", 404, doc)
    user = rest_user(login) | {
        "permissions": PERMS[role],
        "role_name": "" if role == "none" else role,
    }
    print(
        json.dumps(
            {
                "permission": LEGACY[role],
                "user": user,
                "role_name": "" if role == "none" else role,
            }
        )
    )


def events(number: str) -> None:
    if number in [str(n) for n in fail.get("events") or []]:
        http_error("Server Error", 502, "https://docs.github.com/rest")
    out = []
    for k, e in enumerate((state.get("events") or {}).get(number, []), start=1):
        row = {
            "id": k,
            "node_id": f"LE_{k}",
            "url": f"https://api.github.com/events/{k}",
            "actor": rest_user(e["actor"]),
            "event": e["event"],
            "commit_id": None,
            "commit_url": None,
            "created_at": e["created_at"],
            "performed_via_github_app": None,
        }
        if e["event"] in ("labeled", "unlabeled"):
            row["label"] = {"name": e["label"], "color": "ededed"}
        if e["event"] == "renamed":
            row["rename"] = {
                "from": e.get("rename_from", ""),
                "to": e.get("rename_to", ""),
            }
        out.append(row)
    print(json.dumps(out))


def graphql(rest: list[str]) -> None:
    fields: dict[str, str] = {}
    query = ""
    i = 0
    while i < len(rest):
        if rest[i] == "-f" and rest[i + 1].startswith("query="):
            query = rest[i + 1][len("query=") :]
        elif rest[i] == "-F" and "=" in rest[i + 1]:
            key, _, value = rest[i + 1].partition("=")
            fields[key] = value
        else:
            refuse(f"unmodelled graphql argument {rest[i]!r}")
        i += 2
    if "lastEditedAt" not in query:
        refuse("graphql query does not read the body's last edit")
    number = fields.get("number", "")
    if number in [str(n) for n in fail.get("graphql") or []]:
        print("gh: Something went wrong while executing your query.", file=sys.stderr)
        sys.exit(1)
    recorded = (state.get("edits") or {}).get(number) or []
    history = recorded if isinstance(recorded, list) else [recorded]
    edit = max(history, key=lambda e: e["lastEditedAt"]) if history else {}
    editor = None
    if edit.get("editor"):
        bot = bool(edit.get("editor_bot"))
        editor = {
            "login": edit["editor"].removesuffix("[bot]") if bot else edit["editor"],
            "__typename": "Bot" if bot else "User",
        }
    issue = {
        "number": int(number),
        "lastEditedAt": edit.get("lastEditedAt"),
        "editor": editor,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    print(json.dumps({"data": {"repository": {"issue": issue}}}))


def issue_view(rest: list[str]) -> None:
    if not rest or not rest[0].isdigit():
        refuse("issue view without an issue number")
    opts = flags(rest[1:], {"--repo", "--json"})
    if "--json" not in opts or "--repo" not in opts:
        refuse("issue view without --repo and --json")
    if fail.get("issue_view"):
        print("GraphQL: Could not resolve to an issue or pull request with the number of "
              f"{rest[0]}. (repository.issue)", file=sys.stderr)
        sys.exit(1)
    found = [i for i in state.get("issues", []) if str(i["number"]) == rest[0]]
    if not found:
        print(f"GraphQL: Could not resolve to an issue or pull request with the number of "
              f"{rest[0]}. (repository.issue)", file=sys.stderr)
        sys.exit(1)
    i = found[0]
    login = f"app/{i['author'].removesuffix('[bot]')}" if i.get("bot") else i["author"]
    full = {"number": i["number"], "title": i["title"], "body": i.get("body", ""),
            "author": {"id": "U_kgDOAAAAAQ", "is_bot": bool(i.get("bot")), "login": login,
                       "name": i.get("author_name", "")},
            "url": f"https://github.com/{opts['--repo']}/issues/{i['number']}"}
    fields = opts["--json"].split(",")
    unknown = [f for f in fields if f not in full]
    if unknown:
        refuse(f"unmodelled --json field(s) {unknown}")
    print(json.dumps({f: full[f] for f in fields}))


if args[:2] == ["issue", "list"]:
    issue_list(args[2:])
elif args[:2] == ["issue", "view"]:
    issue_view(args[2:])
elif args[:2] == ["api", "graphql"]:
    graphql(args[2:])
elif args and args[0] == "api":
    rest = args[1:]
    paginate = "--paginate" in rest
    paths = [a for a in rest if a.startswith("repos/")]
    if len(paths) != 1 or len(rest) != (2 if paginate else 1):
        refuse("unmodelled api call")
    path = paths[0]
    m = re.fullmatch(r"repos/[^/]+/[^/]+/collaborators/([^/?]+)/permission", path)
    e = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)/events\?per_page=100", path)
    if m and not paginate:
        permission(unquote(m.group(1)))
    elif e and paginate:
        events(e.group(1))
    else:
        refuse("unmodelled api path, or events read without --paginate")
else:
    refuse("unexpected call")
