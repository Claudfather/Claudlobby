# Supported-platform testing

The default suite runs in three GitHub-hosted lanes: the existing `pytest`
(Ubuntu/Python 3.11), `supported (ubuntu-latest, py3.10)` (the declared Python
minimum), and `supported (macos-latest, py3.11)`. The matrix includes exactly
these additional two combinations and does not expand our support promise.
The only default-suite deselection remains the existing hook-command case
tracked by #229. Optional vault/model/auth tests retain their skip reasons.

Each added lane installs the package in a local virtual environment. Its log
and JUnit report are uploaded even on failure. Run targeted checks in an
isolated checkout with scratch HOME and Plane isolation first; this repository
can also be a live fleet installation, so running an unreviewed full suite in
that installation is unsafe.

Native macOS coverage uses `/bin/bash`, `plutil`, `ps`, and the native long-path
bridge process test. The launchd lifecycle test additionally requires both
`CLAUDLOBBY_CI_NATIVE_SMOKE=1` and `RUNNER_ENVIRONMENT=github-hosted`. A local Mac
skips service mutation by default. On the hosted runner it composes a plist
with a unique `claudlobby-ci-<random>` prefix and a scratch launcher, observes
its harmless process, and boots out only that job. Cleanup is asserted both
for normal completion and an assertion raised after startup. No fleet
installer, authenticated bot session, Telegram account, or live fleet is used.
An unavailable launchd domain is a failure in this opted-in lane. A JUnit
check also refuses missing, skipped, duplicate, or failed native observations,
including each no-pidfile, early-exit and post-readiness startup-cleanup case.

Bridge process startup owns cleanup from the moment it spawns: failure to
write readiness, an early exit, and failures after yielding all reap the
private process group. The group ID is the session leader's original PID,
which remains usable after the leader exits. The post-readiness case injects an
exception after the leaf has written its readiness PID but before the fixture
returns ownership to its caller.

The hosted macOS lane runs
`python tests/fixtures/native_ci_negative_controls.py --output native-controls`
concurrently with the full pytest suite, within the existing 30-minute job
limit. The suite step waits for both independent commands, even when either
fails, and fails if either exit status is nonzero. The full suite selection,
native JUnit evidence gate, and Linux execution remain unchanged. Separate
logs, completion markers and exit-code files are uploaded for both commands,
and their outcomes are printed in the job log. A marker still reading
`running` means the command did not record completion; cancellation is never
passing evidence. The controls remain children of the job shell and subject
to the runner's cancellation and timeout cleanup.

This command refuses local hosts and exports the exact committed revision into
an owned temporary directory. It first requires the four native bridge cases
to pass, then removes startup cleanup and changes the executable guard to read
the truncated `comm` column in that disposable export. Each mutant must fail
its specific regression assertion; skip, setup error, unrelated failure or an
unexpected pass fails the lane. The tests retain their final process-group
reap, the driver restores each changed file, and the four restored cases must
pass again. Logs, JUnit reports and a revision-stamped summary are uploaded.

The separate one-time workflow failure control changes only the success arm
of `test_native_launchd_scratch_lifecycle` to raise an assertion inside
`_native_scratch_job`. Push that temporary test commit to the PR, record the
failed macOS check and the intended assertion, then revert the control and
require a green final-head run. The context manager must still boot out its
unique job while the assertion propagates. Expected failures caught by the
bridge-control driver do not substitute for this actual failed PR check.

The checks must pass in the PR and then be selected as required checks in the
repository's default-branch ruleset. Adding jobs alone does not enforce that
merge policy. At implementation start, `Protect Main` required one approving
review and no status checks; enforcement of these three names is pending
operator approval. Preserve the original `pytest` name when updating rules.
Removing a lane later requires updating its required-check rule together with
the workflow so a nonexistent check does not block every merge.

Runner labels and availability are documented in
[GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Native observations establish utility and harmless service behavior; they do
not prove authenticated bot startup or fleet rollout.
