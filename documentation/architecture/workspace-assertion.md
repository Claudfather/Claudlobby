# Workspace assertion verification

`plane.workspace_assertion.verify_workspace_assertion` verifies one finite signed
workspace statement against a trusted `ExpectedWorkspaceAssertion` tuple and
caller-supplied public JWKS bytes. It returns a frozen `WorkspaceAttestation`.
The assertion attests website user/workspace ownership and the requested host,
nonce and operational origin. It does not establish that the challenge is
outstanding, authenticate a native principal, approve a connection, consume a
nonce, or grant a session, read access or actions.

The optional `plane-ui` extra pins `PyJWT[crypto]==2.15.1`; the core ledger does
not import it. Importing this module also needs only the standard library.
Missing verification dependencies or unusable configured keys raise fixed
`AssertionUnavailable`; malformed assertions, known-key signature failures and
profile mismatches raise fixed `AssertionRefused`. Unknown key IDs are
unavailable, allowing a future consumer to decide bounded key refresh. This
module performs no fetch, cache, environment read, persistence or retry.

The exact claim roster is `iss`, `sub`, `workspace_id`, `host_uid`, `nonce`,
`operational_origin`, `aud`, `purpose`, `iat`, `nbf`, `exp`, `jti`. Purpose is
`claudfather-workspace-host-bootstrap-v1`. Stable user, workspace and issuance
IDs are canonical UUIDs; host IDs are `host_` plus 32 lowercase hexadecimal
characters. The nonce is canonical unpadded base64url for 32 bytes. Audience is
`urn:claudfather:host:<host_uid>:workspace-read-bootstrap:v1`. Origins are exact
canonical HTTPS origins; an explicit configuration flag permits HTTP only for
loopback fixtures. No email/display name is accepted as identity.

PyJWT owns signature and registered-claim validation, with fixed EdDSA,
strict single audience, exact issuer, required claims and five seconds of
leeway. There is no caller-selected clock. Additional checks require positive
integer time fields (excluding booleans), equal issuance/not-before times and a
positive lifetime of at most 120 seconds. Signed ordinary user JWTs are refused.
Token headers allow only `alg`, bounded `kid` and optional standard `typ: JWT`;
key URLs, embedded keys, critical extensions and custom types are refused.

Inputs are bounded to 8 KiB ASCII JWT and 64 KiB public JWKS, with at most eight
unique key IDs of up to 128 ASCII characters. Every configured key must be an
Ed25519 public OKP key with canonical 32-byte public material, optional matching
EdDSA/signature/verify metadata, and no private or unknown members. Duplicate
JWKS members and unusable keys refuse the entire configured set; PyJWK parses
the public keys. Nothing silently selects another algorithm or drops bad keys.

Later consumers must independently join a real outstanding challenge, trusted
native principal, current source/owner revision and explicit local approval,
then consume the nonce once atomically. Re-signing the same nonce is not new
permission. Network bounds, key freshness/rotation, replay protection,
revocation, connected credentials and actual browser transport remain separate
work. Ephemeral Python signing tests prove this verifier; they are not proof of
an installed website signer or real host/browser access.

Dependency references: [published package metadata](https://pypi.org/project/PyJWT/2.15.1/),
[tagged verifier](https://github.com/jpadilla/pyjwt/blob/2.15.1/jwt/api_jwt.py) and
[tagged JWK parser](https://github.com/jpadilla/pyjwt/blob/2.15.1/jwt/api_jwk.py).
