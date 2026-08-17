---
title: GitHub MCP
tool_grants:
  - "mcp__github__*"
---

# GitHub MCP

Wire config: `library/mcp/github.json` (uses `${GITHUB_PAT}`).

#### Common Ops

- **List PRs:** `mcp__github__list_pull_requests` — returns open PRs for a repo
- **Read a PR:** `mcp__github__get_pull_request` + `mcp__github__get_pull_request_files`
- **Create issue:** `mcp__github__create_issue` — title, body, labels, assignees
- **Post review:** `mcp__github__create_pull_request_review` — approve, request changes, or comment
- **Search code:** `mcp__github__search_code` — regex across repos

#### Gotcha: 30-File Pagination

`mcp__github__get_pull_request_files` returns **only the first GitHub API page** — max 30 files. PRs with > 30 files silently truncate.

**Canonical full-file list:**

```bash
gh pr view <NN> --json files --jq '.files[].path'
```

If you see exactly 30 files in the MCP response, assume truncation and re-fetch via `gh`.

#### Same-Identity Fleet

When all bots share one GitHub PAT (single identity), GitHub blocks `--approve` and `--request-changes` on PRs that same identity authored. Use the `same-identity-fallback` protocol: post the verdict as a COMMENT with `**Approve**` or `**Request Changes**` in the body.

#### Gotcha: reading a piped `gh` call's exit status

**The mechanism is not a `gh` fact and is stated once in the `exit-status-through-pipes` guardrail:** a
pipeline's `$?` is the *last* stage's status, so `gh api ... | head` reports `head`'s — exit `0` — however
`gh` failed. Use `${PIPESTATUS[0]}` or run the call unpiped. It has bitten `pytest` and `claudron` here
too, so do not read it as a GitHub gotcha.

What follows is the part that *is* `gh`-specific.

Verified live under a fleet-wide 401: two bots independently ran `gh api ... | head` while diagnosing it, all three probes returned rc `0` against a body reading `Bad credentials`, and both first offered those rc values as evidence that auth was fine. Both reported correctly only because they read the response *bodies*. A warning filed under the wrong failure mode is nearly as good as absent — which is why this one is not filed under any mode.

**`${PIPESTATUS[0]}` alone is not sufficient for `gh`**: `gh api` writes the error body to stdout as well as stderr, so anything already reading the stream is parsing an error that looks like a payload — a `--jq` default (`.field // "none"`) substitutes silently, and even a non-empty `grep -c` can be counting the error text. Read the body, not any single status.

**This warning is not the fix, and prose here is known to be insufficient — see #1066**, which tracks a checked helper. The failure wears the exact shape of the answer you were looking for, so vigilance is the wrong instrument; treat the paragraph above as an interim measure, not a solved problem.

#### Gotcha: a 403 blocks REST, not GitHub

**Scope: 403 throttling only.** Everything in this section is about a throttled-but-live token. A 401 is a different failure with the opposite response — see the rung below, and do not apply this reroute pattern to it.

REST core, search and GraphQL throttle in **independent buckets**, so a REST block is not an outage and rarely blocks the actual work. Verified live under an active secondary block, reads *and* writes: `gh pr list`, `gh issue list`, `gh issue view`, `gh issue create` and `gh api graphql` all kept working while `gh api repos/...` 403'd on the same token in the same second. Reroute to those rather than idling.

The GitHub MCP is REST-backed, so `mcp__github__*` failing while `gh pr` / `gh issue` succeed is **expected, not a broken MCP** — *when the code is 403*. Not every `gh` subcommand is GraphQL-backed either (`gh api` and `gh pr diff` are REST), so test the specific one you need instead of assuming porcelain is safe.

**The status code is the discriminator, because that same observable has two unrelated causes.** `gh` and the MCP do not share a credential: `gh` reads `~/.config/gh/hosts.yml`, the MCP reads `${GITHUB_PAT}` from `.env`, and `gh auth login` writes only the first. So one can be healthy while the other is revoked, stale, or absent.

| divergence cause | code | correct response |
|---|---|---|
| one token, different throttle bucket | 403 | expected — reroute and keep working |
| two credentials, one revoked or absent | 401 | a real auth failure — stop and report |

A 401 is **never** the throttle case, so "expected, not a broken MCP" does not apply to it and neither does anything else in this section.

**Rejected is not the same as absent, and a bare 401 cannot tell them apart.** Run the same URL twice, once unauthenticated and once with the credential: `200` then `401` is a credential being **presented and rejected**. Conversely an `mcp__github__*` success proves nothing about authentication — an anonymous read of a public repo returns real data at `200`, so treat it as a public read unless the endpoint required a credential (`gh api user` does).

#### Gotcha: a 401 kills every door — there is nothing to reroute to

A 401 means the credential is revoked or expired **server-side**. Unlike a 403, no bucket survives: `gh auth status`, REST, GraphQL and `gh` porcelain all fail together. Verified live fleet-wide. The reroute reflex from the 403 section is the wrong instinct here and costs real time — a bot following it proved the same failure four ways before concluding.

The token is per-**user** (`~/.config/gh/hosts.yml`), so every bot running as that user is blocked identically. One bot's 401 is the whole host's 401; confirming it on a second bot tells you nothing new.

What to do:

1. **Stop GitHub operations.** One probe is enough. `git fetch` and `git push` count.
2. **Commit locally on a branch. Do not push.** Nothing is lost — already-pushed work is safe and a local commit survives. What is frozen is *delivery*, not work.
3. **Report blocked** via `report-back.sh --task <id>`, naming the branch instead of a PR.
4. **Do NOT attempt to re-authenticate.** A bot does not hold the human's credentials, and where the cause is a secret-scanning revocation a naive regenerate can get the replacement revoked too. Escalation is the human's, not yours.
5. **Carry on with everything else** — local code, tests, analysis, shared docs and vault captures are entirely unaffected.

#### When `gh` CLI Is Better

- Bulk operations across many PRs (`gh pr list --json ...`)
- Anything needing pagination control
- Operations the MCP doesn't expose (`gh pr merge --admin`, `gh pr review`)
- Fetching raw diff content (`gh pr diff <NN>`)

#### Failure Modes

- `401 Bad credentials` → credential revoked or expired server-side. **Every door is dead and there is nothing to reroute to** — see "Gotcha: a 401 kills every door". Commit locally, report blocked, and do NOT try to re-authenticate; regenerating the token is the human's call.
- `403 rate limit` → **reroute, do not wait.** Two different limits return this. **Primary** (5,000/hr) is visible in `gh api rate_limit` and publishes a reset. **Secondary** (abuse) is invisible there — the meter reads a full quota while every REST call 403s — and publishes **no reset**, so "wait" has no defined end. Do not read the meter to decide whether you are throttled; test the door: `gh api repos/<org>/<repo>`. Then see "Gotcha: a 403 blocks REST, not GitHub" — much of the work is still reachable.
- `422 Validation Failed` → usually a missing required field or invalid label name.
- Context deadline exceeded → proxy/network issue. Retry once, then report blocked.
