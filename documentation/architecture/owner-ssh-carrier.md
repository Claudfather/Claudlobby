# Owner operations over a Mac SSH connection

A managed Mac has no admitted HTTPS owner door. The browser reaches it over the
SSH connection the person already authenticated, and that connection carries the
same owner operations the HTTPS door serves. This page is the source contract.
It has been exercised with synthetic fixtures only: real Remote Login behavior,
the source address a real `sshd` reports and the native principal it resolves to
have not been observed on a Mac, and nothing here claims they have.

The Linux and HTTPS paths are unchanged. The private Unix socket verifier, the
grant and workspace schemas and the Tailscale principal namespace are the same
ones every other transport uses.

## Entry

The immutable prelude `claudlobby/preinstall/owner-guide-v1.sh` has four closed
modes: `setup`, `status`, `api` and `computer` (the computer run, below). `api` reads the same one-line installation
admission as the guide, refuses when the session has no `SSH_CONNECTION`, and
then executes the admitted stable launcher as `host owner api --stdio`. The
prelude passes the server's `SSH_CONNECTION` through without reading it for
identity. The browser sends a fixed command with no environment requests.

One `api` process carries exactly one operation and exits after its drain. It
holds the stable launcher's runtime reader for that time only. The one exception
is a provider sign-in conversation (below): one process, one bounded attempt.

## Who is asking

`claudlobby/plane/owner_ssh.py` captures `SSH_CONNECTION` once, before the fixed
profile replaces the environment. The four fields must be canonical, both
addresses must be tailnet addresses, and the destination must be one of the
addresses the native daemon reports for itself. A missing, malformed or
mismatched tuple refuses; `SSH_CLIENT` is never used instead.

The captured client address is resolved through the same bounded policy as the
HTTPS door (official realm, then WhoIs, then official realm again), on every
operation. No frame, header, account name, UID or existing grant selects the
principal. The Unix socket verifier is not reused with a fabricated scope.

This rests on the existing trusted-account boundary. A process running as the
same account, or as root, can rewrite its own environment; that adversary is out
of scope here exactly as it is for the private socket.

## Frames

Frames are canonical sorted ASCII JSON, one per line, with `version`, `type`,
`seq` and `session_id`. Unknown or duplicate fields refuse.

| Direction | Frame | Fields beyond the common four |
|---|---|---|
| server | `hello` | `profile: "owner-api-v1"` |
| client | `start` | `profile`, `expected_uid`, `source_revision`, `binding_sha256`, `operation`, `input`, `token`, `last_event_id` |
| server | `headers` | `status`, `headers` (only `content-type`, `cache-control`, `retry-after`) |
| server | `chunk` | `chunk_id`, `data` (base64, at most 16384 decoded bytes) |
| client | `ack` | `chunk_id` |
| client | `cancel` | none |
| client | `call` | `call_id`, `operation`, `input`, `token` (sign-in conversation only) |
| server | `reply` | `call_id`, `status`, `headers`, `data` (sign-in conversation only) |
| server | `end` | `status`, `bytes_sent`, `cancelled` |
| server | `error` | `code`, `retry: false` |

The server sends one chunk and waits for its `ack`. `end` is sent only after the
policy handler has returned or been cancelled and drained, the owned components
and the application lifespan have shut down successfully and the control reader
has joined. A failed or timed-out shutdown is an `error`, never an `end`. Every
owned component shutdown is attempted and the control reader is always stopped and
joined; a cleanup failure never replaces an earlier carrier or operation failure
(an incomplete response included), and surfaces only when there is none. A
handler that returns is not by itself a response: a normal `end` needs the final
body (`more_body` false) acknowledged, or an explicit caller `cancel`. A caller
`cancel` stays `cancelled: true` even after the final body was acknowledged
(completion and cancellation are independent; this never makes a dispatched
mutation safe to replay). A response a canonical finite read abandoned, or a
started response with no final body that nobody cancelled, refuses with
`response_incomplete` and sends no `end`. A normal `end`, cancelled or not,
exits 0. `end` alone releases nothing: the runtime lease is released only by the
process exit that follows. A refusal sends `error` and exits 4. A cancel that
arrives before any response headers ends with `status: 0, bytes_sent: 0,
cancelled: true` and exits 0; the operation never reaches a policy handler if
the cancel arrived first.

