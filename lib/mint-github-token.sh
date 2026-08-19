#!/bin/bash
# mint-github-token — print a fresh GitHub App installation token to stdout.
#
# App-auth P1 (#1271). Thin wrapper over lib/git-credential-github-app: pipes
# a github.com credential context STRAIGHT INTO THE HELPER and prints the
# password field. It deliberately never runs `git credential fill` — that is
# the D1/D10 program invariant: outside a bot session, ambient git config
# resolves github.com through whatever helper answers first (commonly gh via
# the OS keychain), so a fill-based mint SUCCEEDS with the operator identity
# mislabeled as an App token. Helper-direct is the only shape that survives
# every context (bot, operator shell, cron); the helper's own config-file
# fallback (~/.config/claudlobby/github-app.conf) covers hosts with no env.
#
# Usage:
#   token=$(lib/mint-github-token.sh) || exit 1
#   GH_TOKEN=$(lib/mint-github-token.sh) gh pr list        # per-call, F5:
#   never export GH_TOKEN at boot — the token dies in about an hour.
#
# Failure (D9): empty stdout, reason on stderr, nonzero exit. The helper has
# already emitted its auth_mint_failed JSONL event by then.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$LIB_DIR/git-credential-github-app"

if out="$(printf 'protocol=https\nhost=github.com\n\n' | "$HELPER" get)"; then
    token="$(printf '%s\n' "$out" | awk -F= '/^password=/{print $2; exit}')"
else
    token=""
fi

if [ -z "$token" ]; then
    printf 'mint-github-token: no token from git-credential-github-app — see its stderr above and documentation/runbooks/github-app-setup.md\n' >&2
    exit 1
fi

printf '%s' "$token"
