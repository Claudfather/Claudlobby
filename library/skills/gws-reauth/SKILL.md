---
name: gws-reauth
description: "Re-authorize expired Google Workspace OAuth tokens via Telegram. Probes token freshness, walks the user through browser auth, exchanges the code, and verifies — no SSH tunnels or port forwarding needed."
allowed-tools: Bash(python3 *), Bash(cat *), Bash(ls *), Bash(date *), mcp__plugin_telegram_telegram__reply
argument-hint: "[instance-name]"
---

# GWS Re-Auth

Re-authorize Google Workspace OAuth tokens interactively via Telegram. No SSH tunnels, no port forwarding — works from any device with a browser.

## Arguments

Parse `$ARGUMENTS`:
- Optional instance name (e.g., `gws-personal`, `gws-work`). If omitted, probe ALL GWS instances and re-auth any that are stale.

## How It Works

Google OAuth on a headless machine normally requires a browser on the same host (or an SSH tunnel). This skill sidesteps that: the user authorizes on their phone, the redirect to `localhost` fails (expected), but the auth code is visible in the URL bar. The user pastes the full URL back via Telegram, and the bot exchanges the code for tokens.

**Secret transport rule (non-negotiable):** the client secret, refresh token, and auth code must never appear on a command line (`ps`/`/proc` on a multi-bot host shows argv to every process, and the transcript records commands verbatim). Every step that touches them runs as a `python3` heredoc that reads credentials from the environment and the pasted URL from stdin. Never inline a secret into a shell command.

## Steps

### 1. Discover GWS instances

Read the bot's `.mcp.json` (at the bot's working directory: `$PWD/.mcp.json` or the bot dir). Find all MCP server entries whose key starts with `gws`. For each, extract from the `env` block:
- `GOOGLE_OAUTH_CLIENT_ID` env var name
- `GOOGLE_OAUTH_CLIENT_SECRET` env var name
- `WORKSPACE_MCP_CREDENTIALS_DIR` env var name
- `USER_GOOGLE_EMAIL` env var name
- `WORKSPACE_MCP_PORT` env var name

Note the resolved env var NAMES (strip `${...}`). Do not echo their values — the python snippets below read them from `os.environ` directly.

### 2. Probe token freshness

For each discovered instance (or the one specified in args), run one probe invocation, substituting only the env var *names* and the non-secret paths:

```bash
python3 <<'EOF'
import json, os, sys, urllib.parse, urllib.request

creds_dir = os.environ["<CREDS_DIR_VAR>"]
email = os.environ["<EMAIL_VAR>"]
token_file = f"{creds_dir}/{email}.json"

try:
    tok = json.load(open(token_file))
except FileNotFoundError:
    print("STALE: no token file"); sys.exit(0)
if not tok.get("refresh_token"):
    print("STALE: no refresh_token"); sys.exit(0)
# Note: an expiry in the past is NORMAL for the access token — the refresh
# probe below is the only real health signal. Do not declare staleness
# from the expiry field.

data = urllib.parse.urlencode({
    "client_id": os.environ["<CLIENT_ID_VAR>"],
    "client_secret": os.environ["<CLIENT_SECRET_VAR>"],
    "refresh_token": tok["refresh_token"],
    "grant_type": "refresh_token",
}).encode()
try:
    resp = json.load(urllib.request.urlopen(
        urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=15))
    print("HEALTHY" if resp.get("access_token") else f"STALE: {resp}")
except urllib.error.HTTPError as e:
    err = json.load(e)
    code = err.get("error", "")
    if code == "invalid_grant":
        # Revoked/expired grant — re-auth fixes this.
        print("STALE: invalid_grant")
    else:
        # invalid_client / config errors — re-auth CANNOT fix this; a browser
        # round-trip would fail the same way. Report and stop this instance.
        print(f"CONFIG ERROR: {code} — fix client credentials, not tokens")
EOF
```

`HEALTHY` → report and skip. `STALE` → continue to step 3. `CONFIG ERROR` → post the error to Telegram and do NOT proceed for this instance.

### 3. Construct and post the OAuth URL

Build the authorization URL as ONE line (Telegram linkifies up to the first whitespace — a URL with raw spaces truncates and Google 400s). Scopes come from the token file when present, else the default set below; join them with `%20` and percent-encode each value (`urllib.parse.quote` / `urlencode`):

```
https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=<CLIENT_ID>&redirect_uri=http%3A%2F%2Flocalhost%3A<PORT>%2Foauth2callback&scope=<SCOPES joined with %20>&access_type=offline&prompt=consent&login_hint=<EMAIL, percent-encoded>
```

`login_hint` matters: with multiple Google accounts signed in on the phone (exactly the multi-instance case), it preselects the right account so the wrong account's grant doesn't get written under the right account's filename. The client ID is not a secret (it rides every browser URL) — it may appear in the message.

Default scope set when the token file has none:
```
openid
https://www.googleapis.com/auth/userinfo.email
https://www.googleapis.com/auth/userinfo.profile
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.labels
https://www.googleapis.com/auth/gmail.settings.basic
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/calendar.readonly
https://www.googleapis.com/auth/calendar.events
```

Post to Telegram:
```
GWS tokens expired for <instance> (<email>).

To re-auth:
1. Open this link in Safari/Chrome — NOT the Telegram in-app browser (long-press → "Open in Safari")
2. Authorize with <email> specifically
3. You'll land on a "can't reach this page" error — that's expected
4. Copy the FULL URL from your browser's address bar
5. Paste it here in this chat

<auth URL>
```

**Important:** The Telegram in-app browser often blocks OAuth redirects to localhost. Always instruct the user to open in a real browser.

