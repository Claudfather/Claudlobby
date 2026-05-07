#!/bin/bash
# bootstrap-bot.sh — scaffold a new bot dir from the manager/ or examples/worker/ template.
#
# Creates per-bot dir, channel state, and seeds Claude Code workspace trust.
#
# Usage:
#   bootstrap-bot.sh <bot-name> manager [options]
#   bootstrap-bot.sh <bot-name> worker  [options]
#
# Options:
#   --telegram-token <token>      Inline BotFather token (otherwise fill .env manually)
#   --group-chat-id <id>          Telegram group chat ID to auto-add (flows into bot.conf + access.json)
#   --service-prefix <prefix>     e.g. "com.example.claudlobby" — flows into bot.conf + skill docs
#   --fleet-org <org>             GitHub org the fleet operates in (scope)
#   --github-user <handle>        Your GitHub handle (used by /prs routing)
#   --manager-tmux <name>         tmux session name of the manager bot (defaults to BOT_NAME when template=manager)
#   --fleet-repos "<list>"        space-separated repo names under FLEET_ORG (e.g. "repo-a repo-b")
#   --personal-repos "<list>"     space-separated personal repos (optional)
#   --claudlobby-root <path>      defaults to ~/claudlobby
#
# Does NOT do (you handle these yourself):
#   - Create the bot via BotFather
#   - Pair the first human via /telegram:access
#   - Install the service unit (systemd: sudo cp + systemctl; launchd: cp plist + launchctl bootstrap)
#   - Fill in remaining secrets in .mcp.json
set -u
BOT="${1:?Usage: bootstrap-bot.sh <bot-name> <template>}"
TEMPLATE="${2:?Usage: bootstrap-bot.sh <bot-name> <template (manager|worker)>}"
shift 2

TOKEN=""
CHAT_ID=""
SERVICE_PREFIX=""
FLEET_ORG=""
GITHUB_USER=""
MANAGER_TMUX=""
FLEET_REPOS=""
PERSONAL_REPOS=""
ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"

while [ $# -gt 0 ]; do
  case "$1" in
    --telegram-token)  TOKEN="$2"; shift 2 ;;
    --group-chat-id)   CHAT_ID="$2"; shift 2 ;;
    --service-prefix)  SERVICE_PREFIX="$2"; shift 2 ;;
    --fleet-org)       FLEET_ORG="$2"; shift 2 ;;
    --github-user)     GITHUB_USER="$2"; shift 2 ;;
    --manager-tmux)    MANAGER_TMUX="$2"; shift 2 ;;
    --fleet-repos)     FLEET_REPOS="$2"; shift 2 ;;
    --personal-repos)  PERSONAL_REPOS="$2"; shift 2 ;;
    --claudlobby-root) ROOT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Default MANAGER_TMUX to BOT_NAME if template=manager and not provided
if [ -z "$MANAGER_TMUX" ] && [ "$TEMPLATE" = "manager" ]; then
  MANAGER_TMUX="$BOT"
fi

SRC=""
case "$TEMPLATE" in
  manager) SRC="$ROOT/manager" ;;
  worker)  SRC="$ROOT/examples/worker" ;;
  *) echo "template must be 'manager' or 'worker'" >&2; exit 2 ;;
esac

[ -d "$SRC" ] || { echo "template not found at $SRC" >&2; exit 1; }
BOT_DIR="$ROOT/$BOT"
[ -e "$BOT_DIR" ] && { echo "bot dir already exists: $BOT_DIR" >&2; exit 1; }

echo "Creating $BOT_DIR from $TEMPLATE template..."
mkdir -p "$BOT_DIR"
cp -R "$SRC"/. "$BOT_DIR/"

