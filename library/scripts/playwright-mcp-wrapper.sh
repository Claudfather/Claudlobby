#!/usr/bin/env bash
#
# playwright-mcp-wrapper.sh — wrapper around @playwright/mcp that injects
# an authenticated Playwright storageState at spawn time, so designer
# personas (Issey, Takahashi) take screenshots as a logged-in test user
# instead of anonymous.
#
# How it works:
#   1. Reads invest-frontend's .env.local to get STYTCH_PROJECT_ID +
#      STYTCH_SECRET (already in dev/invest-frontend-secrets) +
#      TEST_USERNAME + TEST_PASSWORD (test user creds, also in that
#      bundle).
#   2. Runs mint-stytch-storage-state.py (sibling under library/scripts/) —
#      that calls Stytch's `passwords.authenticate` API server-side, gets a
#      session_token, writes a Playwright storageState JSON to stdout that
#      seeds `localStorage[ARTEMIS_STYTCH_TOKEN]` for localhost:3000.
#   3. Spawns `npx @playwright/mcp@latest` with `--isolated --storage-state
#      <tempfile>`, so the MCP server opens browser contexts that are
#      already logged in.
#
# Resolves Artemis-xyz/artemis-invest-frontend#1100.
#
# ---------------------------------------------------------------------
# Prerequisite
# ---------------------------------------------------------------------
#
# `dev/invest-frontend-secrets` must contain STYTCH_PROJECT_ID,
# STYTCH_SECRET, TEST_USERNAME, TEST_PASSWORD. The local invest-frontend
# checkout's `.env.local` must be populated from that bundle (the
# .cursor/environment.json install command does this automatically).
#
# If the wrapper can't mint a session (env vars missing, Stytch returns
# unauthorized, network unavailable), it falls back to spawning Playwright
# MCP anonymously — same behavior as before this wrapper existed. The
# error gets logged to stderr but the bot stays usable.
#
# ---------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------
#
#   ARTEMIS_INVEST_ENV_FILE — path to the FE .env.local file
#                             (default: $HOME/work/artemis-invest-frontend/.env.local)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ARTEMIS_INVEST_ENV_FILE:-$HOME/work/artemis-invest-frontend/.env.local}"
MINT_HELPER="$SCRIPT_DIR/mint-stytch-storage-state.py"

ARGS=("@playwright/mcp@latest")
STORAGE_STATE_FILE=""

# Best-effort load of FE .env.local — wrapper runs even without it
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

# If we have all 4 env vars + the helper script is present, try to mint
if [ -x "$MINT_HELPER" ] && \
   [ -n "${STYTCH_PROJECT_ID:-}" ] && \
   [ -n "${STYTCH_SECRET:-}" ] && \
   [ -n "${TEST_USERNAME:-}" ] && \
   [ -n "${TEST_PASSWORD:-}" ]; then
  CANDIDATE="$(mktemp -t playwright-mcp-storage-state.XXXXXX)"
  if "$MINT_HELPER" > "$CANDIDATE" 2>/tmp/playwright-mcp-mint.log; then
    STORAGE_STATE_FILE="$CANDIDATE"
    ARGS+=("--isolated" "--storage-state" "$STORAGE_STATE_FILE")
  else
    rm -f "$CANDIDATE"
    echo "playwright-mcp-wrapper: stytch mint failed, falling back to anonymous (see /tmp/playwright-mcp-mint.log)" >&2
  fi
fi

# Ensure the temp storage-state file is cleaned up after the MCP server exits
if [ -n "$STORAGE_STATE_FILE" ]; then
  trap 'rm -f "$STORAGE_STATE_FILE"' EXIT
fi

# Forward any extra args from the MCP host (currently none, future-proofing)
ARGS+=("$@")

exec npx "${ARGS[@]}"
