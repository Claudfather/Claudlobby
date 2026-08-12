---
title: Linear MCP
tool_grants:
  - "mcp__linear__*"
---

# Linear MCP

Wire config: `library/mcp/linear.json` (uses `${LINEAR_API_KEY}`).

Linear ships a **hosted** MCP server — there is nothing to install and no `npx`
package. It is one of only two HTTP fragments in this library; every other
server is stdio.

| | |
|---|---|
| Endpoint | `https://mcp.linear.app/mcp` |
| Transport | Streamable HTTP (`"type": "http"`) |
| Auth | `Authorization: Bearer <key>` |

## The workspace is the credential, not the URL

There is **no workspace in the endpoint** — every Linear workspace uses the same
hosted URL, and the API key decides which one you reach. Two consequences:

- Nothing workspace-specific belongs in the fragment. It is generic by
  construction, not by convention.
- A second workspace is a second **instance**, not a second server:

```yaml
mcp:
  - linear:
      instances: [default, acme]   # → LINEAR_API_KEY, LINEAR_ACME_API_KEY
```

`API_KEY` is declared `scope: instance`, so the default instance resolves to
`LINEAR_API_KEY` — which is also the variable name Linear's own docs use.

## Getting the key

Linear account settings → **Security & access** → API keys. A workspace admin
may instead point you at the workspace's MCP settings page; either way the value
is a fleet-tier secret and goes in `local/<fleet>/.env`, never in this library.

**Grant only what the bot needs.** A key created with the `Read` permission only
gives read access through the same endpoint — the cheapest way to run a
read-only Linear bot, and preferable to granting write and relying on prose.

## Bearer beats OAuth here, and that is the reason this works headless

Linear's default flow is interactive OAuth 2.1 with dynamic client registration,
which a headless bot cannot complete. Linear also documents passing an API key
directly in the `Authorization: Bearer` header **instead of** the interactive
flow — that is what this fragment uses, and it is why Linear is equippable by a
supervised bot at all.

## Read-only endpoint (available, not wired)

Linear publishes `https://mcp.linear.app/mcp/readonly`, which only ever exposes
read tools. This fragment targets the read-write endpoint and takes the
all-or-nothing wildcard grant, matching `github`/`notion` — issue tracking is
work a bot does, not just reads.

A fleet wanting a hard read-only rail has two routes, and the endpoint is the
stronger one: a `Read`-scoped key or the `/readonly` URL enforce it *server
side*, where our `read_only_tools` mechanism only withholds the local
permission. Wiring that is a fleet decision and a separate fragment; it is
deliberately not done here.

## The tool list is corroborated, not authoritative

**Linear does not publish an enumerated tool list**, and the hosted server
returns `401` to an unauthenticated `tools/list`, so it cannot be introspected
without a real credential.

Four names are confirmed against the live server by a third-party bug report
(`list_teams`, `list_issues`, `get_issue` working; `create_issue` advertised).
The remainder of `_permissions_contract.tools` comes from a public directory
that describes its list as catalogued rather than introspected.

**This gates nothing today.** With no `read_only_tools`, the compositor emits a
single `mcp__linear__*` wildcard and never reads the individual names — the list
is documentation. Anyone adding `read_only_tools` later should verify the names
against a live session first, because at that point they *do* become
load-bearing.

## Gotchas

- **SSE is deprecated.** `https://mcp.linear.app/sse` exists as a fallback for
  clients without Streamable HTTP support. Do not use it for new setups.
- **`create_issue` has been observed advertised-but-missing** on the hosted
  server (`MCP error -32602: Tool create_issue not found`) while reads on the
  same connection worked. If a write fails that way, it is a server-side
  mismatch, not a wiring error — check reads before debugging the config.
- **Identifiers, not names.** Teams and projects are addressed by key/ID; a
  human-readable name is not always accepted.
