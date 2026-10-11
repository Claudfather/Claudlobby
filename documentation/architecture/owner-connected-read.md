# Connected read sessions

Core's opt-in connected read backend lets an isolated Plane read an approved
host directly with an opaque credential held in browser memory. It does not
proxy private data through the website or grant fleet actions. The canonical
browser carrier, workspace entry and independently granted action interface
are implemented below; actual device/Serve/browser acceptance remains a
separate gate. Direct-host Strict cookie access is unchanged.

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
neither token can authorize the other's credential door. A connected read
session grants no fleet action by itself; separate action doors require the
existing explicit local grants.

Ready JSON contains version1,state:ready,relative expires_in, exact existing
read_profile `{version:1,profile:"direct-owner-read-v1",host_uid}` and
session_context with exactly issuer,subject,workspace_id,binding_revision,
binding_generation,operational_origin. The profile names the canonical
direct-to-host read protocol, not cookie authentication. The canonical connected client compares authenticated context before admitting
profile and pins the canonical host using the existing core checker. Construction of a profile/reader is not
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
are header-only; the canonical fetch-SSE client carries Authorization because EventSource cannot
set it. The backend supplies read doors; the browser interface and separate
grant-bound action doors are described below.
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

`readHandle`, lifecycle controls, `snapshot()`, `subscribe()` and the entry-owned
`createActions()` factory are public; no credential-returning API exists.
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


Account checks resume the renderer only when leaving its initial paused state;
steady-state checks do not create a second board refresh loop. Periodic checks
run only while the mounted document is visible; hiding cancels the timer and
becoming visible performs one fresh check. Explicit checks remain available.
Temporary checking uses aria-disabled with an activation guard so keyboard focus
stays on the same control. Account retirement disables all navigation/debug
controls after clearing private content, including focus-title text and datasets.
Partial carrier/transport admission disposes locally constructed resources before
an explicit retry. Disconnect stays available for an admitted but unavailable host.
The website's actual v2 context route returns 401 for missing/expired sessions,
403 for missing/lost workspace ownership, and context mismatches retire here;
it does not invent 404/410 deleted-workspace states. Document expires_at is a
shape-checked publisher lifetime bound; fresh website membership/session checks
and canonical host session admission determine current access, not client clock
interpretation of that field. The connected-context marker is supplied by the separately verified publisher;
this core entry does not mint or verify its document ticket.

## First-use workspace approval in the connected document

An explicit “Approve workspace on this computer” control consumes the existing workspace challenge, maintained website host assertion, and host stage protocol. It never confirms a binding, initializes the host, grants fleet actions, or connects automatically. Disconnect a currently ready read session before starting enrollment. A successful staged proposal displays its short-lived code only in that computer’s panel, beside the intended workspace, issuer and hostname.

On that computer use `claudlobby --root DATA_ROOT host owner confirm-workspace`, replacing DATA_ROOT with the existing installation’s data root. Paste the code at the hidden prompt, inspect the local issuer/account/workspace and prior-binding preview, then approve locally. Later choose Connect explicitly. These are existing-host approval instructions, not installation or native principal provisioning. An existing active binding must be inspected/managed through the local owner CLI; this consumer never revokes or replaces it.

Stage refusal, malformed reply, network loss or timeout is uncertain: the proposal may already exist. The original code remains briefly available for local inspection; the consumer does not replay stage. “Forget approval code” clears and aborts browser state only, without cancelling or revoking server state. Expiry, account/reference retirement, document disposal or successful explicit Connect clears the code. Nonces/assertions are private in-memory request data, never URL arguments, storage, logs, postMessage or command arguments. The panel’s display is the explicit exception for the approval code; public snapshots contain only phase/expiry/busy metadata.

This core module is part of the finite static graph. It reuses the document’s authenticated metadata check and account-refusal owner; no second account timer, renderer, setup authority or website orchestration is added. Synthetic transport/controller tests do not prove browser cross-site cookies, private-network permissions or a real host approval.

Enrollment controllers participate in the same local admission transaction as read carriers, subscriptions and aggregate streams. Any construction failure disposes all partial resources before an explicit fresh account check; rollback never publishes a partially constructed computer as readable. Corrected visibility, paused-only resume, keyboard focus and unavailable Disconnect behavior remain shared with the read entry.

Local retention bounds allow five seconds of browser clock skew; the host remains the authority on assertion and proposal expiry. Enrollment assertions share the host's 8192-character limit. Updated account metadata refreshes the displayed workspace name, and retirement clears it alongside the approval code. Forgetting a code returns focus to that computer's approval control, or its group while the pending request settles.

### Connected action interface

The canonical connected entry can expose ordinary messages, task nudges and task
feedback through a separate source-bound action interface. Its bearer remains
private to the carrier; a core read handle still exports no actions. Every host
operation requires the current native owner, workspace binding and the existing
independent local message/nudge/feedback grant. A read profile, website membership
or coverage count is never an action grant. These local grants are not
workspace-specific; the approved binding and the local grant are checked together.

