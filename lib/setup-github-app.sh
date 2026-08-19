#!/bin/bash
# setup-github-app — one-time host setup + validation for GitHub App auth.
#
# App-auth P1 (#1271; runbook: documentation/runbooks/github-app-setup.md).
# Validates the App credentials end to end THROUGH THE REAL HELPER (the closed
# loop: the exact code path production mints with, not a lookalike), writes
# the operator/cron config file, and prints the fleet wiring values.
#
# What the fork version of this script did that this one deliberately does
# NOT: no `git config --global` writes (the composer owns per-bot git config
# via compose_bot_gitconfig — hand-editing global config is the collision the
# per-org routing plan exists to end), and no /usr/local/bin or sudo helper
# install (the composed gitconfig references the helper by absolute lib/
# path, so nothing needs to be on PATH).
#
# Usage:
#   lib/setup-github-app.sh --app-id 1234567 --installation-id 7654321 \
#       --private-key ~/keys/my-app.private-key.pem --slug my-fleet-bot
#
# Steps: dependency check -> PEM validation (with the CRLF diagnostic) ->
# live test mint via lib/git-credential-github-app -> ghs_ assert -> bot
# user-id lookup -> noreply email -> config-file write (0600) -> config-only
# re-mint (proves the cron/operator fallback path) -> print the fleet .env
# names and the fleet.yaml github_app: snippet.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

HELPER="$LIB_DIR/git-credential-github-app"

usage() {
    cat >&2 <<'EOF'
Usage: setup-github-app.sh --app-id ID --installation-id ID --private-key PATH --slug SLUG

Required:
  --app-id           GitHub App ID (numeric, top of the App settings page —
                     NOT the Client ID directly below it)
  --installation-id  Installation ID (the org installation URL ends in it)
  --private-key      Path to the App private-key .pem on this host
  --slug             The App slug (its URL name); the bot identity is <slug>[bot]

Optional:
  --config-path P    Where to write the helper config
                     (default: ~/.config/claudlobby/github-app.conf)
  --no-write-config  Validate only; do not write the config file
  -h, --help         Show this help

Example:
  setup-github-app.sh --app-id 1234567 --installation-id 7654321 \
      --private-key ~/keys/private-key.pem --slug my-fleet-bot
EOF
    exit "${1:-0}"
}

APP_ID=""
INSTALLATION_ID=""
PRIVATE_KEY=""
SLUG=""
WRITE_CONFIG=1
CONFIG_PATH="${CLAUDLOBBY_GITHUB_APP_CONF:-${HOME:-/nonexistent}/.config/claudlobby/github-app.conf}"

while [ $# -gt 0 ]; do
    case "$1" in
        --app-id)          APP_ID="$2";          shift 2 ;;
        --installation-id) INSTALLATION_ID="$2"; shift 2 ;;
        --private-key)     PRIVATE_KEY="$2";     shift 2 ;;
        --slug)            SLUG="$2";            shift 2 ;;
        --config-path)     CONFIG_PATH="$2";     shift 2 ;;
        --no-write-config) WRITE_CONFIG=0;       shift ;;
        -h|--help)         usage 0 ;;
        *) printf 'setup-github-app: unknown argument: %s\n' "$1" >&2; usage 1 ;;
    esac
done

[ -n "$APP_ID" ]          || { printf 'setup-github-app: --app-id is required\n' >&2; usage 1; }
[ -n "$INSTALLATION_ID" ] || { printf 'setup-github-app: --installation-id is required\n' >&2; usage 1; }
[ -n "$PRIVATE_KEY" ]     || { printf 'setup-github-app: --private-key is required\n' >&2; usage 1; }
[ -n "$SLUG" ]            || { printf 'setup-github-app: --slug is required\n' >&2; usage 1; }

# --- 1. dependencies ---------------------------------------------------------
for dep in openssl curl jq; do
    if ! command -v "$dep" > /dev/null 2>&1; then
        printf 'setup-github-app: required tool missing: %s\n' "$dep" >&2
        printf '  App-mode auth needs openssl, curl and jq (macOS: brew install %s; Debian: apt install %s)\n' "$dep" "$dep" >&2
        exit 1
    fi
done
printf '1/6 dependencies present (openssl, curl, jq)\n'

# --- 2. private key ----------------------------------------------------------
PRIVATE_KEY="$(cd "$(dirname "$PRIVATE_KEY")" && printf '%s/%s' "$PWD" "$(basename "$PRIVATE_KEY")")"

if [ ! -r "$PRIVATE_KEY" ]; then
    printf 'setup-github-app: cannot read private key at %s\n' "$PRIVATE_KEY" >&2
    exit 1
fi
if ! head -1 "$PRIVATE_KEY" | grep -q 'BEGIN.*PRIVATE KEY'; then
    printf 'setup-github-app: %s does not look like a PEM private key\n' "$PRIVATE_KEY" >&2
    exit 1
fi
if ! openssl rsa -in "$PRIVATE_KEY" -check -noout > /dev/null 2>&1; then
    cat >&2 <<EOF
setup-github-app: $PRIVATE_KEY does not parse as a valid RSA private key.
  Common causes:
    - CRLF line endings from a Windows editor or some transfers
      (fix: tr -d '\\r' < key.pem > key.fixed.pem)
    - a truncated file from an interrupted scp
    - a PUBLIC key was supplied by mistake
EOF
    exit 1
fi
printf '2/6 private key parses as RSA\n'