# Substitute placeholders across all template files (bot.conf, CLAUDE.md, .mcp.json.template, skill files)
BOT_UPPER=$(echo "$BOT" | tr '[:lower:]' '[:upper:]')
find "$BOT_DIR" -type f \( -name '*.md' -o -name 'bot.conf' -o -name '.mcp.json*' \) -print0 | \
while IFS= read -r -d '' f; do
  sed -i.bak \
    -e "s|<BOT_NAME_UPPER>|$BOT_UPPER|g" \
    -e "s|<BOT_NAME>|$BOT|g" \
    -e "s|<BOT_DIR>|$BOT_DIR|g" \
    -e "s|<CLAUDLOBBY_ROOT>|$ROOT|g" \
    -e "s|<GROUP_CHAT_ID>|$CHAT_ID|g" \
    -e "s|<SERVICE_PREFIX>|$SERVICE_PREFIX|g" \
    -e "s|<FLEET_ORG>|$FLEET_ORG|g" \
    -e "s|<USER_GITHUB>|$GITHUB_USER|g" \
    -e "s|<GITHUB_USER>|$GITHUB_USER|g" \
    -e "s|<MANAGER_TMUX>|$MANAGER_TMUX|g" \
    -e "s|<FLEET_REPOS>|$FLEET_REPOS|g" \
    -e "s|<PERSONAL_REPOS>|$PERSONAL_REPOS|g" \
    "$f" && rm "$f.bak"
done

# Keep .mcp.json as .template by default — do NOT auto-rename.
# Reason: the template still contains <GITHUB_PAT>, <NOTION_INTEGRATION_TOKEN>, <SLACK_USER_TOKEN>
# which a fresh install hasn't filled in yet. An auto-renamed .mcp.json would cause MCP servers to
# authenticate with literal placeholder strings on first launch.
#
# Once you've filled in the secrets, rename manually:
#   mv "$BOT_DIR/.mcp.json.template" "$BOT_DIR/.mcp.json"

# Telegram state dir
STATE_DIR="$HOME/.claude/channels/telegram-$BOT"
mkdir -p "$STATE_DIR"/{approved,inbox}
umask 077

if [ -n "$TOKEN" ]; then
  echo "TELEGRAM_BOT_TOKEN=$TOKEN" > "$STATE_DIR/.env"
  chmod 600 "$STATE_DIR/.env"
fi

POLICY="pairing"
GROUPS="{}"
if [ -n "$CHAT_ID" ]; then
  # Include the group with requireMention:true by default for workers, false for manager
  REQUIRE=$([ "$TEMPLATE" = "manager" ] && echo "false" || echo "true")
  GROUPS="{\"$CHAT_ID\": {\"requireMention\": $REQUIRE, \"allowFrom\": []}}"
fi
cat > "$STATE_DIR/access.json" <<JSON
{
  "dmPolicy": "$POLICY",
  "allowFrom": [],
  "groups": $GROUPS,
  "pending": {}
}
JSON
chmod 600 "$STATE_DIR/access.json"

# Pre-seed Claude Code workspace trust (avoids interactive prompt on first launch)
CCJSON="$HOME/.claude.json"
if [ -f "$CCJSON" ] && command -v jq >/dev/null; then
  TMP=$(mktemp)
  jq --arg dir "$BOT_DIR" '.projects[$dir] = {
    "allowedTools": [],
    "mcpContextUris": [],
    "mcpServers": {},
    "enabledMcpjsonServers": [],
    "disabledMcpjsonServers": [],
    "hasTrustDialogAccepted": true,
    "projectOnboardingSeenCount": 99,
    "hasClaudeMdExternalIncludesApproved": true,
    "hasClaudeMdExternalIncludesWarningShown": true
  }' "$CCJSON" > "$TMP" && mv "$TMP" "$CCJSON"
fi

echo
echo "✅ Bootstrapped $BOT ($TEMPLATE)"
echo "   Bot dir:       $BOT_DIR"
echo "   Channel state: $STATE_DIR"
echo
echo "Unfilled placeholders you may still need to address (grep for <ALL_CAPS> in files):"
grep -rEo '<[A-Z_]+>' "$BOT_DIR" 2>/dev/null | sort -u | sed 's/^/   /' || true
echo
echo "Next steps:"
[ -z "$TOKEN" ] && echo "  1. Create the bot via @BotFather → paste token into $STATE_DIR/.env"
echo "  2. Fill in $BOT_DIR/CLAUDE.md — persona, scope, any role-specific rules"
echo "  3. Fill in $BOT_DIR/.mcp.json.template — MCP server tokens — then rename to .mcp.json"
echo "  4. Install the service unit (systemd/launchd) pointing at bot-common/start-bot.sh"
echo "  5. Start the bot, then DM it to pair (runs /telegram:access automatically)"
