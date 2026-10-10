# Connected read sessions

Core's opt-in connected read backend lets an isolated Plane read an approved
host directly with an opaque credential held in browser memory. It does not
proxy private data through the website, grant fleet actions or provide the
connected browser UI. Actual issuer integration and device/Serve/browser
acceptance remain separate gates. Direct-host Strict cookie access is unchanged.

The host requires an existing native paired owner, effective active local
workspace binding, matching canonical recorder source and fixed HTTPS issuer/
operational Origin configuration. Explicitly prepare optional session storage:

```
claudlobby --root DATA_ROOT host owner initialize-connected
```

Replace DATA_ROOT with the intended existing installation. The interactive
preview describes only optional connected-session tables in the existing owner
authority database; approval issues no binding, session or action grant. Optional
workspace authority must already be initialized. Normal requests/status never
create, migrate or repair either schema. Partial/unsupported schemas refuse.
Existing `host owner serve --workspace-issuer ... --workspace-origin ...` selects
the exact issuer and operational Origin; no CORS origin comes from client JSON.
Serve's contained Unix socket and freshly verified native principal remain
required; bearer possession alone is insufficient.

## Finite credential protocol

Under `/api/owner/connected/`:

| Door | Input | Success |
| --- | --- | --- |
| POST challenge | JSON `{}`, no credential | 202 awaiting_session_assertion, version1, nonce, issuer, host_uid, operational_origin, binding_revision/generation, expires_at |
| POST issue | exactly `{nonce,assertion}`, no credential | 200 ready plus token |
| GET status | one Authorization: Bearer token | 200 ready without token |
| POST renew | bearer and JSON `{}` | 200 ready plus replacement token, old token invalid |
| POST logout | bearer and JSON `{}` | 200 version1 signed_out |
| GET/HEAD read/<suffix> | bearer, canonical queries | unchanged canonical read envelope or SSE |

All POSTs require JSON and X-Claudlobby-Owner:1; all doors require exact Host and
operational Origin, no Cookie, no credential URLs or redirects. Finite OPTIONS
checks current native owner/source/binding, requested method and exact header
set. Boundary-valid responses allow only that Origin, no Allow-Credentials or
Set-Cookie. All responses are private/no-store/no-referrer/nosniff; wrong boundary
has no CORS. Fixed400 invalid_request,403 denied,405 finite Allow,503 unavailable
retain refusal versus unknown semantics. Exception/native/JWKS output is never
returned. Bounded body/deadline/work ownership is shared with the bootstrap
handler; issue35seconds, other lifecycle/preflight15seconds, eight outstanding
workers. Cancellation retains started synchronous work until drained; queued
expired work cannot create authority. Shutdown stops admission and drains owned
work and read streams.

The fresh session assertion is NOT the enrollment assertion. Exactly14 claims:
iss,sub,workspace_id,host_uid,nonce,operational_origin,binding_revision,
binding_generation,aud,purpose,iat,nbf,exp,jti. Purpose is
`claudfather-workspace-host-session-v1`, audience
`urn:claudfather:host:<host_uid>:workspace-read-session:v1`. Maintained EdDSA
verification uses the fixed issuer's bounded public JWKS; iat==nbf, lifetime<=120
seconds. Website subject/workspace must exactly match the current locally
approved binding. JWT/key/profile shape alone is not host authority. Enrollment,
document-ticket and ordinary-user purposes fail. No assertion is persisted.

A120second host challenge retains nonce digest and complete immutable owner/
binding snapshot. Atomic issue consumes it once and stores a900second session's
SHA256 digest plus that snapshot. Token is32 random bytes/canonical43char
base64url, returned only on successful issue/renew, never persisted cleartext.
Both pending challenges and sessions cap32; stale/expired connected rows can
be cleaned at issuance. Direct cookie tables remain purpose-separated, so
neither token can authorize the other's door or a fleet action.