# --- 3. live test mint through the real helper (the closed loop) ------------
mint_err="$(safe_mktemp)"
mint_out=""
if mint_out="$(printf 'protocol=https\nhost=github.com\n\n' \
        | GITHUB_APP_ID="$APP_ID" \
          GITHUB_APP_INSTALLATION_ID="$INSTALLATION_ID" \
          GITHUB_APP_PRIVATE_KEY_PATH="$PRIVATE_KEY" \
          "$HELPER" get 2> "$mint_err")"; then
    :
else
    printf 'setup-github-app: test mint FAILED. Helper said:\n' >&2
    sed 's/^/  /' "$mint_err" >&2
    if grep -q '401' "$mint_err"; then
        cat >&2 <<FIXES

GitHub could not verify the JWT against any key registered for App ID ${APP_ID}.
Most likely causes, in order:
  1. The .pem on this host belongs to a DIFFERENT App than --app-id.
     Compare its fingerprint to the one shown on the App settings page:
       openssl rsa -in ${PRIVATE_KEY} -pubout 2>/dev/null \\
         | openssl rsa -pubin -outform DER 2>/dev/null \\
         | openssl dgst -sha256 -binary | openssl base64
  2. --app-id is wrong. The App ID is the small NUMERIC value at the top of
     the App settings page — not the Client ID (Iv23li...) below it, and not
     the Installation ID.
  3. The key was deleted or revoked in the App settings; generate a new one.
  4. Clock skew: JWT iat/exp checks fail on a wrong clock. Compare date -u
     against real UTC and resync NTP (RTC-less hosts drift at every boot).
FIXES
    fi
    exit 1
fi

token="$(printf '%s\n' "$mint_out" | awk -F= '/^password=/{print $2; exit}')"
case "$token" in
    ghs_*) printf '3/6 test mint OK (installation token, ghs_...)\n' ;;
    *)
        printf 'setup-github-app: helper returned a credential that is not an installation token (got prefix %.4s...)\n' "$token" >&2
        exit 1
        ;;
esac

# --- 4. bot identity ---------------------------------------------------------
BOT_USERNAME="${SLUG}[bot]"
auth_cfg="$(safe_mktemp)"
printf 'header = "Authorization: Bearer %s"\n' "$token" > "$auth_cfg"
printf 'header = "X-GitHub-Api-Version: 2022-11-28"\n' >> "$auth_cfg"
bot_id="$(curl -fsS --globoff --max-time 15 --config "$auth_cfg" \
    "https://api.github.com/users/${BOT_USERNAME}" | jq -r '.id // empty')" || bot_id=""
if [ -z "$bot_id" ]; then
    printf 'setup-github-app: could not look up user id for %s — is --slug the App URL slug?\n' "$BOT_USERNAME" >&2
    exit 1
fi
BOT_EMAIL="${bot_id}+${BOT_USERNAME}@users.noreply.github.com"
printf '4/6 bot identity: %s (id %s)\n' "$BOT_USERNAME" "$bot_id"

# --- 5. config file (operator/cron fallback path) ---------------------------
if [ "$WRITE_CONFIG" -eq 1 ]; then
    config_dir="$(dirname "$CONFIG_PATH")"
    mkdir -p "$config_dir"
    chmod 700 "$config_dir"
    umask 177
    cat > "$CONFIG_PATH" <<EOF
# Written by lib/setup-github-app.sh — read by lib/git-credential-github-app
# when the GITHUB_APP_* env vars are absent (operator shells, cron).
# Bot sessions get the same values from the fleet .env tier instead.
GITHUB_APP_ID="$APP_ID"
GITHUB_APP_INSTALLATION_ID="$INSTALLATION_ID"
GITHUB_APP_PRIVATE_KEY_PATH="$PRIVATE_KEY"
EOF
    umask 022
    chmod 600 "$CONFIG_PATH"

    # Closed loop for the fallback: re-mint with the env deliberately EMPTY so
    # only the file we just wrote can supply the credentials.
    if env -u GITHUB_APP_ID -u GITHUB_APP_INSTALLATION_ID -u GITHUB_APP_PRIVATE_KEY_PATH \
            CLAUDLOBBY_GITHUB_APP_CONF="$CONFIG_PATH" \
            bash -c 'printf "protocol=https\nhost=github.com\n\n" | "$1" get > /dev/null' _ "$HELPER" 2> /dev/null; then
        printf '5/6 config written: %s (0600) — config-only re-mint OK\n' "$CONFIG_PATH"
    else
        printf 'setup-github-app: config file written but the config-only re-mint FAILED — check %s\n' "$CONFIG_PATH" >&2
        exit 1
    fi
else
    printf '5/6 config write skipped (--no-write-config)\n'
fi

# --- 6. fleet wiring values --------------------------------------------------
printf '6/6 done. Wire the fleet:\n\n'
printf '  Fleet-tier .env (local/<fleet>/.env) — names are the contract, values stay out of git:\n'
printf '    GITHUB_APP_ID=%s\n' "$APP_ID"
printf '    GITHUB_APP_INSTALLATION_ID=%s\n' "$INSTALLATION_ID"
printf '    GITHUB_APP_PRIVATE_KEY_PATH=%s\n\n' "$PRIVATE_KEY"
printf '  fleet.yaml (per-bot git routing lands with App-auth P3 #1273):\n'
printf '    defaults:\n'
printf '      github_app:\n'
printf '        slug: %s\n' "$SLUG"
printf '        bot_user_id: %s\n' "$bot_id"
printf '        # orgs: [YourOrg]   # optional: route only these orgs via the App\n\n'
printf '  MCP (App-token GitHub server, App-auth P2 #1272): mcp: [github-app]\n'
printf '  Commit identity when P3 lands: %s <%s>\n\n' "$BOT_USERNAME" "$BOT_EMAIL"
printf '  Runbook: documentation/runbooks/github-app-setup.md\n'
