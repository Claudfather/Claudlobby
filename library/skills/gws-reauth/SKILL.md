---
name: gws-reauth
description: "Re-authorize expired Google Workspace OAuth tokens via Telegram. Probes token freshness, walks the user through browser auth, exchanges the code, and verifies — no SSH tunnels or port forwarding needed."
allowed-tools: Bash(curl *), Bash(python3 *), Bash(cat *), Bash(ls *), Bash(date *), mcp__plugin_telegram_telegram__reply
argument-hint: "[instance-name]"
---

# GWS Re-Auth

Re-authorize Google Workspace OAuth tokens interactively via Telegram. No SSH tunnels, no port forwarding — works from any device with a browser.

## Arguments

Parse `$ARGUMENTS`:
- Optional instance name (e.g., `gws-personal`, `gws-work`). If omitted, probe ALL GWS instances and re-auth any that are stale.

## How It Works

Google OAuth on a headless machine normally requires a browser on the same host (or an SSH tunnel). This skill sidesteps that: the user authorizes on their phone, the redirect to `localhost` fails (expected), but the auth code is visible in the URL bar. The user pastes it back via Telegram, and the bot exchanges it for tokens directly via curl.

## Steps

### 1. Discover GWS instances

Read the bot's `.mcp.json` (at the bot's working directory: `$PWD/.mcp.json` or the bot dir). Find all MCP server entries whose key starts with `gws`. For each, extract from the `env` block:
- `GOOGLE_OAUTH_CLIENT_ID` env var name
- `GOOGLE_OAUTH_CLIENT_SECRET` env var name
- `WORKSPACE_MCP_CREDENTIALS_DIR` env var name
- `USER_GOOGLE_EMAIL` env var name
- `WORKSPACE_MCP_PORT` env var name

Resolve these env var references (strip `${...}`) and read their values from the current environment.

### 2. Probe token freshness

For each discovered instance (or the one specified in args):

```bash
CREDS_DIR="<resolved credentials dir>"
EMAIL="<resolved email>"
TOKEN_FILE="$CREDS_DIR/$EMAIL.json"
```

Check if `$TOKEN_FILE` exists. If it does, read it and check:
- Does it have a `refresh_token`?
- Is the `expiry` in the past?

Then attempt a lightweight probe — use the refresh token to get a fresh access token:

```bash
curl -s -X POST https://oauth2.googleapis.com/token \
  -d "client_id=$CLIENT_ID" \
  -d "client_secret=$CLIENT_SECRET" \
  -d "refresh_token=$REFRESH_TOKEN" \
  -d "grant_type=refresh_token"
```

If this returns a valid `access_token`, the instance is healthy — report it and skip. If it errors (e.g., `invalid_grant`), the instance needs re-auth.

### 3. Construct and post the OAuth URL

For each stale instance, build the authorization URL:

```
https://accounts.google.com/o/oauth2/auth?
  response_type=code
  &client_id=<CLIENT_ID>
  &redirect_uri=http://localhost:<PORT>/oauth2callback
  &scope=<SCOPES from token file, space-separated>
  &access_type=offline
  &prompt=consent
```

If scopes aren't in the token file, use the full default set:
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
2. Authorize with your Google account
3. You'll land on a "can't reach this page" error — that's expected
4. Copy the FULL URL from your browser's address bar
5. Paste it here in this chat

<auth URL>
```

**Important:** The Telegram in-app browser often blocks OAuth redirects to localhost. Always instruct the user to open in a real browser.

### 4. Wait for the callback URL

The user will paste back a URL that looks like:
```
http://localhost:<port>/oauth2callback?code=<AUTH_CODE>&state=<STATE>&scope=<SCOPES>
```

Extract the `code` parameter from this URL.

### 5. Exchange code for tokens

```bash
curl -s -X POST https://oauth2.googleapis.com/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "code=$AUTH_CODE" \
  -d "client_id=$CLIENT_ID" \
  -d "client_secret=$CLIENT_SECRET" \
  -d "redirect_uri=http://localhost:$PORT/oauth2callback" \
  -d "grant_type=authorization_code"
```

This returns JSON with `access_token`, `refresh_token`, `expires_in`, `scope`, `token_type`.

### 6. Save tokens

Write the token file to `$CREDS_DIR/$EMAIL.json` in the format the workspace-mcp server expects:

```python
import json
from datetime import datetime, timedelta, timezone

token_data = {
    "token": response["access_token"],
    "refresh_token": response["refresh_token"],
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scopes": response["scope"].split(" "),
    "expiry": (datetime.now(timezone.utc) + timedelta(seconds=response["expires_in"])).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
}
```

Write this atomically (write to a temp file, then rename).

### 7. Verify

Re-run the refresh token probe from step 2. If it returns a valid access token, post to Telegram:

```
GWS re-auth complete for <instance> (<email>). Tokens verified and working.
```

If it fails, post the error and suggest the user try again.

## Rules

- Never log or display the full client_secret, refresh_token, or access_token. Redact to last 4 chars if debugging.
- Always use `prompt=consent` in the auth URL to force a new refresh token.
- Always use `access_type=offline` to get a refresh token.
- If multiple instances are stale, handle them one at a time — don't overwhelm the user with multiple auth URLs at once.
- The redirect_uri MUST match exactly what's registered in the GCP console. Use `http://localhost:<PORT>/oauth2callback` with the port from the instance config.
- **Auth codes are single-use.** The exchange + save MUST happen in a single bash/python invocation. Never exchange the code in one call and try to use the response in a second call — the code will be burned.
- The credential file path is `$CREDENTIALS_DIR/$EMAIL.json` — NOT `$CREDENTIALS_DIR/credentials/$EMAIL.json`. The env var already points to the credentials subdirectory.
- Write tokens atomically: write to a temp file in the same directory, then `os.rename()` to the final path.
- Post all user-facing messages via the Telegram reply tool, not stdout.
