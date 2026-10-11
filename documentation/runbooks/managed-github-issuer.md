# Managed GitHub issuer

The shared package can serve the token endpoint used by named managed GitHub
connections. The issuer keeps the App key; customer computers keep a random
issuer credential bound to their approved workspace and deployment. The website
does not mint tokens or proxy repository data. Independent App connections
continue to mint locally without this service.

This is the service/operator interface. Hosted App installation consent,
owner-to-installation verification and guided credential delivery still need to
be connected before self-service onboarding. The HTTP endpoint cannot approve
its own callers. Do not turn a client-provided binding or website login into an
issuer grant without independently confirming the owner, deployment,
installation and selected repository. No public service is enabled by installing
the package.

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
