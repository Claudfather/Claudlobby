# Unified CLI finalization after main integration

This batch follows `cc63d665` in #1989. #1985 remains unchanged review history;
#1980 and #1985 are ancestors of #1989, which is the sole landing vehicle. The
[main-integration record](2026-09-30-unified-cli-main-integration.md) keeps the
per-port owner/test table for all 19 upstream commits (main
`dd789c524281f2b315dcbe8682fdc6216304207e`). This document covers subsequent
review corrections and CI failures. It does not claim completion of the migration,
merge readiness or approval to deploy.

## Review corrections

All links refer to the original `cffa258` review. “Corrected in code” describes
the implementation and focused private-fixture evidence, not native acceptance.

| Finding | Current disposition and proof |
|---|---|
| [S1-07](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137291831) | Corrected in code. Check-in validation returns every defect in structured data and text; existing check-in CLI cases pin the full list. |
| [S1-08](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137291841) | Corrected in code. Independently confirmed empty backlogs pass; failed or missing observations still refuse. Existing selection and CLI cases cover both. |
| [S1-09](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137291853) | Corrected in code. Proven-unrecorded check-ins rebuild and compare the exact frozen fact before same-ID retry. Release selection is checked before effects but excluded from semantic identity. The existing outage/retry/replay case passes; replay through a real release switch remains unmeasured. Pre-change unreleased receipts use the older semantic digest. |
| [S1-11](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137291860), [S1-12](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137291877) | Scoped corrections. Busy requests, selection races, empty selectors and retained-request inspection get actionable results. Existing tests add worker refusal, same-ID outage recovery and content-conflict checks. This is not the review's proposed exhaustive test matrix; persistence-after-commit, unavailable-database listing and `--limit 0` cases were not added. |
| [S5b-02](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137356484) | Brief remedies and the selection-race behavior are corrected. All eleven reader commands raise the shared retryable [`selection_read_conflict`](../../claudlobby/command_result.py) result; no resend is performed. Substring classification of older activation errors and a fully typed generated-selector exception remain follow-ups. |
| [S5b-01](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137356478), [S5b-03](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137356492) | Partial. Context reports YAML position without echoing source lines. Actual source-backed root-mode fleets refuse before their library can override a sealed release ([`paths.py`](../../claudlobby/paths.py) `_refuse_checkout_root`). Host data roots with local/vault fleets remain supported, including pinned old checkouts. Other config diagnostics and host-level library authoring remain outside this correction. |
| [S5a-01](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137346479) | Partial. Staging checks 2,000-batch/32-MiB bounds and reports refusal; serving replay limits each tick to 200 batches/0.5 seconds. Concurrent producers can exceed the check by simultaneous writes. Disarmed-daemon activation policy and pending-schema supervision (S5a-08) remain separate findings. |
| [S5a-02](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137346491) | Corrected in code. Doctor, status and trust expose staged depth, withheld unreadable counts, and pending rather than committed state through the shared [`plane/health.py`](../../claudlobby/plane/health.py). A never-armed daemon on an initialized plane is attention. Existing daemon and trust cases cover these observations. |
| [S5a-04](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137346498) | Corrected in code. The client and recorder share stdlib [capture policy](../../claudlobby/plane/capture_policy.py) and body-proof calculation before durable storage. Missing policy refuses instead of falling back. A broken policy or a typed `IdentityConflict` (identity parent conflict) keeps pending batches for a later tick rather than quarantining them. Shell and daemon tests cover the refusal and replay paths, including an identity-conflict pause with nothing quarantined. |
| [S2-04/S6-08](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137294121), [S2-05/S6-09](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137294141) | Corrected active dispatch/delegate/lifecycle recipes and orchestration grants. Task admission, assignment and delivery remain separate; manager coaching uses public lifecycle owners and honors holds. Bare Bash and native supervisor grants are removed from orchestration. The existing composition test now includes the real orchestration role. No broad command-extraction harness was added. |
| [S2-17](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137294276), [S6-16](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137368324) | Corrected named recipes, literal own-fleet command forms, skill/deadline flags, report vocabulary and restart coaching. Telegram guidance uses the equipped reply tool and discloses failures. |
| [S5b-14](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137356566), [S6-13](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137368295) | Existing composition tests now reject representative operator-only grants. A standalone `uuidgen` grant supplies request IDs; coaching requires retaining each ID and lowercase spelling. Mac availability was checked; Pi availability remains part of native acceptance. |

The ledger retains unverified and partial findings. All 199 review findings are
**not** resolved; unlisted and partial findings remain queued. Settled contracts
are unchanged: no compatibility shims, one implicit fleet manager, canonical task
admission/assignment/delivery, O1 degraded ordinary/unlinked delivery beside
committed linked/task mutations, and no automatic resend.

## Published blockers

A read-only reconciliation of the ten published blockers against this tree found
S1-01, S3b-02, S3b-03, S4b-01, S4b-03, S4b-04, S4a-02 and the S9-03 standing
data/queue blockers corrected in code with stub-flow tests only. S9-01 remains an
operational delivery prerequisite: under the no-shim decision, old installed
source pullers must be held before their tracked branch receives this merge. S9-02
populated Linux first adoption has no coordinator-level test, stub or native, and
walk-back after quiescence (S3b-01/S9-04) is still absent.

That reconciliation also found one demonstrated code gap: the S3b-05
roster/stopped-worker/retired-bot/handoff-section refusals ran only after the
estate was quiesced. Forward `--resume` re-entered the same refusal, and no route
restored the parked units.

