#!/usr/bin/env python3
"""
mint-stytch-storage-state.py — server-side Stytch session minter.

Reads STYTCH_PROJECT_ID + STYTCH_SECRET + TEST_USERNAME + TEST_PASSWORD
from the environment, calls Stytch's `passwords.authenticate` API to mint
a session, and writes a Playwright storageState JSON to stdout.

The storage state populates two layers:

1. Cookies — `stytch_session` + `stytch_session_jwt`. The Stytch JS SDK
   reads these to hydrate the user session client-side. Without them,
   `useStytchUser()` returns null and the FE renders the logged-out UI.

2. localStorage — `ARTEMIS_STYTCH_TOKEN`. The Artemis API client
   (`src/lib/api/client.ts:authenticatedFetch`) reads this and attaches
   it as the `Authorization` header on data-svc requests.

Both layers are seeded for http://localhost:3000 (local dev).

Used by playwright-mcp-wrapper.sh (sibling under library/scripts/) —
designer personas pass the resulting JSON to @playwright/mcp via
--storage-state, so headless screenshots render as the test user
instead of anonymous.

Exit codes:
  0 — success (storageState written to stdout)
  1 — auth failed / missing env / network failure (stderr has detail)

Avoid Python deps so the wrapper can call this with system python3.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode

ARTEMIS_INVEST_ORIGINS = [
    {"origin": "http://localhost:3000", "cookie_domain": "localhost", "secure": False},
]
TOKEN_KEY = "ARTEMIS_STYTCH_TOKEN"
STYTCH_SESSION_COOKIE = "stytch_session"
STYTCH_SESSION_JWT_COOKIE = "stytch_session_jwt"
SESSION_DURATION_MIN = 60

required = ["STYTCH_PROJECT_ID", "STYTCH_SECRET", "TEST_USERNAME", "TEST_PASSWORD"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"missing required env: {missing}", file=sys.stderr)
    sys.exit(1)

project_id = os.environ["STYTCH_PROJECT_ID"]
secret = os.environ["STYTCH_SECRET"]
base_url = os.environ.get("STYTCH_BASE_URL", "https://api.stytch.com/v1")
auth = b64encode(f"{project_id}:{secret}".encode()).decode()

req = urllib.request.Request(
    f"{base_url}/passwords/authenticate",
    data=json.dumps({
        "email": os.environ["TEST_USERNAME"],
        "password": os.environ["TEST_PASSWORD"],
        "session_duration_minutes": SESSION_DURATION_MIN,
    }).encode(),
    headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
except urllib.error.HTTPError as e:
    err_body = e.read().decode(errors="replace")[:300]
    print(f"stytch auth HTTP {e.code}: {err_body}", file=sys.stderr)
    sys.exit(1)
except urllib.error.URLError as e:
    print(f"stytch network error: {e.reason}", file=sys.stderr)
    sys.exit(1)

session_token = body.get("session_token")
session_jwt = body.get("session_jwt")
if not session_token or not session_jwt:
    print(f"stytch returned no session_token/jwt: keys={list(body.keys())}", file=sys.stderr)
    sys.exit(1)

# Cookies expire when the Stytch session does
expires_at = int(time.time()) + SESSION_DURATION_MIN * 60

cookies = []
for o in ARTEMIS_INVEST_ORIGINS:
    for cookie_name, cookie_value in [
        (STYTCH_SESSION_COOKIE, session_token),
        (STYTCH_SESSION_JWT_COOKIE, session_jwt),
    ]:
        cookies.append({
            "name": cookie_name,
            "value": cookie_value,
            "domain": o["cookie_domain"],
            "path": "/",
            "expires": expires_at,
            "httpOnly": False,
            "secure": o["secure"],
            "sameSite": "Lax",
        })

storage_state = {
    "cookies": cookies,
    "origins": [
        {
            "origin": o["origin"],
            "localStorage": [{"name": TOKEN_KEY, "value": session_token}],
        }
        for o in ARTEMIS_INVEST_ORIGINS
    ],
}
print(json.dumps(storage_state))