A channel that closes without a complete `end` and a process exit is unknown to
the caller. Nothing is replayed: a mutation keeps its original request ID and is
recovered by inspecting that original.

The operation names and input fields are a closed set compiled into internal
routes (`operation_request` in `claudlobby/commands/owner_api_stdio.py`). There
is no caller-selected method, path, header, command or environment. Cookies do
not cross this transport.

## Provider sign-in: one conversation per attempt

The provider sign-in attempt lives inside one process: its worker thread, its
terminal child, the sign-in link and the code hand-off are held in memory, and
the retained record admits one current attempt (another attempt ID only after an
ending it reports as retryable; see **Try again** below). A one-operation process
would start that attempt and then cancel it in its own mandatory drain. So on
profile `owner-api-v1`, `setup-signin-begin`, `setup-signin-code` and
`setup-signin-cancel` still refuse with `operation_requires_persistent_owner`
before any policy code runs, and `setup-signin-status` stays a read.

Profile `owner-signin-v1` is the owner of one attempt. The prelude, the fixed
command, the `hello` bytes and the `start` fields are the same; only the
`profile` value differs. Such a start must carry `setup-signin-begin` with a null
`last_event_id`, and anything else is `invalid_request`. A core that predates
this profile refuses it the same way, before any policy code. The process then
keeps one policy app (and so one setup door, engine and attempt owner) until it
exits. This has been checked with synthetic fixtures only; a real `setup-token`
run over SSH has not been observed.

**Calls.** Begin is call 1. Each later `call` names `setup-signin-status`,
`setup-signin-code` or `setup-signin-cancel`, with `call_id` 2, 3, … up to 512
and the generation and attempt ID of the start. One call is outstanding at a
time, and a call is outstanding until the final byte of its `reply` has been
written and committed. Input reads and the final output write with its commit run
as short nonblocking steps under one I/O owner: each syscall sets the descriptor's
nonblocking flag for that step and restores it, and would-block or a short write
returns to a readiness wait outside the lock (never held while waiting). A
frame is classified once, by the commits that owner had counted when its bytes were
read. A frame already read while a reply is blocked or half written stays early,
however soon the write finishes; no timing threshold decides it. Order is defined by
complete frames observed and writes committed, not by packet arrival. A cancel or
fault stays readable during output backpressure and halts the reply write in its own
thread, so the drain starts while the peer is still not reading; the writer is joined
before return. A reply left half written leaves the channel torn: a truncated reply never produces a
successful `end`, and nothing further is written. Cancellation during delivery is
incomplete, and earlier carrier errors remain preserved: a disconnect or
`invalid_request` already seen keeps its precedence, and a write failure or timeout
can be `output_unavailable`. A `call` that arrives before
the previous `reply` is complete, repeats or skips an ID,
names another operation or identity, or follows the closing reply is refused as
`invalid_request` when it is read; it is never held for later and never reaches
policy code. A frame that follows a completely written reply is an ordinary next
call and is admitted. There are no `headers`, `chunk` or `ack` frames on this profile.

Every call is compiled by the same `operation_request` and sent through the same
policy route as any other operation, so the principal, bearer token, workspace
binding, grant, generation and expiry are checked again each time. The carrier
keeps nothing from an earlier call as authority, and adds no session renewal.
The canonical browser client therefore holds its own renewal while a conversation
is admitted or live (explicit, automatic and setup-only alike), renews once
before begin unless at least 645 seconds remain, and does not begin if that
renewal is not confirmed. That margin covers the 630-second conversation plus
admission. Begin waits for any renewal already in
flight (a redundant request never replaces it), and the guided panel keeps its
approved context across that confirmed pre-begin renewal; real expiry, sign-out,
reference change or disposal still clear it. It never extends a session locally;
disconnect, expiry or disposal ends the hold, an unknown answer does not.
The attempt's worker keeps the reader it was started with: a renewed, revoked or
expired session stops the attempt, and its final state may then be unreadable.