The entry routes an exact team from the admitted source roster. One transport
boundary projects canonical raw host/actor/task/assignment IDs into the selected
source's qualified UI IDs and team name, and reverses only that exact qualifier
before the fixed connected action POST. Mixed sources, another host, changed
scope or undeclared recipients refuse without a send. HTTP v1 message and v2 task
metadata remain canonical and raw; the shared ActionState saves the corresponding
qualified metadata, original UUID and digest, never the body. Source qualifiers
are `c-` plus the complete reference UUID interpreted as a base36 integer,
without truncation. Display labels remain ordinal; removing/reordering another
reference cannot change a retained reference’s pending receipt identity. Direct-owner and
browser-local contracts remain unchanged.

Context/prepare/receipt have15-second deadlines; send has45seconds. Renewal,
logout, account/reference change and observed source loss fence late replies.
A missing action grant performs at most one pure status probe and does not end a
healthy read session. Independent capability refusal keeps other drafts and task
reads. A source lifecycle refresh preserves settled task details and message
recipient/draft selection, while acquiring fresh capabilities. Preparation never
sends; uncertain send retains the original saved UUID. Receipt lookup never
resends. Recorded means delivery unconfirmed, delivered requires the canonical
receiver evidence, and neither means that task work or approval occurred.

Context admission keeps only the latest request per kind, including same-team
refreshes. A late previous-team response cannot replace the current capability.
Quiet recorder recovery refreshes action contexts once when the existing board
overview observes that source healthy again; no new poller or send retry is added.

Stable routing qualifiers remain in data attributes and saved metadata. Team
cards, tabs, composer scope and task detail headings show the source display
label and raw team alias (for example Computer 1 / engineering). Recovery
consumes its loss ticket only on an overview started after that loss; a stale
healthy result cannot exhaust the later recovery refresh.


## First-team setup

A connected session is identity evidence. A fresh computer can return
`setup_only` with the finite `first-team-setup-v1` profile; this is not a dashboard
read profile. Its setup panel remains independent of other computers' admitted
reads. Every ordinary read/action still requires the bound recorder and its
existing explicit action grants.

An operator can explicitly approve **one** first-team setup with local
`host owner allow-setup` (see its maintained help). Approval fixes the shipped
`revenue-web-app` template, target fleet, repository, sealed release and native
install directory, and pins the current owner/workspace binding. It lasts at most
24 hours. It authorizes the connected browser to author a goal, preview the
canonical configuration plan, and explicitly launch that exact plan through the
foreground owner server. Workspace approval alone grants none of this. Setup
status and withdrawal use the local owner CLI. Expired/stale approvals require a
new explicit approval; withdrawal never undoes already-started work. Browser
receipt/session recovery is bounded by the same grant's expiry/revocation and
current owner/workspace binding; it is not permanent journal authority. On expiry
keep the original request UUID and plan ID. Locally inspect `host status --json`
and `config diff PLAN_ID`; the retained exact journal is
`ROOT/state/activations/REQUEST_UUID/activation.json` (read only), decoded/verified
by `claudlobby.activation_state.read_activation(ROOT, REQUEST_UUID)`. A new
approval does not authorize the browser to inspect the previous generation's
journal. No receipt should be inferred from a new empty grant.

The same approval accepts `--github-config FILE --github-connection ID` to select
one connection from a local fleet YAML's canonical `fleet.github.connections`.
It freezes that nonsecret policy in the approval; later edits to the input file
cannot change a browser preview. Claudlobby's template command assigns the chosen
repository to the lead, engineer and reviewer through the existing project
registry. Context shows the connection name, mode, repository and permissions;
credential references and App identifiers stay out of that browser summary.
The browser cannot supply or replace a connection. Changes require a fresh local
approval. Without these flags, existing template credential requirements apply.
Neither approval nor an active launch proves provider access.

The finite connected setup routes are POST `context`, `preview`, `launch` and
`receipt` under `/api/owner/connected/setup/`, with the existing exact Origin,
header-only session and private/no-store boundary. Optional setup tables are
created only by local approval. Preview rejects other fleets and target overlay
or project declarations; the whole-host plan must contain exactly the approved
first fleet. The canonical plan identity, sealed release and input/output
freshness checks remain unchanged. The preview expires after ten minutes, never
later than its approval. No browser field selects the install directory.

The browser saves the original UUID before one launch request. The authority
store reserves that UUID/plan before canonical activation begins. Lost responses
and receipt reloads inspect only that original journal. A canonical context
request supersedes a conflicting saved tab request, retaining both identifiers
for inspection. An explicit new generation clears stale blocking pending state;
a same-generation null request never licenses retry. Compatible local reapproval
retains content-checked ownership of its prior declaration, not authority to
launch an expired preview; unrelated authored declarations cannot be overwritten.
Receipts never replan, resume or resend activation. A reservation without a journal is **unknown**, requiring
local operator investigation or a new explicit grant, not automatic gap filling.
Current installation identity, private owner/workspace/session and the exact
retained grant allow this receipt recovery even if partial activation left an
unbound recorder. This grants no Plane-data read capability. New previews and
launches retain the strict first-fleet boundary. Started canonical work stays
owned and drained after response loss or server shutdown.

The browser displays team/repository/goal and plan changes, with technical
identifiers behind a disclosure. `active` is an observed canonical activation
state; it does not prove provider credentials, GitHub access, bot health or
business readiness. No JWT purpose, detached activation, general job store,
credential initialization or HTTP schema repair is added. The existing operator
shell, sealed interpreter and native kernel ancestry checks still apply in the
foreground server. This source slice is not cold-host/native/browser acceptance.
