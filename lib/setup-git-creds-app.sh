#!/bin/bash
# setup_git_creds_app.sh — one-time setup for GitHub App installation token auth
set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 --app-id ID --installation-id ID --private-key PATH --bot-slug SLUG

Required:
  --app-id           GitHub App ID (e.g., 1234567)
  --installation-id  Installation ID for the org/account
  --private-key      Path to the App's private-key.pem on this box
  --bot-slug         The App's slug (e.g., 'artemis-infra-botfarm'); used to derive the bot's username

Optional:
  -h, --help         Show this help

Example:
  $0 --app-id 1234567 \\
     --installation-id 7654321 \\
     --private-key ~/.config/botfarm/private-key.pem \\
     --bot-slug artemis-infra-botfarm
EOF
  exit "${1:-0}"
}

APP_ID=""
INSTALLATION_ID=""
PRIVATE_KEY=""
BOT_SLUG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-id)          APP_ID="$2";          shift 2 ;;
    --installation-id) INSTALLATION_ID="$2"; shift 2 ;;
    --private-key)     PRIVATE_KEY="$2";     shift 2 ;;
    --bot-slug)        BOT_SLUG="$2";        shift 2 ;;
    -h|--help)         usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 1 ;;
  esac
done

[[ -z "$APP_ID"          ]] && { echo "Error: --app-id is required"          >&2; usage 1; }
[[ -z "$INSTALLATION_ID" ]] && { echo "Error: --installation-id is required" >&2; usage 1; }
[[ -z "$PRIVATE_KEY"     ]] && { echo "Error: --private-key is required"     >&2; usage 1; }
[[ -z "$BOT_SLUG"        ]] && { echo "Error: --bot-slug is required"        >&2; usage 1; }

# Resolve to absolute path
PRIVATE_KEY=$(cd "$(dirname "$PRIVATE_KEY")" && echo "$PWD/$(basename "$PRIVATE_KEY")")

# Validate inputs
if [[ ! -r "$PRIVATE_KEY" ]]; then
  echo "Error: cannot read private key at $PRIVATE_KEY" >&2
  exit 1
fi

if ! head -1 "$PRIVATE_KEY" | grep -q "BEGIN .* PRIVATE KEY"; then
  echo "Error: $PRIVATE_KEY does not look like a PEM-formatted private key" >&2
  exit 1
fi

if ! openssl rsa -in "$PRIVATE_KEY" -check -noout >/dev/null 2>&1; then
  echo "Error: $PRIVATE_KEY does not parse as a valid RSA private key." >&2
  echo "       Common causes: CRLF line endings (run: tr -d '\\r' < key > key.fixed)," >&2
  echo "       truncated file from scp, or a public key was supplied by mistake." >&2
  exit 1
fi

if ! python3 -c "import jwt, cryptography" 2>/dev/null; then
  echo "Error: pyjwt and cryptography are required (cryptography is needed for RS256 signing)." >&2
  echo "       Run: uv pip install pyjwt cryptography --break-system-packages --system" >&2
  exit 1
fi

