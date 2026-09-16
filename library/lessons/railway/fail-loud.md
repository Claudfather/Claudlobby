---
title: "Lesson: Railway — fail loud, never fabricate deploy state"
---

Railway auth lives in the `.env` tiers as `RAILWAY_PERSONAL_TOKEN` (ACCOUNT-scoped: answers `me`, reaches every workspace) and `RAILWAY_PERSONAL_PROJECT_TOKEN` (WORKSPACE-scoped: answers `projects`, and cannot answer `me` — that is the token kind, not a broken credential). The CLI picks them up when the tiers are sourced, plus the per-directory project link in `~/.railway/config.json`. **A rejected token still returns HTTP 200** with the refusal in the body, which is the first thing the rule below is about.

**The rule:** if Railway returns an auth or link error, report it verbatim. **Never fabricate deploy state.** A truthful "Railway auth broken, could not confirm deploy" beats a hallucinated "deploy landed at 18:42Z." When a worker reports Railway data to you, require the raw command output or a GraphQL 200 payload — "I ran `railway status` and it says X" is not evidence; the output is.

### Error-mode playbook

Applies to every worker that touches Railway:

- **`No linked project found`** — the current directory isn't in `~/.railway/config.json`. Run the project's `railway-link-fleet.sh` (idempotent; links every fleet checkout). If the script fails, the worker must report blocked — do not let them proceed on a guessed deploy state.
- **`401 Unauthorized` / `403 Forbidden`** — token rejected. Workers must NOT retry with a different token. Worker escalates via `report-back.sh blocked "railway token rejected (HTTP <code>) — rotation needed"`, and the manager flags the human to regenerate the account token in Railway dashboard → Account Settings → Tokens, then update `.env.shared`.
- **Timeout / network error** — one retry, then escalate as `blocked`.
- **Any other non-200 response** — treat as broken, report verbatim.

### Output discipline

**Never** surface a deploy-status line in a dispatch summary or a PR comment unless the worker's report includes a verbatim `railway status --json` or GraphQL 200 payload with a matching service. If Railway was unreachable, say so — don't paper over it.
