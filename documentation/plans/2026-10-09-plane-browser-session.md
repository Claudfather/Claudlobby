# Direct-host browser pairing and sessions

Status: COMPLETE (internal implementation and local verification only).
Started and verified: 2026-10-09. Runtime activation remains pending.

Build the HTTP lifecycle connecting an already verified host visitor to the
owner policy and protected canonical Plane. This is an internal factory with
an explicitly injected identity verifier, not a Tailscale verifier or runtime
activation. Website OAuth infrastructure remains independent.

## Decisions and source comparison

- Reuse `OwnerAccess` for all challenges, approval state, session expiry,
  rotation and revocation. No new identity database or token type.
- Reuse `create_owner_app` for all private reads, static files, errors and SSE
  so per-body authorization survives this transport layer.
- Expose only exact `/api/owner/status`, `/pair`, `/login`, `/renew`, `/logout`
  lifecycle routes. No HTTP confirmation, owner revocation, message grant or
  bot action route. Local approval remains `confirm_pairing` with the exact
  challenge and displayed principal.
- Pin an external canonical HTTPS origin in trusted configuration, validate
  Host against it, and require exact Origin plus `X-Claudlobby-Owner: 1` and
  an empty JSON object for POSTs. Reject cross-origin preflight; no CORS.
  This avoids an unnecessary second CSRF-token store. Reject ambiguous
  Host/Origin/intent headers; duplicate cookies are not accepted as session
  credentials. Never infer identity from forwarded headers.
- Session cookie: `__Host-claudlobby-owner`, Secure, HttpOnly, SameSite=Strict,
  Path=/, no Domain. Tokens appear only in Set-Cookie, never JSON or URLs.
  Pairing responses return an expiring local-confirmation challenge, not a session.
  Only logout deletes cookies; stale status/renewal responses cannot erase a
  newer session from another tab. Explicit sign-in recovers a stale/malformed
  cookie using the verified principal and current local grant.
- No hosted-site iframe or cross-origin cookie promise. The first protocol is
  for the host's own origin; cross-host website integration remains separate.

## Implementation Plan

### Steps

1. **COMPLETE:** Add internal browser factory and bounded lifecycle requests.
2. **COMPLETE:** Exercise pairing refusal before local approval; cookie login, protected read,
   rotation, expiry, logout, principal mismatch and revocation.
3. **COMPLETE:** Exercise hostile Origin/Host, cookie ambiguity, body limits, unknown routes,
   and existing static/SSE boundaries. Include a real private HTTP subprocess
   with a synthetic identity verifier; no real Tailscale or bot activity.
4. **COMPLETE:** Compare independently prepared before/after exports, review the security
   boundary, update core documentation, and publish a draft stacked on #2245.

## Verification

Separate history-free prepared exports at base
`6c4806415527ab8491d1f937688de2d8927fbb81`, with private HOME/TMPDIR,
independent editable dev installs and Python 3.12:

- Before: **236 passed, 2 warnings**, exit 0.
- Candidate: **281 passed, 2 warnings**, exit 0 (45 new browser transport cases).
- Both runs had the existing Starlette/httpx deprecation and grid-test asyncio
  subprocess cleanup warning; neither had a failed test.
- The delayed stale-status/failed-renewal regression failed against the
  pre-fix implementation (exit 1), then passed with the candidate. Read-only
  review found that race; follow-up review found no further actionable issue.
- The real loopback HTTP subprocess observed cookie sign-in, private reads and
  denial after local revocation using a synthetic verifier and disposable data.
  Held SSE, static protection, body limits, identity mismatch and hostile request
  shapes were exercised separately through the ASGI/TestClient boundary.
- Candidate source/test bytes matched the passing export; staged whitespace
  and sensitive-literal checks passed. No runtime, live fleet or real bot changed.

## Evidence basis and remaining gates

Tailscale Serve removes client-supplied identity headers but documents a
backend-bypass risk if the backend can be called directly. Its injected login
header is not itself an implementation of our trusted PrincipalRef verifier:
https://tailscale.com/docs/features/tailscale-serve#identity-headers

The custom-header plus exact-origin/no-CORS policy follows the API guidance in
https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html

The shared protected view's production startup remains unchanged. A trusted
Tailscale ingress with a protected backend, local confirmation interface,
browser UI, real HTTPS/browser validation, and independent real-bot canary
remain required before runtime/browser action activation. Google setup does
not gate this local transport work.


### Admission and capacity gates before activation

The trusted verifier must admit only the explicitly intended pairing population;
verifying arbitrary tailnet membership is insufficient. Pairing is currently
bounded to 32 outstanding challenges for five minutes across the installation.
Before exposing this factory, limit outstanding challenges per verified principal
and exercise denial-of-service behavior from another admitted principal.

A current owner may hold at most 32 sessions, each lasting fifteen minutes.
Repeated cookie-less sign-ins can exhaust this capacity until those sessions
expire. The browser must serialize explicit sign-in, retain the returned cookie,
and avoid automatic sign-in retries; define and test an explicit local session
recovery or eviction policy before runtime activation. No live session is silently
evicted by this internal experiment. Renewal intentionally ends older SSE streams;
the client must reconnect using the new cookie.

Split HTTP/2 Cookie fields are recombined using semicolon separators; duplicate
session-cookie names remain refused. Missing cookies on renew/logout return
`sign_in_required` without deleting a potentially newer cookie.