**Replies.** `reply` carries the call's status, the same three allowed headers
and `data`: the base64 of the whole response body, at most 16384 decoded bytes
(a larger body is `output_limit`). The carrier never writes a body of its own.
A reply is a receipt for one call. It says nothing about the process: a caller
must keep its channel slot, job and lease, record a live reply as
`call_complete` with the remote state unknown, and treat the conversation as
finished only after `end`, end of output and the process exit.

**When the conversation closes.** The carrier reads this from the reply it just
sent, never from whether the worker is alive:

| Reply | What follows |
|---|---|
| 200 naming the original attempt as `starting` or `waiting` | the next call is awaited |
| 200 naming it as `connected`, `needs_attention`, `cancelled` or `unknown` | drain, then `end` |
| any reply to `setup-signin-cancel` | drain, then `end` |
| not 200, to `setup-signin-code` | the next call is awaited (the one retry) |
| not 200, to begin or `setup-signin-status` | drain, then `end` carrying that status |
| 200 that does not name the original attempt | `invalid_response`, no reply, no `end` |

A refused code and a refused authorization are the same `403` with
`{"state": "denied"}`; the carrier does not tell them apart. Sign-in succeeded
only when a 200 reply says `connected`. A begin that is refused, or that finds an
attempt retained by an earlier process (read as `unknown`, reason `interrupted`,
or as its recorded final state), closes at once and starts no worker. The carrier
never creates, resets or replaces an attempt, and a new setup approval does not
either: a retained attempt that is unresolved, or that ended after its code reached
the CLI, needs owner recovery.

**Try again.** The one exception is decided by the retained record, never by the
carrier. Status replies carry `"retryable": true` only when the attempt ended
`needs_attention` with reason `timeout`, or `cancelled` with reason
`effect_uncertain` (an explicit cancel, or the drain of a lost or idle
conversation), its record says no code reached the CLI
(the host commits that before it types a code; a row from before that column
existed never qualifies), fewer than ten attempts exist for the approval, and no
worker of it is alive in the answering process. A worker writes its terminal row
as its last act, after reaping its child (a reap it cannot prove reads `unknown`),
so that row is the proof that an earlier process's attempt has stopped. A later
conversation whose begin names a new attempt ID then retires the old attempt and
starts the new one under the same grant, generation and admission, with every
check a first attempt gets, `credential_already_present` included. A retired ID is
never admitted again. The conversation that lost or cancelled an attempt never
admits its replacement: it drains and ends, and only a later conversation's begin
can. Every other ending, and any ending after a code was delivered, stays as above.

**Drain and `end`.** Closing always runs the same drain: no more calls, the
handler in flight is cancelled and awaited, the owned components shut down
(which cancels the attempt and joins its worker and child), the lifespan shuts
down, and the reader joins. `end` has its usual shape, with the status of the
last reply and the total reply bytes, and is sent only after all of that
succeeded. It is never a per-call receipt. A caller `cancel` frame closes the
same way with `cancelled: true`. End of input, a signal, a malformed frame, an
expired bound or a failed drain sends `error`, exits 4 and sends no `end`.

| Bound | Value |
|---|---|
| Start frame | 15 s |
| One call, observed by the carrier | 20 s (the policy route keeps its own 15 s) |
| Idle between a reply and the next call | 45 s |
| Whole conversation, from the admitted start | 630 s; no frame renews it |
| Provider attempt | 600 s |

These bound what the carrier admits and waits for. They are not proof that a
process stopped: an expired bound starts the drain, and the drain still joins
the worker without a timeout of its own. The process never exits, and so never
releases its runtime reader, while that worker is alive.

