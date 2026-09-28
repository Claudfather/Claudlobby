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
| P0 evidence safety | CI running | #1846 integrated locally; private HOME/XDG/temp and executable-origin changes in draft #1928 (current `af1cc44d`). First Linux run passed all 14 isolation cases but failed on private-env placement/socket length; corrected, fresh pytest pending, two conformance jobs green. |
| P1 command loading/package | Partial validation | Lazy argparse dispatch and canonical resource packaging in draft #1929 (current `c2b8188a`). Initial CI collection found two main-added recording tests using retired fixtures and a wrong test import; corrected. Bootstrap and artifact assertions passed in isolated exports; normal pytest and host acceptance remain outstanding. |
| P2 context/release | In progress | Explicit package/data/overlay foundation and caller migration underway; untested. Explicit fleet manager and native caller wiring implemented; fixture migration underway. Release assembly/activation and installed composition remain pending. |
| A0–A4 tasks/messages | Pending | Follow the plan's migration audit and ordered semantic cutover. |
| B lifecycle/setup | Pending | Reuse supervisor consolidation; verify affected native platforms. |
| C coordination/jobs/plane | Pending | Migrate complete operation/caller bundles. |
| D completion | Pending | Retired-door scan, full operation coverage, cold onboarding, adoption and acceptance evidence. |

Tracking limitation: Linear team configuration is unavailable; no ticket was
created in an unverified team. GitHub #1747 and this record track the work.

## Evidence so far

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
  #1846 and the P0 change. Its two new implementation commits are `a2a654e2`
  (loading) and `870010d1` (packaging); its published merge tree is identical to
  `870010d1`. Retarget after prerequisite reviews/merges; none is merged here.

Local execution artifacts are under `/tmp/claudlobby-cli-evidence-1747`:
`p1-parser-parity.json`, `p1-standalone/bootstrap-timing.json`, and
`p1-standalone-3/{bootstrap-results,artifact-result}.json`. Hosted CI and PR
descriptions must carry final results before a slice is presented as ready.

- Initial #1928 Linux suite: 5,798 passed, 12 failed, 13 skipped, 2 errors. The
  failures exposed environment directories inside fixture data and long Unix
  socket paths. Short independent private directories fix that shared cause;
  the amended suite must pass before this slice is ready.
- #1929 current head also carries that correction and migrates the newer
  pull-root/vault-hook recording tests to #1846's scratch fixture. Current-head
  conformance passes; full Linux pytest remains running.
- P2 uses `state/releases/` under the selected host data root as the canonical
  immutable release store. Code guards and composed isolation protect its real
  retained targets as well as the installed package assets. This is implementation
  in progress, not activation or canary evidence.

- A second hosted run exposed pytest lazily creating its session base inside a
  function-private TMPDIR that was subsequently removed (P1: 1 failed, 2,157
  passed, 12 skipped, 3,823 errors). Session initialization now owns that base.
  Exact fixture-only before/after probe: 1 failed + 1 error before, 2 passed
  after. Current P0/P1 full suites rerun; conformance passes.
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

- P0 latest completed full suite: 5,812 passed, 13 skipped, two teardown errors;
  P1: 5,973 passed, seven failed, 13 skipped, two teardown errors. Shared cleanup
  now belongs to the session after filesystem monkeypatches restore. Existing
  source-state suite proves the correction: 23 passed + two errors before,
  23 passed afterward. P1's seven failures were newer recording callers missing
  the scratch fixture; those callers are migrated. Current heads rerun in CI.
- P2 indexed disposable-export resource preparation passed: builds a private
  wheel and installs only its resource/native assets for editable test imports.
- After composition fixture repairs, 597 tests passed with one stale anchor-set
  assertion and two export-index setup errors; the assertion is corrected. Twelve
  subprocess-dependent cases were excluded from that scoped pure-Python run.
  The export-index cases require the normal indexed CI checkout. Full P2 CI,
  installed-composition smoke, native macOS, Pi and real-agent canary remain
  unverified. The source/resource/caller migration is deliberately one coherent
  breaking bundle; mechanical fixture changes dominate its file count.
