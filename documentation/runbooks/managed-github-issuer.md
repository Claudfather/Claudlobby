# Managed GitHub issuer

The shared package can serve the token endpoint used by named managed GitHub
connections. The issuer keeps the App key; customer computers keep a random
issuer credential bound to their approved workspace and deployment. The website
does not mint tokens or proxy repository data. Independent App connections
continue to mint locally without this service.

This is the service/operator interface. Browser GitHub consent verifies the
person's App installation and selected repository access. The operator can stage
a grant, or explicitly enable account-bound enrollment as described below.
Guided host credential persistence and the browser setup integration remain to
be connected before self-service onboarding. Installing the package does not
enable a public service.

## Provision an approved connection

On the issuer machine, use a private directory (0700) and private files (0600).
Install Claudlobby with its `plane-ui` extra. The App key must be readable only by
the issuer user. Configure the TLS reverse proxy separately; `serve` binds only
to loopback and disables access logging. Do not log Authorization or response
bodies at the proxy. Apply request/rate limits there before public exposure.

Prepare a cryptographically random base64url credential with at least 32 random
bytes (43 characters without padding) in `deployment-credential.txt`. Deliver
it securely into the customer's configured `issuer_token_env` through the
host's credential setup. Never paste it into a URL, browser form or command
argument. One credential may cover explicitly approved bot bindings on the same
workspace/deployment; it cannot be reused across deployments.

The operator-reviewed `grant.json` has these fields:

```json
{
  "grant_id": "grant-example",
  "workspace_id": "workspace-example",
  "deployment_id": "deployment-example",
  "binding": { "...": "exact managed row from the composed github-bindings.json" },
  "repository_id": 123456,
  "expires_at": 1893456000
}
```

The binding placeholder is intentionally incomplete: use the actual approved
composed row, including bot, connection, attachment, assignment, permission and
revision fields. The numeric repository ID must be verified from GitHub for that
installation and current repository name. Set a Unix expiry in the future,
within one year; the sample timestamp is illustrative. App/installation changes
or changed binding revisions require a new explicit grant.

```sh
claudlobby host github-app issuer approve --store /private/issuer/grants.db \
  --grant-file /private/issuer/grant.json \
  --credential-file /private/issuer/deployment-credential.txt

claudlobby host github-app issuer serve --store /private/issuer/grants.db \
  --app-key 12345=/private/issuer/app.pem --port 8091
```

