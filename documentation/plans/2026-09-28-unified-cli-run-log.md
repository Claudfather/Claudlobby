# Unified CLI implementation record

### 2026-09-29 16:56 UTC — CI green; real outage canary found manager alert defect

**Measured:** all seven hosted checks passed at `a780a96`, including Linux 3.11
(6,279 passed), Linux 3.10 (6,280 passed) and macOS 3.11 (6,259 passed):
[run 36596333003](https://github.com/Claudfather/Claudlobby/actions/runs/36596333003).
The sealed wheel then activated on the independent Mac canary. Its real manager
used composed `fleet-ops`, read context/brief/the retained completed task, and
received exit 4 for an operator-only host-job probe. That proves CLI authority
refusal, not a provider permission denial. Production remained untouched.

**Measured:** a bounded write lock on only the canary Plane refused task admission
(exit 6, no task event), while one ordinary message arrived with its degraded
label (exit 11, unrecorded, request persisted, submitted once). The independent
manager alert failed. No database rows were changed by the fault injection;
the lock was released in `finally`, the Plane recovered with empty spool and
quarantine, and neither uncertain request was resent. Evidence:
`~/.local/share/claudlobby-candidates/20260929-cli-a780a96/evidence/o1/`.

**Read from code / measured:** `recording_alerts.py` passed `=session` to a pane
helper; real `capture-pane` refused that target while `=session:` succeeded.
`8bae16e` changes the target to the existing message transport's spelling. Three
focused recording-alert checks passed, including manager delivery with no
Telegram channel configured. Fresh hosted CI and a repeat of this one outage
check remain required. This is one observed failure and one targeted repair;
no new migration harness or full local suite was added.


**Measured / read from code:** the same bare target made `display-message`
return only `-`, omitting both manager-instance fields. `89314c2` corrects that
lookup as well; the same three focused checks passed with the fixture enforcing
the pane target for both calls. `721b1b0` replaces stale script commands in memory
alerts and GitHub App recovery advice with their existing public CLI commands.
The same cleanup updates five active operator guides, including the distinction
between stopping a declared bot and permanently removing an omitted bot.

**Measured:** a bounded caller inspection covered 560 active source/coaching
files and found no executable invocation of the 41 deleted `lib/` files relative
to `fb113fa`. The canary manager's 24 generated coaching files had no matching
retired lifecycle/task/setup route. Its latest trace contained six Bash calls:
five supported fleet operations used the public CLI, one was local file listing;
zero retired doors, direct state/transport bypasses or unknown operations in
that five-call sample. Four CLI calls succeeded; one correctly refused bot
access to an operator-only command. These small canary samples do not establish
estate-wide adoption or coverage of external integrations.

### 2026-09-29 — CI integration repairs; rollout held

**Measured:** the operator's screenshot matches all four job durations from
[`1667434`](https://github.com/Claudfather/Claudlobby/actions/runs/36561883504).
Its handoff-grant expectation was corrected in `db1d186`, whose complete
[test matrix passed](https://github.com/Claudfather/Claudlobby/actions/runs/36564193139).
That repair is retained; it is not evidence for later revisions.

**Measured:** [`af8b9fe`](https://github.com/Claudfather/Claudlobby/actions/runs/36593143788)
then failed the same eleven cases on Linux 3.11 and 3.10. Eight were missed
references to moved development instruments: the boot-summary stub still
replaced `LIB_DIR`, and Claude-version checks still invoked `lib/` paths.
Three were obsolete caller/diagnostic expectations. The caller inventory and
schema-read fixture were already repaired in `4990f72`; both passed when
rechecked against that exact source. The remaining daemon check reproduced
exit 4 versus its old expected 1; `ec64a72` updates the assertion to the common
attention contract, retaining its diagnostic assertions. All three focused
diagnostic/caller checks then passed in 1.25 seconds.

**Read from code / measured:** `6f75fc7` retargets only the affected test callers,
uses the existing built-CLI fixture where private composition needs packaged
resources, and leaves runtime code unchanged. Its nine-case focused selection
passed in 4.86 seconds after reproducing the failures. No test was removed or
weakened and no new test framework was added. New feature pushes and further
rollout are held until the current revision's complete CI matrix passes.

### 2026-09-29 16:02 UTC — direct generation retired; cold fleet move exercised

**Read from code:** the public `generate` route and the 508-line
`lib/migrate-fleet-to-system.sh` are deleted, without aliases or shims. The new
`--root ROOT --fleet F fleet move --system SYSTEM` moves cold authored files
only. It reuses the activation lock and complete native inventory, refusing a
selected release, retained activation history, a runtime directory, owned native
consumers, redirected/ambiguous paths or a cross-filesystem move. It performs
one directory rename, preserves contents, and neither composes nor starts bots.
Active fleet relocation is explicitly unsupported; it would require a separate
recovery design. This uses the simpler default presented to the operator while
continuing the authorized cleanup. Existing native and diagnostic recovery hints
now direct callers to `config plan` and `host activate`.

**Measured:** wheel source `d21451b` executed that public command on a private
cold root using the real macOS native inventory. The move succeeded, the authored
content remained intact, a repeat returned `already_nested`, and public
`config validate` resolved the nested fleet. The retired `generate` invocation
returned exit 2 and created no runtime. The wheel contains neither the retired
migration script nor the moved development harness. Evidence:
`~/.local/share/claudlobby-candidates/20260929-cli-cold-move/evidence/`
`{fleet-move.json,acceptance.json,package.json}`;
[PR #1985](https://github.com/Claudfather/Claudlobby/pull/1985).

**Measured / read from code:** `plane registry` now supports the common JSON
contract. Its verify result explicitly compares authored config with recorded
projection; incomplete enumeration, drift or unvalidated tombstones cannot report
verification success. The built CLI read the independent live canary's registry
at exit 0, returning one bot row in structured JSON. This was a read by the new
artifact, not activation of that artifact. Seven focused registry cases, eleven
cold-move/caller-inventory cases and 42 CLI parsing cases passed. The revised
inventory recovery assertion passed its exact case. No full local suite or new
migration harness was added.

**Limits:** the independent running canary remains on `ceaa249`; production
remains on `c32396c` with Lumbergh's no-restart hold. Hosted `af8b9fe` has passed
its harness and three fast checks while its Linux/macOS suites are still running;
no current-batch hosted success is claimed. Cold Linux/Pi evidence, normal-load
Pi timings, current-head hosted CI and protected production adoption remain.

### 2026-09-29 15:46 UTC — public diagnostic skill passed on the live canary

**Measured:** source `ceaa249` is active only on the independent Mac canary,
activation `f66485d5-e8cb-4e25-a07e-1b08d87b90b7`. The manager invoked the
composed `doctor` skill and ran five literal commands under normal `auto`
permissions: `context show`, `config validate`, `host doctor --no-delivery`,
`fleet reconcile` and `plane doctor`, all with `--json`. Actual tool results
returned `ok=true`; the bot reported no blocking permission prompt. This proves
these composed grants work, not that unrelated commands are denied. The trace
retains honest warnings and unknowns: disabled default plugins, npx cache,
unchecked delivery, unknown running-release observation and unrederived registry
drift. Evidence: `~/.local/share/claudlobby-canary-live-1747/evidence/doctor-skill/`
`{doctor-cli-proof.md,trace-tools.json,trace-results.json,grants.json}`;
[PR #1985](https://github.com/Claudfather/Claudlobby/pull/1985).

**Read from code:** Plane doctor now uses the common result contract and returns
exit 4 for attention, including failed switch resolution. Selected-release
admission protects spool mutations and live retention; retention updates both
lanes and their watermarks in one transaction. Twenty-five existing development
instruments moved from `lib/` to `harness/` and are excluded from installed runtime
resources. Their private compose entry reuses the existing compositor and refuses
selected roots. No new migration or measurement framework was introduced.

**Measured:** the combined focused run passed 49 cases. Six remaining development
instrument cases failed because their private helper compared a prepared artifact
with a later fixture Git revision. `86d1305` checks the captured artifact identity
for prepared trees; those six cases then passed. The initial run with an incorrectly
bound editable interpreter was rejected by the origin guard and is not product
acceptance. No full local suite was rerun. All three failures from hosted `ddeaa63`
are repaired and their exact checks passed; hosted CI for this batch is pending.

**Measured limits:** Lumbergh PID 2598, primary Plane PID 1498 and production
selection remain unchanged. The latest code includes one private-helper correction
beyond canary source `ceaa249`; it does not enter the installed runtime. Remaining
work is fleet-container migration and public `generate` retirement, current-head
hosted CI, cold Linux/Pi evidence and protected production adoption. The epic is
not complete.

### 2026-09-29 15:16 UTC — retired-bot cleanup passed on the live canary

**Measured:** source `0868310` is active only on the independent Mac canary,
activation `50854030-71df-4799-8495-9d4b32c02c21`. After the authored and
activated fleet omitted the completed worker, `bot remove cli-worker` returned
`ok=true`, `native_outcome=observed`, `purged=false`. The worker unit is absent
and its directory is retained. The teardown intent is recorded as
`ev_f5b54e78471e4ffab9e6a433a8fb5a6e`. Evidence:
`~/.local/share/claudlobby-canary-live-1747/evidence/bot-remove/`
`{remove-fixed.json,observed-after.json,events-after-fixed.json}`;
[PR #1985](https://github.com/Claudfather/Claudlobby/pull/1985).

**Measured / read from code:** the first removal refused because an exited
tmux server had left a socket file. The private supervisor owner now accepts
only tmux's exact no-server result for that retired bot's socket, including
macOS's canonical `/private/tmp` spelling. Unknown/permission failures still
refuse. Three focused socket cases and both supervisor-ratchet checks passed.
The stale socket node may remain; cleanup does not claim it is a live process
or remove a socket belonging to another server.

**Read from code / measured checks:** `lib/setup-fleet` and `lib/setup-fleets`
are deleted. Guidance points to sealed `fleet setup` or `config plan` and
`host activate`. Obsolete setup-wrapper test classes were removed; the existing
briefing rehearsal still checks composition, pruning and delivery, with one
activation-owner case covering omitted timer removal while preserving a
foreign timer. Focused owner/composition, generated-table, doctor, packaging
and briefing checks passed; the README count checks passed after deletion.
This cleanup removes substantially more code than it adds and creates no
new migration harness.

**Measured:** `plane status --json` read the actual canary database at schema
13, with zero pending or quarantined spool entries. Production activation
`437af124-851e-449c-bfd2-2fd6eab545b8`, Lumbergh PID 2598 and primary Plane PID
1498 remain unchanged. The [published `ddeaa63` Linux 3.11 run](https://github.com/Claudfather/Claudlobby/actions/runs/36586580070/job/109468411198)
finished with 6,320 passed and three failures: two obsolete setup-command
expectations and the direct-supervisor-call ratchet. All three exact cases
pass on this local cleanup (0.43 seconds). Linux 3.10 and macOS 3.11 completed
with the same three failures; their other 6,321 and 6,300 cases passed,
respectively. This checkpoint does not claim current-head CI success.
Remaining work includes the fleet-layout migration decision, remaining Plane
administration contracts, development-instrument placement, Linux/Pi evidence
and protected production adoption. The epic is not complete.

### 2026-09-29 14:54 UTC — real handoff/restart/resume passed

**Measured:** source `9fab743` is active only on the independent Mac canary,
activation `e56e3f08-10fc-4ff2-b518-aea60b69840d`. The actual manager invoked
`fleet-ops`, requested one worker handoff, received `saved/fresh_file_verified`,
then requested one restart and observed `session_ready`. The worker's private
session PID changed from 51960 to 65516; the new session read its handoff and
recovered marker `handoff-proof-20260929-1037`. This used the existing clauDNA
session provider composed locally under normal `auto` permissions, with bypass
disabled. Evidence: `~/.local/share/claudlobby-canary-live-1747/evidence/session-provider/`
`verified-write-{acceptance.json,manager-results.md,worker-trace.json}`;
[PR #1985](https://github.com/Claudfather/Claudlobby/pull/1985).

**Measured / read from code:** the first auto-mode attempt saved within the
native capture window but invented a `last_updated` seven minutes in the
future. The manager correctly withheld restart on the unverified result.
`c6a6853` now uses the native-observed checksum change plus fresh filesystem
time for an explicit handoff; owned-file/frontmatter validation and the
stricter self-restart check remain. The two focused handoff cases passed.
The new controlled canary succeeded without automatically resending the prior
unknown request. This supersedes the provider-continuity limitation below.

**Read from code / measured checks:** `bot remove` now cleans up only a bot
omitted by both active and authored configuration, using its retained activated
declaration; it refuses reused native labels and project WIP before explicit
purge. Selected Plane expiry requires a committed batch and rechecks candidates
inside the existing write transaction, preventing a completion race or spool
from being reported as expiry. Their combined 14 focused checks passed; the
existing teardown shell test passed 27 assertions. `plane status` now has the
common JSON result; its built-artifact subprocess check and private JSON
invocation passed. The live selected expiry dry-run found zero candidates.
No new general migration harness or full local suite was added.

**Measured:** the prior completed task and linked receipt remain intact after
this upgrade. Production selection, protected manager PID 2598 and Plane PID
1498 are unchanged. Hosted `687f3d2` failed two checks: stale literal grant
expectations and the scalar provenance reader's source-read tripwire. Both
were repaired in `044289a` and their exact local checks passed. Its next Linux
3.11 run found eight failures: five workstream-import calls shared a removed
`emit_batch` import, two assertions expected retired parser wording, and the
README script count was stale after deletion. The missing import was restored;
all seven exact runtime/parser cases passed against a disposable built artifact.
The README count and generated switch tables passed 10 focused checks. These
repairs are included in the next push; its hosted CI remains pending.
Remaining work includes old
composition/setup callers and fleet-container migration, remaining Plane
administrative contracts, final code/guidance placement, Linux/Pi acceptance,
and production adoption after the protected work completes.

### 2026-09-29 — independent live canary and public-door retirement

**Measured:** an independent `cli-isolated-1747` manager/worker fleet on this
Mac completed admission, assignment, delivery, worker acceptance and a linked
completion report through the composed `fleet-ops` skill and public CLI.
Canonical reads preserve task `wi_d13b42a73f7a4adcb98ee91d1a5f35c3`, assignment
`asg_25d4c65610d248bbb6a25262c39596a6`, and report
`msg_a9529955b3be46d0922362126a6faa7f` through subsequent canary recompositions.
Unconfirmed initial sends were inspected and never automatically resent.
This supersedes the earlier real-agent acceptance status below. Evidence is in
`~/.local/share/claudlobby-canary-live-1747/evidence/` and
[implementation PR #1985](https://github.com/Claudfather/Claudlobby/pull/1985).

**Measured:** production remains on source `c32396c`, activation
`437af124-851e-449c-bfd2-2fd6eab545b8`; the protected manager's pane PID is 2598
and the Plane PID is 1498. The operator authorized live fix-forward but separately
required leaving Lumbergh running until his work finishes. The independent
canary's namespaced units and root allow progress without restarting him.

**Read from code:** later slices add `bot interrupt/compact`, `fleet notify`,
explicit `migration` converter verbs, scalar `config explain` provenance,
`plane emit/emit-batch`, current `config diff`, and `config validate --runtime`.
The startup canary also exposed an oversized context response (the worker
reported 67.5 KB and read only its beginning). Orientation now reports registry
status, scan provenance and counts; detailed entity rows stay with Plane reads.
Their former public spellings are removed with actual callers. The native
telemetry socket/stdlib path remains private and does not acquire a Python CLI
start per event. Runtime-audit mode reuses the existing audit verdict rather
than adding a new migration gate. Focused existing checks cover each changed
owner; the full platform suites remain hosted CI's responsibility.

**Measured:** configured session-provider handoff reached a real worker's
permission prompt for `.claude/session.md.tmp` in `acceptEdits` mode. The manager
returned timeout/unknown and did not restart or retry. An exact `Edit` grant did
not remove the prompt and was reverted. Claude's documented
[protected-path policy](https://code.claude.com/docs/en/permission-modes#protected-paths)
runs before allow rules; an allowlist is not unattended-handoff evidence.
The next canary uses `auto`, matching both production fleet manifests, with
permission bypass disabled. Successful provider saving and post-restart resume
remain acceptance work, not a claimed pass. The minimal provider-absent canary
previously returned an honest capability skip.

**Measured:** all seven hosted checks passed at `c94a1ad`; the later `687f3d2`
platform suites are still running. No result for an older head certifies later
changes. A parser inventory at `118822e` finds 127 public leaf routes; 12 still lack
the common result, including the two remaining legacy composition doors and
Plane administrative/foreground-service routes. Remaining work includes
residual composition/setup callers, explicit
bot removal, final private/harness placement and active guidance, current-head
checks, cold-host/platform acceptance, and protected production adoption.
No normal-load Pi timing has been measured.

### 2026-09-28 — working delegation candidate after CI repair

**M:** repair head `56bc75ffec991d3724d371415d399db7e1d9dc98` is green in
[tests 36425920693](https://github.com/Claudfather/Claudlobby/actions/runs/36425920693)
and [conformance 36425920782](https://github.com/Claudfather/Claudlobby/actions/runs/36425920782).
Linux 3.10/3.11 each passed 6,390 tests with 15 skipped; macOS passed 6,368
with 37 skipped. Each reported four warnings. All seven native macOS checks
executed and passed. These results cover the repair head, not this later feature.

**R:** this candidate adds public message/request observations, ordinary send,
manager assignment delivery, assignee linked reports, withdrawal/reassignment,
and the default fleet-ops skill with narrow composed grants. One native-attempt
owner handles ordinary sends and strict notifications. Task/report facts commit
before transport; final receiver byte proof is separate from submission and
assignment acceptance. Failures after a known commit retain IDs and disclose
request-persistence/notification uncertainty. Ordinary recorder-outage behavior
remains O1; reports tied to tasks never fall back to an unlinked send.

**M:** the existing query suite reproduces the registered `system:task-recheck`
reader failure (parent 1 failed/7 passed; candidate 8 passed), and the existing
request-receipt suite passes 9 tests. These were private source exports with
`--noconftest`, exercising stdlib/SQLite logic rather than full application
fixtures. The combined staged bootstrap/query/receipt run then passed all 27
checks in 0.83 seconds; Python syntax and focused Ruff checks passed. The next
hosted run must validate the complete candidate. Real-agent
canary delegation/recomposition/permissions and normal-load Pi timing remain
unrun. A private HOME alone cannot isolate the host-wide user-manager Plane
unit; the canary needs an isolated OS account/manager domain or another host.

The operator's canary-first direction is now explicit in plan PR #1925 at
`c74db0a4a139d32bcfa14113648a7a529e514412`. Extra migration witness work remains
held outside this slice; no new general migration harness was added. Dara's
bounded waiting-state/usage findings are assigned to C1, not this delegation
candidate. Production is unchanged; full task/caller cutover is not complete.

### 2026-09-28 13:03 UTC — remaining bootstrap assertion repaired

**M:** PR #1935 at `84539521` completed all platform jobs with one stale
syntax-error assertion failing. Linux 3.10/3.11 each passed 6,389 tests with
15 skipped; macOS passed 6,367 with 37 skipped. Each had four warnings.
All 14 other previously failed/blocked nodes now pass, including the seven
new runtime cases. Native macOS evidence and both conformance checks passed.
[Complete run](https://github.com/Claudfather/Claudlobby/actions/runs/36422763759).
These are failed candidate results, not acceptance.

**R:** `7b1e5796` changes only the existing bootstrap assertion to the stable
public syntax error and requires empty stdout; exit 2, import isolation and
traceback checks remain. **M:** the entire existing bootstrap test file ran in
private source exports: parent 1 failed/5 passed, candidate 6 passed. This
narrow run used stdlib environment setup, not the full suite's conftest.
Hosted verification of the amended revision remains pending; no exclusions.

The operator reaffirmed a working-system/canary-first approach: the next slice
is usable messaging plus the default fleet-ops skill and narrow composed grants,
then actual isolated delegation/recomposition. Further migration machinery is
deferred until that slice needs it. Reuse existing tests; no new general harness.
Sol implements bounded slices; parent reviews and publishes. Production is unchanged.

Status: IN PROGRESS. Started 2026-09-28 UTC. Tracks #1747 and the design in
[PR #1925](https://github.com/Claudfather/Claudlobby/pull/1925), revision
`c74db0a4a139d32bcfa14113648a7a529e514412`.

The operator authorized starting implementation while the other fleets await
quota. External review remains open; later valid findings will be incorporated.
This authorization does not merge the plan or activate production releases.

## Execution boundaries

- Start from main `fb113fa310461f97d3e681b332d1ac97f1a8963c` in an isolated checkout.
- Test exports with private HOME, temp, sockets and default-disabled emission.
  Preserve production checkouts, runtime state, credentials and supervision.
- Full public CLI; no compatibility shims or retired public aliases. Keep one
  operation owner and private native hot paths. Move consumers with each door.
- Fleet-owned tasks, one implicit fleet manager, separate admission/assignment/
  delivery and assigned-bot acceptance. Trusted local callers, no new auth layer.
- O1: ordinary communication survives recording outages with independent fleet
  alerts and honest degraded results; task mutations require committed records.
- Every executable slice inherits the plan's isolated recomposition/delegation/
  permissions gate. Unrun platform, real-agent or Pi checks remain unverified.

## Dependency and publication strategy

Use the existing open #1846 test-isolation implementation at
`23181a4828d34ddee33e6d1c87ee45d74b996101` as a declared prerequisite, preserving
its commits and caller migrations in the local integration branch. It is not
merged on GitHub. New changes stay in separate logical commits; do not re-file
the prerequisite's work as a competing fix. Before publishing each slice,
refresh main and dependency heads and show the slice-only diff. Reconcile
squashed parents before continuing a stack; never force-update another owner's
branch. Merges and production activation remain operator decisions.

## Progress

| Step | State | Evidence / remaining work |
|---|---|---|
| P0 evidence safety | Linux CI passed | #1846 integrated locally; private HOME/XDG/temp and executable-origin changes in draft [#1928](https://github.com/Claudfather/Claudlobby/pull/1928). Current head `af1cc44d`: 5,812 passed, 13 skipped, 3 warnings; all three checks green. Native macOS remains unverified. |
| P1 command loading/package | Linux CI passed | Lazy argparse dispatch and canonical resource packaging in draft [#1929](https://github.com/Claudfather/Claudlobby/pull/1929). Current head `c2b8188a`: 5,980 passed, 13 skipped, 3 warnings; all three checks green. Bootstrap and artifact assertions also passed in isolated exports; installed composition and host acceptance remain outstanding. |
| P2 context/release | P2a and assembly foundation Linux CI passed; activation in progress | [#1930](https://github.com/Claudfather/Claudlobby/pull/1930) `8b8c73b6`: 6,022 passed, 12 skipped. [#1931](https://github.com/Claudfather/Claudlobby/pull/1931) `ca8658c4`: 6,041 passed, 12 skipped. Both have three green checks. Explicit migration [#1932](https://github.com/Claudfather/Claudlobby/pull/1932) repairs are in CI; real installed planning [#1933](https://github.com/Claudfather/Claudlobby/pull/1933) exposed a spaces-in-root validator defect. Full activation/coherent canary remains incomplete. |
| A0–A4 tasks/messages | Task/read foundations hosted; working delegation candidate under validation | Admission/assignment/acceptance and read foundations are hosted at green #1935. Message send/delivery, linked reporting, default skill/grants and request/message reads are the current candidate. Reply/unlinked reporting, full caller cutover and real-agent canary remain incomplete. |
| B lifecycle/setup | Pending | Reuse supervisor consolidation; verify affected native platforms. |
| C coordination/jobs/plane | Pending | Migrate complete operation/caller bundles. |
| D completion | Pending | Retired-door scan, full operation coverage, cold onboarding, adoption and acceptance evidence. |

Tracking limitation: Linear team configuration is unavailable; no ticket was
created in an unverified team. GitHub #1747 and this record track the work.

## Evidence so far

The private `lib/runtime-admission.sh` helper admits cold native lifecycle
entries through the selected release and activation lock. It is sourced by
native callers, not a public command or a compatibility shim; per-tool hooks
do not acquire a Python startup through this helper. Native carrier guards and
their exact-target activation grants have separate focused coverage.

Current hosted results, checked on 2026-09-28:

| Slice | Exact head | Hosted result |
|---|---|---|
| P0 / #1928 | `af1cc44d293f4456f2ef49d9d391afd39d974ade` | [All three checks green](https://github.com/Claudfather/Claudlobby/pull/1928/checks); 5,812 passed, 13 skipped, 3 warnings in 1,035.53 s; zero failures/errors. |
| P1 / #1929 | `c2b8188a6ca974601bcbeba3ef41c61a9396f29f` | [All three checks green](https://github.com/Claudfather/Claudlobby/pull/1929/checks); 5,980 passed, 13 skipped, 3 warnings in 1,047.77 s; zero failures/errors. |
| P2a / #1930 | `8b8c73b62b502a6cd719dcf8a3c06ac012ec23d9` | [All checks green](https://github.com/Claudfather/Claudlobby/actions/runs/36385782579/job/108810745124): 6,022 passed, 12 skipped, 3 warnings; zero failures. |
| Release foundation / #1931 | `ca8658c4f85086e9d2e676b86e3ee863cdcfd22d` | [All checks green](https://github.com/Claudfather/Claudlobby/actions/runs/36387658552/job/108816373238): 6,041 passed, 12 skipped, 3 warnings; zero failures. |

Full P0/P1 logs are `/tmp/claudlobby-cli-evidence-1747/p0-af1-ci.log` and
`p1-c2b-ci.log`. Both have the same 13 skips:

| Count | Test / reason |
|---|---|
| 2 | `test_boot_strand_sampler.py`: real boots require `BOOT_SAMPLER_REALBOOT=1`. |
| 1 | `test_claude_session_pid.py`: no live Claude process for the control. |
| 4 | `test_claudron_loop.py`: three cases lack `claudron.hooks`; contention case needs the `[vault]` extra and git. |
| 1 | `test_env_cascade.py`: shallow history cannot establish historical transcription drift. |
| 1 | `test_freshbox_boot_harness.py`: real boot requires `FRESHBOX_REALBOOT=1`. |
| 1 | `test_macos_supervision.py`: Linux has no `launchctl`. |
| 1 | `test_plane_daemon_units.py`: regular site-packages installation does not satisfy this user-site-only regression's premise. |
| 1 | `test_rename_map_gate.py`: no local clauDNA skills checkout; network clone belongs to its separate CI job. |
| 1 | `test_send_size_probe.py`: real Claude boot requires `SEND_PROBE_REAL=1`. |

The three pytest warnings remain unresolved in both runs: Starlette's `httpx`
test-client deprecation; an invalid backslash escape in
`tests/test_pr_review_state.py:147`; and a `PytestUnraisableExceptionWarning`
reported during `test_grid_fleet_filter_keeps_twin_named_bots_apart`, where
`BaseSubprocessTransport.__del__` encounters a closed event loop. The last
traceback contains a workflow error annotation, but pytest records it as a
warning, with no failed test or teardown error. This is distinct from the
corrected session-cleanup errors below. Workflow Node deprecation notices are
outside the three pytest warnings. These Linux passes do not cover the skipped
real-agent/platform/dependency cases or establish installed-composition smoke.

- P0 local attempts on Python 3.11 and 3.12 stalled while loading third-party
  native extensions, before pytest collection. They are not passing or failing
  test evidence. Read-only stack sampling placed a wait inside dynamic-library
  loading; a complete root cause is not established. No host security policy or
  production environment was changed. Owned stalled probes were terminated.
- P1's six bootstrap assertions passed with application/dependency imports
  blocked in fresh `-I -S` interpreters. Parent help refuses under that same
  boundary. A transient AST comparison preserved all 43 handler bindings and
  argument grammar. These assertions ran with a private environment helper in
  place of application conftest loading; they are not full pytest/P0 evidence.
- The artifact assertion passed: direct wheel and sdist-rebuilt wheel payloads,
  paths, executable bits and frozen source identity match; ignored local/token
  files stay excluded; removed code cannot survive a repeated build; resource
  reads work after source removal. Artifact identity does not claim to identify
  the host's installed dependency/runtime assembly; P2 binds that release ID.
- On this arm64 Mac, dependency-blocked help took median 0.02255 s and p95
  0.02399 s over 20 samples at load approximately 3.1. No Pi acceptance claim.
- #1929 compares against `codex/unified-cli-integration-base`: current main plus
  #1846 and the P0 change. The original implementation commits are `a2a654e2`
  (loading) and `870010d1` (packaging); the current head also carries subsequent
  isolation and recording-fixture corrections. Retarget after prerequisite
  reviews/merges; none is merged here.

Local execution artifacts are under `/tmp/claudlobby-cli-evidence-1747`:
`p1-parser-parity.json`, `p1-standalone/bootstrap-timing.json`, and
`p1-standalone-3/{bootstrap-results,artifact-result}.json`. Hosted CI and PR
descriptions must carry final results before a slice is presented as ready.

- Initial #1928 Linux suite: 5,798 passed, 12 failed, 13 skipped, 2 errors. The
  failures exposed environment directories inside fixture data and long Unix
  socket paths. Short independent private directories fix that shared cause;
  the current-head full suite passes as recorded above.
- #1929 carries that correction and migrates the newer pull-root/vault-hook
  recording tests to #1846's scratch fixture. Its initial collection failure
  also exposed a wrong packaging-test conftest import, now corrected.
- P2 uses `state/releases/` under the selected host data root as the canonical
  immutable release store. Code guards and composed isolation protect its real
  retained targets as well as the installed package assets. This is implementation
  in progress, not activation or canary evidence.

- A second hosted run exposed pytest lazily creating its session base inside a
  function-private TMPDIR that was subsequently removed (P1: 1 failed, 2,157
  passed, 12 skipped, 3,823 errors). Session initialization now owns that base.
  Exact fixture-only before/after probe: 1 failed + 1 error before, 2 passed
  after. The current P0/P1 full-suite passes include this correction.
- P2 paths assertions: 12 passed, 2 YAML-dependent cases not run, using
  --noconftest in a disposable export. This is scoped path evidence, not the
  full resource/composition lane. A session-CLI stub execution attempt on macOS
  timed out; no semantic pass is claimed and its owned children were stopped.

- P2 normal conftest, scoped pure-Python PyYAML fallback: 117 configuration/path
  tests passed. Composition/roles/isolation run with optional PyYAML/MarkupSafe
  extensions disabled: 444 passed, 11 failed before interruption of a slow
  subprocess. Failures include stale native-path expectations, unscoped native
  helper environments and synthetic libraries accidentally reading the package
  base. These are being repaired; this is not a native macOS acceptance claim.

- The previous P0 full suite reported 5,812 passed, 13 skipped and two teardown
  errors; P1 reported 5,973 passed, seven failed, 13 skipped and two teardown
  errors. Shared cleanup
  now belongs to the session after filesystem monkeypatches restore. Existing
  source-state suite proves the correction: 23 passed + two errors before,
  23 passed afterward. P1's seven failures were newer recording callers missing
  the scratch fixture in fleet-pulse escalation/events and plane dispatch tests;
  those callers now receive the owned recording fixture. The exact current heads
  above pass with zero failures/errors, including session-owned cleanup and
  those caller migrations.
- P2 indexed disposable-export resource preparation passed: builds a private
  wheel and installs only its resource/native assets for editable test imports.
- After composition fixture repairs, 597 tests passed with one stale anchor-set
  assertion and two export-index setup errors; the assertion is corrected. Twelve
  subprocess-dependent cases were excluded from that scoped pure-Python run.
  The export-index cases require the normal indexed CI checkout. P2a #1930's
  first full suite failed as recorded above; conformance is green. Installed-composition smoke,
  native macOS, Pi and real-agent canary remain
  unverified. The source/resource/caller migration is deliberately one coherent
  breaking bundle; mechanical fixture changes dominate its file count.

- P2a first full suite exposed an overly broad source-time host-context check in
  `lib-common.sh`: pure helpers and supervisor queries require no host storage.
  The correction moves the explicit-root requirement to stateful boundaries,
  without restoring checkout/PATH fallbacks. Other failures expose old implicit
  manager, synthetic package, native-helper and CLI fixture assumptions. Existing
  assertions are migrated to explicit owned resources, not weakened.
- The history-free naked-bot probe now builds its exported source into an owned
  wheel/environment and checks code, resources, defaults and CLI origin before
  invoking real generate. Its private dependency closure comes from installed
  distribution inventories without network access. Existing composition cases
  remove source assets and poison ambient CLI/module paths. Setup/provenance
  passed locally; 52 pure cases passed, three composition cases were excluded.
  Direct CLI execution then hit the same bounded macOS stall; full composition
  and the complete repaired suite still require hosted confirmation.

- P2a full-suite repair checkpoint `5345a1e0`: 76 failed versus the prior
  472. Remaining failures include source harnesses without explicit managers or
  selected CLI, native-sibling stubs left under data-root/lib, obsolete source
  path assertions and a registry fixture using a different artifact identity
  than its CLI verification. The example lost its authored sync marker; the
  marker/introduction are restored without weakening the byte-parity assertion.
  Full hosted log: `/tmp/claudlobby-cli-evidence-1747/p2-5345-ci-failed.log`.
  Eighteen scoped composition/skill-path assertions pass after repairs; actual
  shell/installed CLI callers still require the next full hosted run.

## P2a hosted completion and P2b work in progress

- **M:** `8b8c73b62b502a6cd719dcf8a3c06ac012ec23d9` passed full Linux CI:
  6,022 passed, 12 skipped, 3 warnings, zero failures/errors (1,070.90 s).
  [Run](https://github.com/Claudfather/Claudlobby/actions/runs/36385782579).
  Vault and rename-map conformance passed on the same head. P2a remains a draft
  dependency; no merge, native canary, Pi measurement or production activation.
- **M:** P2b private-export checks cover immutable seals/runtime format metadata,
  configuration proposals/application/rollback, activation state, stale CLI
  refusal and task audit. Latest combined run: 63 passed, two real diagnostic
  import cases deselected after a bounded macOS import stall. This is scoped
  evidence, not full integration acceptance. Separate migration preview/apply
  and recording-supervisor checks are retained with their implementation commits.
- **R:** ordinary readers/writers are being changed to require an applied SQL
  schema; the explicit activation migration owner saves a SQLite API backup and
  reconciles interrupted SQL against its exact saved inputs. Test recording
  fixtures now initialize their own scratch schema explicitly. These changes
  are not part of the green P2a revision above.
- **R:** generated sealed-release context includes `CLAUDLOBBY_RELEASE_ID`;
  ordinary mutation admission holds the shared host lock and requires a matching
  completed activation. These foundations still need wiring into the complete
  command/caller cutover. Installed unit coverage, real native pause/restoration,
  candidate startup and real-agent delegation remain required.

### 2026-09-28 07:39 UTC — migration and native activation boundaries

- The initial #1932 full run at `51e5f9b4` failed 165 cases, with 5,972
  passed and 12 skipped. The follow-up at `baedd4d8` provisions positive private
  recording fixtures explicitly, preserves absence/disabled controls, classifies
  inaccessible parent paths as storage failures, and allows first identity only
  for a schema with no retained ingest/identity history. Full CI is running.
  Current integrated migration/configuration checks: 78 passed, two application
  diagnostic cases excluded because this Mac's native loader still stalls.
- Real Linux offline release assembly, seal verification, repeated assembly,
  installed help and native parsing succeeded in #1933. Installed configuration
  planning failed on a data-root path containing spaces. The diagnostic run at
  `6d4cd9de` identifies `path_audit.improper_fleet_paths` splitting that root;
  this is being repaired, not a successful end-to-end release rehearsal.
- Reused native adapter ownership from #1862/#1835 and supported-platform lanes
  from #1859. Strict native inventory, exact file parking/restoration, staged unit
  manifests and phase-specific candidate publication are implemented as internal
  backends. The activation coordinator, complete quiescence proof, readiness,
  recovery and original/candidate integration remain unfinished.
- One uniquely named disposable launchd sleep job on macOS 26.1 / Bash 3.2.57
  was loaded, paused for 12.225 seconds, and restored with original definition,
  mode and override state. Its hosted caller was refused. Cleanup removed the
  owned job and both owned processes. This proves the scoped native primitive,
  not full fleet activation or actual-agent acceptance.
- Added a durable start gate to generated candidate units: interrupted activation
  refuses future auto-starts; a coordinator can admit one exact startup phase.
  The guard does not replace quiescence or grant blanket mutation permission.
  Real rendered-guard and candidate file publication checks are being joined.
- Plan review watch at 07:39 UTC found the same two handled Ravi comments and no
  new formal or inline reviews at `1a403962`. Additional team coverage remains
  outstanding; no merge or production action is authorized.

### 2026-09-28 07:46 UTC — installed release rehearsal passes

The real Linux offline assembly step at #1933 head
`f79fd42eedb3a0f3d350894ddc4510a33b997c11` passed after correcting the configured
root tokenizer. It built/installed the artifact, verified the seal and repeated
assembly, ran installed help/native parsing, and staged configuration under a
path containing spaces without activating it. The full Linux/macOS suites
remain running ([job](https://github.com/Claudfather/Claudlobby/actions/runs/36393261596/job/108833650375)).
The path audit has parent-RED (2 failures) / candidate-GREEN (22 passed) evidence;
foreign and cross-fleet paths remain refused.

## Integration checkpoint — 2026-09-28 08:24 UTC

**Measured:** first #1934 hosted integration at
`6def1a15685053388eb7f45257a341cd3226ff19` is RED.
[Linux 3.11](https://github.com/Claudfather/Claudlobby/actions/runs/36393905425/job/108835701078)
reported 9 failed, 6,235 passed, 15 skipped and four warnings;
[Linux 3.10](https://github.com/Claudfather/Claudlobby/actions/runs/36393905425/job/108835701097)
reported 15 failed, 6,229 passed, 15 skipped and four warnings.
Corrections cover the stdlib optional Jython import probe, explicit fixture SQL
initialization, Python 3.10 SQLite/TOML differences, the native-admission test
collaborator, script documentation and the observed script count. Local focused
verification passed 45 tests; the remaining lifecycle fixture timed out starting
an owned stub on this Mac. Its process group is now reaped on timeout. No native
local pass is claimed. Hosted re-verification is required.

**Measured, incomplete:** the
[macOS 3.11 job](https://github.com/Claudfather/Claudlobby/actions/runs/36393905425/job/108835700805)
reached the existing real tmux validation harness at 91% before the 30-minute job
cap cancelled it. The retained partial log contains 5,617 passed, 79 failed,
37 skipped and two error outcomes, not a full-suite result. All seven dedicated
native launchd/bridge cases show PASSED in that partial log; the final XML gate
could not run successfully without completed XML. CI now stops after 20 failures
(without skipping any green-path coverage), streams its output, preserves the
pytest exit status and permits 60 minutes for the supported-platform suite.
The Mac fixture failures remain under investigation.

**Read from code / private foundations, not public cutover:**
- `task_queries.py` supplies fleet-scoped canonical reads and recovery hints.
- `request_receipts.py` retains scoped IDs/digests/outcomes; `request_facts.py`
  compares committed rows using ingest's existing projection. Neither is a
  second task-state store or a transport replay queue.
- `task_operations.py` adds admission, assignment and assigned-bot acceptance
  through request-then-task locks and committed-only ingestion. Delivery is
  deliberately a separate, still-unimplemented operation.
- The daemon exposes its serving identity and a bounded, explicitly reviewed
  drain through the existing ingest/spool owners. A drain response alone does
  not prove the host is quiescent or ready to activate.
- Migration manifests bind observed retained task/receipt formats and exact
  receipt files; recovery checks every retained version.

**Measured local limits:** receipt codec 4 passed; operational-format/migration
checks 59 passed, followed by 18 after the recovery-caller integration; native
start/readiness collaborators 143 passed. These runs overlap and must not be
summed. The task-operation slice has 2 passed and 5 deselected locally; its five
real-ingestion cases, the new exact-fact proof cases and daemon runtime cases
await hosted execution because local Pydantic native loading stalls. AST checks
are syntax evidence only. Cold bootstrap coordination is still being integrated,
including durable Linux enablement. No new public task API, production change,
actual-agent canary, upgrade/recovery acceptance or Pi latency result is claimed.

## Integration checkpoint — 2026-09-28 08:53 UTC

**Measured:** #1932 at `d8706d16` passed 6,138 tests, with 12 skipped and
three warnings ([Linux job](https://github.com/Claudfather/Claudlobby/actions/runs/36396017194/job/108842476433)).
#1933 at `e2390a67` passed 6,141 tests on Python 3.11; Python 3.10 had one
fixture failure assuming `sqlite_errorcode` exists. Its Mac run stopped at the
20-failure limit (18 failed, two errors, 4,296 passed, 33 skipped), exposing
platform-specific fixture assumptions and an unbounded version probe.

**Measured:** #1934 at `783f7c92` remains RED:
[Linux 3.11](https://github.com/Claudfather/Claudlobby/actions/runs/36397351950/job/108846781273)
had one failure, 6,282 passed and 15 skipped;
[Linux 3.10](https://github.com/Claudfather/Claudlobby/actions/runs/36397351950/job/108846781578)
had two failures, 6,281 passed and 15 skipped. Both had four warnings. The
new committed task/fact and controlled-drain cases ran; the drain case found a
real defect: a file at a queue directory path was classified as empty. Queue
inventory now refuses wrong/redirected nodes without changing generic source
reader semantics. The additional Python 3.10 assertion is corrected.
The [Mac run](https://github.com/Claudfather/Claudlobby/actions/runs/36397351950/job/108846781535)
stopped with 18 failed, two errors, 4,370 passed and 33 skipped; later cases did
not run. Repairs select Linux explicitly for systemd fixtures, use a short
private tmux socket path, avoid duplicate process-group kills, and exercise
mktemp failures without assuming GNU behavior. CI now supplies GNU timeout on
macOS; version measurement refuses if no bounded runner exists. Reverification
on the next integrated head is required.

**Read from code:** cold bootstrap now orders empty-host proof, migration,
selection, journaled configuration/unit publication, ingest readiness, committed
registry seeding, exact identity binding, serial manager/worker startup and
producer enablement. It is an internal cold-only coordinator, not upgrade or
recovery support. Linux persistent enablement uses owned journaled links.
Task withdrawal/reassignment and linked assignment reports reuse the same
request/task locks and atomic ingest. Reports freeze manager/message IDs and
leave notification pending. The shared report reader preserves typed repeated
evidence and explicit capture states. No public message/task cutover has landed.

**Measured local limits:** 46 bootstrap/configuration/migration collaborator
checks passed; 11 registry controls passed with an explicit emit collaborator;
8 linked-report/codec checks passed; 10 report-reader/codec checks passed using
real SQLite but no ingest; 24 queue/source-state checks passed. These overlapping
selections are not additive. One native mktemp control passed before the
next owned stub stalled; the bounded runner reaped it. No local fallback-rescue
or full native-suite pass is claimed. New real-ingest report/registry and actual
native activation acceptance remain hosted/canary work.

## CI repair checkpoint — 2026-09-28 10:10 UTC

**Measured:** integration `517d0749` passed both Linux lanes with 6,337 passed,
15 skipped and four warnings: [Python 3.11](https://github.com/Claudfather/Claudlobby/actions/runs/36403947539/job/108868069608)
in 924.16s and [Python 3.10](https://github.com/Claudfather/Claudlobby/actions/runs/36403947539/job/108868069257)
in 1,189.72s. Its [Mac run](https://github.com/Claudfather/Claudlobby/actions/runs/36403947539/job/108868069547)
completed the selected suite with 11 failed, 6,304 passed, 37 skipped and four
warnings in 1,714.98s. Eighteen of the previous 20 failed cases now pass; two
updater failures remain and nine failures are newly observed. All seven
dedicated native launchd/bridge cases executed and passed in the retained XML.

**Measured:** prerequisite `6e876c96` passed both Linux lanes with 6,142 passed,
15 skipped and three warnings. Its [Mac run](https://github.com/Claudfather/Claudlobby/actions/runs/36404307850/job/108869238738)
completed with the identical 11 failed test IDs, 6,109 passed, 37 skipped and
three warnings in 1,922.40s. Neither revision has platform acceptance.

**Read from code / focused evidence:** Sol-authored repair `462209d9` removes
GNU-only vault canonicalization by using the existing Python decider, handles
an expected executable-lookup miss inside its substitution on Bash 3.2, and
repairs native harness fixtures. The manager fixture drains its tty, the
foreign-poller fixture creates its owned process group through Python, and
existing-path canonicalization works on BSD. The cold-route check requires an
explicit disclosure for either supported fallback route. No tests are skipped.

**Measured private limits:** the real Bash error-handler probe preserves the
missing lookup's empty output and rc 0 while removing its false `script_error`;
a present lookup still resolves without an error event. A private tmux probe
retained 9 of 40 notices with the sleeping fixture and all 40 with the draining
fixture. Private vault-hook deny/allow controls and parent syntax/AST/diff checks
passed. These focused probes do not establish a full-suite pass or actual-agent
acceptance. Local complete dependency environments remain unreliable; the new
exact-head hosted matrix is required. Feature expansion stays held until green.

## Supported-platform gate cleared — 2026-09-28 10:43 UTC

**Measured:** prerequisite #1933 at `462209d98f8e832959afe123005aa614cdd72046`
passed Linux 3.11 and 3.10 (6,143 passed, 15 skipped, three warnings per lane),
and macOS 3.11 (6,121 passed, 37 skipped, three warnings):
[complete run](https://github.com/Claudfather/Claudlobby/actions/runs/36408134459).
All 11 previously failing macOS cases now pass by class/name comparison of
retained JUnit results. All seven native launchd/bridge checks executed and
passed. Real offline release assembly and installed planning passed on Linux.

**Measured:** integration #1934 at `2d99fe9fc0e649bacb3765bf89e9645cae162654`
passed [Linux 3.11](https://github.com/Claudfather/Claudlobby/actions/runs/36408144875/job/108881679927)
(6,338 passed, 15 skipped, four warnings, 876.87s),
[Linux 3.10](https://github.com/Claudfather/Claudlobby/actions/runs/36408144875/job/108881680166)
(the same counts, 1,242.52s), and
[macOS 3.11](https://github.com/Claudfather/Claudlobby/actions/runs/36408144875/job/108881680160)
(6,316 passed, 37 skipped, four warnings, 1,849.09s). The seven native checks
executed and passed; release assembly and both conformance checks also passed.
These are per-platform results, not additive coverage counts. No new exclusion
was added to clear this gate. The feature hold is lifted on this evidence.

**Read from code / next candidate, not yet hosted validation:** the preserved
work adds sealed active configuration, cold-only `host activate` and recorded
`host status`, scoped message/receipt/reply reads, and a bounded single-attempt
native transport adapter. Active routing must keep the activated manager and
project policy when authoring files change or disappear; missing/corrupt sealed
inputs and redirected fleet directories refuse. Host commands retain explicit
operator/ancestry gates and distinguish recorded state from runtime observation.
Message reads reuse the delivery proof query and reject wrong-fleet receipts;
transport preserves the native chunk owner and reports uncertain sends without
automatic retry. These foundations do not expose the full task/message API.

**Limits:** syntax/diff checks and earlier private collaborator runs are not
acceptance of this new candidate. Its hosted Linux/macOS matrix remains required.
The operator Mac's native dependency/interpreter stalls remain unresolved; no
further local interpreter workaround is planned. Actual-agent canaries,
upgrade/recovery, O1 public sends/alerts, caller/skill/grant cutover and normal-load
Pi timing remain outstanding. Production activation has not been performed.

## Frozen-input CI repair — 2026-09-28 11:15 UTC

**Measured:** candidate `0edbe2177feca2e2535efff218d5add57105155d` failed
[Linux 3.11](https://github.com/Claudfather/Claudlobby/actions/runs/36411869941/job/108893728091)
and [Linux 3.10](https://github.com/Claudfather/Claudlobby/actions/runs/36411869941/job/108893728414)
with one failure, 6,361 passed, 15 skipped and four warnings in each lane.
The sole failure was `test_no_unguarded_raw_source_reads`, identifying the
frozen fleet/project YAML parse sites. Both conformance checks passed; macOS
was still running at this checkpoint. Further feature integration is held.

**Read from code:** staging previously parsed mutable authoring separately
from retaining its input bytes. A temporary edit during that parse, restored
before the next fingerprint, could make the validated/rendered manager differ
from the manager retained for active routing. Staging now parses the exact
captured bytes, validates/renders that Context, and seals those same digests.
The source-safety inventory registers only these two reviewed parser sites.

**Measured:** the existing AST tripwire, executed with stdlib dependencies,
fails on the parent with two new sites and passes on the repair with none.
Parent AST/diff checks pass. The new regression checks retained bytes,
validated manager and rendered manager together; its runtime parent/candidate
outcome is **not measured locally**. Hosted execution remains required.
The additional identity/index commits and operator/read/alert work are held
in the separate feature checkout and are not part of this repair publication.

## Operations candidate — 2026-09-28 12:00 UTC

**Measured:** repair #1934 at `296c5e1989c64c4c1ac500a1a0e817978aacf24f`
passed [Linux 3.11](https://github.com/Claudfather/Claudlobby/actions/runs/36414595473/job/108902571220)
and [Linux 3.10](https://github.com/Claudfather/Claudlobby/actions/runs/36414595473/job/108902571034)
with 6,363 passed, 15 skipped and four warnings per lane, and
[macOS 3.11](https://github.com/Claudfather/Claudlobby/actions/runs/36414595473/job/108902571284)
with 6,341 passed, 37 skipped and four warnings. All seven native macOS checks,
both conformance checks, the source-safety tripwire and the retained-input
regression passed. The previous macOS `0edbe217` run also completed with the
same sole source-safety failure as Linux. The repair gate is now clear.

**Read from code, awaiting candidate CI:** this separate operations draft
retains activation-verified identities, exposes canonical task reads and
admit/assign/accept commands, registers a cold local human through committed
ingest, and keeps `--by` provenance separate from caller authority. Check-in
assignment links commit atomically and their reader scopes current events by
recorded fleet identity. Wrong-kind references retain the scoped canonical
recovery hint; no legacy argument is silently converted or acted on.

**Read from code, awaiting candidate CI:** message routes use sealed active
configuration and retained identities, including same-host cross-fleet targets.
Version-1 request receipts reserve each native attempt before sending and
retain its nonsecret observation before transmission-fact recording. Submitted
attempts cannot authorize another send. This is the unshipped receipt format,
not a compatibility decoder. The recording-alert adapter uses existing carriers
and debounce behavior under an owned kernel lock, with bounded waits and no
Plane callbacks. Unusable debounce state is disclosed, never promised durable.
The public O1 send owner is still pending; these pieces alone do not deliver it.

**Measured narrow evidence:** an isolated receipt-owner unit export passed
seven tests in 0.23s, including interrupted persistence and explicit retry.
An exact-files stdlib-only CLI smoke passed task/assignment help and structured
syntax refusal. AST/native-script syntax and diff checks passed; Ruff found no
new findings against the parent (15 inherited findings in two existing files).
These checks do not exercise Pydantic, real activation or native carriers.

**Measured query evidence:** the actual old check-in join missed a human
dispatcher and included a local-looking alias with a foreign recorded fleet;
the candidate returns the human and historical null-fleet rows while excluding
the foreign row. The direct-reply query over a private 4,096-unrelated-row fixture
required 20,536 SQLite VM steps before the partial reply index and 68 after it,
with the same result. These are query-work measurements, not Pi latency.
Migration 13 adds that index and preserves existing rows/IDs; rollback after
new writes requires a schema-compatible release, not a schema-12 binary.

**Limits:** the new full matrix remains required. Local native/dependency stalls
are unresolved. Upgrade/recovery, ordinary sends and replies, reporting,
caller/skill/grant cutover, remaining operations, actual-agent canary acceptance
and normal-load Pi timing remain incomplete. No production activation occurred.
