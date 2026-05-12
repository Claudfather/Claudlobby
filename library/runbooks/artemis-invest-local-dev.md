---
title: Artemis Invest — Local Development Runbook
slug: artemis-invest-local-dev
type: runbook
status: current
owner: jian-yang
created: 2026-05-12
last_verified: 2026-05-12
repos: [artemis-invest-frontend, artemis-data-svc]
tags: [local-dev, fullstack, stytch, auth, playwright, screenshot]
links:
  - /Users/ak/work/artemis-invest-frontend/.cursor/environment.json
  - /Users/ak/work/artemis-data-svc/.cursor/environment.json
  - /Users/ak/Claudlobby/library/scripts/mint-stytch-storage-state.py
  - /Users/ak/Claudlobby/library/scripts/playwright-mcp-wrapper.sh
---

# Artemis Invest — Local Development Runbook

Three recipes covering the common local-dev shapes for the `artemis-invest-frontend` ↔ `artemis-data-svc` pair. Each repo's `.cursor/environment.json` is the canonical install recipe — these runbook entries wrap it with the right env overrides and follow-up steps.

## Prerequisites (all recipes)

- AWS credentials present in fleet env (`/Users/ak/Claudlobby/local/farm-artemis/.env` → `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`).
- `aws` CLI on PATH (Homebrew: `/opt/homebrew/bin/aws`).
- Both repos cloned at `~/work/artemis-invest-frontend` and `~/work/artemis-data-svc`.
- `~/work/artemis-data-svc/venv-artemis` populated (one-time: `source syncenv` does this and starts Valkey).
- `~/work/artemis-invest-frontend/node_modules` populated (one-time: `npm install`).

The recipes assume an interactive Mac session; on cursor's remote VM the `install` block in `.cursor/environment.json` handles deps via `--system` instead.

## Recipe 1 — Start the fullstack app locally (FE ↔ local BE)

The FE points at the BE on `127.0.0.1:8001`. Both servers run on the host; BE talks to Snowflake + Postgres + Valkey via the secrets pulled into `.env.local`.

```bash
# --- backend ---
cd ~/work/artemis-data-svc

# 1. Pull secrets from AWS Secrets Manager (overwrites .env.local).
set -a; . /Users/ak/Claudlobby/local/farm-artemis/.env; set +a
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export CLAUDE_CODE_REMOTE=true
export CLAUDE_PROJECT_DIR="$PWD"
./scripts/pull_secrets_from_secrets_manager.sh dev/data-svc-secrets

# 2. Run uvicorn from the project venv (don't rely on `make localserver`
#    finding uvicorn on PATH; activate the venv first).
export PATH="$PWD/venv-artemis/bin:$PATH"
export PYTHONPATH="$PWD"
set -a; . ./.env.shared; . ./.env.local; set +a
uvicorn src.app.main:app --host 127.0.0.1 --port 8001 &

# --- frontend ---
cd ~/work/artemis-invest-frontend

# 3. Same pattern: pull secrets, overwrite .env.local.
set -a; . /Users/ak/Claudlobby/local/farm-artemis/.env; set +a
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export CLAUDE_CODE_REMOTE=true
export CLAUDE_PROJECT_DIR="$PWD"
./scripts/pull_secrets_from_secrets_manager.sh dev/invest-frontend-secrets

# 4. Point FE at local BE (NEXT_PUBLIC_DATA_SVC_URL is NOT in Secrets
#    Manager — the recipe leaves it unset after overwrite). Also reset
#    NEXT_PUBLIC_ENCRYPTED_TOKEN_SEED to the local-dev value; the bundle
#    ships the prod seed and they must match the BE for auth to round-trip.
sed -i.bak '/^NEXT_PUBLIC_DATA_SVC_URL=/d; /^NEXT_PUBLIC_ENCRYPTED_TOKEN_SEED=/d' .env.local && rm -f .env.local.bak
cat >> .env.local <<'EOF'
NEXT_PUBLIC_DATA_SVC_URL=http://127.0.0.1:8001
NEXT_PUBLIC_ENCRYPTED_TOKEN_SEED=snxr_frperg
EOF

# 5. Start Next.js (Turbopack).
npm run dev
```

**Verify:** `curl -s http://localhost:3000/ -o /dev/null -w "%{http_code}\n"` → `200`. Theme Performance bars, Trending Research feed, ticker tape all populate from local BE (first market-overview row may lag 30–120s while caches warm).

## Recipe 2 — Start the local FE, pointed at prod BE

Faster setup when the change being tested is FE-only and you don't want to bring up Postgres / Valkey / Snowflake. The FE renders against the prod data-svc at `https://data-svc.artemisxyz.com`.

```bash
cd ~/work/artemis-invest-frontend

# 1. Pull secrets (same as Recipe 1).
set -a; . /Users/ak/Claudlobby/local/farm-artemis/.env; set +a
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export CLAUDE_CODE_REMOTE=true
export CLAUDE_PROJECT_DIR="$PWD"
./scripts/pull_secrets_from_secrets_manager.sh dev/invest-frontend-secrets

# 2. Point FE at the prod BE. With prod BE you must also use the prod
#    encrypted token seed (the values are paired — local BE expects
#    snxr_frperg, prod BE expects the bundle's shipped seed). The bundle
#    already carries the prod seed after the overwrite, so we only need
#    to set the URL.
sed -i.bak '/^NEXT_PUBLIC_DATA_SVC_URL=/d' .env.local && rm -f .env.local.bak
echo 'NEXT_PUBLIC_DATA_SVC_URL=https://data-svc.artemisxyz.com' >> .env.local

npm run dev
```