**Progress in the canonical UI.** While the original attempt is `starting` or
`waiting`, the setup panel sends one status call about every two seconds (at most
315 in 630 seconds), never overlapping a code, cancel or manual check. While the
page is hidden (the Claude sign-in tab in front) it keeps the same read-only call
every 25 seconds instead, inside the 45-second idle bound, so authorizing at the
provider does not drain the conversation and cancel the attempt. Polling
stops on a terminal state, an error, expiry, a context change or disposal, and
never begins again, replays, mints a new attempt ID or clears the retained
attempt. Only an explicit **Try again**, shown while the latest answer carries
`retryable`, mints a new attempt ID and begins it. A `waiting` reply that carries
the link also carries `expires_at`: in whole host-clock seconds, the last moment the
host accepts a code, 20 seconds before the earlier of the attempt's timeout and the
setup approval's expiry. A later code is refused (`signin_code_unavailable`), so a
code is never cut off mid-exchange by the timeout. An attempt with no code taken by
that moment ends there as `needs_attention`/`timeout`, so Try again is offered at
once rather than after a link nobody can use. A begin is refused
(`signin_approval_ending`) before anything is staged when the approval has less than
50 seconds left (the 20-second margin plus a 30-second code window), and
`retryable` is reported only under the same check, so the panel never offers a Try
again that begin would refuse; it says instead that the approval expired or ends
too soon. The panel shows `expires_at` in local time as **Paste the code by …**; it
is text only and starts no timer. A Try again keeps the ID it replaces beside the
new one in storage until a host answer names the new one; the record is written
before anything changes in memory. A refused or never-sent Try again (also one this
browser could not save) restores the ended attempt at once, in memory and in storage, even if the
connection changed meanwhile. After a lost answer, a context that still names the
replaced attempt shows only that the new ID is not admitted yet: its begin can
still commit after the browser stopped waiting. The panel then follows the ended
attempt and keeps the new ID beside it as its accepted alternate, and follows the
new one once an answer names it (admitting it retires the ended one for good). Only
an answer naming the new ID, or another Try again, drops the alternate; any third ID
is refused. While a Try again is unsettled, a refused status check reads the
context once instead of reporting the refusal. The link and code are cleared
when it is lost. Preview and Launch stay
unavailable in the panel while that sign-in is open; the canonical policy and the
drain remain the authority.

**Afterwards.** Once the process has exited, `setup-signin-status` on
`owner-api-v1` with the original generation and attempt ID reads the retained
record. An attempt still owned by a live conversation reads there as `unknown`,
which is an observation, not proof that the child stopped. Preview stays blocked
by the website while a conversation is live, and while a provider-requiring context
has no attempt confirmed connected (none, unknown, cancelled or needs_attention).

## The guide on an SSH-controlled Mac

The typed guide (`host owner setup --stdio`) captures the same server tuple when
its fixed profile is the managed Mac runtime. It then runs without a listener:

- its `prepare_directories` proposal also creates one private settings file,
  the host override, containing only `plane-owner: enroll: false`. The file is
  created only when absent. An existing file is never read for repair,
  replaced or rewritten, and an existing policy that still enrolls the owner
  service stops the guide;
- pairing, workspace approval and setup approval use the same local
  confirmations as every other transport, while the browser's side of each
  arrives as a sibling `api` operation;
- its `pairing_ready`, `workspace_ready`, `preview_launch_ready` and
  `launch_handoff` frames carry `transport: "mac_openssh"`.

No owner service is enrolled to keep the computer reachable. Reopening after the
handoff means a fresh authenticated SSH connection.

## The computer run

`computer` (operation `owner-guide-computer-v1`) executes the admitted launcher
as `host owner setup --stdio-computer`. It asks the cards that add a computer,
in this order and each only if an earlier run has not: `prepare_directories`,
`initialize_owner`, `pair_owner`, `initialize_link_storage` (one card for both
storages a workspace link needs) and `bind_workspace`. Then it drains its own
listener and asks `start_workspace`, which dispatches the start with no team.
It never asks a Claude card or a team card. Its state frames are the existing
`pairing_ready` and `workspace_ready`, then one `type: "result"` frame:

| Result | Fields beyond the common four | Meaning |
|---|---|---|
| `computer_starting` | `state`, `request_id`, `config_fingerprint`, `target` | The start is dispatched or under way; the run exits so the updater can proceed |
| `computer_ready` | `state`, `origin`, and `transport` over SSH | The computer is started; on Linux its lasting service answered `/owner` |

