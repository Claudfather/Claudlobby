# Unified CLI implementation record

Status: IN PROGRESS. Started 2026-09-28 UTC. Tracks #1747 and the design in
[PR #1925](https://github.com/Claudfather/Claudlobby/pull/1925), revision
`1a403962e0845e07e1c9a89b8679f3f1b51addca`.

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
| A0–A4 tasks/messages | Read model/audit foundation implemented; mutation/delivery pending | A0 audit and A1 typed task reducer share legacy/new closure semantics; 31 focused checks and 22 legacy report comparisons passed. Existing ingest has a committed-only mode. Request receipts, typed mutations, messaging/reports and caller cutover remain pending. |
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