# Mint a test token to validate app_id + installation_id + private key
echo "Validating credentials by minting a test token..."
test_token=$(BOTFARM_APP_ID="$APP_ID" \
             BOTFARM_INSTALLATION_ID="$INSTALLATION_ID" \
             BOTFARM_PRIVATE_KEY_PATH="$PRIVATE_KEY" \
             python3 - <<'PY'
import json, os, sys, time, urllib.request
import jwt

now = int(time.time())
app_jwt = jwt.encode(
    {"iat": now - 60, "exp": now + 540, "iss": os.environ["BOTFARM_APP_ID"]},
    open(os.environ["BOTFARM_PRIVATE_KEY_PATH"]).read(),
    algorithm="RS256",
)
req = urllib.request.Request(
    f"https://api.github.com/app/installations/{os.environ['BOTFARM_INSTALLATION_ID']}/access_tokens",
    method="POST",
    headers={
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(json.loads(r.read())["token"])
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"FAIL: GitHub returned {e.code}: {body}", file=sys.stderr)
    if e.code == 401 and "could not be decoded" in body:
        print("", file=sys.stderr)
        print(f"GitHub couldn't verify the JWT signature against any public key", file=sys.stderr)
        print(f"registered for App ID {os.environ['BOTFARM_APP_ID']}.", file=sys.stderr)
        print("Most likely causes, in order:", file=sys.stderr)
        print("  1. The .pem on this box is for a DIFFERENT App than --app-id.", file=sys.stderr)
        print("     Compute its fingerprint and compare to the App settings page:", file=sys.stderr)
        print(f"       openssl rsa -in {os.environ['BOTFARM_PRIVATE_KEY_PATH']} -pubout 2>/dev/null \\", file=sys.stderr)
        print("         | openssl rsa -pubin -outform DER 2>/dev/null \\", file=sys.stderr)
        print("         | openssl dgst -sha256 -binary | openssl base64", file=sys.stderr)
        print("  2. --app-id is wrong (you may have passed Installation ID or Client ID).", file=sys.stderr)
        print("     The App ID is the numeric value at the top of the App's settings page.", file=sys.stderr)
        print("  3. The private key was deleted/revoked in App settings; generate a new one.", file=sys.stderr)
    elif e.code == 401 and ("iat" in body or "expired" in body):
        print("", file=sys.stderr)
        print("This usually indicates clock skew. Check `date` against UTC and resync NTP.", file=sys.stderr)
    sys.exit(1)
PY
)

if [[ -z "$test_token" || ! "$test_token" =~ ^ghs_ ]]; then
  echo "Error: failed to mint a valid installation token" >&2
  exit 1
fi
echo "✓ Test token minted successfully"

# Look up the bot's numeric user ID for the noreply email
BOT_USERNAME="${BOT_SLUG}[bot]"
bot_id=$(curl -fsS --globoff \
  -H "Authorization: Bearer $test_token" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/users/$BOT_USERNAME" | jq -r '.id')

if [[ -z "$bot_id" || "$bot_id" == "null" ]]; then
  echo "Error: could not look up user ID for $BOT_USERNAME" >&2
  exit 1
fi
echo "✓ Bot user: $BOT_USERNAME (id: $bot_id)"

BOT_EMAIL="${bot_id}+${BOT_USERNAME}@users.noreply.github.com"

# Install the credential helper to a directory git can find without PATH changes
helper_src="$(dirname "$0")/git-credential-botfarm"
if [[ ! -f "$helper_src" ]]; then
  echo "Error: git-credential-botfarm not found next to setup script" >&2
  exit 1
fi

# Pick an install location that's already on PATH for non-interactive shells.
helper_dst=""
for candidate in /usr/local/bin /opt/homebrew/bin; do
  if [[ -w "$candidate" ]]; then
    helper_dst="$candidate/git-credential-botfarm"
    install -m 755 "$helper_src" "$helper_dst"
    break
  fi
done

if [[ -z "$helper_dst" ]]; then
  if sudo -n true 2>/dev/null || sudo true; then
    helper_dst="/usr/local/bin/git-credential-botfarm"
    sudo install -m 755 "$helper_src" "$helper_dst"
  else
    echo "Error: cannot install helper to /usr/local/bin or /opt/homebrew/bin." >&2
    echo "       Re-run with sudo, or grant write access to one of those directories." >&2
    exit 1
  fi
fi
echo "✓ Helper installed: $helper_dst"

# Write config to ~/.config/botfarm/config — helper reads from here.
# No need to edit shell rc files; the helper is fully self-configured.
config_dir="$HOME/.config/botfarm"
config_file="$config_dir/config"
mkdir -p "$config_dir"
chmod 700 "$config_dir"

cat > "$config_file" <<EOF
# Auto-generated by setup_git_creds_app.sh — safe to edit manually.
# Read by git-credential-botfarm at every git auth invocation.
BOTFARM_APP_ID="$APP_ID"
BOTFARM_INSTALLATION_ID="$INSTALLATION_ID"
BOTFARM_PRIVATE_KEY_PATH="$PRIVATE_KEY"
EOF
chmod 600 "$config_file"
echo "✓ Helper config written to $config_file"

# Configure git
git config --global --unset-all credential.helper 2>/dev/null || true
git config --global credential.helper 'cache --timeout=3000'
git config --global --add credential.helper botfarm
git config --global user.name "$BOT_USERNAME"
git config --global user.email "$BOT_EMAIL"
git config --global url."https://github.com/".insteadOf "git@github.com:"

# Sanity check: ask git itself what it would send to github.com
fill_output=$(printf 'url=https://github.com\n\n' | git credential fill 2>/dev/null || true)
fill_password=$(printf '%s\n' "$fill_output" | grep '^password=' | head -1 | cut -d= -f2-)

if [[ ! "$fill_password" =~ ^ghs_ ]]; then
  echo "Error: git is not returning an installation token for github.com." >&2
  echo "       Got password starting with: ${fill_password:0:4}" >&2
  echo "       Another credential helper may be intercepting requests." >&2
  echo "       Common cause: 'gh' CLI registered itself as a helper." >&2
  echo "       Run: git config --list --show-origin | grep credential" >&2
  exit 1
fi

echo "✓ Git credentials configured"
echo "  App ID:           $APP_ID"
echo "  Installation ID:  $INSTALLATION_ID"
echo "  Private key:      $PRIVATE_KEY"
echo "  Bot identity:     $BOT_USERNAME"
echo "  Bot email:        $BOT_EMAIL"
