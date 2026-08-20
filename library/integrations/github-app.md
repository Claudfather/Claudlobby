---
title: GitHub MCP (App identity)
tool_grants:
  - "mcp__github-app__*"
---

# GitHub MCP (App identity)

Wire config: `library/mcp/github-app.json` — the GitHub MCP server authenticated as the
fleet's **GitHub App** with ~1h installation tokens minted at use time, instead of a
long-lived `${GITHUB_PAT}`. Reference via `mcp: [github-app]`; tools are
`mcp__github-app__*`. The token refresh wrapper (`lib/github-app-mcp-wrapper.py`)
re-mints and respawns the server every ~50 minutes — token expiry never requires a bot
restart, and in-flight MCP requests fail for ~2s during a respawn (retry on transient
MCP errors).

Setup: run `lib/setup-github-app.sh` once per host (validates the App credentials end
to end and prints the fleet `.env` names), then set `GITHUB_APP_ID`,
`GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` in the fleet `.env`.

#### Short-lived tokens outside the MCP

Skills and shell calls that need the App identity mint per call — never a boot-time
export, the token dies in about an hour:

```bash
GH_TOKEN=$("$CLAUDLOBBY_ROOT"/lib/mint-github-token.sh) gh pr list
```

The composed `tools/gh` shim makes this mechanical inside a bot session: `gh` on an
App bot already mints and runs as the App, so a bare `gh pr list` is enough there. The
explicit mint above is for skills that build their own env, and for your own shell.

#### Identity — why the App, not a PAT

Fleet-scope GitHub operations authenticate as the **App bot identity** the fleet commits
as (`<slug>[bot]`), never a developer's personal PAT. A personal PAT scopes to that
person's repo access — it misses org repos they are not on and rotates when they do — and
attributes every bot action to a human. The App installs on the orgs it needs, its
installation tokens are stable across team changes and short-lived, and branch protection
can bind the bot specifically (the operator's admin bypass does not extend to it).

This is the **MCP server's** identity — one App token, for the `mcp__github-app__*` tools. The
**git commit** identity is a separate axis: whether the bot commits as `<slug>[bot]` or as the
operator, and whether that is scoped per org (bot on the App's org, you on your company's org —
the App needing no access to the latter), is configured in `github_app:` — see the runbook's
"Choosing the identity"
([`../../documentation/runbooks/github-app-setup.md`](../../documentation/runbooks/github-app-setup.md))
and [`fleet-yaml-schema.md`](../../documentation/fleet-yaml-schema.md#fleetdefaultsgithub_app--botsnamegithub_app).

**Failure signal — `Could not resolve to a Repository` on a known org repo.** The token's
identity does not have access. **Do not rotate a personal PAT** — wire the App token. The
tell: `gh repo list <org>` under a personal PAT returns only that developer's visible
subset, while the App sees the org's full install.

#### Threat framing — honest

The App private key mints tokens indefinitely and never expires, so if a bot host is
compromised the blast radius is **not smaller** than a shared PAT. The real wins are:
decoupling bot actions from a human account, suspend/rotate ergonomics (overlapping keys,
no human-account churn), short-lived tokens in transit, and branch protection that
actually binds the bot. Freshbox FAILs a private key that is group/other-readable; keep it
`0600`. Rotation: [`../../documentation/runbooks/github-app-setup.md`](../../documentation/runbooks/github-app-setup.md).

#### Token lifetime caveat (cache)

The composed gitconfig layers `cache --timeout=3000` in front of the helper, and git
re-stores the token with a fresh TTL on every successful auth — so under continuous pushing
a cached token can outlive its ~1h `ghs_` lifetime. This is self-healing: the next 401
erases the cache and re-mints, at the cost of one failed round trip. Nothing to configure.

#### MCP respawn caveat

`lib/github-app-mcp-wrapper.py` respawns the MCP server every ~50 minutes to rotate the
token; in-flight MCP requests fail for ~2s per respawn (retry on transient MCP errors).
Respawn transparency is validated against the pinned server package only — a server swap
re-validates post-respawn tool calls.

#### Everything else

Ops guidance identity-independent of App-vs-PAT — 401 vs 403 reroute rungs, pagination
gotchas, the piped-`gh` exit-status trap — lives in the paired [`github.md`](github.md)
integration; read it alongside this file. A fleet can equip both `github` and `github-app`
during a migration window; the two servers compose side by side with distinct tool
prefixes.