Configure the customer's `issuer_url_env` with the HTTPS address ending in
`/v1/github/token`. The existing core Git, CLI and MCP consumers use that exact
URL; no consumer change is required. The issuer ignores ambient proxy settings,
refuses provider redirects, requests the approved repository **ID** and exact
permissions, and verifies the response before returning a token. Repository
rename/transfer or excess scope refuses issuance until reauthorized. See
[GitHub's installation-token contract](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

## Revoke and recover

```sh
claudlobby host github-app issuer revoke --store /private/issuer/grants.db \
  --grant-id grant-example --json
```

Revocation waits for any issuance transaction already in progress, then durably
blocks further issuance for this grant. Its response reports the latest issued
token's expiry separately from host enforcement, which remains unconfirmed.
Already issued tokens can remain usable until that expiry; this command does
not contact the customer host or revoke its issued token at GitHub. The issuer
does not retain raw tokens. Stop affected operations and revoke known tokens on
the reachable host separately. Reusing a revoked grant ID is refused.

Store or GitHub outages return a fixed unavailable error; missing, ambiguous,
expired or revoked grants return a fixed denial. Nothing falls back to personal
credentials or another App. The issuer retains only authorization references,
a credential hash and last token expiry, not tasks, conversations or checkouts.
Preserve this private store across service restarts; loss requires explicit
reapproval, not automatic reconstruction from customer requests.

## Verify the repository owner through GitHub

Use `invite` instead of manual `approve` to require browser consent and provider
verification. First configure the managed App's OAuth callback as the issuer's
exact HTTPS origin plus `/v1/github/callback`, and save a private consent config:

```json
{
  "app_id": 12345,
  "client_id": "Iv1.example",
  "client_secret_path": "/private/issuer/client-secret.txt",
  "origin": "https://issuer.example.invalid"
}
```

The client secret is separate from the App signing key. Its file and this
configuration must be mode 0600. Add `--consent-config` to `serve`, then stage an
invitation using the same reviewed grant and deployment credential:

```sh
claudlobby host github-app issuer invite --store /private/issuer/grants.db \
  --grant-file /private/issuer/grant.json \
  --credential-file /private/issuer/deployment-credential.txt \
  --consent-config /private/issuer/consent.json
```

Open the returned authorization link. It expires in 15 minutes and shows the
selected bot, repository, permissions, workspace and computer before continuing
to GitHub. The invitation itself allows no token issuance. The browser callback
must return with its original cookie and one-time OAuth state; the exchange also
uses PKCE. Personal access and refresh tokens are never persisted or returned
to the browser.

The core verifies the consenting GitHub user, exact App/installation, current
repository ID and name, and requested installation permissions. This initial
owner path requires repository administrator permission because it authorizes
independent App tokens, which could otherwise exceed a collaborator's access.
The App must already be installed on that repository; pending organization
approval, missing access, revocation, cancellation or SSO refusal does not grant
access. Retry from the invitation link in the same browser to start a new
authorization attempt, including after leaving GitHub without finishing. This
invalidates the previous attempt. Conflicting grant IDs or deployment credentials
are refused before an invitation is created; a conflict introduced during sign-in
instead shows that setup needs a corrected invitation.
After success, that link displays the completed result without granting twice.
See [GitHub's user-authorization contract](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app).

Cancel an unfinished invitation with `issuer cancel --store PATH --ticket TICKET`.
Cancellation during provider verification prevents the eventual grant. Once
approved, use `issuer revoke --grant-id GRANT_ID` instead. Configure the reverse
proxy to preserve the issuer Host header and not log callback queries, cookies
or response bodies. OAuth callbacks contain temporary authorization codes.

## Receive GitHub revocations

Configure a distinct, randomly generated webhook secret for each managed App.
Save it in a private file (0600, 32–1024 bytes; surrounding whitespace is removed)
and add `--webhook-secret 12345=/private/issuer/webhook-secret.txt` to `serve`.
Set that App's JSON webhook URL to the issuer's HTTPS origin followed by
`/v1/github/webhook/12345`. Never reuse a webhook secret across Apps. The endpoint
is absent unless explicitly configured; it verifies HMAC-SHA256 over the raw body
before parsing or changing grants. Keep webhook secrets separate from App keys
and OAuth client secrets. See [GitHub's signature contract](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).

Verified authorization revocation disables grants approved by that GitHub user
for that App. Installation deletion/suspension disables that installation's
grants, including manual operator grants. Repository removal disables matching
repository IDs. An empty removed-repository list conservatively disables the
installation's grants because GitHub uses it when narrowing all repositories to
selected repositories. Added repositories and unsuspension never restore grants;
reconnect through a fresh approval. No checkout or work history is deleted.

The same store transaction records the delivery ID, body digest and numeric
revocation scope and disables matching grants. A redelivery of the same event
is acknowledged without disabling newer approvals again; a later event with a
new delivery ID is processed even when its body matches. Consent begun before a matching revocation cannot commit
a grant afterward. Successful webhook responses mean the store committed; they
do not claim cached host tokens were invalidated. Existing token expiry and host
enforcement remain separate, as described above. No raw webhook payload is saved.

GitHub delivery availability is not continuous authorization verification. Monitor
failed deliveries and explicitly redeliver them; the handler does not fetch or
replay provider deliveries itself. It rejects bodies over 2 MiB and takes no
automatic recovery action for missing events. Personal repository-role changes
that do not emit an App authorization revocation are not currently rechecked
after consent. Configure and accept real webhook delivery before public use,
alongside the remaining website enrollment and secure host handoff. No real App
consent, webhook delivery or host credential handoff has been accepted yet.

## Account-bound enrollment

Add `--enrollment-config /private/issuer/enrollment.json` to `serve` to enable
`POST /v1/github/enroll`. This requires the consent configuration and a webhook
secret for that same App. The private JSON file contains only these exact HTTPS
origins:

```json
{
  "website_origin": "https://website.example.invalid",
  "operational_origin": "https://plane.example.invalid"
}
```

The host prepares a UUID request, its workspace and host IDs, a random nonce,
the exact managed binding, numeric repository ID, and SHA-256 digest of its
locally retained random credential. `github_enrollment.prepare_request` and
`request_digest` define this contract. The website signs the request and
credential digests with a short-lived, enrollment-specific account assertion.
The issuer reads public keys only from the configured website's `/api/auth/jwks`.
The website's `GITHUB_ISSUER_ORIGIN` must equal the configured consent origin.
A read-session assertion cannot enroll access.

The host sends `{ "request": { ... }, "assertion": "..." }` as JSON. This
endpoint does not accept browser Origin requests. It returns the existing GitHub
consent invitation, never a credential or access token. A repeated request ID
with the same account and exact request returns that invitation; changed input
is refused. Expired, cancelled or conflicted invitations return HTTP 409
`enrollment_restart_required` and require a new request ID. Each
workspace can have at most 16 unexpired pending enrollment invitations.

Enrollment grants expire 30 days after staging and still require the same-browser
GitHub administrator consent before any issuance. Duplicate active approval for
the same credential and exact binding is refused under the approval lock,
including when two pending invitations finish concurrently. The account assertion proves
website workspace ownership, not physical host ownership or permission to run
host actions. The host must separately authorize setup and retain its credential;
this service does not write host secrets or configure fleets. Account closure
does not revoke already approved GitHub grants: revoke them explicitly using the
issuer command, or through the configured GitHub revocation events.

The host/browser enrollment wiring and real-provider acceptance are outstanding.
This endpoint is a core integration contract, not a finished self-service flow.
