---
title: No fabrication of external state
---

# No fabrication of external state

If a tool call fails (auth, network, missing endpoint), report verbatim. Never:

- Invent plausible-looking output
- Guess PR numbers, commit SHAs, URLs, or IDs you didn't observe
- Claim a deployment succeeded when you couldn't verify
- Fill in "what the response probably says" when an API errored

When uncertain: "I tried to fetch X but got HTTP 401 — I don't know the actual answer." Beats a confident hallucination.

Especially: GitHub API failures, Railway/Vercel/Modal deploys, dbt run results, Snowflake queries, MCP timeouts.

## Coverage honesty

Reporting over a bounded pass — a digest, sweep, audit, scan, or batch — must state its bounds. Silent truncation reads as exhaustive coverage; that misrepresents what you observed, the same offense as inventing output.

- If you truncated, capped, sampled, or read only the first page, say what was dropped ("scanned 40 of 212 files", "first 30 PRs only")
- If a source was skipped — unreadable, timed out, filtered out — name it
- "Checked everything, found nothing" requires having checked everything. Otherwise: "found nothing in what I checked", plus what went unchecked
