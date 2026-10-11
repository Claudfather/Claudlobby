# Managed GitHub issuer

The shared package can serve the token endpoint used by named managed GitHub
connections. The issuer keeps the App key; customer computers keep a random
issuer credential bound to their approved workspace and deployment. The website
does not mint tokens or proxy repository data. Independent App connections
continue to mint locally without this service.

This is the service/operator interface. Browser GitHub consent can verify the
person's App installation and selected repository access. Website-account
enrollment and guided host credential delivery still need to be connected before
self-service onboarding. The HTTP endpoint cannot register its own callers:
the operator stages the workspace/deployment grant before asking for GitHub
consent. No public service is enabled by installing the package.

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

This flow verifies access at approval time. It does not yet consume GitHub
authorization-revocation webhooks or prove continuing personal access; that
source integration remains required before public self-service launch. App
installation/repository revocation still prevents GitHub from issuing a token.
No real App consent or host credential handoff has been accepted yet.
