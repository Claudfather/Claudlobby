#!/bin/bash
# Pre-fetch data before scheduled briefings
# Schedule 30 min before each briefing so data is fresh when the bot reads it
#
# Example cron (briefing at 8:30 AM, pre-sync at 8:00 AM):
#   0 8 * * * /path/to/claudlobby/my-bot/finance-presync.sh

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

# Source env vars (API keys, etc.)
# shellcheck source=/dev/null
[ -f ~/.env ] && . ~/.env

SNAPSHOT_DIR="$(dirname "$0")/data/snapshots"
mkdir -p "$SNAPSHOT_DIR"

DATE=$(date +%Y-%m-%d)

# Example: fetch portfolio data from an API and save as JSON
# Replace with your actual data source
curl -s -H "Authorization: Bearer ${API_TOKEN:-}" \
  "https://api.yourdatasource.com/v1/portfolio" \
  > "$SNAPSHOT_DIR/portfolio-$DATE.json" 2>/dev/null || true

# Example: fetch transaction data
curl -s -H "Authorization: Bearer ${API_TOKEN:-}" \
  "https://api.yourdatasource.com/v1/transactions?since=$(date_relative '7 days ago')" \
  > "$SNAPSHOT_DIR/transactions-$DATE.json" 2>/dev/null || true

echo "$(ts_iso) Pre-sync complete" >> "$(dirname "$0")/data/presync.log"