Ready JSON contains version1,state:ready,relative expires_in, exact existing
read_profile `{version:1,profile:"direct-owner-read-v1",host_uid}` and
session_context with exactly issuer,subject,workspace_id,binding_revision,
binding_generation,operational_origin. The profile names the canonical
direct-to-host read protocol, not cookie authentication. The future client must
compare authenticated context before admitting profile and pin the canonical
host, using the existing core checker. Construction of a profile/reader is not
authentication.

## Canonical reads and loss

The read prefix maps once onto the SAME canonical view instance and sampler:
summary,overview,fleets,channel,tasks,exact tasks/wi_ID,identities,inventory,org,
utilization,equipment,search,grid,presence,trust,stream. No action, root, asset or
arbitrary added route is exposed. Canonical query/window/ownership/source
snapshot contracts remain authoritative, including detail beyond the board cap.
Duplicate/unknown query keys refuse. GET/HEAD remain reads only.

Native principal verification and exact current owner, token time, binding
revision/generation/full snapshot and host UID admission bracket private bodies.
Connected SSE refreshes native proof before EVERY event/comment chunk. Current
owner/binding revocation or principal loss prevents subsequent delivery. The
canonical same-snapshot data contract remains unchanged: source ownership in
queries is admitted inside that snapshot; closing admission checks current
host-UID file/session/binding, not a fresh ledger-marker query for every byte.

SSE preserves canonical Last-Event-ID > cursor query > HEAD precedence, source-
local numeric cursors, named source frames, retry and ping comments. Credentials
are header-only; a future fetch-SSE client is required because EventSource cannot
set Authorization. No connected JS/action exports are supplied by this backend.
Finite reads and first stream headers have15second bounds; healthy admitted
streams have no idle timeout. Held denial prevents headers/private data; a
denied started stream ends, while other partial responses remain incomplete.

Renewal is host-local under a valid session, current native owner and current
binding. It is not a recurring website lease. Fresh issuance always needs fresh
account evidence; a retained enrollment JWT or claimed identity cannot recover
an expired session. Source preflight refusal retains the old renewal token;
loss after rotation is unknown. Lost issue/renew/logout replies are not no-effect
proof, must not be retried automatically and offer no plaintext-token recovery.
Explicit fresh sign-in can recover; abandoned sessions expire within their
bounded lifetime. Logout affects only its connected session, not pairing,
binding, direct sessions or grants.

Remote account/membership/reference removal is not instantly known to an offline
host: enforcement is pending until applied locally or observed by the browser.
An existing approved session can renew during provider outage while the local
binding remains current. Observed account/workspace change must close streams,
abort reads, erase memory credential/private display and fence late results.
A stolen token from another native principal fails; another device of the SAME
native owner could use it. Memory is not XSS protection or device key binding.
Operational release/CSP/literal rendering and actual device proof remain gates.

Connected HEAD reads execute the same canonical GET query and protected source/
response admission as GET, retaining status/headers while suppressing all body
bytes. Stream HEAD stops the canonical producer after its first admitted chunk, without
rewriting query parameters or leaving an idle SSE reader alive. The explicit connected route/query allowlist remains
a deliberate ceiling; focused contract checks detect drift in its selected
canonical query parameters without automatically exposing new routes.

## Connected browser carrier

The canonical `connected-read-client.js` module exports
`createConnectedReadClient` with a selected reference
`{reference_id,endpoint_origin,revision}`, trusted `documentContext`
`{issuer,operational_origin,subject,workspace_id,context_generation}`, a
`currentContext()` callback and `obtainSessionAssertion(input,{signal})` callback.
The current callback supplies the actual authenticated v2 website DTO:
`{version:2,user:{id},workspace:{id,name},context_generation,references}`.
Reference objects have exactly the three keys above. The operational Origin
must match the current document Origin; trusted issuer/document configuration
is separate from website metadata. Parent messages and object shapes establish
no authority. The entry supplies verified document configuration and fresh child
metadata, and disposes the controller on observed identity/reference loss.

