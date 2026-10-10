# Host-authorized owner messages

Status: IMPLEMENTED; local verification complete. Started: 2026-10-08.
Real bot canary and runtime/browser activation remain pending.

Continue the shared Plane work loop while website OAuth infrastructure is
pending. This increment connects an explicitly verified owner to canonical
ordinary-message operations inside the host package. It does not expose an
HTTP action endpoint or activate a fleet.

## Decisions

- Keep the existing owner session read-only by default. Local approval adds
  one fleet's ordinary-message grant, bound to the current owner revision and
  an existing canonical human actor. No email/OS-account equivalence.
- Store the optional grant in the existing private authority database. Its
  table is created only on explicit local approval; reads never migrate state.
- Require current host/fleet/actor identities, active-release admission, and an
  exact declared recipient UID. Refuse generated bot/timer environment carriers.
- Extract the existing message command's effect/receipt workflow unchanged;
  share its recording, request UUID, held-box repair and byte-integrity proof.
- Support ordinary messages only. No browser retry control, cross-fleet target,
  reply, task feedback/nudge, command subprocess, or implicit actor registration.
- Reauthorize at dispatch start and before returning receipt reads. Revocation
  prevents subsequent admission; it cannot recall an effect already in progress.

## Codebase comparison

| Need | Existing owner | Change |
| --- | --- | --- |
| Owner verification/session | `owner_access`, injected `VerifiedReader` | Explicit local message grant; verifier still external |
| Attribution | `bind_task_context(operator_alias=...)` | Bind a host-approved canonical human UID and alias |
| Effects and proof | `commands/message_write.py` | Extract shared callable; preserve CLI behavior |
| Recovery | `request_queries.read_request`, `message_queries.receipt` | Reauthorize and bind request parties before returning evidence |

## Implementation Plan

### Steps

1. COMPLETE: optional per-owner-revision, per-fleet grants and refusal tests.
2. COMPLETE: shared delivery workflow and internal owner adapter.
3. COMPLETE: active-root, identity, recording, request and receipt owners tested
   in private prepared exports with a synthetic native receiver.
4. COMPLETE: before/after comparison and independent source review. Publication
   is a draft PR stacked on the owner-session foundation, with canary pending.

## Verification

Separate history-free exports, each indexed, installed and resource-prepared,
used Python 3.12 and private HOME/TMPDIR. No test ran from a live fleet root.

| Arm | Affected tests | Failures | Exit |
| --- | ---: | ---: | ---: |
| Owner-session base `c908dd61` | 169 passed | none | 0 |
| Candidate including new owner adapter cases | 215 passed | none | 0 |

Both arms emitted the existing Starlette/httpx deprecation warning. Scope:
owner policy/read gate, ordinary message CLI, held-box repair, message routes
and operations, request queries, runtime admission; candidate adds the owner
adapter suite. Full CI and real bot delivery are separate evidence gates.

The synthetic receiver recorded exact wire-byte evidence through canonical
ingest. Tests observed one native attempt across same-UUID replay and read-back,
conflicting-body refusal, correct human attribution, no send on invalid grants,
activation lock refusal, and unavailable/unknown receiver proof after either
a prepared-only crash or a submitted message with failed communication recording.
Review caught the latter recovery gap; its regression cases pass. The shared
workflow extraction also preserves existing CLI reply/report tests.

## Acceptance and remaining work

Prove correct human attribution, one effect for a replayed UUID, conflicting
UUID refusal, response-loss recovery by read only, submission versus receiver
proof, and no effect without a current exact grant. Preserve existing CLI and
read-gate behavior. No production bot receives this test's messages.

Trusted Tailscale ingress, browser carrier/origin protection, local pairing and
grant UI, HTTP status projection, shared-UI adapter wiring, and a real isolated
bot canary remain later acceptance gates. This is internal executable plumbing,
not a shipped browser messaging feature or a live authentication claim.
