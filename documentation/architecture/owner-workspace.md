# Local host workspace approval

This optional core foundation binds one workspace to an existing host and its
current native Tailscale paired owner. It does not connect a website, issue a
session, expose a service, or grant fleet actions. Existing direct owner pairing,
reads, sessions and explicit message/nudge/feedback grants remain independent.

A local operator uses an explicit data root, outside generated bot/fleet/timer
contexts, with a real terminal:

```
claudlobby --root DATA_ROOT host owner initialize-workspace
claudlobby --root DATA_ROOT host owner confirm-workspace
claudlobby --root DATA_ROOT host owner workspace-status --json
claudlobby --root DATA_ROOT host owner revoke-workspace
```

Replace DATA_ROOT with the intended local installation. Initialization requires
an active native owner pairing and typed INITIALIZE-WORKSPACE consent. It adds
versioned optional tables to the same private owner-access database. Missing,
partial or unsupported optional schema fails unavailable; reads never initialize
or repair it. The original owner initialize door is unchanged.

The future trusted consumer must obtain a fresh native principal through the
canonical verifier. `OwnerAccess.workspace().begin(principal, issuer=...,
operational_origin=...)` accepts trusted exact HTTPS configuration, captures the
current owner and binding revisions, and creates a 300-second nonce. Its
`expected(nonce, principal)` returns verifier expectations from that real pending
challenge, never from an unverified token. After pure assertion verification,
`stage(attestation, principal)` freezes the exact metadata, including assertion
identity and expiry. A dataclass constructor is not authentication: this internal
door must receive verified evidence, not browser-supplied JSON. No JWT is stored;
the nonce is stored only as a digest. At most 32 pending challenges are retained.

Confirmation accepts the code with hidden terminal input, displays the exact
host, native principal, owner revision, issuer, stable application user UUID,
workspace UUID, operational origin and UTC expiry, plus the retained previous
binding state, revision and generation, then requires BIND-WORKSPACE.
It compares the complete immutable preview, including the previous binding
snapshot, again under the existing authority write transaction. It checks the
final write timestamp against the challenge/proposal validity interval without
verifier leeway, then atomically consumes
all outstanding workspace nonces and creates a fresh binding generation/revision.
A competing confirmation, changed owner/binding, clock rollback, expiry, altered
proposal or storage failure cannot partially approve or silently retarget it.
Staging the identical verified statement is idempotent; a different statement
cannot replace it or extend its deadline.

The binding persists until explicit local revocation or owner revision loss; the
short assertion expiry limits approval, not the retained binding lifetime.
Status reports absent, active, revoked or stale-owner from retained metadata.
Only the latest revision is current. Status JSON exposes `effective_active`
derived from the current owner and state; retained `binding.active` and
`binding.owner.active` describe historical approval, not current authority. A
future consumer must admit on the effective state together with a fresh verified
owner, never on retained flags alone. Binding timestamps must be finite and
nonnegative; a backward wall-clock correction does not prevent local revocation.
Pending proposals still refuse time before their creation or at/after expiry.
Re-pairing cannot revive a stale binding;
a current native owner must explicitly revoke it before approving a replacement.
Revocation requires REVOKE-WORKSPACE, changes only this binding revision and clears
pending workspace proposals. Reapproval creates a new generation. Direct owner
sessions, action grants and running fleets are unaffected.

There is deliberately no remote consumption/credential issuance endpoint here.
Later HTTP admission, trusted issuer/JWKS configuration and retrieval, operational
browser carrier, session expiry/revocation and actual device/browser validation
remain separate reviewed work. Website membership alone never confers host reads
or actions, and metadata/profile shape alone never confers authentication.
