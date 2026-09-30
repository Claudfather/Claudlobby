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

## Implementation batch above `2278690a`

This batch is recorded in code commit `ac3ef171cb21d12b5ab15f96ec7a83c4cc926eff`,
above `2278690a6141a2e34cfa8b48c699ce172769253d`. Its tested code tree is
`b9855cbc4c1ed7bd1e08a747567e13a629b034b1`. Each finding was checked against source
before acceptance. A static integration review of the staged diff found three
issues, and all three were addressed before the final run:

- a stale updater test string;
- the check-in read order;
- a stale-socket move refusal.

### Lifecycle and move

| Finding | Disposition |
|---|---|
| [S4b-06](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137332649) purge WIP | Corrected in code. The one shared `move_bot.source_wip` owner refuses before any deletion on a dirty tree, detached or unreadable HEAD, missing upstream, unpushed commits on any local branch, a stash, or any Git error or timeout. It strips inherited `GIT_*` variables. `bot remove --purge` reads WIP only after a quiet precondition, under the lifecycle lock ([`bot_remove.py`](../../claudlobby/commands/bot_remove.py) `_purge_quiet`): preflight must prove the native unit retired, and both the retained private socket and `.tmux-env` must be absent. Otherwise purge refuses, with `native_outcome: unattempted`, before WIP or teardown. `bot move` also refuses unpushed source work. **Limits:** detached processes outside tmux and the retired unit are not covered, matching `assert_quiescent`. Non-Git `projects/` directories and ignored files are not guarded. A socket file left by a clean tmux exit blocks purge until it is removed by hand. |
| [S4a-03](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137319942) stale socket after stop | Corrected for `bot stop`. After the exact unit is disabled or booted out, `svc_bot_disenroll_exact` retires a stale private server only when tmux reports no server for that exact socket. A live server holding another session, or any other tmux failure, still refuses. `assert_quiescent` keeps its own socket probe. |
| [S4b-02](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137332630) move from a stopped source | Corrected in code. Move observes the source through the frozen installed path and native target. `absent` needs no `--force`, and `--force` accepts only `ready`. When the adapter reports `unknown` after a clean stop that left a stale socket, move runs the existing `assert_quiescent` kernel proof. Only if that proof passes is the source treated as absent; nothing is cleaned up. Every other unknown result refuses, even with `--force`. The adapter classification was deliberately not changed. This is existing proof reused, **not** new native evidence, and the connect probe's behavior under a full listen backlog on macOS is unmeasured. |
| [S4b-10](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137332673) fleet restart | Corrected in code. `fleet restart` records a de-enrolled bot as `skipped` (`reason: de_enrolled`) and continues. Every other refusal, effect or unavailable result still stops the sweep, and `start`/`stop` are unchanged. The skip reason appears only in JSON. |
| [S4b-07](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137332659) lock contention | Minimum mapping. A busy host lifecycle lock returns a retryable `conflict` with nothing attempted. There is still one host-wide lock. `bot remove` under contention and the fleet-sweep busy message remain unchanged. |
| [S4a-05](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137319964) missing timeout | Corrected in code. Without `timeout`/`gtimeout`, the updater logs a distinct skip, sends a `binary_update_skipped` notice naming coreutils, installs nothing and no longer reports a false unrunnable binary. Such hosts never update in place. |
| [S4b-11](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137332679) move activates unreviewed host config | The initial file-level guard was partial. The later correction below compares both manifests with the shared frozen FleetConfig parser, refuses unrelated fields, binds staging to reviewed manifest states, and scopes added/removed inputs to the moved bot. It adds no CLI flag or separate plan-review subsystem. |

### Setup and activation refusals

| Finding | Disposition |
|---|---|
| [S3b-07](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137307952) | Corrected for handoff refusals. `ActivationRefusal` carries product-authored text only, and `host activate` discloses it as a conflict. Other activation errors still show only generic native results. With no activation record, the result says `recording: unchanged` and gives no resume advice. Timeouts are `unavailable`. Other pre-record refusals in `activation.py` remain generic, and on `--resume` a refusal gets a less specific message. |
| [S3a-01](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137296113), [S3a-02](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137296129) | Corrected in code. `fleet setup` checks the authored name, the YAML and the selection before copying. It restores prior bytes on a staging failure, leaving a concurrent edit alone. It refuses system containers and nested or ambiguous destinations before any flat write. Nothing is restored once activation begins. Staging in the test is stubbed. |
| [S3a-06](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137296154), [S3a-11](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137296198) | Corrected in code. Assembly failures disclose only allowlisted text, the step or the retained release path, never subprocess output. `host setup` requires `jq`. |
| [S6-01](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137368228) | Documentation corrected. Getting started probes a real disposable venv and no longer exits the pasting shell. The mandatory cold-host onboarding run has **not** been performed. |