**Corrected in code for standing conditions ([S9-03 / S3b-05](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137380723)).**
Running and first-adoption activation now call a read-only
`preflight_canonical_handoffs` under the activation lock. It runs before phase
membership, the external-caller check, `store.prepare` and any pause or handoff
([`activation.py`](../../claudlobby/activation.py) `_running_activation`). The
preflight shares its predicates with the quiesced writer
([`activation_handoffs.py`](../../claudlobby/activation_handoffs.py)):

- manager coverage (now applied to first-adoption rosters too);
- assignees that are stopped or uninstalled;
- retired bots that still own open work;
- handoff sections that cannot be parsed.

It writes nothing and freezes no audit. `persist_canonical_handoffs` keeps the same
order and messages and remains the **authoritative post-quiesce recheck**.

Tests pin the preflight at two levels, stub flow only:

- Owner-level cases (stopped worker, uninstalled manager, retired worker, appended
  section) assert the refusal and no handoff bytes.
- A first-adoption coordinator case (stopped worker, uninstalled manager, appended
  section) asserts no activation record, no selection, no pause or handoff call,
  and an unchanged `session.md`.
- The retired-bot rule is pinned only at owner level.

Limits that remain:

- **Live churn** between preflight and quiescence can still refuse at the
  authoritative recheck. Examples are a new assignment to a stopped worker or a
  changed audit.
- **Handoffs written during this activation's own handoff step** are not standing
  conditions. If a session skill appends after an existing canonical section, the
  malformed-section refusal still fires after quiescence. The skill's
  append/rewrite behavior was not verified.
- **There is no rollback or walk-back route** (S3b-01/S9-04). After such a
  post-quiesce refusal the estate stays paused until an explicit recovery decision.
  Resume never repeats a recorded handoff and never resends automatically.
- **Still open for S9-03 / S3b-05:**
  - old-ingest `drain_daemon` wiring;
  - a public legacy migration preview;
  - extra-session and dead-session checks.
- **No Linux first-adoption success test.** There is no populated coordinator
  case, stub or native, that runs Linux first adoption to success (S9-02).

Not all ten blockers are closed. S9-01 is an operational hold, S9-02 needs
Linux/Pi evidence, walk-back is absent, and several code corrections still await
native acceptance.

## CI corrections and validation

Hosted run [36722345010](https://github.com/Claudfather/Claudlobby/actions/runs/36722345010)
failed on `cc63d665`: all three test lanes found the system example out of sync;
Python 3.10 also rejected a fractional-second boundary in `plane samples`.
`system.yaml.example` now copies the package tier exactly. The shared read-window parser
([`commands/checkins.py`](../../claudlobby/commands/checkins.py), also used by
`plane samples`) normalizes fractional seconds before Python 3.10's narrower ISO
parser. The existing sample-boundary assertion now checks exit status as well as
rows. These are local fixes; a fresh hosted run after the next push is required.

Tests ran only in private Git exports with private HOME/TMPDIR, packaged resource
preparation and `PLANE_EMIT_DISABLED=1`. Individual recording tests explicitly
arm only their scratch roots and sockets. Counts below overlap; do not add them.

- The first combined selection: **584 passed, 3 failed, 7 skipped**. All three
  failures were the checkout guard treating a root that held only native helpers
  (`lib/`, no package) as a source checkout. Narrowing the source marker to the
  package itself and repairing copied-client fixtures passed **94 tests**, with
  the same seven Linux-only cases skipped on macOS.
- The eleven-reader result consolidation passed **96 tests**.
- The final plane daemon/identity/registry selection passed **96 tests in 42.54
  seconds** at candidate tree `068b051d5b3cb8f133bf52f8ebdcd45d9fa85bd3`, after
  identity-parent conflicts were changed to keep and retry pending batches.
- After the parent inspected the full activation diff, the activation,
  handoff, state, unit, supervision-inventory and host-command modules passed
  **116 tests in 16.13 seconds** at tree
  `a1caca9db2d73dc0f73471b4dc6aabee2a9f6c52`. Native supervision was an adapter
  fixture throughout; no native service was tested.
- Python 3.10 passed **41 tests** covering samples, the system example and Stop
  hook composition. The original `.5Z` parsing failure was independently
  reproduced with that interpreter before the fix.
- The staging shell suite passed; the orphan-reaper shell suite passed **22/22**
  checks with stub processes. `node --check` on the Plane UI passed. These are
  not native service or browser-rendering evidence.

All the corrections above, including the activation preflight, are in
commit `12a1bcc6a9afc6a88af17a528fb1523c61aeed96`. Before that commit, current main
was still `dd789c52` and the published #1989 head was still `cc63d665`. Hosted checks must be read from the current PR head; these local results do
not establish a hosted pass.

## Host conversion and outstanding gates

Fresh hosted checks and external review of the final commit are required; no
current-revision approval exists. The
[conversion runbook](../existing-host-release-conversion.md) still requires
old source-puller holds, a same-OS isolated canary and a host-wide adoption
window through `host activate --adopt-existing`. No shim converts a host.

A read of the Mac's current-user LaunchAgents and `launchctl` inventory found no
pull-root match. Other launchd domains and the Pi were not checked, so this is
not proof that the estate is held.

The following remain unverified:

- a populated Linux first-adoption coordinator run;
- real Pi/user-systemd adoption;
- interrupted native activation recovery, with no walk-back route;
- normal-load timing;
- narrow-grant session proof;
- protected production adoption. Lumbergh's restart hold remains in force. No live checkout,
fleet generation, service, deployment, GitHub comment or review request was made.
