---
title: No fabrication of external state
---

If a tool call fails (auth, network, missing endpoint), report verbatim. Never:

- Invent plausible-looking output
- Guess PR numbers, commit SHAs, URLs, or IDs you didn't observe
- Claim a deployment succeeded when you couldn't verify
- Fill in "what the response probably says" when an API errored

When uncertain: "I tried to fetch X but got HTTP 401 — I don't know the actual answer." Beats a confident hallucination.

Especially: GitHub API failures, Railway/Vercel/Modal deploys, dbt run results, Snowflake queries, MCP timeouts.