After a declined card the next run asks the cards that remain, from the
declined one on. After the hand-off it is the way back in and asks no card: on
a started computer it answers `computer_ready`, and while the start is under
way `computer_starting` with the same identifiers, read without a card or a
dispatch. It never waits for the updater, never continues a record and never
answers `computer_ready` in the process that dispatched a start. Its refusals
are `type: "error"` frames, as today's guide sends them (`code`, `state:
"stopped"`, `retry: false`): `use_full_guide` when today's guide retains a
first-team approval or Launch, or the computer is in any other state;
`start_failed` when the start stopped or a started computer's lasting service
did not answer within 15 seconds, with `line` (display text only; it can name a
path under the account's home) and the record's `request_id`;
`revision_differs`, from the prelude itself, when the installed revision is
not the requested one. A computer part-way through today's cards, with no
approval or Launch retained, is taken over by this run. A stopped start is continued by its request
id over the account channel, never by a run: while its record is in maintenance
no run can begin.

Its admission line and its `start` frame carry one more key than today's guide,
`update_record`: `null`, or the `request_id` and `target` of the update record
the client read. A computer's receipt keeps the revision it was prepared at for
good and is never rewritten. The computer run is admitted at that revision as
today's guide is, or at a later installed revision only when `update_record`
names the record on disk, that record is `done` as strictly as today's finish
reads one (an empty health table, out of maintenance, configuration committed,
no stage error), it ends at the installed revision, and the receipt's revision
is an ancestor of it. The
prelude only skips the receipt's revision for this mode; the record and
ancestry checks are the profile's and the run's. Everything else is checked as
today: the receipt's shape, the binding, the system Python and the installed
source exactly at the requested revision. `setup`, `status` and `api` keep
their request keys and their own admission; one more key refuses them.

## Updating a started computer

The preparation script `host-install-v1.sh` has three more actions. Each reads
one canonical line, then EOF: `schema_version` 1, `expected_uid` and the
action's own keys. Another key, a missing key or a line that is not canonical
is `invalid_request` (exit 2). Each answer is one line of at most 8192 bytes:
`schema_version`, `operation`, then the answer's fields, or `state` and `code`.

| Action | Operation | Request keys | Answer |
|---|---|---|---|
| `update-status` | `host-update-status-v1` | `compare_revision` (optional), `ingress_node_key` (optional) | `state` (`absent` or `recorded`), `recorded_revision`, `installed_revision`, `receipt_is_ancestor`, `binding_sha256`, `setup` (`untouched`, `in_use` or `started`), `interpreter` (`same` or `changed`), `update`, `relation` (`same`, `ahead` or `other`) when asked, and `ingress` (`exact`, `differs` or `unknown`) when asked |
| `update` | `host-update-v1` | `expected_installed_revision`, `target_revision` | `state` `updating`, `request_id`, `target` |
| `update-continue` | `host-update-continue-v1` | `request_id` | `state` `updating`, `request_id`, `target` |

`ingress` tells a client whether it can leave out the root ingress card
(`linux-system-v1.sh`, kind `ingress`) because that card would change nothing.
`ingress_node_key` names the node the client expects (`nodekey:` and 64
lowercase hex digits). The script then reads, last and writing nothing, the
system `tailscaled`'s local API (`status`, then `serve-config`) over its
root-owned socket from a peer running as root; the executable behind that peer
is not checked, because an ordinary account cannot read it. The answer is
`exact` when the daemon is `Running` with `TUN`, at the `Version` the root card
admits, its own `PublicKey` is the named key, its DNS name is a `ts.net` name
listed in `CertDomains`, and Serve already holds exactly what the card would set
for this account: the card's own predicate (`ingress_after`), copied byte for
byte and held to the original by a test. Routes on other ports do not matter, as
they do not to the card. `exact` means only that the card could not change
Serve, not that the card would succeed or that a root ingress receipt exists,
because the card also checks what this read does not or cannot: the daemon's
executable, the current profile, the configuration's ETag, operations pending in
the root store, and the account's shell and the shape of its home. For the
team account `claudlobby` the card also needs that account to exist and still
conform: its own ids and group only, `/home/claudlobby` private to it, a locked
password, no expiry and no subordinate ids. It is
`differs` when a fact read is otherwise or the card would refuse what Serve
holds (Funnel on the route, another mount or another target on port 443, a TCP
forward on 443, another account's socket). It is `unknown` when anything could
not be read within the read's own time (at most 4 seconds, ending 5 seconds
before the action's budget), and always on a Mac. An absent computer is read
the same way. The key never makes the action refuse (the action's own SIGTERM,
SIGINT and budget alarm still end it, as anywhere else), and without it the
answer is unchanged.

`update` in a status answer is `null` or the update record in the names a
client reads: `request_id`, `kind` (`update`, `start`, `first_team` or
`addition`), `phase`, `target`, `maintenance`, `dispatched`, `failed` and
`health_errors` (unit names only), then where the update is:

| Field | Values | Read from the record |
|---|---|---|
| `stage` | an updater stage (`authorize`, `retire-render`, `render`, `prepare`, `conversion-check`, `quiesce`, `quiet`, `publish`, `restore`), `other`, or `null` | `stage_error.stage`; `null` when there is no stage error |
| `units` | `{"done": n, "total": n}` or `null` | `consumers`: during `quiesce` the units whose `stop` is done, during `restore` those whose `restore` is done; a unit with a `restore_error` is not done. `null` in any other phase or with no units |
| `runner` | `none`, `pending`, `entered` or `finished` | `job_dispatch.state` |
| `pending_seconds`, `entered_seconds` | whole seconds, or `null` | the computer's clock less `job_clock.pending_at` / `job_clock.entered_at` |
| `outcome` | `finished`, `unfinished`, `refused` or `null` | `job_outcome.result`, only when its `attempt` is the current dispatch's |
| `waiting` | boolean | `runtime_wait` is present: the updater is waiting now for the runtime lock |
| `runtime_busy` | boolean | the latest `runtime_waits` row ended `refused` |
| `unhealthy_units` | count | the number of `health_errors` |

Nothing here looks at the native job: a slow look never reads as stopped.
`failed` is true for a stage error, for `done` with health errors, for a
dispatch that `finished` before `done`, and for an `outcome` of `unfinished` or
`refused` before `done`. A `runner` that `entered` with no outcome is running,
however old it is; a hard stop of the job leaves it so. No field carries record
text, unit names beyond `health_errors`, or a credential, and a key in a shape
the script does not know reads as `null` or false and refuses nothing.

The updater's job writes those keys. Each dispatch saves `job_clock` with
`pending_at` and drops any earlier `job_outcome`; the job adds `entered_at` and
drops any earlier `job_outcome` when it enters the update, so a run again on
the same dispatch is judged by its own end. A job run that has shown it is the
job then saves, in one write with its dispatch `finished` when it entered,
`job_outcome`: `attempt`, `at` (epoch seconds) and `result` (`finished` when it
returned, `unfinished` when it entered and did not return, an interruption
included, `refused` when it was refused before entering). A process refused
before it has shown it is the job, and a record with no dispatch, get no
outcome; so `outcome` is `null` whenever there is no dispatch. The notice that
the update did not finish is sent even when that save fails.

The receipt is checked against its own
revision, never against one a client sent; the installed revision is whatever
the updater moved the checkout to. `setup` is `started` once the computer's
configuration is committed. A record is unfinished by core's rule: not `done`,
or `done` with health errors.

No action waits for the updater, rewrites the receipt or asks a card. Each runs
the installed launcher as `host update`, which goes round the maintenance
check, so all three answer while an update is in maintenance.

`update` runs `host update apply <target>`: forward only, from the receipt's
remote, on a started computer. The same target in flight is asked for again
and attaches or continues. Under its own lock the updater refuses, with exit 7
and before any dispatch, to apply a Preview or a prepared first team or
addition for that same target that no Launch dispatched; the script answers
that as `update_in_progress`. A Launch never takes this route. Its refusals are `installed_changed` (the installed
revision is not the one shown), `update_in_progress` (an unfinished record for
another target, or a team preparation that no Launch dispatched),
`already_current`, `needs_reset` (prepared but not started: an untouched
computer or one partway through its cards), `not_forward` (the target is not a
descendant of the installed revision) and `not_prepared`.

`update-continue` runs `host update continue --expected-request <request_id>`.
The updater holds the same id under its own lock and exits 7, before any
dispatch, for a Preview, a prepared first team or a prepared addition that no
Launch dispatched; only its Launch dispatches it. A start (dispatched or not),
an ordinary update and any dispatched record are continued, a `done` record
with health errors included. Its refusals are `no_such_request`,
`nothing_to_continue`, `not_continuable` and `not_prepared`. Without the flag
`host update continue` is unchanged.

Those are the only codes the three actions answer, beside `invalid_request`
(exit 2). Anything else is `unavailable` (exit 6): the shell's own refusals (no
system Python, a Mac runtime that is not admitted), every check of the
installation itself (another installation, a changed identity or source, a
preparation in progress, an unusable prerequisite, an unsupported platform), a
record the updater cannot read, an updater that refuses or does not know the
flag, and a budget that runs out. A client reads `update-status` again after
any of them.

A team preparation is told by two keys of the update record: an
`initialization_scope` whose `manifests` list is not empty (a first team) and
`addition_scope` (an addition). `addition_scope` is part of this contract: the
phase that writes additions must use that key, or an undispatched addition
would read as an ordinary update and could be continued or applied.
"Dispatched" means the record has `job_dispatch`.

What `update` installs is decided by its caller. The updater installs any
commit the caller names that is a fast-forward of the checkout and that the
receipt's remote serves; nothing checks that the commit is on a particular
branch. The client's pinned revision is the trust root, as it is for
`prepare`.

`update-continue` serves an unfinished record whatever happened to its approval
after dispatch: withdrawing an approval does not stop it, by design. The request
id it is held to proves only that the record has not been replaced since the
client read it; it is not proof that anyone approved it.

The three actions build a fresh environment: the guide script's four pinned
selectors (`CLAUDLOBBY_ROOT`, `CLAUDLOBBY_INSTALLATION`,
`CLAUDLOBBY_HOST_SYSTEM_YAML`, `CLAUDE_CONFIG_DIR`), so a render reads this
computer's own settings and keeps its lasting owner service; on Linux
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`, so the operator check and the
cutover reach the account's user manager; and never `PLANE_EMIT_DISABLED`. On
a Mac it is the managed runtime's environment without `PLANE_EMIT_DISABLED`,
plus the selectors. `prepare` and `status` keep their own. A system Python
replaced since preparation is reported as `interpreter: changed`; on Linux the
trusted system Python is admitted by trust rather than bytes, so every action
goes on and the receipt keeps its digest.

