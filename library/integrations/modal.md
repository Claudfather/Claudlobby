---
title: Modal
type: cli
env_contract:
  MODAL_TOKEN_ID:
    description: Modal API token id (the id half of the token pair)
    default_tier: fleet
    secret: true
  MODAL_TOKEN_SECRET:
    description: Modal API token secret (the secret half of the token pair)
    default_tier: fleet
    secret: true
---

# Modal

Modal runs serverless apps and GPU workloads. You reach it either through a Modal
domain skill or agent, or through the `modal` CLI.

**What you will need to do with it:**
- Check deployed apps, containers, and workspace state
- Read and debug logs when a function fails
- Deploy or update an app

**How to do it:** use any Modal domain skill or agent you have available —
clauDNA ships `/claudna:modal`, so check your own skill list first. If none is
installed, use the `modal` CLI directly; everything above is reachable that way.

**Gotchas:**
- Two vars, and Modal needs BOTH: `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` are one token
  pair, not a token and an option — either alone authenticates nothing
- `modal token set` writes `~/.modal.toml` instead, so a bot can be authenticated with
  neither var present; check both before concluding a credential is missing
- Modal apps are serverless — cold starts are normal, not failures
- Secrets and volumes are workspace-scoped — ensure the right workspace is active
- `modal deploy` is idempotent — safe to re-run