`connect()` explicitly performs challenge, maintained website signer callback,
and issue. The callback receives the original eight-field website request,
including reference_id and expected_context_generation. Host UID comes only
from the exact-origin challenge; issue/status/renew must match its pinned host
and binding tuple. Genuine host responses go through the existing single owner
profile checker before installing a credential or admitting a core read handle.
Missing/unsupported capabilities or changed authenticated context fail closed.
`start()` and `check()` only inspect an existing session; they never sign in.
`renew()` and `logout()` are explicit serialized controls; an unknown mutation
reply is never replayed. Successful logout retains local pairing and binding.

Only `readHandle`, lifecycle controls, `snapshot()` and `subscribe()` are public.
The handle is core minted and works with the finite two-source reader; it has no
action export or global sign-in redirect. Session token, nonce and assertion
are held only in private memory/body/header ownership, never returned by the
controller, stored, logged or placed in URLs. Disposal through either controller
or handle aborts reads, closes streams and removes credential references.
Reload requires fresh sign-in. Relative lifetime uses a finite nondecreasing
monotonic clock; expiry or clock failure stops private reads.

The direct fetch carrier uses credentials:omit, no-store and redirect:error for
the finite connected read/lifecycle roster. Its fetch SSE parser handles split
UTF8 and CRLF, numeric source-local id/cursor, default data, named source,
retry hints and comments. Frames/body/header deadlines and size bounds refuse
malformed or oversized streams. It delegates reconnection and quiet-stream
budgets to the existing owner controller and introduces no parallel retry loop.
Successful HTTP reads never rearm persistent stream failures. Token rotation,
source loss and disposal fence old requests/chunks without mutation replay.

This library does not fetch account metadata, mount a central operational entry,
choose CSP/private-network permissions or prove a real device/carrier. Those
remain separate reviewed integration and browser gates. Unchanged trusted
account context can support host-local renewal through provider outage under
the documented non-lease policy; unknown metadata is unavailable, and observed
account/workspace/reference changes require a new authenticated entry.

## Canonical connected workspace entry

The canonical index has an inert `<!--CONNECTED_CONTEXT-->` replacement marker.
Only the reviewed operational publisher supplies a nonexecutable
`workspace-document-context` JSON element after verifying its document ticket.
The static `api-client.js` imports the complete canonical module graph. Without
a context element it retains the original same-origin read behavior and exports
no session controls; it makes no owner/session probes. An invalid context element
refuses connected reads and never falls back to direct-host access. Direct owner
browser mounting still selects its existing cookie transport independently.

`connected-workspace.js` accepts the publisher's version1 document context:
`{version,user_id,workspace_id,context_generation,shell_origin,operational_origin,
references,expires_at}`. Before creating a host controller it reads the actual
credentialed website v2 context and matches the account, selected workspace,
generation and complete exact saved-reference set. The publisher owns ticket
validity and dynamic CSP; merely injecting a JSON shape provides no host access.
The entry uses the carrier's canonical host challenge, website assertion callback
and genuine read handles; account metadata alone never admits private reads.

Zero saved computers has an explicit empty state. One or two saved computers
start with read-only session checks, leaving explicit Connect available; no
automatic sign-in, session mutation retry or host initialization occurs. Per-host
Connect, Check connection, Keep connected and Disconnect operate independently.
Disconnect ends only that connected session; closing the document disposes its
streams/credentials without sending logout. Stable controls preserve keyboard
focus while labels and enabled states change. Canonical Activity, team choices,
tasks and exact linked task detail use the existing aggregate reader/renderer.
All agents, Team details, Recorder, search and bot equipment inspection are
explicitly disabled because the bounded aggregate does not supply those reads.

Website metadata reads have one eight-second total header/body deadline and a
16KiB body bound. The visible document rechecks on visibility and every 30 seconds.
A provider outage cannot create an admitted account context; a previously verified
unchanged context may retain existing host sessions with an unavailable account
check note under the documented non-lease policy. Observed 401/403/409, changed
account/workspace/generation/reference set or disposal retires all controllers,
aborts owned work, clears private regions and fences held reads/stream callbacks.
This document never revives after observed loss; reopen through the authenticated
website. Same-site cookie/CORS/private-network/device validation and actual
operational publisher integration remain separate empirical gates.
