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

**Browser-carrier setup view.** Over the browser carrier
(`fetch.browserOwnerTransport`) the document is the body of the website's setup
step until the bound computer is ready with a team; from then on it is the
connected view of that one computer (the working form, below). The carrier adds two cosmetic markers beside that flag:
`browserOwnerReference`, the `reference_id` its parent's admitted offer bound
(`null` before an offer, kept after retirement), and `browserOwnerSettled`, which
resolves, never rejects, once an offer is admitted or the carrier retires
without one. Neither is a grant or a network authority. The account check still
matches the complete saved-reference set, a controller still exists for every
saved reference, and each carrier request is still checked against its live
bound target. The entry uses the bound reference only for presentation. Other
saved computers are left out of the computer list, attention callbacks, and the
aggregate sources, coverage, team choices and blocker text (the reader's
`shown` source filter). Before an offer, or when none arrives, every computer
stays listed. A later offer repaints, and `presentationChanged` asks the boards
to read again.

In the setup form the document marks `<html data-workspace-view="setup">`.
`workspace.css` then shows only the setup host. The header (Rooms, Tasks, More), the sidebar (teams,
computer cards, overview, diagnostics), the room heading and search, activity,
tasks, Recorder and the Message team composer are all hidden. The page grows
with its content and has no internal scroll container, and the panel fills the
frame width. The bound computer's panel is the view in every connection state,
except that a started computer shows its parts instead ("A started computer's parts").
Its first step, Connect this computer, mirrors that computer's own Connect or
Check connection. One click sends one connect. Nothing connects, signs in or
retries a session mutation automatically, and failures say what to choose next
in plain sentences. After Connect, the existing steps follow: approve setup,
sign in to Claude, describe the goal, launch. When the computer is still in
first-team setup (`setup_only`), a Connect chosen in the panel is followed by one
setup approval read (`context`, which changes nothing on the computer), so
approve setup is done without a second click. A computer that is already set up
gets no automatic approval read. If the read fails, no approval is shown, Check
setup approval stays the step's action and nothing retries it. When the computer
has said (`launch_reserved`) that the request this browser keeps holds a Launch,
the browser keeps that word beside the request (`<storage key>:launch`). After
each Connect or Check connection chosen in the panel, and after any approval
read, the page then reads that launch's status once (`receipt`, which also
changes nothing on the computer), exactly as Check launch status would, but only
when the computer's word for the kept request was stored before that Connect. It
never reads when the launch is already shown finished, when a Preview can be
launched, or while the launch hand-over is already reading. The read lands on
Team launched or the launch's unfinished state. A failed or refused read leaves
the view as it was and shows nothing, and nothing retries it. The same word
hides "Nothing runs on this computer until you choose Launch team." from the
first paint. A request kept from a Preview alone, a word stored for another
request, or a word this browser cannot read gets no launch status read.
Sign-in, preview and launch still each wait for their own click. Focus then moves to the next shown,
enabled action, or stays where it is if there is none. A clean done receipt for the
current request marks every shown step done, with "Team launched. Your team is
running on this computer." This holds even after the launch handoff's status
check moves the connection on and clears the approval context. A request
rebound later is not shown as launched. Keyboard focus moves on after a panel
Connect only while the frame has focus and nothing else took it. The setup
form never reveals the rest of the workspace. While no panel can show, one status
line says why: checking, account unavailable, no saved computer, or reopen setup.

