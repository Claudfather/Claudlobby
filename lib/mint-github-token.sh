#!/bin/bash
# mint-github-token — print a fresh GitHub installation token to stdout.
#
# Uses git's credential helper chain. With `git-credential-botfarm`
# installed (see documentation/runbooks/github-app-setup.md), this
# signs a JWT with the GitHub App's private key and exchanges it for a
# `ghs_` installation token (~1-hour lifetime). The cache helper layer
# in front of it amortizes the JWT exchange across calls.
#
# Usage:
#   token=$(lib/mint-github-token.sh) || exit 1
#   curl -H "Authorization: Bearer $token" https://api.github.com/user
#   GH_TOKEN="$token" gh pr list
#
# Exits non-zero if no token could be minted. Use this in skill scripts
# that need a short-lived GitHub token without baking a long-lived PAT
# into the fleet's .env.
set -euo pipefail

token="$(
    printf 'protocol=https\nhost=github.com\n\n' \
    | git credential fill 2>/dev/null \
    | awk -F= '/^password=/{print $2; exit}'
)"

if [ -z "$token" ]; then
    echo "mint-github-token: no token from git credential helper. Is git-credential-botfarm installed and ~/.config/botfarm/ configured? See documentation/runbooks/github-app-setup.md." >&2
    exit 1
fi

printf '%s' "$token"