### 4. Wait for the callback URL

The user pastes back a URL like:
```
http://localhost:<port>/oauth2callback?code=<AUTH_CODE>&scope=<SCOPES>
```

Do not extract the code in shell. Feed the ENTIRE pasted URL to step 5 via stdin — the code is single-use and secret-adjacent; it must not appear on argv either.

Handle the failure shapes before exchanging:
- URL contains `error=access_denied` (user declined) → post that, offer to resend the link.
- URL has no `code=` parameter (user pasted the auth link itself, or something else) → explain what to paste, re-send the instructions.
- With multiple instances pending, match the URL's `localhost:<port>` to the pending instance's port — never assume paste order.

### 5. Exchange, save, and verify — ONE invocation

Auth codes are single-use and expire in minutes. The exchange, atomic save, and identity check happen in a single `python3` heredoc (never exchange in one call and save in another — the code burns and the tokens are lost).

Getting the pasted URL into that heredoc without argv exposure: write it to `<bot dir>/data/.gws-callback-url` **using the Write tool** (never `echo "$URL" > file` — that puts the code on argv), then run the heredoc, which reads and deletes the file (the path is not a secret; its content is):

```bash
python3 <<'EOF'
import json, os, sys, tempfile, urllib.parse, urllib.request

url_file = os.path.expanduser("<bot dir>/data/.gws-callback-url")
pasted = open(url_file).read().strip()
os.remove(url_file)

qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
if "error" in qs:
    print(f"AUTH DECLINED: {qs['error'][0]}"); sys.exit(0)
if "code" not in qs:
    print("NO CODE in pasted URL — user pasted the wrong thing"); sys.exit(0)

client_id = os.environ["<CLIENT_ID_VAR>"]
client_secret = os.environ["<CLIENT_SECRET_VAR>"]
creds_dir = os.environ["<CREDS_DIR_VAR>"]
email = os.environ["<EMAIL_VAR>"]
port = os.environ["<PORT_VAR>"]

data = urllib.parse.urlencode({
    "code": qs["code"][0],
    "client_id": client_id,
    "client_secret": client_secret,
    "redirect_uri": f"http://localhost:{port}/oauth2callback",
    "grant_type": "authorization_code",
}).encode()
try:
    resp = json.load(urllib.request.urlopen(
        urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=15))
except urllib.error.HTTPError as e:
    err = json.load(e)
    # The code is now burned regardless. NEVER retry the same code —
    # the fix for any error here is a FRESH auth URL (back to step 3).
    print(f"EXCHANGE FAILED: {err.get('error','?')} — send a fresh auth link")
    sys.exit(0)

# Identity check: the refresh probe only proves the token WORKS, not that
# it belongs to <email>. A wrong-account grant here would silently write
# the wrong account's tokens under the right filename.
who = json.load(urllib.request.urlopen(urllib.request.Request(
    "https://www.googleapis.com/oauth2/v3/userinfo",
    headers={"Authorization": f"Bearer {resp['access_token']}"}), timeout=15))
if who.get("email", "").lower() != email.lower():
    print(f"WRONG ACCOUNT: authorized {who.get('email')} but this instance is {email} — send a fresh link, authorize {email}")
    sys.exit(0)

from datetime import datetime, timedelta, timezone
token_data = {
    "token": resp["access_token"],
    "refresh_token": resp["refresh_token"],
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": client_id,
    "client_secret": client_secret,
    "scopes": resp["scope"].split(" "),
    "expiry": (datetime.now(timezone.utc) + timedelta(seconds=resp["expires_in"])).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
}
# Atomic + 0600: the file holds client_secret + refresh_token.
fd, tmp = tempfile.mkstemp(dir=creds_dir)
with os.fdopen(fd, "w") as f:
    json.dump(token_data, f, indent=2)
os.chmod(tmp, 0o600)
os.rename(tmp, f"{creds_dir}/{email}.json")
print(f"SAVED + VERIFIED for {email}")
EOF
```

### 6. Confirm

On `SAVED + VERIFIED`, re-run the step-2 probe once (expect `HEALTHY`), then post to Telegram:

```
GWS re-auth complete for <instance> (<email>). Tokens verified and working.
```

On any of the failure prints (`AUTH DECLINED`, `NO CODE`, `EXCHANGE FAILED`, `WRONG ACCOUNT`): post the message and — where the fix is a retry — send a FRESH auth URL from step 3. Never reuse a code.

## Rules

- **No secrets on argv, ever.** client_secret / refresh_token / auth code ride env + stdin/files only; the pasted callback URL is written via the Write tool, never `echo`'d. Redact to last 4 chars if debugging.
- Never log or display the full client_secret, refresh_token, or access_token.
- Always use `prompt=consent` + `access_type=offline` (forces a new refresh token) and `login_hint=<email>` (preselects the right account).
- The auth URL must be a single line, scopes `%20`-joined and percent-encoded — Telegram truncates at whitespace.
- If multiple instances are stale, handle them one at a time, and correlate pasted URLs by port.
- The redirect_uri MUST match exactly what's registered in the GCP console: `http://localhost:<PORT>/oauth2callback` with the instance's port.
- **Auth codes are single-use and short-lived.** Exchange + identity-check + save happen in ONE invocation; any failure means a fresh URL, never a retried code.
- The credential file path is `$CREDENTIALS_DIR/$EMAIL.json` — NOT `$CREDENTIALS_DIR/credentials/$EMAIL.json`. The env var already points to the credentials subdirectory.
- Token file writes are atomic (mkstemp in the same dir + rename) and 0600 before rename.
- Post all user-facing messages via the Telegram reply tool, not stdout.