**Verify:** `localhost:3000` renders the same UI as `invest.artemisxyz.com`, sourcing all data from prod. If the page is anonymous, that's expected — Recipe 3 layers a signed-in state on top.

**Caveat:** Anything that mutates state (favouriting, watchlist add/remove, etc.) hits prod. Don't run write traffic against prod from a local FE unless you mean to.

## Recipe 3 — Generate a signed-in state (Playwright)

Mints a Stytch session from the test user creds and writes a Playwright `storageState` JSON. Use that JSON when launching a browser context so `useStytchUser()` hydrates and user-scoped widgets (Theme Performance, Trending Research, etc.) render fully.

Works with either Recipe 1 (local BE) or Recipe 2 (prod BE), as long as `.env.local` has the Stytch keys from `dev/invest-frontend-secrets`.

```bash
# 1. Mint the storageState. The script reads STYTCH_PROJECT_ID,
#    STYTCH_SECRET, TEST_USERNAME, TEST_PASSWORD from the FE's .env.local.
cd ~/work/artemis-invest-frontend
set -a; . ./.env.local; set +a
/Users/ak/Claudlobby/library/scripts/mint-stytch-storage-state.py > /tmp/stytch-storage.json

# 2. Use the storageState in Playwright. The `npx playwright screenshot`
#    CLI does NOT support --storage-state, so drive the browser with a
#    short Node script:
cat > /tmp/screenshot-signedin.mjs <<'EOF'
import { chromium } from '/Users/ak/work/artemis-invest-frontend/node_modules/playwright/index.mjs';
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  storageState: '/tmp/stytch-storage.json',
});
const page = await context.newPage();
await page.goto('http://localhost:3000', { waitUntil: 'networkidle', timeout: 30000 });
await page.waitForTimeout(8000);
await page.screenshot({ path: '/tmp/artemis-invest-signedin.png', fullPage: true });
await browser.close();
EOF
node /tmp/screenshot-signedin.mjs
```

**What gets seeded into the browser:**

- **Cookies** (`localhost:3000`): `stytch_session`, `stytch_session_jwt` — `@stytch/nextjs` reads these to hydrate `useStytchUser()`.
- **localStorage** (`localhost:3000`): `ARTEMIS_STYTCH_TOKEN` — `src/lib/api/client.ts::authenticatedFetch` attaches as `Authorization` header on every data-svc call.

**Verify:** screenshot shows `Good Afternoon, <test-user-handle>` instead of `Log In / Sign Up`.

**For designer fleet bots:** they consume this same flow via `/Users/ak/Claudlobby/library/scripts/playwright-mcp-wrapper.sh`, which wraps `npx @playwright/mcp@latest --storage-state=<tempfile>` (the MCP server, unlike the CLI, accepts `--storage-state`). The wrapper falls back to anonymous Playwright if any mint step fails and logs to `/tmp/playwright-mcp-mint.log`.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Failed to load market data` / `Failed to load theme data` | FE pointing at local BE that isn't running, or `NEXT_PUBLIC_DATA_SVC_URL` is missing entirely. | Confirm BE listening on `:8001` (`/usr/sbin/lsof -i :8001`) or flip URL to prod per Recipe 2. |
| Page stuck on `Loading…` with `snowflake.connector.errors.DatabaseError 250001` in uvicorn log | Stale Snowflake creds in `dev/data-svc-secrets`. | Rotate the AWS secret, re-pull. |
| `401 unauthorized_credentials` from `mint-stytch-storage-state.py` | Test user password drifted from the bundle. | Resync the bundle: `aws secretsmanager get-secret-value --secret-id dev/invest-frontend-secrets`. |
| Mint succeeds but FE still shows `Log In / Sign Up` | `localhost` (or `http://localhost:3000`) missing from Stytch dashboard's Authorized Domains. Symptom in browser console: `400 bad_domain_for_stytch_sdk`. | Add at https://stytch.com/dashboard/sdk-configuration. |
| `make localserver` fails with `uvicorn: command not found` | Project venv (`venv-artemis/`) not on PATH; the bare `make` target doesn't activate it. | `export PATH="$PWD/venv-artemis/bin:$PATH"` before `make localserver`, or run uvicorn directly. |
| `pull_secrets_from_secrets_manager.sh` exits silently with "No secret name provided" | Missing CLI arg or `$AWS_SECRET_NAME`. | Always pass the secret name: `... dev/data-svc-secrets` or `... dev/invest-frontend-secrets`. |

## Cross-links

- BE recipe: `~/work/artemis-data-svc/.cursor/environment.json` → `dev/data-svc-secrets`.
- FE recipe: `~/work/artemis-invest-frontend/.cursor/environment.json` → `dev/invest-frontend-secrets`.
- Mint script: `/Users/ak/Claudlobby/library/scripts/mint-stytch-storage-state.py`.
- Designer wrapper: `/Users/ak/Claudlobby/library/scripts/playwright-mcp-wrapper.sh`.