### Read truthfulness and coaching

| Finding | Disposition |
|---|---|
| [S5a-07](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137346532) | Corrected in code. Only a complete, clean scan of an existing trusted directory reports an idle bot as observed zero. Unavailable bot or fleet coverage refuses with `usage: null`. `brief --usage-since` still prints zeros beside `unavailable`. |
| [S5a-05](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137346507), [S5a-06](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137346518) | Corrected in code. Board and stale-task arms join canonical task deliveries by task, assignment, recipient, fleet and host, preferring `dispatch_msg_id`. Overview attention and overdue use the board's own card owner and disclose their scopes and the 200-card cap. Per-poll cost has no Pi timing. The stdlib readers and the brief paths were not reviewed for the same join. |
| [S2-05](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137294141), [S6-10](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137368255) residual | fleet-status, selfcheck, fleet-pulse and fleet-digest now use own-fleet public reads, with no tmux loops or `--fleet` grants. The check-in skill's `--fleet` grants and fleet-digest's scripted assembly step remain. |
| [S2-02](https://github.com/Claudfather/Claudlobby/pull/1989#discussion_r4137294084) check-in residual | Corrected in code. Manager check-in scans the staged and spool queues (bounded) **before** the committed database read. This closes the commit-then-delete race in which ingest could land between the two reads. A pending, unreadable or over-bound queue skips the beat without firing or writing. Tokens are matched literally, quarantined batches are not consulted, and an emission that fails outright can still let the next beat fire. There is no retry, and O1 is unchanged. |

### Batch validation

All runs used private Git exports, a private HOME/TMPDIR, packaged resources and
`PLANE_EMIT_DISABLED=1`, with no live or native services. The counts overlap; do
not add them.

- **First run, tree `dce1e5f1`:** 26 focused modules gave **542 passed, 2
  failed, 2 skipped** in 157.61 seconds. The skips were Linux/proc-only cases on
  macOS. The two failures:
  - a move fixture lacked its frozen native target;
  - an updater log literal tripped the version-reader guard.

  The fixture and the log wording were corrected; no production refusal changed.
- **Affected rerun, tree `74e93f4c`:** `test_move_bot.py` and
  `test_claude_version.py` gave **75 passed** in 15.80 seconds.
- **Shell suites:** the supervisor adapter passed **130/130**; the updater
  passed **19/19**, including the rerun after the wording correction.
- **Final follow-up, tree `b9855cbc`:** the move, bot-remove, bot-operations,
  fleet-operations and manager check-in modules gave **83 passed** in 6.97
  seconds.

Hosted checks on `2278690a` are historical and do not certify this batch: six
passed, while [Ubuntu/Python 3.10](https://github.com/Claudfather/Claudlobby/actions/runs/36729475605/job/109934938363)
was cancelled after an hour in the apt prerequisites step. It never reached
tests, so that lane remains unverified. No current-head review,
real native or Pi proof, or production change exists for it.

## Narrow follow-up: concrete failures, no new recovery framework

Main #2034 (`030059de`) is integrated by `ed46a1f9`, preserving both changelog
entries and the Claudron 0.6.1 pin. Compatibility/loop checks passed 71 tests;
six optional-engine cases skipped because that private test environment has no
Claudron installed. The hosted pinned-engine conformance lane remains required.

Code commit `40a99d43` closes the demonstrated S4b-11 unrelated-config path:
source and target declarations use the same `load_fleet_snapshot` model as the
selected configuration. Only the moved bot, its team membership and a necessary
source-manager replacement may differ. Staging must retain the exact reviewed
manifest states. New inputs are limited to the target bot directory, removed
inputs to the source directory; the moved bot's owned Telegram access rewrite is
explicit. Other changes refuse. Moves requiring new external account/skill inputs
or edits to other bots must activate those separately first.

Private-export evidence:

- Tree `cecc0419`: 71 activation, handoff, unit-journal and migration-apply tests
  passed. The new malformed-handoff case uses real durable owners with a stubbed
  native boundary and stops at the migration boundary.
- Final tree `a2b704e6`: all 19 move tests passed. The authored fixture now parses
  real manifest bytes rather than returning an empty roster, and its default
  Telegram path assertion was corrected.
- Against the previous `6a9fce58` move implementation, another bot's field edit
  and a fleet mission edit both failed to raise a refusal. Restoring the correction
  passed both cases. The third parameter already refused extra roster membership;
  its old-version failure was message matching, not a new behavioral defect.

The malformed-handoff test establishes a narrow existing repair-forward route:
with pending `queues_classified`, correcting that bot's stale canonical handoff
section and resuming the same activation ID reaches the migration boundary,
preserves the notes and performs no repeated native pause/handoff/start. It does
not prove completed migration, native recovery or a general rollback path. No
production recovery code was added. Post-preflight task churn is not covered by
this procedure; paused task writes still refuse.

The existing Linux bootstrap fixture cannot represent populated legacy adoption
without substantial new simulation. That simulation was not built. Ubuntu CI
provides Linux x86_64 code/harness and offline release-assembly evidence; actual
Pi/user-systemd adoption remains unproven. Tailscale discovery earlier on
September 30 found the Pi offline, so no hardware canary ran then.

**Later Pi evidence (Linux aarch64, read-only natively).** These results are at
`4adfb7b2` and use two separate sources.

- **Tests:** a disposable source export with a private home, temporary directory
  and virtual environment passed **170 tests, with no failures or skips**, in 35.71
  seconds. The files were the heavy-slot guard, run, redaction and #2023
  classifier tests plus `test_supervision_inventory.py`.
- **Native catalogue read:** the selected adapter's `svc_inventory_catalog` parsed
  the real user-systemd catalogue:
  - 16 search directories;
  - 186 service, timer, socket and path names;
  - 158 loaded names;
  - 36 escaped device and mount rows, correctly ignored.

  Production was not changed.

This closes the concrete S4b-04 escaped-unit concern on real hardware. It does not
establish Pi bootstrap, adoption, interrupted recovery or timing.

The merge standard is concrete failures and plausible dangerous paths plus a
bounded real canary. Performance speculation, broad platform matrices and a
general rollback framework are not automatic merge requirements. Remaining
substantive findings still require an explicit fix or deferral; this is not a
claim that all 199 findings are resolved.

## Accepted policy dispositions

- **S2-03, no automatic Enter repair.** A repair keystroke after an uncertain
  submission is a resend risk. Uncertain sends stay disclosed and held, and
  `message send` performs exactly one native submission. Cleanup of the stale
  docstring and the uncalled `pane_await_receipt` helper is a follow-up.
- **S5a-01 / S5a-08, honest recording outages.**
  - **Disarmed daemon:** disarming it is an operator choice. Hook and timer emits
    stage within their bound, stay unrecorded and are shown as attention by
    `plane doctor`. Canonical task and linked mutations still commit in-process,
    and ordinary sends degrade under O1.
  - **Pending schema:** the daemon exits 7 and supervision relaunches it,
    deliberately without a restart-prevention setting. Schema changes belong only
    to `host activate`, so the remedy is `host status` followed by a same-ID
    `--resume` repair-forward.
- **S6-11, operator-only exception.** The broad setup-assistant grants are a
  deliberate opt-in exception for the seed's operator onboarding bot. The default
  fleet-ops grants stay narrow. Operator-only activation still refuses a hosted
  caller.

Future optimizations, a general rollback framework and exhaustive variant matrices
remain follow-ups.

## Host conversion and outstanding gates

**CI status.** All seven hosted checks passed on `e0236a13`, and the operator
signed off on that revision after the extensive reviews recorded here. The main merge at `4adfb7b2` and the adoption corrections in `57401cc5` require
their own hosted checks; an earlier green head does not certify them. New focused changes need their own tests and the repository's
existing required GitHub approval rule; no fresh broad review panel is required.

The [conversion runbook](../existing-host-release-conversion.md) still requires
old source-puller holds, a same-OS isolated canary and a host-wide adoption
window through `host activate --adopt-existing`. No shim converts a host.

A read of the Mac's current-user LaunchAgents and `launchctl` inventory found no
pull-root match. Other launchd domains were not checked, so this is not proof
that the estate is held.

The remaining gates and their dispositions are:

- a populated Linux first-adoption coordinator run;
- real Pi/user-systemd adoption;
- interrupted native activation recovery, with no walk-back route;
- normal-load timing;
- narrow-grant session proof;
- protected production adoption;
- the mandatory cold-host onboarding run;
- retirement of already-installed updaters and pullers on each host;
- the remaining migration items: old-ingest drain and a public legacy preview;
- **Adoption corrections in `57401cc5` (S1-13 / S3b-06 / S9-06), focused tests passed.**
  The pre-record guard refuses a candidate that would re-enroll an explicitly
  stopped bot or resume a legacy-paused runner without a recorded pause.
  Installed inactive bots remain under supervision; transient inactivity does
  not establish a deliberate stop. Resume follows the frozen intent; external
  start/stop/marker changes during the window are unsupported.
  Run-intent and migration blockers use the existing typed refusal, while native
  stderr and pending payload values remain hidden. Quarantine is inventoried
  and retained, including malformed payloads, without replay or discard.
  Active pending queues, unreadable quarantine and changed evidence still refuse.
  **99 tests passed in 13.76 seconds** in a private macOS export at tree
  `e2b196589148c0ea1ed3cbabe3a4a8eec3325766`: activation, handoffs, units,
  migration plan/apply and host commands. These use native adapter fixtures;
  they are not successful populated Pi adoption evidence.
- **Runbook coverage.** The
  [conversion runbook](../existing-host-release-conversion.md) now covers
  `fleet.manager`, run intent and runner markers, open legacy work, staged, spool
  and inflight drain by the old ingest, and quarantine kept as evidence rather
  than deleted or replayed.
- **S9-01 Pi source-pull hold, verified read-only.** The timer remains enabled,
  but its host override pins the source at `8bc588a8` for this migration, dated
  through October 7. The installed script matches that main revision and its
  last run read this exact hold. The script keeps holding after expiry, so the
  date does not authorize a later pull. The service subsequently finished
  successfully and was inactive. No scheduler or service was changed. This
  verifies the Pi ceiling; it does not establish holds on other hosts.
- **Next native acceptance: a bounded same-uid Pi canary** (not yet operated;
  historical, superseded by the canary recorded under "Runtime script
  relocation" below).
  - Its safety scope: a distinct fleet `service_prefix`, and a private host
    override whose `unit_prefix` does not begin with `claudlobby-`, which the
    old installation's walk-back can remove.
  - Its shared-resource decisions: the account directory, host `.env` and Git
    config.
  - Its teardown, planned in advance by exact unit names.

Lumbergh's restart hold remains in force. This record makes no claim that the
branch is ready to merge or that the epic is complete. No live checkout, fleet
generation, service, deployment, GitHub comment or review request was made.

## Runtime script relocation (#1989)

- **Move.** All 77 tracked `lib/` files moved to `claudlobby/_runtime_scripts/`
  with executable modes kept; top-level `lib/` is gone. `library/` equipment and
  `harness/` instruments remain top-level. Source and installed wheel use the
  same package path, so there is one reader layout and no alias, symlink or
  shim. `CLAUDE.md` and `personal/finance-presync.sh` stay out of the wheel and
  sdist. Existing releases keep their recorded `_native` path in `release.json`.
- **Unset-root fallback, disposed.** `vault-sync.sh`, `manager-checkin.sh` and
  `selfstart-snapshot.sh` derive a root from their own directory only when
  `CLAUDLOBBY_ROOT` is unset. Supported callers always set it: composed job
  units carry `native_environment()` (`CLAUDLOBBY_ROOT`, `CLAUDLOBBY_NATIVE_DIR`
  and so on), and `selfstart-snapshot.sh` runs under `boot-capture.sh`, which
  refuses without `CLAUDLOBBY_ROOT`. A bare private invocation is unsupported,
  and installed releases already resolved that fallback inside the package. No
  code change was made.
- **Evidence.** 394 focused tests passed on the private candidate `4215445f`,
  including the direct wheel and sdist payload, modes and exclusions. All 7
  hosted checks passed on the prior revision `3d41c899`; CI for the new
  revision is pending.
- **Pi canary, scope.** The earlier native canary at `d60cdac9` passed 16 public
  commands with a stub Claude, real user systemd, tmux and Plane, an empty
  private root, and teardown. It is not a populated production adoption, and it
  is not evidence for this relocated-path revision.

Every earlier unresolved gate stands. This section makes no merge or
completion claim.
