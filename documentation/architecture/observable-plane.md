# The observable plane

The plane is the fleet's flight recorder: one append-only SQLite database per
host that records what the fleet did — every dispatch, report, transmission,
heartbeat, registry keyframe and operator message — as typed facts with minted
identities, and derives everything else (open work, attention, presence,
utilization) from those facts at read time. The operator plane (`claudlobby
plane view`) renders it; `brief` answers from it; the legacy JSONL ledgers are
gone — retired reader by reader (the F18 cutover, below) and then removed
outright with the F18 closure (R1: no door writes a file any more).

Design of record: `documentation/plans/2026-08-18-observable-plane-design-v2.md`
(the forks F1–F18 are LOCKED there; this page describes what shipped). Phase
plans: `2026-08-2x-observable-plane-phase-*.md`; the cutover walk:
`2026-09-02-plane-cutover-f18-design-walk.md`. The view daemon's runbook:
`documentation/runbooks/plane-view.md`.

## Where it lives

| Path (under the host root, `CLAUDLOBBY_ROOT`) | What |
|---|---|
| `state/plane/plane.db` (+ `-wal`, `-shm`) | the database, WAL mode |
| `state/plane/capture.json` | per-fleet capture policy: `full` (the shipped default — bodies recorded) or `metadata` (the opt-out — bodies stripped at the door, proof triple kept). A named fleet beats `*`; a malformed file fails loud and resolves to no mode at all |
| `state/plane/ingest.sock` | the ingest daemon's socket (`claudlobby plane serve`) |
| `state/plane/staged/` | bounded policy-applied batches pending daemon recording; never committed proof |
| `state/plane/spool/` | the filesystem spool — the valve that must not depend on the db it protects |

