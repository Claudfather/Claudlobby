#!/bin/bash
# Trigger an automated code audit in the manager bot's session
# Schedule via cron: 0 21 * * 1-5 (weekday evenings)
#
# The manager bot picks up the prompt and uses global skills
# like /tech-debt or /security-audit to run the audit.

MANAGER_SESSION="${1:-claude-bot}"

if ! /usr/bin/tmux has-session -t "$MANAGER_SESSION" 2>/dev/null; then
    echo "$(date): No manager session, skipping evening audit"
    exit 0
fi

/usr/bin/tmux send-keys -t "$MANAGER_SESSION" 'Run a rolling code audit. Check which repo/area is most stale using the audit tracker, then run /tech-debt or /security-audit on it. Log the results.' Enter
