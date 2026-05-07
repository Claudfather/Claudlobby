#!/bin/bash
# Pull latest changes for all git repos in a directory
# Usage: git-pull-all.sh /path/to/projects/dir
#
# Schedule via cron:
#   30 6 * * * /path/to/claudlobby/bot-common/git-pull-all.sh /path/to/projects

DIR="${1:?Usage: git-pull-all.sh /path/to/projects/dir}"
LOG="$(dirname "$DIR")/git-pull.log"

echo "$(date -Iseconds) Starting git pull for repos in $DIR" >> "$LOG"

for repo in "$DIR"/*/; do
    if [ -d "$repo/.git" ]; then
        REPO_NAME=$(basename "$repo")
        cd "$repo"
        RESULT=$(git pull --ff-only 2>&1)
        if [ $? -eq 0 ]; then
            echo "$(date -Iseconds) $REPO_NAME: $RESULT" >> "$LOG"
        else
            echo "$(date -Iseconds) $REPO_NAME: FAILED — $RESULT" >> "$LOG"
        fi
    fi
done
