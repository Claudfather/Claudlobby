#!/bin/bash
# Pre-fetch data before scheduled briefings
# Schedule 30 min before each briefing so data is fresh when the bot reads it
#
# Example cron (briefing at 8:30 AM, pre-sync at 8:00 AM):
#   0 8 * * * /path/to/claudlobby/my-bot/finance-presync.sh

# Source env vars (API keys, etc.)
[ -f ~/.env ] && . ~/.env

SNAPSHOT_DIR="$(dirname "$0")/data/snapshots"
mkdir -p "$SNAPSHOT_DIR"

DATE=$(date +%Y-%m-%d)

# Example: fetch portfolio data from an API and save as JSON
# Replace with your actual data source
curl -s -H "Authorization: Bearer $API_TOKEN" \
  "https://api.yourdatasource.com/v1/portfolio" \
  > "$SNAPSHOT_DIR/portfolio-$DATE.json" 2>/dev/null

# Example: fetch transaction data
curl -s -H "Authorization: Bearer $API_TOKEN" \
  "https://api.yourdatasource.com/v1/transactions?since=$(date -d '7 days ago' +%Y-%m-%d)" \
  > "$SNAPSHOT_DIR/transactions-$DATE.json" 2>/dev/null

echo "$(date -Iseconds) Pre-sync complete" >> "$(dirname "$0")/data/presync.log"
