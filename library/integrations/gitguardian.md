---
title: GitGuardian
type: cli
env_contract:
  GITGUARDIAN_API_KEY:
    description: GitGuardian personal access token with the incidents:read scope only
    default_tier: bot
    secret: true
requires:
  skills: [gitguardian]
  protocols: [secret-incidents]
---

# GitGuardian

This bot reads GitGuardian's secret incidents through the `gitguardian` skill, and handles them by the Secret Incidents protocol.

- **The token** is `GITGUARDIAN_API_KEY`, in this bot's own `.env` (the bot tier), so no other bot's environment holds it. It needs the `incidents:read` scope and no other. The helper sends only GET requests on the incident routes, whatever the token's scopes, but anything else that reads the variable could use a wider one.
- **An EU workspace** also sets `GITGUARDIAN_API_URL=https://api.eu1.gitguardian.com`. The helper sends the token to no host but GitGuardian's two.
- **No detected secret reaches you.** The incident routes return no secret values, and the helper prints no token, response body, path, commit or hash.
- **Removing it:** delete the `integrations:` entry and activate. Then delete the token's line from the bot's `.env` and revoke the token in GitGuardian's dashboard, since removing the entry does not revoke it.
