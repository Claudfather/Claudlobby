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


## Optional HTTP bootstrap precursor

The foreground owner server accepts `--workspace-issuer` and
`--workspace-origin` together as exact fixed HTTPS origins. This explicitly
opts in only POST/OPTIONS `/api/owner/workspace/challenge` and
`/api/owner/workspace/stage`; startup checks optional schema/dependencies without
initializing or fetching keys. Existing direct lifecycle, cookies, reads and
actions keep their original Origin boundary. No ambient configuration is used.

The operational caller omits host credentials; these routes reject Cookie and
Authorization and require exact configured operational Origin, host, JSON and
intent. Their finite preflight allows only POST, Content-Type and
X-Claudlobby-Owner. Native contained Serve/WhoIs identity must be the current
paired owner; Origin is not identity. Headers/errors are private/no-store and
never expose assertion, private paths or exception details. Local confirmation
remains mandatory; staging does not issue a session, read or action grant.

Challenges accept only `{}` (1024 bytes), stages only `{nonce,assertion}`
(16384 bytes; assertion8192 ASCII characters), with duplicate/unknown fields
refused. Eight owned workers include body/identity/key/DB work and survive caller
response cancellation; body has a five-second total bound and bootstrap a
35-second response deadline including threadpool wait. Every queued local call
checks the deadline again before starting; no new begin/stage starts after it.
SQLite locks remain bounded by the existing five-second timeout; already started
local work stays owned/counting until completion and can finish with an unknown
response outcome. App shutdown stops admission, cancels owned async work once
and drains started local calls before completing the read app lifespan. Lost/denied response may leave a staged
proposal: inspect locally rather than automatically replaying. Closing admission
uses fresh native identity and exact challenge/proposal in one read transaction,
including expiry/owner/binding/installation; it never confirms or renews.

One fixed issuer `/api/auth/jwks` is fetched without credentials, redirects or
ambient proxies, with verified TLS, three-second inactivity and eight-second
TOTAL bounds including lock wait/body/key validation. Raw identity-encoded JSON
is limited to64KiB/eight public Ed25519 keys. Whole-set cache lives only in memory
for300 seconds under wall and monotonic clocks; rollback expires it. Refresh
attempts have a30-second cooldown, including failures. Unknown kid is only a
bounded, finite-header refresh hint only; malformed/unsupported warm-cache
headers use existing fresh bytes for local refusal without consuming cooldown.
Signature/claims still pass the maintained pure verifier. No stale-cache
fallback or generic discovery. Removed keys may remain trusted until expiry;
local owner/workspace revocation supplies immediate host enforcement.

This backend is a precursor: the canonical operational entry/acquisition
controller is not mounted here. It must obtain issuer evidence using the
selected same-site separate-origin website topology, keep it only in memory,
and stage it directly at the pinned host. Current different-site deployment
aliases, actual browser local-network consent, real native ingress and connected
read carrier require separate proof. No browser or real host connection is
claimed by synthetic ingress/HTTPX mock tests.

## Before the first team

An assembled sealed release can explicitly prepare identity and owner authority
with `claudlobby --root DATA_ROOT host owner initialize --before-first-fleet`.
The terminal confirms this preparation; the default `initialize` behavior is
unchanged. The optional preparation marker is recorded in the unpublished
initialization transaction, before complete authority bytes are published;
absence of a recorder alone never enables setup fallback. Ordinary authority
without this optional marker never triggers automatic source binding on resume.
No release selection, begun activation, runtime, recorder, queues, or
unknown Plane files are accepted as a fresh host. Pairing and workspace approval
grant neither activation nor fleet actions. Private reads remain recorder-bound.

If initialization is killed, private `.owner-access-*` temporary files or their
SQLite sidecars can remain in `state/plane`. The fresh-host check deliberately
refuses these unknown files. With initialization and the owner server stopped,
inspect the exact files and remove only confirmed abandoned, operator-owned
0600 regular temporary files, then retry initialization. Never remove
`owner-access.db`, its sidecars, recorder data or unfamiliar files to force
fresh-host admission; use the existing recovery path instead.

For this pre-activation state the owner server defaults to the private
`state/owner/owner.sock`, outside recorder storage. An explicit socket must also
be outside `state/plane`; existing hosts retain their original socket default.
After first activation, continue using the explicit original socket if Serve is
configured for that path; no socket is moved automatically.

First activation preserves only validated private owner authority files. It
attests its newly initialized recorder with the existing source-binding owner
before ingest or bots start, then permanently retires the preparation marker.
Bootstrap resume can retire it again but cannot rearm it; loss of external
recorder/selection/runtime files never makes a used installation fresh again.
Imported/existing recorder data still requires
explicit source binding and the established activation/recovery path. Browser
team launch authority and actual cold-host acceptance are separate work.
If ordinary owner authority was initialized after an interrupted bootstrap,
inspect the recorder's identity and use
`claudlobby --root DATA_ROOT host owner bind-source` before resuming the same
activation. Resume never silently attests existing recorder data.
