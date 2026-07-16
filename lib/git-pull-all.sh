#!/bin/bash
# Pull latest changes for all git repos in a directory
# Usage: git-pull-all.sh /path/to/projects/dir
#
# Schedule via cron:
#   30 6 * * * /path/to/claudlobby/lib/git-pull-all.sh /path/to/projects

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

DIR="${1:?Usage: git-pull-all.sh /path/to/projects/dir}"

# When DIR is a fleet runtime projects path (.../runtime/bots/<bot>/projects), a
# stale per-bot cron entry for a bot no longer declared in that fleet would — via
# the log-dir mkdir below — resurrect the departed bot's runtime dir on every run,
# which fleet supervision then flags as a cross-fleet orphan. Consult fleet.yaml
# (the authoritative roster) and no-op for undeclared bots. Non-fleet paths have no
# fleet.yaml alongside → bot_in_fleet treats the bot as declared → generic behavior
# (pull any directory of repos) is unchanged.
#
# Defense-in-depth across both departure vectors: a bot removed via teardown /
# move-bot is best handled by reaping its per-bot cron at teardown (a complementary
# fix), but a bot dropped by editing fleet.yaml and re-generating runs no teardown —
# so this authoritative-roster check is the durable line of defense for that case,
# not a stopgap.
_gpa_dir=${DIR%/}
case "$_gpa_dir" in
    */runtime/bots/*/projects)
        _gpa_bot=$(basename "$(dirname "$_gpa_dir")")
        _gpa_fleet_root=${_gpa_dir%/runtime/bots/*/projects}
        bot_in_fleet "$_gpa_bot" "$(parse_fleet_bots "$_gpa_fleet_root/fleet.yaml")" \
            || exit 0
        ;;
esac

LOG="$(dirname "$DIR")/git-pull.log"
setup_log_dir "$LOG"
FAILURES=0

echo "$(ts_iso) Starting git pull for repos in $DIR" >> "$LOG"

# Derive this bot composed MCP-trust allowlist once, to pre-trust the same
# servers in each checkout below (empty for a generic, non-fleet projects dir ->
# seeding no-ops). See seed_checkout_mcp_trust.
MCP_ALLOWLIST="$(_home_mcp_allowlist "$(dirname "$_gpa_dir")" 2>/dev/null || true)"

for repo in "$DIR"/*/; do
    if [ -d "$repo/.git" ]; then
        REPO_NAME=$(basename "$repo")
        if RESULT=$(cd "$repo" && git pull --ff-only 2>&1); then
            echo "$(ts_iso) $REPO_NAME: $RESULT" >> "$LOG"
        else
            echo "$(ts_iso) $REPO_NAME: FAILED — $RESULT" >> "$LOG"
            FAILURES=$((FAILURES + 1))
        fi
        # Pre-trust the bot MCP servers for sessions rooted in this checkout so
        # dev sessions boot clean (see seed_checkout_mcp_trust). Independent of
        # pull success; skipped when there is nothing to trust.
        [ -n "$MCP_ALLOWLIST" ] && seed_checkout_mcp_trust "$repo" "$MCP_ALLOWLIST" || true
    fi
done

if [ "$FAILURES" -gt 0 ]; then
    echo "$(ts_iso) Done — $FAILURES repo(s) failed" >> "$LOG"
    exit 1
fi
