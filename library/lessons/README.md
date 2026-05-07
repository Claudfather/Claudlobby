# library/lessons/

"We learned this the hard way" notes. Specific incidents, retro findings, or empirically-discovered failure modes that bots should know about — distinct from rules (guardrails) or workflow patterns (protocols).

## What belongs here

- **Postmortem-style notes** — "On 2026-04-15 the Railway CLI auth failed silently because the token format changed. Now we always probe with `railway whoami` before any deploy operation."
- **Empirical workarounds** — "When the Notion MCP returns a 502, retrying immediately fails. Wait 30s before retry."
- **Subtle bugs in tools we depend on** — "Anthropic API streams sometimes split a tool-use block across two SSE messages; the SDK handles it, but custom parsers must buffer."
- **Internal incidents that motivated current rules** — "issue #NN: PATs silently expire; `creds-check.sh` was added to surface this within 24h."

## What does NOT belong here

- **Rules to follow** — that's guardrails (`no-push-main`, `pii-protection`)
- **Workflow patterns** — that's protocols (`dispatch`, `report-back`)
- **Capability / domain knowledge** — that's expertise

## Composition

Each `<lesson>.md` is appended under a `## Lessons` section, in the order listed in `fleet.yaml` `lessons:`.

A bot only needs the lessons relevant to its role and tooling — a designer doesn't need to know about Snowflake auth quirks.

## Example

`library/lessons/credential-keepalive.md`:

```markdown
## Lesson: silently-expiring credentials

The fleet hit a stretch where multiple service tokens (PaaS deploy keys, MCP
auth tokens) expired on different schedules. Each expiration only surfaced
when the next bot tried to use the credential — meaning bots failed at random
times for reasons that had nothing to do with what they were working on.

**Fix:** `lib/creds-check.sh` runs daily, probes each fleet credential,
alerts Telegram on state transitions.

**What this means for you:** if you see a credential failure, check
`lib/creds-check-state.json` and the Telegram archive — the failure may
already be known and tracked.
```

## Tone

Lessons should read like postmortem notes — what happened, why it surprised us, what we changed. Not commands; observations.
