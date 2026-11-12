# Task permission decisions

Claudlobby owns permission requests and their decisions. A message, escalation,
feedback comment or passing review does not grant permission. The website must
use this owner through an independently authorized host action adapter.

`task permission request TASK_ID --scope-file FILE --reason TEXT --request-id UUID`
records a request from the generated task worker or fleet manager. The scope
file has exactly three fields:

```json
{
  "action": "merge",
  "target": "https://github.com/example/app/pull/12",
  "constraints": "Only head aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; squash after required review and CI pass."
}
```

Actions are `merge`, `deploy`, `spend` and `delete`. The target and constraints
describe the exact proposed effect: include the revision, environment, budget
ceiling or deletion selection that the human needs to assess. Never include
credentials. These fields and the reason are retained in the host's Plane.
The scope digest covers all three fields; it is not a bearer credential.

`task permission show PERMISSION_ID` reads the original scope, requester, task,
assignment and latest decision. A local operator uses
`task permission approve|decline|revoke PERMISSION_ID --scope-sha256 DIGEST
--reason TEXT --request-id UUID`. The CLI previews the complete record and
requires confirmation through the operator terminal. Generated bot and timer
contexts cannot use this door: it refuses their selectors and, on an activated
host, refuses before the terminal opens when the process runs inside a managed
unit. Composed bot permissions also deny the three decision forms. The actor
comes from the resolved local account, never a supplied `--by` claim. The
terminal is an accident boundary, not a sandbox against other processes with
the operator's OS privileges.

The request is bound to one host, fleet, bot and task assignment. Approval is
valid only while that task remains open, the assignment is unchanged, and the
requester remains its declared manager or assignee. A changed assignment needs
a new request. Decline and revocation remain available for stale work. An
already decided request cannot be approved again; revocation does not restore
it to pending. A repeated request UUID returns the retained outcome, and an
altered payload with that UUID conflicts. A lost response must be inspected or
retried with the original UUID. No command automatically resends a notification.

`task permission check PERMISSION_ID --scope-file FILE` checks the current bot
and exact scope. It executes nothing. An approved scope remains a standing
authorization for those exact terms until revoked or made stale by task state;
this is not a single-use execution ticket. Callers still need the operation's
own idempotency, review, credentials and result reconciliation. Checking a
permission never proves that a merge, deployment, purchase or deletion happened.

The internal `require_permission` function is the common policy read for action
adapters. An adapter must construct the scope from its actual operation inputs,
check it at admission, and enforce the stated constraints. A user-supplied
scope digest alone is not sufficient. Revocation stops subsequent admission;
it cannot undo an effect already admitted. Arbitrary shell commands are not
intercepted by this module. Specific effect adapters must enforce the scope
before claiming the complete execution flow.

Requests and decisions use existing immutable Plane system events, request
receipts and task locks. There is no new website permission database. Incomplete,
truncated or inconsistent history refuses authorization rather than being
interpreted as permission. Every permission fact must match the exact projection
in a receipt from the canonical command. Generic `plane emit` data cannot grant
permission by claiming a human subject or a reserved-looking emitter name.
Conflicting or unprovable records make that request unusable; they never reopen
an older approval after a missing revocation.

Ordinary restarts preserve both the Plane and its request receipts. The existing
database-only replacement-host backup preserves event history but excludes these
receipt files along with other local authority. Restored event rows alone cannot
authorize work: make a fresh request and obtain a new operator decision. Restoring
the database also does not restore host login credentials or browser grants.

## Host-authorized browser decisions

The existing direct-owner and connected-workspace action endpoints accept the
fixed `permission` kind. A local operator must first approve the independent
`host owner allow-permissions --target-fleet F --actor human:ALIAS` capability
at a terminal (with separately confirmed `--register-actor` if needed). Read,
message, nudge, feedback and workspace grants never imply this capability.
`host owner revoke-permissions --fleet-uid FLEET_UID` removes it without touching
other capabilities. This stops new decisions; already recorded approvals must
be individually revoked through `task permission revoke` or an authorized
browser decision. Normal task closure or reassignment also invalidates their use.

The context endpoint accepts `kind`, `room`, and canonical `task_id`, returning
up to 50 newest proven requests and an explicit `has_more` flag (also set when
the bounded 1,000-event scan is exhausted). Unproven request rows are omitted; a
genuine request with ambiguous history is shown as unavailable, never approved. Preparation
binds the permission ID, task, exact scope digest, active release, decision,
reason and request UUID to the current owner/grant/workspace generation. Sending
calls the same canonical decision owner as the CLI. Receipt inspection never
submits, repairs or retries: it proves the original recorded decision and reports
the permission's *current* state, including a later revocation. An uncertain
response keeps the original UUID; it does not authorize another decision.

After the first recorded decision, the CLI and HTTP adapter attempt a notice to
the original requesting bot through canonical messaging. The decision UUID
determines a separate durable message request. The independent permission grant
authorizes this fixed notice; it does not grant ordinary messaging. Delivery is
reported separately as `received`, `unknown`, or `failed`. Only matching receiver
byte evidence confirms receipt. An unreachable bot does not undo the decision.
Replay and receipt inspection never resend or fill a crash gap between recording
and notification. The notice names the record; it is not an execution ticket.
These endpoints execute no approved effects. All records remain on the host.

The shared task detail panel displays the requested action, target and limits.
An admitted owner can approve or decline a pending request, or revoke an approved
request, with an explicit reason and confirmation. The same controls serve direct
host access and connected workspaces; connected identifiers stay qualified by host.

Before sending, the browser retains the original request UUID and exact intent
metadata in session storage. The reason stays in memory. An uncertain response
leaves a **Check receipt** action; inspection never resubmits the decision. A
recorded approval can show a later revocation. The panel reports the current
permission state without claiming that the requested action was executed.
If the decision is recorded but bot notification is still unknown, its saved
UUID remains available after reload for receipt inspection. A confirmed approval
can still be revoked while that earlier notification is unconfirmed. Nothing
automatically resends either notice.