**The working form.** Over the browser carrier, once the bound computer's
session is `ready`, its parts read answers `started` with at least one team, and
the page no longer follows a Launch, the document removes `data-workspace-view`
and shows the connected view a direct connection gets for that one computer: its
parts as a strip above the team's rooms, tasks and Message team composer
(`data-setup-focus="shared"`). Every `workspace.css` rule that hides the header,
sidebar, room or composer applies only under the setup marker or the `only`
focus. A page that sent or followed a Launch stops following it at a clean done
receipt for the current request; a parts read held back while it followed is then
sent once, so the page that launched and a page opened again end on the same
view. A receipt that is not clean keeps the panel and its Check launch status. A
`setup_only` session, a lost connection or no team keeps, or brings back, the
setup form. A renewal of the same session changes nothing, and neither does a
session check (Check connection, the recovery after a refused read, or the
hand-off's status check) that ends `ready` on the computer the parts answer came
from: the answer and the form are kept through it, and no height, form or parts
read is posted or sent. A check that ends anywhere else brings back the setup
form. A parts refresh that is refused, lost or unusable keeps the answer already
shown for that connection, so one failed read never takes the working form away.
Focus that was in the panel moves to the parts, never into the composer. Rooms,
Tasks and All agents are on; Team details, Recorder, search and bot equipment stay
disabled, as in any connected view. The card's Disconnect and each message's
Technical detail are reachable.

The working form's computer already has a team, so its first-team panel is not
offered anywhere in that form: it is detached from the page, not only hidden, and
the card's disclosure no longer holds it: in that form the disclosure is labelled
"Connection options" and drops its guided-setup paragraph. Whenever the view
returns to the setup form the panel comes back, in the setup host or in the
disclosure, labelled "Connection options and first-time setup" again.

Messaging in the working form is reach for an existing grant, not new authority.
With a message grant the person has approved on that computer (today the guide's
"Allow messages from you to this team" step), the composer and the "Message"
buttons beside bots send through the calls the carrier already admits
(`action-context`, `action-send`, `action-receipt`). Without one the composer says
"Messages: not authorized." and sends nothing. Nudges, task feedback and
permission decisions cannot be sent over this connection: their preparation is
refused locally. The form adds no request names, grants or server routes.

After its ready message, the framed document observes its root element with
`ResizeObserver`. Browsers deliver at most one notification per rendering
frame, and in the setup form each one measures the document height. When the
value changes, the document posts exactly
`{type:"claudfather.workspace.size",version:1,height}` to the document's
`shell_origin`, never `*`. `height` is a whole number of CSS pixels from 1 to
20000. No other data crosses, and disposal stops observing. The website must
accept the message only from its own frame window and the Plane origin, and must
clamp it.

The framed document also says which form it shows, so the website can size the
frame for it. Under the browser carrier only, right after its ready message, it
posts exactly `{type:"claudfather.workspace.form",version:1,form}` to the
document's `shell_origin`, where `form` is `"setup"` or `"working"`, and posts it
again only when the form changes; an unchanged form is never posted again, and
nothing is posted after disposal. It carries nothing about the computer: no name,
count or identifier. A website that does not know the message ignores it. The
working form fills its window instead of growing with its
content, so on entering it the document posts one fixed height of 640 and
measures nothing while it lasts; going back to the setup form measures again.
Native documents post no size and keep the full workspace.

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

## A started computer's parts

A `ready` session on a started computer offers the dashboard reads as before and
one more read that needs no setup approval: the **parts read**, `POST
/api/owner/connected/setup/state` with body `{}` (`setup-state`, input `{}`, on the
SSH route and in the browser carrier's closed list). It is admitted by the
connected session alone, checked before and after, and it writes nothing. It
answers

`{"version":1,"computer":"started"|"not_started","provider":{"state":"not_linked"|"linked","link":"available"|"not_here"},"team":{"state":"none"},"teams":<count>,"bots":[],"helper":{"state":"asleep"|"later"}}`

`computer` is `started` for a computer started with no team (`start_state`) or
one whose committed configuration lists a team; `teams` counts those teams. It
is a count, not a running state: it is also true mid-Launch, after a failed
Launch and after a stop, so the pane says "1 team set up" for exactly one team,
never "running", and no status sentence for more (no plural is approved).
`provider` is `linked` exactly when a Claude sign-in line with a value is saved
in this computer's `.env` (`environment_linked`), under the same file checks every
sign-in attempt already makes (`environment_ready`, unchanged); the line's value
is never read out, and no approval and no attempt's record decides it. These read
as `not_linked`: no `.env`; a `.env` that is not a single-link regular file of
this user with mode 0600 and at most 64 KiB, or that changes while read; a line
before the sign-in line that the check cannot read; an empty value
(`CLAUDE_CODE_OAUTH_TOKEN=`, `''` or `""`); and an API key or Claude's own
`claude login` files, which are not this line. `provider.link` says whether
this computer takes a link approval (below): `available` on a Linux computer
that `start_state` reads as `started` and where no team approval or Launch is
retained, `not_here` on every other computer (one today's nine-card guide set
up, one not started, a Mac, or one whose team tables cannot be read). It depends
on no approval and no attempt, so it reads the same for a computer read after
read; a view that finds no `link` key is talking to an older core. Every other
byte of the answer is as it was before the key existed. `provider.signin` is
present only while the computer holds a link approval (below) that has not
lapsed unused and Claude is not linked; once linked it is left out. It is
`{"generation","phase":"approved"|"waiting"|"ended","attempt_id","state","reason","code_sent","retryable","unconfirmed"}`:
`approved` before the approval's first attempt (the attempt fields `null`,
the flags false), `waiting` while its current attempt is starting or waiting
with the sign-in lease held, else `ended`. `state` and `reason` are that
attempt's, except that a starting or waiting attempt whose lease is free reads
`unknown`/`interrupted`, as the status call reads it. `code_sent` says a code
was delivered; `retryable` that a begin would now admit a new attempt under
the same approval; `unconfirmed` that the attempt ended
`unknown`/`child_quiet_unproven` and its lease is still held. It never holds a
url, a code or a token: the url stays in the status call. A view resumes from
this key alone. `team.state` is
`launch_unfinished` while a team preparation that a Launch dispatched has not
finished cleanly (its update record is not `done`, or is `done` with health
errors), and when the update record cannot be read; otherwise `none`. A Preview
nothing launched is never dispatched, so it reads `none`. `helper` is
`asleep` until Claude is linked, then `later`. Later versions add keys and states
inside a part and entries in `bots`; `version` stays 1 while nothing is removed
or renamed.

The pane (`workspace-parts.js`) sends the parts read once for each new `ready`
connection to a shown computer, again whenever the window regains focus, and
once when a read held back while the page followed a Launch is released.
A renewal of the same session (`ready`, `renewing`, `ready`), or a session check
that ends `ready` on the computer the answer came from, keeps the answer
shown and sends no read; a connection change while a read is in flight is
followed by one read for the new connection when it ends.
Nothing else repeats it, nothing retries it, and the pane still connects only
when the person chooses Connect. A `setup_only` session is never sent it. When the
answer says `started`, the computer's parts show in the setup host: this
computer, Claude, bots and, only when the embedding page names it, the helper.
Each part is one element with `data-part` and `data-state`, a name, one status
sentence and at most one control; the only control is a "Create a bot" that is
visibly not available. With no team the parts are the whole view; with a team
they stay as a strip above its teams. Over the browser carrier the five-step
panel is the view only while the parts read has no answer or says `not_started`,
and while that panel follows a launch in this page's lifetime: once this page
sent a Launch or a launch status check, or followed a managed hand-off, the
panel stays to its ending and no parts read is sent until that follow ends at a
clean done receipt for the current request. A page opened again keeps
the panel, with its Check launch status, while the parts read says
`launch_unfinished`; once the Launch is done the parts show, and with a team the
working form follows. Before that page
reads anything, the panel shows what the computer said, for display only: the
line that nothing runs yet is hidden, and with this browser's own request kept
the approval step shows done. It sends, resumes and follows nothing. A browser
with no request of its own shows approval as the step to check; Check setup
approval reads the request, and Check launch status then follows it. A request kept in
the browser's storage does not hold the panel by itself, so a Preview nothing
launched never does.
A refusal, a lost answer, an answer the pane cannot use, or a computer whose core
has no parts read leaves the view exactly as it was.

The connected document may carry `part_names`: an object whose keys are from
`provider`, `bots` and `helper` (never a computer, which keeps its own saved
name), and whose values are 1 to 40 ASCII letters and digits in words joined by
one space or one hyphen, and that holds none of the view's own status sentences
as whole words ("Linked", "Not linked", "None yet", "Coming soon", "Asleep until
Claude is linked"), so a name cannot read like a status. The pane uses
`part_names[<part>]` as that part's name when the whole field passes; anything
else drops the whole field, never one name, and the document is still admitted.
Without it the computer's saved name, "Claude" and "Bots" are used, and the
helper row is not drawn: core holds no name for it. The browser carrier admits a
document with the field and never sends it to the parent. A pane older than this
refuses any document with the field, so the embedding page adds it only once it
serves this pane.

## A link approval for Claude sign-ins

A started computer from the newer setup has no team approval and no team tables
(`setup_metadata`, `setup_grant`, `setup_previews`). A **link approval** is one
yes, on such a computer, to Claude sign-ins for a short time. Core records it
only after a yes on the computer run's `link_provider` card, asked only with that
run's link intent (`link_claude: true`, see the
[website boundary](website-boundary.md#the-computer-runs-link-intent)); nothing
a person sees changes until the website sends that intent.

**The record.** One row in its own table, `setup_link(singleton, snapshot)`,
never a `setup_grant` row: any team row makes the computer read as set up to the
computer run, and a core without this record reads only the three team tables,
so to it the computer still has no team setup. The snapshot holds exactly
`generation` (a UUID string the writer makes), `binding_snapshot` (the active
workspace binding, read inside the write), `installation` and `source_revision`
(the active ones), `provider_signin` (the sign-in scope, as a team approval
carries it), `created_at`, `unused_until` (`created_at` + 900) and `expires_at`
(`created_at` + 1500). A row with any other key set, a non-UUID generation,
other offsets, a time that is not a finite number or a scope of another shape
does not read, and every door refuses it as unavailable. The approval is
**spent** once an attempt row has its generation: that row is inserted in
begin's own transaction, so a refused begin spends nothing, and nothing in the
record changes, so every later admission of the same approval still matches.

**The writer** runs in one write transaction and writes nothing when it refuses.
In order: the owner's grant and the active binding (their own refusals); the
installation and active revision (`setup_source_changed`); a retained team
approval or Launch, by the computer run's own test (`use_full_guide`);
`start_state` is `started` (`not_started`); the sign-in lease is free
(`signin_live`, or `signin_unconfirmed` when the lease is held over an attempt
that has ended). A worker in another process closes its lease a few
milliseconds after its last row, so a lease held over an ended attempt other
than `unknown`/`child_quiet_unproven` is probed again, up to 50 times 0.01
seconds apart, and reads as free once released; the computer run's own check
before its card and begin decide the same way. The writer waits inside its
write transaction and begin inside its lock and write transaction, so no new
attempt can be committed meanwhile (the computer run reads the attempt again
after waiting); the wait is at most half a second, about two seconds under
begin's lock at worst, and cannot deadlock, because a worker closes its lease
outside that lock with no database write. A `starting` or `waiting` attempt
with the lease free has ended (its worker is gone) and is left as it is. A new
yes replaces the row.

**The doors.** The four sign-in calls (`setup-signin-begin`, `-status`,
`-code`, `-cancel`) keep their names and keys. On a computer that holds no link
approval they are exactly today's team calls, in today's order: the team
approval is admitted first, then the payload, so every answer is as before. On a
computer that holds one, their door checks the headers, the principal, the
connected session alone, the binding's issuer and origin, and the payload, then
reads which approval the payload's `generation` names: with none of the team
tables, the link approval; with some but not all, unavailable; with all three,
the team approval when it is active and names that generation, and otherwise
the link approval. The engine call follows (begin's own checks are below),
then the principal again and the same approval's own admission with the
approval it read. A link approval is admitted with the team approval's fences:
the connected session, the row, the same binding, its time window, the exact
approval, and no team approval or Launch retained; then the update fence
(`managed_setup`), maintenance and the provider's pinned binary, as a team
sign-in has. Admission reads only the database, never `start_state`: the
worker re-admits about every 0.1 seconds. `setup-preview`, `-launch`,
`-receipt` and `-context` never read a link approval, so they refuse its
generation.

**Begin under a link approval.** The same attempt ID again returns its status,
as today. A new attempt, inside begin's write transaction and in this order:
the approval is read again and must be the one admitted (a yes that landed
since replaced it: `setup_denied`); while no attempt has its generation,
`unused_until` must not have passed (`setup_denied`, as an expired approval);
`start_state` is `started` (`not_started`; the writer and this are the only
places a link sign-in reads it); then the current attempt. A current attempt
that has ended while the sign-in lease is still held (after the same probes
again as the writer's), or this process's own
attempt that ended `unknown`/`child_quiet_unproven`, refuses
`signin_unconfirmed`. Another approval's attempt that has not ended refuses
`signin_live`; ended means a terminal row, or a starting or waiting row whose
lease is free and whose worker is not this process's. Under the same approval
today's retry rule holds (a retryable ending with no code sent, the attempt
settled), and its cap counts only that approval's attempts (10); a new approval
starts a new count, and the team approval still counts every attempt on the
computer. Then today's window, scope and environment checks, and the lease. A
link attempt that the service's own stop cancels ends `cancelled` with the
reason `service_stopped`, not `effect_uncertain`; a team attempt still ends
`effect_uncertain`.

**The sign-in lease.** `state/provider-signin.lock`, one private file per
computer (a regular file of this user, one link, mode 0600), never removed.
Only a link sign-in takes it: begin, on the path that inserts a new attempt,
inside its write transaction just before the insert, with non-blocking tries
only; a refused or rolled-back begin releases it at once. The worker closes it
after its last write and outside its lock; a worker whose child's reap is
unproven keeps it until its own process exits. Workers run in the process that
began them: the lasting service, or the process of an `owner-signin-v1`
conversation over the SSH carrier. Any process can probe it
without blocking: held means a sign-in may be live; a file that is absent or
unlocked is free (the system releases a dead holder's lock); anything else (a
link, another owner, another name, another mode) refuses as unavailable, never
as free. A team sign-in takes no lease and is unchanged; the two cannot be live
together, because the writer and the link admission refuse a retained team
approval and begin refuses a current attempt of another approval.

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

A message sent through the connected carrier records where it came from on its
request receipt, outside the request's intent: `provenance` is `{"carrier":
"connected", "account": <the binding's subject>, "binding_revision": <its
revision>}`, taken from the same binding snapshot the action's scope was checked
against. It records the session that sent the message, not proof that a person
typed it. A direct owner session and a message sent on the computer record none;
the same request ID with and without it is the same request. The one other shape,
`{"carrier": "setup_finish"}`, is accepted but not yet written. `request show`
returns the field (and a `provenance` line when present); older receipts have none.


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

When the guided team card carried the messages mark (its scope's grant holds
`"messages": {"sender": "card"}` and its answer the `actor_alias`), the approval
stores `{"alias": "human:<name>"}`. The name must be `human:` and 2 to 40 ASCII
lowercase letters, digits and single hyphens, starting with a letter and not
ending with a hyphen, and not a reserved word (`sender_alias_refusal`); otherwise
the guide stops with `sender_name_refused` and nothing is approved. For that
approval, `context` adds `"sends_goal": true`, and the Preview answer adds
`"sends_goal": true`, `"finish": "page"` and the exact `goal`, which the view
shows above Launch team; Launch approves that text. An unmarked approval answers
exactly as before.

After a healthy Launch, the guide's finish (`--_finish-setup`, run today by the
setup page) does for a marked approval, instead of the three optional cards: it
first checks, read-only, that the approval still stands for an active owner
(otherwise it records `revoked` with reason `setup_withdrawn`, or `stale` with
reason `stale_binding`, and reads or registers no name), and checks again inside
the registration's admission just before the name is written; it then takes the card's name, or the first free `<name>-2` to `<name>-9` (the base cut
to 38 characters) if a recorded person or bot already uses it; registers it;
re-checks the approval, binding and owner and writes the message grant in one
transaction; then, after the same checks plus the grant row, the goal against
the launched configuration, and the computer's update record re-read at send
time (still this Launch's, healthy, at the running revision), sends the Preview's goal to the lead once under a
request id derived from the Launch's. It counts the goal sent only from a
receipt whose sender, recipient and body match. One finish runs at a time
(`state/setup-finish-<launch request id>.lock`); its record lives in the
approval's Preview row, so running it again answers from that record and never
grants or sends twice. Its answer is `{"state": "setup_complete", "request_id":
..., "messages": {"state": "allowed"|"kept"|"name_taken"|"revoked"|"stale"|"in_progress",
"reason": ..., "by": "page"|"terminal", "alias": ..., "renamed": ...,
"goal": {"state": "sent"|"unknown"|"pending"|"not_sent", "reason": ...}}}`.
It reports `allowed` only while the grant row holds; otherwise `revoked` with
reason `messages_revoked`. The status run and `host owner setup-status` carry
the same answer and the goal text under `setup`.

The browser displays team/repository/goal and plan changes, with technical
identifiers behind a disclosure. `active` is an observed canonical activation
state; it does not prove provider credentials, GitHub access, bot health or
business readiness. No JWT purpose, detached activation, general job store,
credential initialization or HTTP schema repair is added. The existing operator
shell, sealed interpreter and native kernel ancestry checks still apply in the
foreground server. This source slice is not cold-host/native/browser acceptance.