## Starting again on a computer that never started

`host-install-v1.sh` has two more actions for a computer that was prepared (and
may have had setup cards approved) but whose team was never started. They take
the same request line as `prepare` and `status`: `schema_version` 1,
`source_revision` (the pin the client wants) and `expected_uid`. They delete
files on the person's computer, so they refuse by default. Each answer is one
canonical line of at most 8192 bytes.

| Action | Operation | Answer |
|---|---|---|
| `start-again-status` | `host-start-again-status-v1` | `state` `eligible` (`next` `start_again`), `absent` (`next` `prepare`), or a refusal |
| `start-again` | `host-start-again-v1` | `state` `removed` (`next` `prepare`), `absent`, or a refusal |

A refusal is `state` `refused` with a `code` (exit 4), or `state` `busy` with
`preparation_in_progress` (exit 4). A malformed request is `invalid_request`
(exit 2). Anything not listed here is `state` `unavailable`, `code` `unavailable`
(exit 6), and the client reads `start-again-status` again. Codes: `account_changed`
(the account, or on a Mac the admitted runtime, is not this installer's),
`already_current` (the receipt is already at the requested revision: use
`prepare`), `identity_changed` (the receipt, `installation.json` or the checkout's
commit is not what this installer wrote), `started` (a sign that a team ran, below),
`in_use` (a lock is held: a launcher, an update, the owner connection, Preview or a
Claude sign-in), `unknown_content` (a name, kind, owner, mode or device the installer
does not write, a link where a folder belongs, or a changed device) and
`first_team_not_empty` (`~/claudfather-projects/first-team` holds something, which
the next guide would refuse). Nothing is removed on any refusal.

The checks run in this order, under the locks. The account check; then a look for an
unfinished `start-again/` folder, first, because after `install/` has moved nothing
else shows the computer was prepared (a family folder with neither is `absent`; one
holding other content but no `install/` is `unknown_content`). Then, in order,
`install/preparation.lock` (the preparation), `data/state/runtime-use.lock`,
`update.lock` (made if missing), `owner/owner.sock.lock`, `setup-preview.lock` and
`provider-signin.lock`. These locks are opened without the preparation lock's empty-file
rule, because some hold bytes. Then: the receipt (every binding field except the
revision, the Linux interpreter digest exempt, `installation.json` exact, the checkout's
`HEAD` equal to the receipt's revision; a preparation that stopped before it finished
may lack or part-have its checkout and environment); the never-started list; the units
folder; the first-team folder; and the exact content list. The never-started list is
`data/runtime`, `data/local`, and in `data/state` `config.json`, `selected-release.json`,
`update.json`, `update-work`, `releases`, `activations`, `migrations`, `config-plans`,
`restored-backup*`, `setup-finish-*` and `plane/plane.db*`; the updater's job definition
in `install/bin`; any systemd unit file (a name beginning `claudlobby`, or ending in a unit
suffix) on Linux; a LaunchAgent named `com.claudlobby.*` or `claudlobby-*` on a Mac. The
content list names, for each folder, every file this product writes there, including the
leftovers a crash can leave (`.update-*`, `.installation-*`, `.launcher-*`, `.host-uid.*.tmp`,
`.owner-access-*`, `.owner-policy-*`, `.claude-stage-*`, a sign-in attempt's folder);
`src`, `env`, `staging` and `provider-profile` are owned trees, checked only for kinds,
owner and device. Removal then moves `data`, `staging`, `provider-profile`,
`provider-bin`, `host-policy` and last `install` into `family/start-again`, syncs, and deletes
with a walk that never follows a link and refuses to cross a device. Every lock is held
through the last rename. A second press finishes an interrupted removal, or runs every check
again if `install/` is still in place.

What is kept: the first-team folder, units, the Mac `runtime/` (Mac only), and on Linux the
team account, linger, System tools, the Serve route and root receipts.

`start-again-status` writes nothing and takes no lock exclusively. It holds the preparation
lock shared, and probes each other lock shared for an instant. It therefore sees a holder of
an exclusive lock (the owner connection, a sign-in, Preview, an update) but not a running
launcher, which holds `runtime-use.lock` shared and is seen only by `start-again`, which takes
it exclusively. A launcher or an owner server that starts in that instant is told its lock is
busy, as by any other holder, and starts again. `start-again` itself makes `update.lock` when
it is missing, an empty private file the updater makes on first use, even when a later check
refuses.

On a Mac, the guide holds no lock between its operations: close other setup tabs before
pressing Start again. On a Mac, a pin that changes the managed runtime descriptor makes these
actions unreachable (the shell's `runtime_not_admitted` answers, as for `prepare`): that is
out of scope here. Update operations remain Linux only; the website carrier admits these two
actions on both systems, with the Mac request shape and `/bin/` prefix. They are inert until
the website admits them, and gated on the owner's decision.

## Launch

The updater cannot advance while any process still holds a runtime reader. The
website stops admitting sibling operations, cancels each active one (a live
sign-in conversation included), and waits for its `end` and process exit before
sending `setup-launch`. A wait that runs out is not a completed drain and does
not permit Launch. The launch
operation returns the original receipt and exits. The guide observes that
retained original Launch, returns `launch_handoff` and exits. A lost reply is
unknown; recovery inspects the original tuple and never sends Launch again.

## Saved references

A saved reference may carry a `transport` field.

| `transport` | Meaning |
|---|---|
| absent | A historical reference: HTTPS, as before |
| `"mac_openssh"` | An SSH computer; reopening needs a fresh authenticated connection |
| anything else | Refused |

The `endpoint_origin` stays the logical authority key in both cases. For an SSH
computer it is not a claim that HTTPS is reachable. The Plane UI carries an SSH
reference only through the browser owner transport: without it the workspace
document is refused, and no native fetch to the origin is ever made. A reference
that gains or loses its transport is not the same reference. Passwords, handles
and bearer tokens are never part of a saved reference.
