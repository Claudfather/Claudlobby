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

DIR="${1:?Usage: git-pull-all.sh /path/to/projects/dir}"
LOG="$(dirname "$DIR")/git-pull.log"
setup_log_dir "$LOG"
FAILURES=0

echo "$(ts_iso) Starting git pull for repos in $DIR" >> "$LOG"

for repo in "$DIR"/*/; do
    if [ -d "$repo/.git" ]; then
        REPO_NAME=$(basename "$repo")
        if RESULT=$(cd "$repo" && git pull --ff-only 2>&1); then
            echo "$(ts_iso) $REPO_NAME: $RESULT" >> "$LOG"
        else
            echo "$(ts_iso) $REPO_NAME: FAILED — $RESULT" >> "$LOG"
            FAILURES=$((FAILURES + 1))
        fi
    fi
done

if [ "$FAILURES" -gt 0 ]; then
    echo "$(ts_iso) Done — $FAILURES repo(s) failed" >> "$LOG"
    exit 1
fi
