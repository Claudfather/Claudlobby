# Shared Plane work loop — first interaction increment

Implemented 2026-10-08. Awaiting review. Builds on the shared renderer in PR #2220.

The operator approved continuing the useful-work experience while website Google
and database setup is pending. This increment puts task inspection, recent
reports, lead messages, task feedback and task nudges in the canonical Plane UI.

## Plan versus codebase

| Intended behavior | Current implementation | This increment |
| --- | --- | --- |
| Select team and inspect its work | Shared team selector and inert task cards | Accessible task details from the same scoped board records |
| Inspect results | Recent report messages in channel | Task-linked recent reports, explicitly bounded; no inferred review approval |
| Message lead / feedback / nudge | Existing CLI owners; no authorized browser write adapter | Shared composer behind explicit transport capabilities; exercise with isolated synthetic adapter |
| Recover uncertain send | Synthetic website-only composer | Canonical receipt UI and bounded pending metadata; lookup same request, never automatic resend |
| Real host actions | Owner session PR #2231 grants reads only | Remain unavailable until P2 attests browser actor and operation authority |

## Implementation plan

1. Keep the existing read API and records. Open task detail from task cards and
   match channel records only by exact task ID. Label recent/absent/redacted
   reports; do not call a task's completed status a reviewed artifact.
2. Add a small transport-neutral interaction controller and shared UI. The
   default same-origin transport remains read-only. A compatible adapter must
   advertise exact workspace/host/fleet/viewer scope, recipient IDs and supported
   verbs. These capabilities control UI availability, never backend authority.
3. Scope in-memory drafts by all target dimensions. Before sending, persist only
   bounded request metadata (no message bodies or credentials). Bind receipts to
   the original scope, verb, recipient, task and request ID. Keep recorded and
   unknown requests pending; clear only confirmed delivery or explicit refusal.
4. Clear visible state immediately on team changes; ignore late context/board/
   action responses for another target. Preserve drafts during same-target
   refresh. Keyboard and mobile entry must remain usable.
5. Test receipt loss, rejection, wrong-target responses, cross-host/same-name
   teams, storage failure, and late updates. Use separate prepared before/after
   exports for existing Python tests. Serve a synthetic local example for real
   browser interaction and visual evidence, without fleet mutation.

## Scope and acceptance

This is the P3 UI increment, not completion of P2/P3 or proof of real bot
delivery. No auth bypass, raw SQL mutations, CLI subprocess bridge, new
framework, or website-owned operational renderer. The direct view remains
GET/HEAD-only. Broader palette work is deferred. The bounded channel cannot
prove complete task history; task detail discloses that limit.

The development sample must explicitly say that messages and receipts are
synthetic. Source defaults must never silently enable that adapter. Real action
integration requires the existing canonical operation owners to accept verified
host actor/grant context, not inherit the service's OS identity.

Linear's installed helper targets the unrelated data team; this work is tracked
with this repo plan and its PR instead of creating an unrelated team ticket.

## Adapter contract for the next integration

The optional `api-client.js` exports are `interactionContext(room)`,
`sendAction(request)` and `actionReceipt(requestMetadata)`. Each must settle or
reject within a bounded transport deadline. The first returns version 1,
`room`, `simulation`, `scope` (workspace, host, fleet, viewer), `recipients`
(id, label, optional lead) and `actions` (message, feedback, nudge).
`scope.fleet` must equal the selected room. An aggregate `all` room cannot send.
A multi-host adapter must qualify room, task and recipient identifiers before
passing both read records and capabilities to Plane; identical host-local names
are not globally unique. The adapter owns that mapping. Task verbs require
exactly one lead; ordinary messages can select any advertised recipient.

Receipts carry version 1, the original request ID, scope, kind, target,
submitted_at and status: recorded, delivered or rejected. A request body is
never persisted by the UI. A matching rejected receipt must mean the operation
was refused, not an ambiguous downstream delivery error. Unknown results stay
pending. The server must independently authorize the full target and retain
idempotent receipts; this UI contract grants no authority. Principal or
workspace changes must tear down the old UI/adapter and create a fresh context.
Real browser sessions and verified host actions remain P2 work.

## Verification and current boundary

Implemented in the shared package, with an opt-in example at
`python3 tests/fixtures/plane_work_loop/serve.py --port 4312`. That launcher binds
only loopback, serves the canonical assets, and substitutes only the transport.
It has no live bot, fleet database, credential or CLI access. Restarting it clears
its synthetic records. `?readonly=1` demonstrates unavailable browser actions.

Prepared, isolated Python 3.12 before/after exports: baseline 94 passed and
candidate 97 passed, both exit 0 in the affected Plane view/grid/shutdown battery
plus the candidate's Node contract wrapper. The Node battery has eight cases,
covering receipt binding, pending reload, draft isolation, refusal, storage
failure, capability/lead checks and recovery. Existing FastAPI/async cleanup
warnings remain. The full repository suite was not run.

Browser checks exercised task reports, keyboard Escape/focus return, lead and
specific-bot messages, feedback, nudges, refused and recorded-only replies,
team-scoped drafts, and disabled read-only actions. At 390px, task details and
feedback remained within the viewport. An instrumented lost-response capture
recorded exactly one action attempt and one receipt lookup for the same request
after reload; no second action attempt. Screenshots/video are PR attachments,
not committed assets. These results prove the synthetic interaction path only;
no real bot received a message and no host was activated.