One database per host. A fleet is a partition inside it (the `fleet_uid`
column on every row); cross-host federation (the Pi's plane joining the
Mini's) is deferred until the Pi's SSD fix.

## The families

Ten tables, one per family, all sharing the common envelope (`origin`
live|legacy, `import_batch`, `confidence`, `source_ref`, `ingest_seq` — the
ordering authority — plus host/fleet uids, `occurred_at`/`observed_at`/
`ingested_at`, correlation and trace ids). The vocabularies are CLOSED enums
enforced by the pydantic wire contracts (`claudlobby/plane/contracts.py`); an
unknown token is a `ContractViolation`, never a silently stored string.

| Table | Family | Notes |
|---|---|---|
| `communications` | a message between actors | `message_class` (task_request, report, question, answer, alert, notice, briefing, nudge, acknowledgement, chat, config_change, raw_control), `command_type` for dispatches (task, cancel, compact, restart, query), reply/supersedes chains, `body` under the capture policy |
| `work_items` | a unit of work | created by a dispatch, linked from its assignment and communication |
| `assignments` | work assigned to a bot with a deadline | `expected_by`; `source_ref = dispatch-log:<task_id>` (or `dispatch-log:sha:<content key>` for an id-less dispatch) is the legacy join key |
| `workstreams` | the per-fleet workstream registry's plane twin | `workstream-update.sh` emits its construct + verb events |
| `events` | everything that happens TO a construct | `kind` = task (22 tokens, `TASK_EVENTS`: progress, completed, failed, returned_blocked, cancelled, superseded, reassigned, expired …, plus the task loop's two HUMAN acts — `escalated` (a manager raises the task for human guidance; NON-terminal, so the task stays open while the human decides) and `nudged` (the operator asks the manager to act) — each carrying its text in the detail (`question` / `reason`, CONTENT-capped) and the person in `by`), transmission (send_attempted, pane_submitted, carrier_queued, carrier_accepted, recipient_acknowledged, failed, duplicate_suppressed, unknown — `ATTEMPT_STATES`), workstream, system (registry-stamped severity; a DIAGNOSTIC cap of 16 KB on `detail`, `detail_truncated` when it hit) |
| `identity_registry` | aliases → uids | kinds host, vault, fleet, actor, bot_instance, session; provisional actors until a registry keyframe confirms them |
| `registry_snapshots` | entity keyframes × observed change | the SCD partition is (host, entity type, entity uid); `payload_hash` gates the write; a tombstone is the one stored operation |
| `metric_samples` | numeric time series | names from the `METRIC_NAMES` registry (`bot.heartbeat`, `host.load`, …); retention 30 days (`plane prune`) |
| `ingest_ledger` | one row per accepted batch | the dedupe horizon; the view's SSE cursor reads it |
| `comms_fts` | FTS over the channel | only permitted content (capture policy) |

`ingest_seq` orders everything; `occurred_at` is when the fact happened.
Derivations never write a table: status, attention, open sets, presence and
utilization are queries (`claudlobby/plane/queries.py` is the ONE definition of
each; `presence.py`, `utilization.py`, `inventory.py`, `orgchart.py`,
`expiry.py` are pure reads over them).

## Identity

Names are aliases; uids are truth. `bot:<fleet>/<name>` resolves to an actor
uid (and to a `bot_instance` uid per spawn); `fleet:<name>`, `host:<name>` and
`session` uids likewise. Resolution mints lazily at ingest, so the first
message from a bot the registry has not seen creates a PROVISIONAL actor that
the next `generate` (the registry scan) confirms or tombstones; `plane doctor`
counts the provisional ones. Session uids are transcript-stable
(`sess_` + sha256 of the platform session id — the bash derivation in
`claudlobby/_runtime_scripts/plane-session-start.sh` is pinned byte-identical to `ids.derive_session_uid(id)` for agent CLI `claude`; another agent CLI derives in Python only, through `ids.session_alias(id, agent_cli)` (#2145 F2)).

## Owner access foundation (not enabled)

`plane/owner_access.py` is an internal policy/state primitive for **direct,
whole-deployment reads**, with separately approved per-fleet ordinary messages.
It does not authenticate a browser. The internal
`plane/owner_view.py:create_owner_app` factory exercises it with an explicitly
injected verifier; no CLI command, environment flag or startup job enables
that factory. The current runtime view is unchanged. This is bounded owner-access work
related to #1623, not completion of that issue or permission to expose Plane.

Explicit initialization requires the existing private `state/host-uid`, using
`ids.read_host_uid` (also used by operation identity resolution). Reads never
mint or repair it. The separate `state/plane/owner-access.db` is 0600 in a 0700
directory, bound to that installation and schema version. It does not migrate
or write the flight recorder. Missing, malformed, redirected, incompatible or
wrong-installation authority fails closed. This is not protection from root
or another process with the same OS identity, nor a solution to a clone that
copies both the host identity and authority store.

The prototype's contract:

- A trusted ingress will supply a namespaced human `PrincipalRef`. This value
  is only a reference; constructing it proves nothing. A pending pairing
  expires after five minutes and confers no access. Separate local approval
  must confirm its exact token and displayed principal. Only one owner is
  active; replacement requires explicit revocation followed by fresh pairing.
- Pairing/revocation append monotonically numbered grant changes. Concurrent
  confirmations serialize; one wins and invalidates all pending challenges.
  Revocation names the expected grant revision, so a stale approval cannot
  remove a later owner's grant. There is no cloud-requested removal door or
  claim that an undelivered removal has been applied.
- A paired principal can establish a fresh direct session without the website.
  Sessions last fifteen minutes in this prototype, are bound to the current
  grant revision, and require the verified principal again for each read or
  renewal. Renewal atomically replaces the token. Lost renewal responses need
  fresh direct authentication; an old token cannot be replayed. Expiry and
  session termination do not revoke the durable pairing.
- `authorize_read` checks the supplied target installation (the caller must
  derive it from the actual data source, never a browser claim), session expiry and current
  owner revision from disk on every call. Applied revocation blocks the next
  admission, renewal and fresh session, including in another process. The
  internal protected view also checks before each response-body delivery and
  closes a stream on refusal. The state module itself has no HTTP behavior.
- Credentials contain 256 random bits; only SHA-256 digests are stored. Token
  fields are omitted from object representations. Each store permits at most
  32 pending challenges and 32 sessions, cleaning expired records on the next
  corresponding write. Grant-change history remains local. Storage is durable
  SQLite with explicit transactions; authorization reads are query-only.
  Initialization publishes a complete private database atomically, without
  replacing existing authority; interrupted preparation can leave only a
  temporary file that is never consulted for admission.

A writer interrupted during a SQLite commit can leave a hot rollback journal.
Read admissions and `current_grant` use read-only connections, so they cannot
roll it back and may report unavailable (503 through the protected factory).
Recovery is an explicit local operation on the existing authority: the internal
`OwnerAccess._connection(write=True)` context opens it in `mode=rw`, lets SQLite
recover the journal, and validates its installation and schema. Entering and
closing that context without changing rows preserves grants and sessions.
An already-authorized pairing/session/revocation operation uses the same write
connection, but still performs its declared mutation. No recovery CLI is
exposed; `initialize` validates existing authority through a read-only
connection and is not a recovery door. HTTP reads never perform this recovery.

These lifetimes and limits are bounded experiment choices, not a selected
browser protocol. Pairing and reader sessions grant no website membership,
workspace binding or operational writes. The separate message grant below
does not replace canonical operation binding or Plane actor attestation (#1622).

### Explicit owner messages (internal, no browser endpoint)

`OwnerAccess.allow_messages` is a local approval primitive: it binds the exact
current owner grant to one canonical fleet UID and one existing human actor UID
and alias. It is never callable by a remote client. Approval transactionally
creates the optional `message_grants` table in the private authority store;
ordinary initialization and reads do not create or migrate that table. A grant
cannot change actor until locally revoked. Each grant includes an opaque durable
`generation`: repeated approval preserves it, but revoke/reallow allocates a new
one even for the same actor. An adapter must bind the complete grant, including
generation, into its action context. Existing four-column stores derive a stable
legacy generation from their persisted binding during reads. The first local
grant write adds and persists that generation in the same transaction; it does
not invalidate other fleets' contexts. New approvals always use fresh random
generations. Invalid stored generations fail closed; reads never repair them.
Owner revocation/re-pairing invalidates
every old message grant. Reader access alone continues to refuse messages.

`plane/owner_messages.py:OwnerMessages` pins an installation and accepts only
an already verified `VerifiedReader`. For each operation it checks the session
and grant, binds current active host/fleet/actor identities, and refuses generated
bot/timer environment selectors. It accepts an exact recipient UID from that
fleet, never an alias supplied as the sender or an OS-account fallback. The
caller must register the intended human identity separately before local approval.

The send holds canonical runtime mutation admission and calls the extracted
`commands/message_write.py:deliver_bound_message` workflow. CLI and internal
owner sends therefore share request UUID conflict handling, recording, native
delivery, first-attempt held-box repair and receiver byte-integrity proof. A
strict owner replay inspects the retained outcome without native sends or Enter
repair; ordinary CLI replay behavior is unchanged. Native submission
alone is not success. Exceptions can follow effects: retain the original UUID,
then `inspect` its bound request and receiver evidence without resending. A
missing request does not prove no previous effect. The internal result types
are evidence for a future adapter, not a ready-made browser response contract.

Admission is rechecked at dispatch start and before returning inspection data
or send outcomes, including canonical `CommandFailure` outcome data.
Revocation prevents subsequent admission; it does not cancel an effect already
in progress. This slice supports ordinary messages only. It supplies no HTTP
route, UI capability, retry control, reply, task mutation, local confirmation
UI, trusted ingress verifier or browser origin/CSRF policy. Those and a real
isolated bot canary remain required before enabling browser operations. Tests
use real private activation/Plane owners with a synthetic native receiver.

### Protected read factory (internal experiment)

`create_owner_app` wraps the canonical `view.create_app` in one outer ASGI gate.
The resolved source root also selects its owner authority and host UID; request
parameters cannot select an authority store or another deployment. There is no
path allowlist: API routes, search, grid, health details, static files, HEAD,
errors and later-added routes all cross the gate. Even an admitted reader
cannot use a write method or WebSocket through this factory.

The required asynchronous verifier supplies an immutable `VerifiedReader`
(principal and session token) from trusted server code. No identity headers,
cookies, URL credentials, website user IDs or workspace IDs are interpreted.
The test verifier is out of band and synthetic; it is not a production
Tailscale verifier. This factory has no HTTP session-creation or pairing endpoint;
the separate browser transport below owns those exact lifecycle routes.

Admission happens before calling the view and again immediately before every
response body chunk, using current host identity, grant and session state.
Headers are held until the first body is admitted. Every response is
`Cache-Control: no-store`; sendfile extensions are disabled so static bytes
cannot bypass the gate. Non-SSE responses preserve Content-Length when supplied.
Before headers are sent, denial is 403 and unavailable authority is 503, both
with generic bodies. After an SSE stream starts, refusal closes it normally
without another private frame or a replacement status. Other started responses
abort without a terminating body chunk, so a truncated response is a transport
failure rather than a successful short 200. Refusal is latched for that response;
subsequent sends cannot resume delivery even if the inner producer catches it.
The existing stream's one-second idle tick supplies the next admission check.
Already delivered/in-flight bytes cannot be recalled; this is admission at
each delivery, not a global transaction between revocation and network output.

The protected factory preserves the existing lifespan and `begin_shutdown`
signal. The normal `plane view` command continues to call the original factory.
This experiment establishes response enforcement, not deployed protection,
browser login, a credential transport choice or website workspace admission.
Protected SQL reads require an explicit local source binding, described below.
The ownership marker, retained host facts, query and response provenance share
one SQLite read transaction. Each SSE batch repeats that check; a copied or
mixed-host source is refused without disclosing its contents.

Before a deployment opens real fleet reads, validate the configured Tailscale
Serve boundary and end-to-end enforcement on its supported host and browsers. Tailscale Serve
[documents user headers and their trust limits](https://tailscale.com/docs/features/tailscale-serve#identity-headers):
they must not be accepted from an arbitrary directly reachable backend.
Website-connected sessions additionally require independently verified website
identity/workspace evidence; a supplied user/workspace string is insufficient.
The view's GET-only contract remains intact, with pairing/session mutations
in the separate authority transport below. No real protected use is claimed by
the synthetic policy tests.

### Local owner administration

The supported local authority door is `claudlobby --root DATA_ROOT host owner`.
`DATA_ROOT` names the existing installation; these commands never create an
installation identity, start a service or change Tailscale. Message allowance
selects only its explicitly named target fleet; other owner doors are host-local.
Run them in the operator terminal on that host:

```sh
claudlobby --root DATA_ROOT host owner initialize
claudlobby --root DATA_ROOT host owner status --json
claudlobby --root DATA_ROOT host owner confirm
claudlobby --root DATA_ROOT host owner revoke
```

`initialize` requires typing `INITIALIZE` and prepares private authority storage.
It preserves existing grants and is not a database-recovery operation. `status`
is read-only; unavailable authority is an error, never an unpaired result.
`confirm` reads the browser's short-lived pairing code with terminal echo disabled,
previews its exact principal, host and expiry, then requires typing `PAIR`.
The operator compares that principal with the browser before approving. The
write transaction rechecks the previewed request; expiry or concurrent approval
cannot turn a stale prompt into authority. No code is accepted in CLI arguments
or printed by the command. `revoke` shows the current grant and requires typing
`REVOKE`; the expected revision prevents revoking a replacement owner.

Changes refuse redirected input, JSON mode and generated bot/fleet selectors.
Composed bot permissions deny these operator-only command forms as well.

Ordinary-message authority is separately approved at the local terminal:

```sh
claudlobby --root DATA_ROOT host owner allow-messages --target-fleet example --actor human:local-owner
# If absent, explicitly approve REGISTER, then approve the allocated UID with ALLOW:
claudlobby --root DATA_ROOT host owner allow-messages --target-fleet example --actor human:local-owner --register-actor
claudlobby --root DATA_ROOT host owner status --json
claudlobby --root DATA_ROOT host owner revoke-messages --fleet-uid fleet_11111111111111111111111111111111
```

Allowance requires an active sealed runtime and a confirmed active fleet. It
shows the current owner principal/revision, host, fleet UID and exact local
human actor binding before typing `ALLOW`. An absent human requires the explicit
`--register-actor` flag and a separate `REGISTER` approval: canonical Plane ingest
records `operator_first_seen` under mutation admission, then the command shows
the allocated actor UID for the second approval. Cancelling the second approval
leaves that explicitly approved first-contact record, with no message grant.
The operator attests the local human label; it is not derived from a browser
claim or granted by a remote caller. No registration occurs during preview.

After confirmation, the CLI rebinds the displayed identities and owner revision
under runtime admission; any changed binding refuses. The store atomically
rechecks the owner revision before granting. Only ordinary messages are covered;
this grants no task mutation, reply, permission decision or website membership.

Local status lists the current owner's retained message grants, including fleet
UID, actor binding and generation, without selecting active configuration or
reading Plane. Grants belonging to earlier owner revisions are inert and are
not listed as current authority.

Revocation displays one retained grant and requires `REVOKE-MESSAGES`. Its exact
fleet UID names the retained authority even if active configuration or Plane
storage is unavailable. The existing authority transaction compares both owner
revision, displayed actor binding and generation, refusing a replacement grant
even when it approves the same actor again. Revocation
keeps owner read access and other fleets' message grants. These doors accept no
credential arguments, generated bot/fleet context, ambient fleet selection,
redirected confirmation or JSON mutation mode.

These checks prevent accidental invocation in a bot context; they do not
isolate a malicious process with the same OS privileges. Revocation invalidates
the pairing and its sessions without stopping fleets or deleting their history.
Local confirmation grants private reads only: website membership and ordinary
message permissions remain separate.

### Direct-host browser transport

`plane/owner_browser.py:create_owner_browser_app` wraps the protected read
factory and exposes exactly five lifecycle routes. It requires an asynchronous
trusted `PrincipalRef` verifier and one configured canonical external HTTPS
origin. There is no default identity verifier: cookies, query parameters and
Tailscale/forwarded identity headers do not establish identity in this factory.
The explicit private-socket server below supplies the production adapter; the
normal Plane service still uses its original read-only factory.

| Route | Method | Effect |
| --- | --- | --- |
| `/api/owner/status` | GET | Reports needs-pairing, sign-in-required or ready; never initializes authority. |
| `/api/owner/pair` | POST | Returns a five-minute challenge and verified principal for separate local approval. |
| `/api/owner/login` | POST | Opens a session after local confirmation; a current session is rotated; stale cookies recover through fresh verified sign-in. |
| `/api/owner/renew` | POST | Rotates the current session, invalidating its old token. |
| `/api/owner/logout` | POST | Ends the current session and removes its cookie. |

Every HTTP request must match the configured Host; any supplied Origin must
match the configured HTTPS origin. POSTs additionally require that Origin,
`X-Claudlobby-Owner: 1`, JSON content type and an empty JSON object (at most
1 KiB, read within five seconds). Cross-origin preflight, query-bearing
lifecycle requests and ambiguous Host/Origin/intent headers are refused. No CORS policy
admits another origin. Local HTTP forwarding is permitted only because the
caller must separately secure its HTTPS proxy/backend boundary.

The session is carried only by `__Host-claudlobby-owner`, with Secure,
HttpOnly, SameSite=Strict, Path=/ and no Domain. Responses never put session
credentials in JSON or URLs, and lifecycle responses are no-store. Only
explicit logout deletes the cookie. Status and failed-renewal responses
do not clear it: a delayed response must not erase a newer cookie installed by
an overlapping renewal. Explicit sign-in recovers malformed or expired cookies
using the freshly verified principal and current local grant. The pairing
challenge is not a session: local `confirm_pairing` must still verify its exact
token and displayed principal. No HTTP confirmation, revocation, message grant
or bot action exists.

The exact public shell routes `/owner`, `/owner-entry.js` and `/owner-entry.css`
contain no private facts and accept only query-free GET/HEAD under the same
Host/Origin boundary. Their CSP permits only same-origin scripts, styles and
requests, denies framing and uses no inline script or external asset. The page
distinguishes unpaired, awaiting local approval, expired, sign-in-required,
ready, signed-out, denied and unavailable states. Its local pairing countdown
uses the supplied relative lifetime, not clock equality with the host; the host
still enforces the absolute expiry on approval. Pairing, sign-in and sign-out
require explicit interaction; page load never opens a session. Pairing details
stay in page memory, and successful sign-out does not immediately sign in again.

The protected Plane selects its owner transport only after the read gate admits
`/api-client.js`. It offers explicit renewal and sign-out. Renewal fences old
reads and streams, refreshes immediately, then refreshes again when SSE opens;
active inventory, equipment and search reads resume too. A refused sign-out
that still has a valid session explicitly says it did not complete. Panel HTTP
errors stay local; network failures or read-gate 503 pause private reads and
permit one automatic status GET. Its recovery budget is rearmed by an admitted
stream message or explicit interaction; renewal and logout are never replayed. An
SSE failure has its own one-probe budget, unaffected by successful board reads
or a short open/error cycle. An admitted stream message, explicit Check or
successful renewal rearms it. A quiet connection that remained open for at
least 30 seconds, measured with a monotonic clock, earns one fresh status probe
on failure; a connection that never opened earns no additional probes.
Inventory preserves the intended equipment
selection in memory and only its latest request can restore that panel; grid
reads also reject results spanning a session pause. An
expired or absent session on GET/HEAD `/` redirects only to same-origin `/owner`.

All other paths still cross the protected canonical read gate, including
the operational renderer's static files and each SSE body delivery. Logout or locally applied revocation
therefore blocks the next private delivery. Tests exercise this with disposable
state and an injected synthetic principal, plus a loopback HTTP subprocess
simulating a proxy. They do not prove actual HTTPS browser cookie behavior or
Tailscale identity. The private-socket adapter has separate native CLI evidence;
actual Serve forwarding, socket connectivity and HTTPS browser acceptance remain
deployment gates. Bot actions need their own independent canary.
This is a same-origin direct-host protocol, not website OAuth, cross-origin
embedding or workspace membership.

### Private-socket owner server and source binding

`host owner serve` is an explicit foreground server for Tailscale Serve's Unix
socket backend. It requires an initialized owner authority, a locally bound
recorder database, an absolute native Tailscale executable and the exact external
HTTPS origin. It neither enrolls a service nor configures Serve. The existing
`plane view` service continues to use its original factory.

Before starting it, the operator uses `host owner bind-source` in a terminal.
The command requires an explicit installation root, initialized authority and
confirmation by typing `BIND`. That is an attestation that all the existing
history, including imported or unattributed history, belongs to this installation.
It adds source metadata and covering indexes for host admission; it does not
migrate the recorder schema or alter records.
Reads never bind or repair a database. A source already marked for another host
cannot be rebound by this door. The marker travels with a database backup, so
copying even a fully pruned database under another host's grant is refused.

```sh
claudlobby --root /path/to/data host owner bind-source
claudlobby --root /path/to/data host owner serve --origin https://plane.example.ts.net --tailscale /path/to/tailscale
```

The default socket is `DATA_ROOT/state/plane/owner.sock`. An explicit `--socket`
can select a shorter absolute path when the host's Unix-socket path limit requires
it. The containing directory must already be owned by the process user with mode
0700; the socket is 0600. Existing socket paths are never deleted on startup.
Graceful shutdown removes only the socket this invocation created. After an
unclean exit, the operator must inspect a retained socket before removing it.
There is no TCP fallback. A daemon unable to reach this socket leaves access
unavailable; it is not a reason to expose an unprotected loopback backend.

The server disables Uvicorn's proxy-header rewriting. The ingress adapter accepts
only a Unix-socket request with exactly one source IP in `X-Forwarded-For`, which
Serve must replace from its authenticated connection. It rejects the Funnel
marker, chains and malformed/ambiguous headers. Each admission calls the explicitly
configured CLI to check the official control realm, perform WhoIs, and recheck
the realm. A positive numeric `UserProfile.ID` must equal `Node.User`; tagged,
expired or address-mismatched nodes are refused. Login names and email addresses
are never authorization keys. Unsupported control servers are refused rather than
sharing the official principal namespace.

CLI subprocesses have bounded output, time and concurrency, a fixed minimal
environment and cancellation cleanup. `TERM=dumb` keeps the bundled macOS CLI in
command-line mode without inheriting caller settings. Diagnostic stdout/stderr
are not returned to browsers. A real native WhoIs capture grounds the anonymized
fixture shape; a read-only native adapter probe established numeric identity and
realm agreement on the installed client. Neither observation proves Serve's
forwarding or its access to this filesystem socket.

Source admission checks every retained family, including legacy, tombstone and
host-global records, plus explicit fleet-parent ownership. Separate indexed
minimum/maximum reads check the full host range without scanning healthy tables.
A bounded fallback refuses unavailable sources rather than holding an unbounded
read transaction; indexes are created only by the explicit binding command. It grants all fleets
within this installation; a shared human identity is not treated as one fleet's
private property. The local operator's source attestation covers unattributed
history, and a malicious process with the same OS privileges remains outside this
boundary. No website membership or bot-message grant is implied.

Disposable subprocess tests exercise pairing, protected reads, process restart,
revocation and socket cleanup through the actual foreground CLI with a synthetic
Tailscale executable. Actual Serve-to-socket connectivity, external HTTPS cookies,
remote laptop/mobile browsers and Mini/Pi deployment remain unverified. No live
fleet or Tailscale setting is changed by these tests.

## The write spine

`emit()` / `emit_batch()` (`claudlobby/plane/emit_api.py`) is the one
programmatic write: validate the RAW envelope, apply the capture policy,
validate the captured form, then one transaction per batch (`ingest.py`) with
dedupe on `event_id` — a batch that mixes duplicates and new rows is REFUSED
("mixed state") rather than half-applied. Public ingest uses
`claudlobby plane emit FAMILY --file FILE|-` or
`claudlobby plane emit-batch --file FILE|-`; `--json` selects the common result.
Exit 0 means committed or already present, while exit 6 means durably spooled
and still pending. Conditional writes can use `--require-commit` to refuse
spooling; an uncertain commit must be reconciled by event ID before retrying.
The hot bash path stays on `claudlobby/_runtime_scripts/plane-emit.sh`: its stdlib socket client
pre-mints event IDs and durably stages an unacknowledged batch for daemon
replay without starting the full CLI. The daemon (`plane serve`, composed as the dormant `claudlobby-plane-daemon`
host service) owns INGEST AND NOTHING ELSE. `PLANE_EMIT_DISABLED=1` is the
harness exemption (a byte-identical no-op); every door calls the shim `|| log`
and never blocks its real action on it.

**What does NOT fall down the ladder** is decided by one question — *would the
cold rung repeat this refusal?* A contract violation would (same validator); a
total failure would (same db). A **downgrade would not**, and treating it as a
verdict is what cost the estate 261 heartbeat samples across 18 bots in ~15
minutes on 2026-09-06 (#1485). A downgrade says the db is newer than the code
**the answering process loaded**, and the daemon is a long-lived process on an
editable install: a `git pull` swaps the files, the daemon keeps its old
modules, and the first migrating door (that day, `plane doctor` applying 0010)
leaves the daemon the only stale thing on the host. The cold rung is a fresh
interpreter on the install's *current* code and commits. So
`plane-socket-client.py` maps a `downgrade` reply to exit 5 — the shim's
fallback trigger — and the daemon itself **exits 4** on the condition, at
startup (immediately after bind, as the first statement inside the serve
loop's own try/finally, so the exit unlinks the socket and drops the lifetime
lock on its way out and nothing that WRITES the db runs first), on the first
request that hits it, or at an interval drain. Its supervisor relaunches it on
the current install; if the install was never updated it exits again and the
supervisor's throttle sets the cadence (systemd `RestartSec=5`, launchd ~10s),
while the cold rung keeps recording.

Two consequences worth stating plainly. The rc 5 also **arms the shim's wedge
marker**, so for the next `PLANE_WEDGE_COOLDOWN_S` (60s default) every
emission — every door's, not just this one's — skips the socket and goes
straight to the cold rung, then the socket is retried: slower, disclosed,
nothing dropped. And the client returns only 0/2/3/5, so the shim's
passthrough arm carries **2 and 3 only**; a `4` there was dead code. A
**cold-rung** downgrade still exits 4, at the tail where the shim returns the
CLI's rc verbatim — there the install itself is behind the db and no rung can
help.

**Durable staging (#1657, unified CLI).** A socket miss or cooldown leaves a
finalized, capture-policy-applied batch in `state/plane/staged/`. This is
pending, not committed recording. The stdlib client imports the same capture
policy as the daemon in its existing process; it never starts a cold CLI on
this path. Missing or invalid policy refuses staging explicitly.

The queue accepts at most 2,000 batches or 32 MiB at each admission check;
concurrent producers can overshoot by their simultaneous batches. A full or
unwritable queue refuses and records a best-effort `.emit-losses` breadcrumb.
Daemon replay uses the normal `emit_batch()` owner, at most 200 batches or
0.5 seconds per serving tick. Invalid capture configuration or an existing identity-parent conflict leaves
batches pending for later repair instead of quarantining them. An interrupted stage's
`.<event id>.tmp` becomes eligible for replay after an hour. One left empty, because the
stage was reaped before writing its batch, never reached the disk: once its writer is
gone, replay records it as a `stage_empty` loss in `.emit-losses` and removes it, instead
of quarantining it as a malformed batch (#2164).

`plane doctor`, `plane status`, and the trust panel expose staged depth.
Doctor flags unreadable, full, stale, or undrainable pending data. An
initialized plane with a never-started daemon needs attention. A staged batch
never satisfies a linked/task operation's committed-recording requirement.

**The quarantine and the counted losses (#2165).**

- **Listing the quarantine.** `plane spool list --quarantined` lists every quarantined entry, newest first. Each item shows when the entry was written and when it was quarantined, its size, whether it is empty, and its reason. The listing pages with `--limit` and `--cursor`, states its coverage, and reads only files, never the database.
- **Reading one entry.** `plane spool inspect NAME` reads a single entry. That includes a refused stage, whose name has the form `<time_ns>-<lead event>[.batch.<pid>].json`.
- **The loss counter.** `emit_losses_summary` in `plane/health.py` reads `state/plane/.emit-losses` once, for three readers:
  - `plane doctor`'s emit-losses rung;
  - `plane status`, as `emit_losses`;
  - the brief, which labels `alerts` (`#2165`) while a known loss sits in the 24 h window.
- **Unreadable is not zero.** An unreadable quarantine or counter is reported as unreadable, never as zero.

**The deadline follows who waits (#1693).** The client's total deadline is
1.0 s unless the caller's class says otherwise. `PLANE_EMIT_CLASS` is `hook`
(a live turn waits), `background` (nothing reads the result;
`plane_emit_bounded`'s default) or `door` (the outcome turns on the result),
and `PLANE_SOCKET_DEADLINE_HOOK_S`, `_BACKGROUND_S` and `_DOOR_S` set that
class's deadline; all three are unset by default. On one SD card both rungs'
commits wait for the same device, so a caller that misses during a stall gains
nothing from the cold rung but a process spawn, and costs every other door on
the host 60 s of cooldown. A caller that can afford to wait should. Every
exit 5 from a socket attempt is recorded in `state/plane/.socket-arms` with
its class, deadline, elapsed time, caller and cause: the marker itself holds
only a time.

There is no separate startup check: the daemon's first writes after bind —
the lifecycle receipt and the startup spool drain — go through `migrate()`,
which refuses a db newer than the code before writing anything, and that
refusal is what exits the daemon. Everything that touches the db runs after
`_bind()`, so a `plane serve` REFUSED for a bad `--socket` parent, or because
another daemon already holds the lock, never touches the plane at all. The
first build ran `migrate()` BEFORE bind, and a refused serve from a newer
checkout migrated the live plane on its way out — the very act that makes a
running daemon stale, with 0010's seconds-long write lock taken outside the
daemon lock.

**Deploying a migration, or a registry change.** A pull that carries one leaves
every resident process on the old modules. Since #1485 the ingest daemon repairs
itself, but bouncing it explicitly is still the fast path and is the only remedy
for a daemon predating that fix. **A registry change never repairs itself:**
there is no schema bump to refuse on, so the daemon keeps stamping severity from
the registry it loaded at start. A new critical type is stored with NULL
severity, invisible to every critical read, until the bounce (`crash_loop`,
#1774; rows stored meanwhile stay NULL):

```
launchctl kickstart -k gui/$UID/claudlobby-plane-daemon    # macOS
systemctl --user restart claudlobby-plane-daemon           # Linux
```

Confirm the bounce took (Linux): the timestamp must read later than the pull. A
new critical type's first row cannot be waited for, so nothing else shows it:

```
systemctl --user show -p ActiveEnterTimestamp claudlobby-plane-daemon
```

**Where the loop shows.** Not `plane doctor`: a newer db makes every
migrating door REFUSE at 4 through `_guarded` *before* a single rung prints,
so its schema rung is unreachable in exactly this condition. What an operator
gets is (a) the `REFUSED — plane.db user_version=N is newer than this code
(supports <=M)` line any migrating door prints (`plane status`, `plane
doctor`), which names both numbers, and (b) the daemon's own exit line, once
per relaunch, in `<root>/state/plane-daemon.log` (launchd, appended by the
composed plist) or the journal (systemd, `journalctl --user -u
claudlobby-plane-daemon`). Nothing records it on the plane — a process that
refuses the db cannot write a row about refusing it — and that log grows for
as long as the loop runs, which is until the install is updated.

There is deliberately **no doctor rung counting the loop**: it would render
only where the condition is absent, which is the dead-signal shape this
program keeps refusing.

## The doors — who writes what

| Door | Records | Silenced by |
|---|---|---|
| `claudlobby task admit` / `task assign` | admission records a queued work item; assignment records its owner separately. Neither sends a message | committed recording required; an unavailable Plane refuses the mutation |
| `claudlobby assignment deliver` | the generated fleet manager records a communication linked to the committed assignment, then attempts native delivery; transmission and receiver receipt remain separate evidence | committed recording required before delivery; an unknown outcome is not automatically resent |
| `claudlobby assignment accept` / `progress` / `block` / `return` / `complete` / `fail` | the assigned bot accepts or transitions its current assignment; linked reports record the transition and communication, with manager notification reported separately | committed recording required; notification failure does not undo the recorded transition |
| `claudlobby message send` / `fleet reports submit` | ordinary messages and explicitly unlinked reports record a communication without transitioning an assignment | recording outages allow delivery with an explicit degraded result and bounded fleet alert; delivery and recording outcomes remain separate |
| `claudlobby/_runtime_scripts/keepalive.sh` | `bot.heartbeat` + `bot.session_up` metric samples per tick (presence's recorded half) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby/_runtime_scripts/plane-telegram-in.sh` / `-out.sh` / `plane-rc-relay-out.sh` (hooks) | the operator's inbound messages, the bot's replies, RC-relayed final answers, with honest transmission states (carrier `telegram-bridge`) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby/_runtime_scripts/plane-dispatch-in.sh` (UserPromptSubmit hook) | a `received` transmission for a tracked dispatch, report, or briefing whose final-line plane marker reaches the receiving bot; records the received wire byte count and hash so delivery can be checked against the sender's proof, while ordinary prompts record nothing | `PLANE_EMIT_DISABLED=1` |
| `claudlobby/_runtime_scripts/tg-post.sh` | a `notice` communication + its transmission for every fleet post to Telegram (carrier `telegram-tgpost`, intent before the send, the outcome after) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby/_runtime_scripts/plane-session-start.sh` (hook) | the session uid + a per-process uid to `$BOT_DIR/data/.plane-session` | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby/_runtime_scripts/plane-host-probe.sh` (host timer) | `host.*` metric samples (load, RAM, disk, Pi thermals) | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby/_runtime_scripts/transcript-digest.sh` (SessionEnd hook) | one `session_digest` system event per finished session on the bot's actor — the capture rubric (context/worked/failed/would_change/reusable), session id + uid, model, turn/tool counts, all in `data`; a `skipped` variant at zero model cost, distinct from an `ok` with empty fields (#1503 moved it off `transcript-digest-<date>.jsonl`, the last production JSONL data record) | `SESSION_DIGEST_ENABLED=1` per fleet (dormant by default); `PLANE_EMIT_DISABLED=1` |
| `claudlobby generate` (`registry_emit.py`) | registry keyframes for every composed entity; the `scan_completed` declaration that validates its tombstones | dormant until `PLANE_EMIT_ENABLED` in the fleet `.env` tier (the tier cascade, not `fleet.yaml env:`) — the flag's only meaning since the closure |
| `lib/workstream-update.sh`, `claudlobby/_runtime_scripts/briefing-trigger.sh` | workstream construct + verb events; briefing communications | `PLANE_EMIT_DISABLED=1` only — always on since F18 R1 |
| `claudlobby plane expire` (host timer) | a terminal `expired` on assignments overdue past the horizon — a Lane-B fact through normal ingest | `PLANE_EXPIRE_ENABLED` |
| `claudlobby task withdraw` / `task escalate` | Fleet-owned acts on a canonical work item: a withdrawal records a terminal `cancelled` task event, and an escalation records a non-terminal question that remains visible while the work is open. Both accept queued work and use a durable request UUID; historical dispatch display and assignment IDs are not aliases. The granted bot forms use `claudlobby --json task ...`. | No action is claimed if the Plane write fails; a replay with the same request UUID returns the prior result. |
| `claudlobby task nudge` | non-terminal `nudged` on canonical open work, including queued intake, plus a linked request from the actual caller to the selected fleet's implicit manager; both commit before shared native notification. `--by` is provenance. Request replay never resends | Recording or request persistence unavailable: refuses before notification; a later transport failure leaves the committed fact intact |
| `claudlobby task recheck` (fleet timer invokes the selected CLI) | One digest to the current implicit fleet manager for up to eight overdue or ageing canonical open tasks, including queued work. The actual caller records one ask per named task before transport; only the primary digest message has a transmission and receiver proof. A request UUID freezes the selection, and a no-op sweep records a system fact so replay cannot acquire new work. | `TASK_RECHECK_ENABLED=0` skips the timer tick loudly; recording failure prevents the send. |

Dormancy is a compose-time fact where the composer can make it one (an unarmed
`unit: service` job composes NO units) and a self-gate where it cannot (host
timers are enrolled regardless, so `plane-prune.sh` and `plane-expire.sh` check
their own flag). The read side (`brief`, `plane view`) needs no flag.

## The read side

**Two fleets on one host (U, #1467).** The operator plane reads the fleet
dimension from the plane itself: `/api/fleets` from the registry's fleet
identities, every per-fleet route scoped through one fleet-axis predicate
(`queries.fleet_alias_range` in SQL, `inventory.fleet_of` in Python —
case-sensitive like the room's equality arms), a typed `unknown` state for a
fleet the plane does not hold, names qualified `fleet/name` wherever two fleets
meet, and an overview whose `open` is the matcher's own rule
(`OPEN_ASSIGNMENTS_AT_SQL`), so the strip and `claudlobby brief` can never
disagree on the same fleet. Details: `documentation/runbooks/plane-view.md`.

- **`brief`** (`claudlobby/brief.py`) — the fleet's one read door; its
  `degraded[]` envelope says which sections were LABELED or OMITTED and why
  (an unreachable source is never an empty answer — `source_state.py`).
- **`plane view`** (`view.py`, the `[plane-ui]` extra) — the operator plane:
  read-only by construction (`mode=ro` + `query_only`, no non-GET route),
  the channel threaded by work item, attention, identity cards, the thumbnail
  grid + one live pane, trust/gaps, SSE off the ingest-ledger cursor, `/healthz`.
  Composed as the dormant `claudlobby-plane-view` host service; Tailscale Serve
  fronts it.
- **`plane samples <metric>`** (#1644) — one `metric_samples` family for one
  subject over `--since`/`--until`, as text or `--json`; the host probe's
  `host.*` families by default (`host.load`, `host.mem_available_mb`, …), the
  subject being the only one of its kind unless `--subject` names it. It is
  read-only by construction: `open_ro`, no `migrate()`, and every row is
  fetched and the connection closed before anything prints. The window
  compares instants through `julianday()`, because ingest keeps each sample's
  own offset. JSON uses the schema-1 command result; malformed arguments exit 2,
  and unavailable storage or no recorded subject exits 6. This is how a host's
  load and memory into a reset are read back.
- **`plane status` / `plane doctor`** — the health page and the pre-flight
  rungs (schema, provisional actors, tombstone validity, reconciliation, the
  WAL against its ceiling).
  These diagnostics open the database read-only and require the selected schema;
  they do not migrate it. `plane prune` and `spool retry` are explicit
  maintenance mutations; a newer db refuses them (`DowngradeError`, rc 4).
  Both support `--json` with the common command result. A spool retry reports
  committed, duplicate, quarantined and still-pending entries separately;
  quarantine or pending entries do not return a clean success. Spool retry,
  operator quarantine and live prune require selected-release mutation
  admission; spool inspection and prune dry runs remain diagnostic reads.
- **`plane registry`** — lists recorded registry rows and their history or
  changes, with `--json` returning one schema-1 result. `--verify` compares
  authored fleet configuration with the recorded Plane projection. Incomplete
  authored enumeration or drift is a conflict, never a match; even a match
  does not prove that the reviewed plan was activated or is running. Review
  authored changes with `config plan` and activate the reviewed plan with
  `host activate`.
- **The stdlib readers** (`claudlobby/_runtime_scripts/plane-readers.py`, `claudlobby/_runtime_scripts/plane-lookup.py`) — the
  plane answered from bash doors without paying the package import: the open
  list and the overdue set (SQL pinned byte-identical to
  `queries.OPEN_ASSIGNMENTS_AT_SQL`), the resolver, the legacy-id join, the
  divergence check, the retirement. One open shared by all: the `mode=ro`
  URI first, and on CANTOPEN a plain connection held read-only by `PRAGMA
  query_only` — under the system `python3` the doors run, a read-only URI
  cannot open a WAL database whose writer has closed (it cannot create the
  shared-memory file), which is what a daemon restart looks like. Review
  attribution now lives in `claudlobby task reviews` (`claudlobby/review_queries.py`).
- **A read pins the WAL while its statement is open (#1905).** The daemon's
  checkpoint cannot reset the WAL past a reader's snapshot, and a loop over a
  live cursor keeps its statement open for the whole loop, so readers fetch
  their rows first and do the per-row work after
  (`tests/test_plane_reader_snapshots.py` fails a loop that queries, yields or
  writes per row). A reader that never lets go, such as an interactive
  `sqlite3` session or a hung process, still grows the WAL, and the daemon
  cannot end another process's transaction. So it is reported: the host probe
  records `host.plane_wal_bytes` every minute (the Host card shows it), and
  `plane doctor`'s `wal` rung turns ATTENTION past the 4 MiB ceiling accepted
  on #1693, naming the holding process from `/proc/locks` on Linux.

## The task loop (#1481) — in the operator's words

A dispatch used to be a one-way street: you sent it, and either a report came
back or the row sat there. The loop closes it, and every step is a plane fact
already described above — nothing new is stored, and there is no second
bookkeeping surface to reconcile.

1. **Every id'd dispatch gets a deadline** (24h by default, per fleet), so the
   watchdog and the re-check have a clock.
2. **The manager can end a row without a report.** `claudlobby --json task
   withdraw TASK_ID --reason "…" --request-id UUID` closes canonical work
   (`cancelled`, terminal for every reader); a
   re-dispatch with `--supersedes` retires it and opens the replacement.
3. **The manager can ask you a question about a task** — `claudlobby --json task
   escalate TASK_ID --question "…" --request-id UUID` — and the work STAYS OPEN while you decide, and is EXEMPT from the
   re-check timer below (item 5) once assigned: it is the human's to answer, not the
   manager's to be nagged about (the M-B fold's F5). Queued work can be escalated too. Each recorded raise is paged
   to the fleet's Telegram chat exactly ONCE, by fleet-pulse, as
   `NEEDS YOU (<fleet>): task <id> escalated by <manager>: <question>`, keyed
   by event id in a PER-FLEET seen-file (`state/pulse/<fleet>.escalated`
   — the fold's F1: `state/pulse/` is host-global, one root composing several
   fleets, so a single shared marker directory let one fleet's forget-loop
   erase another's markers and re-page its whole backlog). The page is keyed
   by the raise, not by a clock: it goes quiet when a later task act clears the raise
   (progress, a report, a withdrawal, a supersede) and speaks again if the
   manager raises the row afresh. A nudge does not clear it.
4. **You can poke open work** — `claudlobby --json task nudge TASK_ID
   --reason "why" --request-id UUID` records the nudge and asks the selected
   fleet's manager to revisit it. From Telegram, ask the manager to run it
   with your provenance in `--by`; the bot remains the actual caller.
5. **The clock pokes for you.** Where a fleet arms `task-recheck`, every 6h
   each manager gets ONE message listing their rows past deadline or older
   than 48h — id, title (clipped to ~80 chars, the fold's F6: the id already
   carries the row's full identity), assignee, age, deadline, last progress,
   and whether anyone nudged it — with the four verbs and their exact
   commands, and is asked to report what it did per row. An escalated row is
   never named (item 3); a "waiting on the human: N row(s)" footer names the
   count where a digest is already going out for other reasons. A row already
   named inside the repeat window (24h) is skipped, and that skip is a PLANE
   READ: the ask itself is recorded per row, stamped
   `source_ref = task-recheck:<task_id>:<current_assignment_id|queued>:<request_id>`,
   so there is no timer state file to lose. Each named row has a committed
   communication fact, but only the primary digest has a physical transmission;
   the other rows have no individual delivery proof. A known failed send is
   eligible on the next tick. A submitted digest holds rows for the repeat
   window; an uncertain attempt or missing readable receipt holds them for
   inspection rather than implying delivery. Replaying the same request UUID
   never sends again.
6. **The same list by hand.** `claudlobby brief --bot <manager>` renders, under
   its own `dispatched` heading, the rows the manager assigned that are still
   open, with those facts, and prints the same four verbs once under that
   heading — the fold's F2: this used to be described here as the bot's own
   `open`/`overdue` rows (the ASSIGNEE's axis), which is a different question
   and read empty for a manager holding no work of its own. A bot's own
   open/overdue rows, if it also carries work as a worker, are the separate
   `open`/`overdue` headings in the same brief.

The re-check is deliberately a COMMUNICATION and never a task: an id'd
re-check would open a row nobody closes, which is the defect the loop exists
to remove.

## The cutover (F18) — history

The plane replaced the JSONL ledgers (`state/dispatch-log.jsonl`, the per-fleet
`runtime/report-back.jsonl`, the per-bot event files, `keepalive.log`,
`workstreams.json`) one reader at a time between 2026-09-02 and 2026-09-05.
The transition machinery — the shadow comparison and its gate, the
`cutover_declared` / `legacy_write_retired` epochs, the `PLANE_READ_*` and
`PLANE_LEGACY_WRITE_*` flags and their composed stamps, the parity and import
doors — was deleted with the closure (#1467: R1 removed every writer, R2a the
matcher and the shadow, R2b the remaining readers, R3 the machinery). What
remains is the end state this document describes: **every door records on the
plane and nowhere else; every reader reads the plane and nothing else; an
unreachable plane refuses, never an empty answer.** The recorded
`cutover_declared`, `legacy_write_retired` and `shadow_parity_*` rows on a
host that lived through the transition stay registered so they still classify;
nothing emits or reads them. The per-chunk design record is the CHANGELOG and
the walk in `documentation/plans/2026-09-02-plane-cutover-f18-design-walk.md`.

## Operations

**Switches — what is on, what is opt-in, how to turn a door off.** Since the
defaults flip (chunk N) **every plane door ships ON**, under the estate rule: a
job or door is on by default unless it deletes data, spends money, mutates
operator source, or sends outbound to people at scale. Nothing a plane door
does is any of those — it records, it reads, and its one DELETE is family-scoped
metric-sample retention.

<!-- BEGIN GENERATED: switches -->
<!-- Generated from claudlobby/switches.py — do not hand-edit. Regenerate: claudlobby host doctor --switches --markdown -->

| Switch | Ships | Scope | Carrier | Flip it with |
|---|---|---|---|---|
| `manager-checkin` | **off** — model spend — one manager turn per idle beat — and it injects into a live session | fleet job | fleet.yaml | defaults.jobs.manager-checkin.enroll: true in fleet.yaml, then config plan, config diff PLAN_ID, and claudlobby --root <data-root> host activate PLAN_ID --install-directory <native-user-unit-dir> |
| `plane-prune-system-events` | **off** — deletes data — and unlike the sample lane beside it, this one could delete a RECORD rather than a sample, which is why it is an allowlist: a wrongly-pruned type breaks selfstart-snapshot.sh's boot gate, which fails closed on an unreachable receipt read but reads an ABSENT receipt as a certain no-receipt | door | host/root .env | PLANE_PRUNE_SYSTEM_EVENTS_ENABLED=1 in the host or root .env |
| `plane-daemon` | **on** | host service | system.yaml enroll | host.jobs.plane-daemon.enroll: false in this host's override, ~/.config/claudlobby/system.yaml, then config plan + config diff + host activate (removes the installed unit) |
| `plane-expire` | **on** | host job | host/root .env | PLANE_EXPIRE_ENABLED=0 in the host or root .env |
| `plane-host-probe` | **on** | host job | system.yaml enroll | host.jobs.plane-host-probe.enroll: false in this host's override, ~/.config/claudlobby/system.yaml, then config plan + config diff + host activate (removes the installed unit) |
| `plane-prune` | **on** | host job | host/root .env | PLANE_PRUNE_ENABLED=0 in the host or root .env |
| `plane-recording` | **on** | door | fleet .env | PLANE_EMIT_DISABLED=1 in the fleet-tier .env — the ruled harness exemption; silences EVERY door at once |
| `plane-view` | **on** | host service | system.yaml enroll | host.jobs.plane-view.enroll: false in this host's override, ~/.config/claudlobby/system.yaml, then config plan + config diff + host activate (removes the installed unit) |
| `registry-scan` | **on** | composition | fleet .env | PLANE_EMIT_ENABLED=0 in the fleet-tier .env |
| `task-recheck` | **on** | fleet job | fleet .env | TASK_RECHECK_ENABLED=0 in the fleet-tier .env |

<!-- END GENERATED: switches -->

Every plane door itself still ships on; the one opt-in row above is a
spending job that merely reports through the plane, not a plane door
declining to record. `claudlobby plane doctor` prints this table
with each row's live state and the tier that set it — for the fleet it was
given; without a `--fleet` the fleet-scoped rows read `unknown` and say so
rather than reporting a scope nobody read. `claudlobby host doctor --switches`
prints the whole estate's. `plane-view` needs the `[plane-ui]` extra: where it
does not import, no unit is composed at all and the table's arm line is the
`pip install` — a supervised unit that cannot start is a crash loop, not an
honest failure.

**Carriers.** Each row's `Carrier` column names the one that reaches THAT
door, because they do not reach the same places. A flag in the fleet (or
host/root) `.env` tier is read by `generate`, stamped onto timer units as
`Environment=` lines, and — for the estate silencer — bridged into `bot.conf`
by the composer, which is what makes silencing a fleet silence its own timers
rather than only its sessions. A tier assignment on its own never reaches a
session: `start-bot.sh` sources the tiers before `set -a`. `fleet.yaml`'s
`env:` (per bot) reaches `bot.conf` and therefore the session — never
`generate`, never a timer — which is the only carrier a door running inside a
bot's Claude session can be reached by. Since these are now **opt-outs** the
composer stamps the tier's **resolved value**, `0` included: a host timer
sources no `.env`, so an off switch that never reached the unit would not be
an off switch at all. Only an exact `0` disarms (an empty assignment wins at
its tier, #1213, but is not a `0`), and a disarmed door no-ops **loudly** —
a silent skip is indistinguishable from a broken timer. `env_tiers.resolves_to`
is the one definition of what a flag value means; the registry of switches is
`claudlobby/switches.py`, and every surface above derives from it.

**Migrations** — `claudlobby/plane/migrations/NNNN_*.sql`, `user_version`-gated
(`migrations.py`); `migration apply` owns initialization and schema changes.
The daemon, normal writers and Plane diagnostics require a prepared schema;
they do not migrate as a side effect. The early migration history is:
0001 kernel · 0002 task-status index · 0003/0004
the fleet room · 0005 FTS · 0006 the registry lane · 0007 `assignments(source_ref)`
(the legacy join) · 0008 `events(actor_uid, occurred_at)` (progress grace, the
resolver's guard) · 0009 `events(fleet_uid, occurred_at) WHERE kind='system'` (Phase B: the fleet-events readers and the escalation window) · 0010 the task vocabulary widened for `escalated` and `nudged` (chunk M-A). A newer db refuses older code (rc 4), never downgrades — and a refusing *daemon* exits so its supervisor relaunches it on the current install (#1485, the write-spine section above). **0010 is the estate's first table REBUILD** — SQLite cannot ALTER a CHECK, so widening the task-event list means the documented 12-step copy of `events`, paid by explicit migration before activating the upgraded writers (it needs the table's size again in free space while it runs — and on a WAL database that means the WAL's copy TOO: an 80 MB plane whose `events` is 67 MB needs ~67 MB of WAL on top of the new table's ~67 MB, so a host at 90% full passes the naive check and fails the real one). It also holds the write lock for SECONDS rather than the milliseconds every earlier migration took, which is long enough for a second migrator's `BEGIN IMMEDIATE` to exceed `busy_timeout` and raise on a benign race — `migrate()` therefore re-reads `user_version` after WAITING for the write lock, so the loser no-ops on the winner's result. The O(1) alternative, a `PRAGMA writable_schema` edit of `sqlite_master`, corrupts the schema outright when the SQL is wrong, which is a worse failure than a slow start on the one database the estate keeps its history in.

**Retention** — `plane prune` ages `metric_samples` past 30 days by
`ingested_at` (the incident-join window). The separately armed system-event
lane deletes only its two allowlisted event types in the same transaction;
neither lane touches the ingest ledger. There is no VACUUM against a live
daemon. `plane expire` is the attention queue's aging
sweep (7-day horizon), idempotent by construction.

**The rule every reader follows** — unreachable is not empty. A missing or
unopenable db, a fleet the plane has never seen, or a plane that holds no bot
of the fleet is REFUSED with a reason on stderr and a nonzero rc; an existing
source with zero rows is an answer. The refusal never rides stdout, because
`report-back.sh` and `fleet-pulse.sh` parse it.

### Explicit owner task nudges (internal adapter)

Task nudges require a distinct local grant; ordinary-message approval and owner
read access never imply it. The local doors are:

```bash
claudlobby --root DATA_ROOT host owner allow-nudges --target-fleet example --actor human:local-owner
# If the human is absent, separately approve first-contact registration:
claudlobby --root DATA_ROOT host owner allow-nudges --target-fleet example --actor human:local-owner --register-actor
claudlobby --root DATA_ROOT host owner status --json
claudlobby --root DATA_ROOT host owner revoke-nudges --fleet-uid fleet_11111111111111111111111111111111
```

Registration requires `REGISTER`; nudge approval separately requires
`ALLOW-NUDGES`. It displays the owner, host, fleet and human actor bindings and
rechecks them under runtime admission after approval. Revocation requires
`REVOKE-NUDGES` and compares the retained generation in the authority write
transaction. Status lists `message_grants` and `nudge_grants`; status and
revocation need no active config or Plane database. Each namespace is independent.
Nudge grants start in a separate optional table with durable random generations;
no existing message grant is converted. Repeated approval of an unchanged grant
retains its generation; revoke/reallow changes it even for the same actor.

`plane/owner_nudges.py:OwnerNudges` is an internal adapter, with no HTTP/UI door.
Trusted callers supply `VerifiedReader`, the exact `OwnerNudgeGrant`, selected
fleet/task/manager IDs, current release ID and `expected_assignment_id` (explicit
null means queued). The adapter binds an existing human identity without an OS
account fallback, admits the active package/release and bound source, and
reauthorizes the exact grant before dispatch and before returning either success
or an outcome-bearing command failure. Revocation prevents subsequent admission;
an already admitted action may finish. It grants no provenance override, arbitrary
bot recipient, reply, restart, permission decision or uncertain retry.

`commands/task_write.py:nudge_bound_task` is shared by this adapter and the CLI.
`task_operations.nudge` checks the optional expected assignment under its task
lock and binds it into request semantics. Omission preserves the CLI's existing
current-assignment behavior; it differs from explicit null. Canonical source
admission covers each task-read snapshot and the fresh post-commit proof. A
completed replay returns its original task/assignment/message coordinates with
today's task state, even if the assignment subsequently changed.

The canonical task event and manager `task_request` commit before the strict,
durable native reservation. Failure before reservation cannot send; recording
may already be committed. Replaying a committed nudge never fills a crash gap
with native input, Enter repair or recording alerts. Unknown transport results
retain their UUID; callers inspect instead of automatically retrying or choosing
a replacement UUID. `inspect` calls the existing request and receipt read owners,
bracketed by source and exact-grant admission, without invoking a mutation. It
preserves the original assignment and reports receiver integrity separately from
recording. A committed task event or submitted transport alone is not delivery.
Prepared-export tests use a synthetic native receiver; real receiver, installed
CLI, trusted ingress and production activation remain separate validation gates.

### Task-linked human feedback

`task feedback` records a human comment for the configured fleet manager (the
lead), including on resolved completed, failed or cancelled work. It records one
`communication` with class `chat`, canonical `work_item_id` and the selected
current assignment or null. It creates no task event, reply parent, approval,
escalation resolution, task completion or nudge. The existing communication
schema owns its links; no new lifecycle vocabulary or database migration applies.

```bash
claudlobby --fleet example --json task feedback wi_11111111111111111111111111111111 \
  --actor human:reviewer --expected-assignment none \
  --text "Please consider this in the next iteration." --request-id 11111111-1111-4111-8111-111111111111
claudlobby --root DATA_ROOT host owner allow-feedback --target-fleet example --actor human:reviewer
claudlobby --root DATA_ROOT host owner revoke-feedback --fleet-uid fleet_11111111111111111111111111111111
```

The local CLI requires an existing explicit human actor and a required
`--expected-assignment ASG_ID` or `none`. It never registers an actor implicitly
or takes its identity from an ambient account, generated bot or fleet timer. `none` is an
explicit selection, not an omitted precondition. Terminal tasks select null even
when they have historical assignments. The canonical operation checks the current
selection under the task lock; unresolved work and stale selections refuse.
Authored text is bounded by `MessageBody` (16,384 UTF-8 bytes); normal capture
policy still controls readable retention. Receipts retain digests, not plaintext.

The local `task feedback` CLI uses an explicitly named, already registered human
actor. It does not require an owner session or feedback grant. Generated bot,
fleet, service and release carriers are refused, including empty markers; manual
root and fleet selection remains supported. The owner adapter requires its own
verified session and exact feedback grant for both submission and inspection.

Owner read pairing, message grants and nudge grants confer no feedback authority.
`allow-feedback` separately confirms `ALLOW-FEEDBACK`; optional
`--register-actor` first requires its own `REGISTER` confirmation. Its independent
`OwnerFeedbackGrant` binds owner revision, host, fleet, existing human actor and
durable generation. `REVOKE-FEEDBACK` removes only that capability. Revoke/reallow
changes its generation even for the same actor. Status lists `feedback_grants`
alongside existing grants without requiring an active selection. Reads of older
stores do not create the optional feedback table or migrate message grants.

`plane/owner_feedback.py:OwnerFeedback` is the bound adapter used by the protected
owner browser feedback capability. `submit` requires a trusted `VerifiedReader`, the exact typed
feedback grant, task/fleet/manager identities, explicit assignment-or-null and
selected release. It binds the existing human, admits the selected runtime and
source, and reauthorizes before dispatch and before disclosing success or raw
canonical failure. `inspect` brackets pure original-request/receiver reads with
the same authority, including on errors. Revocation prevents later admission;
it cannot recall an already executing effect. Feedback grant changes do not
change message/nudge grants or their pending receipt authority.

`task_operations.feedback` commits the one linked fact before
`commands/task_write.feedback_bound_task` invokes strict durable reservation and
native transport. No lock/prepare/recording/reservation failure authorizes a
native effect. A commit followed by failed outcome persistence remains committed
but has no notification from that invocation. Post-native persistence loss stays
unknown. Native submission alone is not delivery: only the independent receiver
byte-integrity proof can establish `received`/`delivered`.

Feedback allows up to 120 seconds for its one native call: JSON quoting can
expand a valid 16 KiB comment to roughly 96 KiB before its envelope. This bound
accommodates ordinary lock and chunk pacing; a slow or hung send still returns
unknown. Other operations retain their existing deadline. Timing out never
authorizes another attempt.

Observed text already in the lead's input box refuses new input: feedback stays
recorded with a failed notification, and replay sends nothing. Text stranded by
an unconfirmed send or timeout also blocks later sends until an operator inspects
the box; inspect and clear a cut or uncertain envelope instead of pressing Enter
to submit it blindly. A new UUID records another comment and does not repair the
original request.

Feedback has no retry flag, operation-level held-input recovery, Enter repair or
recording alert. A retained UUID cannot repeat recording or fill a missing notification.
Committed replay preserves original task/assignment/message coordinates even if
work later changes; retained uncommitted/uncertain requests are inspection-only.
Use `request show UUID` and `message receipt MSG_ID --wait 0`, or the adapter's
pure `inspect`, to recover evidence. Its output distinguishes comment recording
from lead notification and labels the task state as a current read. These rules
do not change existing ordinary-message, nudge or assignment CLI behavior.

### Grant-bound owner browser messages

The trusted direct-host owner browser transport exposes three same-origin POST
doors under `/api/owner/actions/`: `context` accepts exactly `{room}`; `send`
accepts exactly ActionState metadata (`scope`, `kind`, `target`, `request_id`,
`submitted_at`) plus `body`; `receipt` accepts that metadata without a body.
These doors retain the exact Host/Origin, browser intent, trusted principal and
secure session-cookie boundaries. JSON is size/time bounded; duplicate and
unknown fields are refused. They never register identities or author grants.

Context version 1 names the selected fleet alias in `room` and `scope.fleet`,
returns declared bot UIDs as recipients, `simulation: false`, and only the
`message` action. The server recomputes frozen host/fleet/actor bindings and the
current local message grant for every request. Opaque workspace/viewer hashes
are comparison fences, not secret capabilities; viewer binds principal, owner
revision, frozen fleet UID, human actor and durable message-grant generation,
and survives session-cookie rotation. Revoke/reallow creates a new viewer scope
even for the same actor; old contexts, sends, receipts and held responses are refused.
Submitted scope is compared to this recomputed context. Only ordinary messages
with `task_id: null`, canonical UUIDs and nonempty bodies up to 2,000 characters
are admitted. `submitted_at` is browser display metadata, not server acceptance.

The canonical OwnerMessages adapter receives the exact recomputed expected
grant and uses durable preparation/reservation. Source-host/session/grant
admission precedes dispatch and private response release. Native sends run in
an owned threadpool task that survives cancellation of the HTTP waiter. At most
eight action workers may be active or queued; overflow is immediately unavailable,
with no automatic queue retry. Cancelled HTTP waiters retain their slot until the
owned worker ends; all completed or failed workers release capacity. No
request automatically retries or changes UUID. Receipt inspection has no native
effect. `delivered` requires the exact sender/recipient receiver-integrity proof;
`recorded` requires a verified committed communication fact. Send classification
also requires the retained semantic digest to match canonical MessageBody UTF-8
bytes and the ordinary `chat` kind; old UUID proof cannot confirm different text.
Every other result,
including missing retained UUID history and exceptions after a possible effect,
is `unknown`, never safe rejection or permission to resend. Responses contain
metadata only and use `Cache-Control: no-store, private`. Direct-host HTTPS,
trusted ingress and native receiver validation remain separate rollout gates.


A send refusal may include `effect: "not_started"` only when that HTTP submission
was stopped before any message adapter invocation. This is not a `rejected`
receipt for the UUID: an earlier use of the UUID may already have had an effect,
and missing retained history never proves otherwise. The controller can resolve
only the fresh pending row created by that exact in-flight send, keeping its
draft. Receipt checks and reused local IDs cannot use this marker to clear old
uncertainty. Adapter-entry failures and held-response admission refusals remain
unknown, even when their HTTP status is 403 or 503. No path automatically resends.

A send or receipt 403 invalidates only the composer with the same complete
originating scope; context refusals use the controller room epoch. An action 403 probes the read session
once without pausing a valid board/SSE session or recreating a grant. Session
pauses keep the selected team and saved draft and make no action-context request;
a subsequent authorized resume refreshes the context. Unexpected action-context
or response-admission failures return generic unavailable responses without
exception text. Cancellation remains distinct and owned workers retain capacity
until they finish.

Owner source admission also requires the operator-created covering indexes. A
table-rebuild migration may drop them even when the ownership marker survives;
protected reads and owner-server startup refuse immediately when a required
index is absent, without scanning payload tables or repairing schema on reads.
After completing a supported migration, verify the selected installation and
re-run `host owner bind-source` locally to explicitly attest the existing source
and restore missing indexes. The command validates retained host invariants
before committing. Foreign markers or mixed-host records cannot be rebound;
select the correct installation or investigate their provenance instead. Corrupt
sources and incompatible index definitions require local database/schema
investigation, not pairing again. Browser refusals remain generic. A foreground
owner server whose lifespan startup fails returns an unavailable exit rather
than reporting a clean stop; its owned socket is still cleaned up.

### Independently granted owner browser task nudges

The owner transport also supports a separate version-2 nudge capability through
`POST /api/owner/actions/context` with exactly `{room, kind: "nudge"}`. It reads
only the explicit local nudge grant, advertises one configured manager recipient
with `lead: true`, `actions: ["nudge"]`, and the validated current `release_id`.
Its viewer hash has a fixed `task.nudge` namespace and binds the principal,
owner revision, fleet, registered human actor and nudge-grant generation.
Ordinary-message version-1 requests, responses and viewer-hash inputs are
unchanged. Revoking either action grant does not change the other capability.
A read session alone grants neither action; no HTTP path registers an actor or
authors a grant. Feedback has its own capability below; replies and permission
decisions remain unsupported.

Nudge metadata has exactly `version: 2`, `kind: "nudge"`, canonical
`request_id`, `scope`, `submitted_at`, `semantic_sha256`, and `target` containing
`recipient`, `task_id`, **present** `assignment_id` (canonical ID or explicit
null), and `release_id`. Null selects queued work; an absent or empty assignment
is refused. The recipient is always the selected manager UID. The timestamp
requires an offset and remains browser display metadata, not acceptance time.
The reason is nonempty UTF-8 text of at most 2,000 characters.

The additive `POST /api/owner/actions/prepare` accepts that metadata without
`semantic_sha256`, plus `body`. It validates the exact grant-bound scope and
selected release/manager, and reads the resolved open task and assignment
through the canonical task reducer. Source admission and task reading share one
read-only SQLite transaction. It refuses changed selection rather than
substituting current identities. It returns only exact metadata plus the
canonical digest from `task_operations.nudge_semantic_digest`; no body or status
is returned, no request is reserved and no fact or native notification is
written. Private response release rechecks the capability and selected task.

`send` accepts prepared metadata plus the unchanged reason. It recomputes the
canonical semantic digest before adapter entry, then invokes `OwnerNudges.nudge`
once with the exact assignment, release, manager and expected typed grant.
The canonical task lock still checks races after preparation. All failures
from adapter entry onward may follow a committed or native effect: only the
original UUID is inspected, and an exception is never promoted to definite
rejection. A proven pre-adapter refusal can mark only this submission
`effect: "not_started"`; it does not reject any earlier use of its UUID.

`receipt` accepts metadata without body and calls only pure nudge inspection.
It compares the original request's operation, host/fleet/actor, task, manager,
assignment including null, retained route release and semantic digest with the
saved metadata. Current task assignment/state and current capability release
do not replace those retained values. Current package/config, source, session,
grant generation and manager admission still apply. Mismatches and absent or
unavailable proof return `unknown`, without sending or authorizing a retry.
`recorded` requires the exact committed pair of task and communication facts;
`delivered` additionally requires received byte-integrity proof for the exact
human sender and manager destination. Delivery means the nudge was received,
not that the manager acted or the task completed.

Context, prepare, send and receipt share the existing ceiling of eight active
plus queued action workers, bounded strict JSON/body reads, same-origin browser
intent and trusted principal/session boundaries. Workers survive HTTP waiter
cancellation and keep their slot until they actually finish. Metadata responses
are private and no-store. Final response admission uses the independent nudge
namespace; send/receipt retain original assignment/release rather than gating
on moving task state. No automatic retry, recipient override, Enter repair on
replay, HTTP grant authoring or new task system is introduced. Prepared-export
HTTP tests use synthetic identity/activation and receiver fixtures; actual
native receiver, browser, trusted ingress and rollout require their separate
canary evidence.

The browser keeps message and nudge capabilities independently. A nudge refusal
allows one fenced read-only capability recheck; it never repeats preparation or
sending. If ordinary messaging remains granted, its composer and selected
recipient remain available. An unsent reason belongs to the logical task and
manager within the authorized scope, so selecting a changed assignment or
release preserves that draft. Submitted pending metadata retains its original
assignment, release, digest and UUID; a new selection cannot bypass its pending
receipt guard. Session loss still pauses all action kinds.

### Independently granted owner browser task feedback

The same context, prepare, send and receipt endpoints accept the fixed
version-2 kind `feedback`. Context requires its separate `OwnerFeedbackGrant`,
uses the `task.feedback` viewer namespace and advertises only the configured
lead. Read, message and nudge access confer no feedback authority. Revoking or
replacing a feedback grant leaves the other capabilities unchanged.

Feedback uses the same exact metadata shape and bounds as nudges. Preparation
reads the full canonical task and explicit current assignment, validates the
authored text through `MessageBody`, and returns the canonical
`feedback_semantic_digest`. It reserves nothing and writes no fact. Resolved
terminal tasks accept comments with an explicit null current assignment;
historical assignments never become current selections. Nudges remain open-only.

After preparation the browser rechecks its capability, selection and unchanged
draft, saves only immutable metadata, then sends once. The server rechecks the
digest and selection before `OwnerFeedback.submit`. Feedback records one linked
human chat; it changes no task or assignment state and provides no approval.

Pure receipt lookup compares the original task, assignment, release, manager,
scope and digest. `recorded` requires that exact committed communication fact;
`delivered` also requires the independent receiver's matching bytes and identities.
Uncertain requests retain their original UUID. Receipt checks, reload and
capability refresh never resend, repair Enter or fill notification gaps.

The shared core task detail enables Give feedback only with that capability and
a valid canonical selection. Its composer states that the comment goes to the
lead without approving or changing the task. Message, nudge and feedback drafts
and pending requests remain separate. A refused capability gets one fenced
read-only recheck; it cannot erase another draft or restore an old task selection.
Old-grant pending metadata stays visible with receipt checking disabled under a
replacement grant. Authored comment text remains in tab memory only.

`owner_task_action_protocol.py` shares only the strict version-2 metadata codec
between the two fixed task kinds. Their authority, task policy and receipt
classification remain in their respective adapters. Both reuse the existing
owner request gates and eight-worker limit. Synthetic tests and isolated browser
or receiver observations do not establish live host activation or trusted HTTPS.

### Selected task detail

`GET /api/tasks/{TASK_ID}?fleet=RECORDED_ALIAS` reads one canonical `wi_` ID
through the existing selected-ID task reducer. It does not depend on the
200-card board cap or infer identity from a legacy display name. The recorded
fleet UID comes from that task's identity link in the same SQLite snapshot.
Source admission, reduction and envelope provenance share the connection and
read transaction; the existing protected view also admits the response before
releasing private bytes. No read initializes, repairs or mutates the source.

Main-channel conversations with validated task ownership expose `task_link`
(`task_id`, recorded `fleet`, `fleet_uid`, `host_uid`) and a **View task** button.
This opens the same exact protected detail dialog even outside the board cap.
Missing or contradictory ownership shows no opener. Direct-host reads emit
unqualified opaque IDs. An injected transport may supply `qualifier::ID` only
when its exact detail route serves that qualified task ID. Each host/fleet UID
may be unqualified or carry that same task qualifier; it cannot introduce a
different qualifier, and an unqualified task cannot use qualified owner UIDs.
The browser-local demo already serves qualified task IDs (for example,
`workshop::wi_…`) with a raw fleet UID. This transport form does not imply its
current channel already emits `task_link`. The shared renderer preserves the
exact task ID and compares the returned fleet UID without inferring ownership.
A cross-team conversation
uses the task's recorded owner rather than its sender or the current room;
opening it never switches rooms or acquires another team's action context.
The dialog's conversation has no recursive opener. Closing restores focus to
the same task/owner/thread button after a keyed refresh, or the channel region
if that button has gone. Once the browser observes session/source loss, it fences unfinished detail replies;
settled detail refusals retain their specific remediation. Action access
invalidation does not pause readable task navigation.

The response includes the full stored task title/body, canonical lifecycle,
current assignment, assignments, task/assignment history and unresolved issues.
Task, assignment and event fields use explicit public allowlists; recorder Fact
metadata and future reducer fields do not become implicit browser fields. Alias
lookups include displayed assignment histories and terminal events in batches
of 400, with the existing short-label helper. Current attention questions use
the full selected canonical snapshot and shared attention queries in the same
read transaction, so later nudges and display caps cannot hide an unanswered raise.
Presentation keeps the latest 500 task events, 100 assignments and 100 events per
assignment, plus up to 100 issues/display IDs. Each bounded collection discloses
its total, shown count and truncation. Lifecycle is reduced from the complete
selected canonical history before presentation limits; a window is never used
to certify task state. Bodies are not silently shortened: a detail payload above
2 MiB is unavailable with the canonical `task show` remedy. These are response
limits, not a bound on reducing a task with unusually large retained history.

The existing dialog reads this endpoint through `jget`, fences delayed replies
against selection/close/refresh, and refreshes only explicitly while reading.
Closing or pausing access erases the body/history DOM as well as fencing replies.
Known string summary/reason/question fields render as escaped prose; full exact
raw event details (including malformed/non-object JSON) and record identifiers
remain in collapsed disclosures. Action copy names only actually granted actions.
Linked conversation/results remain honestly limited to the existing recent
channel window, and completion alone does not imply review. Older non-owner or
synthetic transports can retain a labelled limited board snapshot when full
detail is unavailable. A task without a recorded team may show its unresolved
board snapshot only on an unprotected read-only transport, without requesting
a guessed team. Protected refusals and mismatched IDs never fall back to stale
private detail. No task actions or grants are enabled by this read slice.


### Authenticated direct-owner read profile

The core-owned direct-host browser and server must match. An authenticated
`GET /api/owner/status` and successful explicit `login`/`renew` return
`read_profile: {version: 1, profile: "direct-owner-read-v1", host_uid: "host_<32 lowercase hex>"}`.
Only these three fields are admitted. The profile promises the existing
same-origin Plane read envelopes and SSE with current direct-owner sessions;
it grants no message, nudge or feedback action. It makes no website workspace,
setup, cross-origin, multi-host or browser-permission claim. Before authentication,
status remains lifecycle-only (`needs_pairing` or `sign_in_required`).

Login/renew first admit the paired principal, existing session when required,
and canonical source before creating or rotating a session. A pre-existing source
outage preserves the old cookie and creates no hidden replacement session.
The server admits the current session and canonical local source before creating
this metadata and again before response publication, including any new cookie.
An unavailable source yields 503; denied source/session yields 403 without the
profile or new cookie. This contract does not change admission of existing task
snapshots. A read-only owner needs no action grant to obtain the read profile.

The canonical owner transport uses one profile validator for status, renewal and
action-refusal status recovery. It pins the first admitted host UID for its entire
lifetime. Missing, malformed, extra-field or unsupported profiles stop reads,
streams and actions with an update-and-reload remedy; a different host requires
reopening Plane on the intended host. There is no automatic login or mutation
replay. A lifecycle-only older server is deliberately incompatible with this
new client, rather than silently treated as the same read contract. The default
read-only transport and injected synthetic transports are unchanged.

### Two admitted source reads

The core `createOwnerReadHandle` factory uses the same authenticated owner status
admission and pinned read profile as the direct browser transport. Its lifecycle
is independent of DOM session controls and global redirects. `start`, `snapshot`,
`subscribe`, `jget`, `createEventSource` and `dispose` expose reads only; a profile
object supplied by a caller cannot stand in for an admitted handle. Direct-host
controls retain their existing login, renewal and sign-out behavior.

`createTwoSourceReadTransport({sources})` accepts exactly two core-issued
handles with immutable distinct source keys and labels. It does not provision
sessions or choose a cross-origin carrier. An injected `/api-client.js` can expose
the resulting read/stream/disposal methods to the one canonical renderer. The
shipped default and direct-owner module selection remain unchanged. The injected
wrapper must import the same cache-versioned owner module URL as the adapter: read
handles belong to that canonical module instance, not a second admission registry.

The finite first slice combines fleets, recent activity, tasks, participants,
overview and summary. Exact qualified team selection routes activity/tasks and
linked task detail to that source, including a task outside the board window.
Task links require the recorded fleet UID and admitted host UID; they never infer
ownership from an emitter or another host. Structural identities are qualified
by source, while literal bodies and event detail are preserved. Each source keeps
its own snapshot provenance, stream cursor and recent-window metadata. An initially unavailable source does not hide the other host; a source may
admit later only through its controller, with distinct pinned host identity. Source
loss fences unfinished replies and omits that source's private rows; previously
admitted fleet labels remain visibly unavailable. Partial totals cover readable
sources, with separate host facts rather than an invented aggregate recorder.

Inventory, equipment, search, grid, presence and trust are explicitly unavailable
in this bounded adapter. No action capability/context/prepare/send/receipt is
exported, and reconnection never replays a mutation. Synthetic injected proof does
not establish OAuth, website bootstrap, private-device reachability or a real
cross-origin connection. A future publisher must explicitly lock any new shared
module instead of forking its renderer or deciding production host authority.

Fleet and overview replies share one source-local routing roster. Later owner
admission and registry changes are learned through the renderer's existing overview
refresh. A newer roster request fences an older reply; changed alias/UID mappings
fence unfinished focused reads. `fleet_choices` retains last-admitted teams with
the current read state, separately from readable overview cards and totals. A
missing endpoint or malformed projection degrades that read only; authenticated
owner lifecycle and stream source facts control source epochs. The summary keeps
each recorder's daemon state and ingest freshness, including down, unknown and
quiet states.

Qualification changes structural references, including assignment history and the
last task event, but leaves canonical wire bodies and event detail literal. Nudge
prose requires a consistent source qualifier on the projected pair; the unchanged
wire body must match its corresponding bare task and assignment identities.
