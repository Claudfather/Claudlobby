# Supported-platform testing

The default suite runs in three GitHub-hosted lanes: the existing `pytest`
(Ubuntu/Python 3.11), `supported (ubuntu-latest, py3.10)` (the declared Python
minimum), and `supported (macos-latest, py3.11)`. The matrix includes exactly
these additional two combinations and does not expand our support promise.
The only default-suite deselection remains the existing hook-command case
tracked by #229. Optional vault/model/auth tests retain their skip reasons.

Every lane installs the package in a local virtual environment. Its log,
JUnit report (including skip reasons), completion marker and exit status are
uploaded even on failure. Run targeted checks in an
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

The one-time mutation and deliberate-failure checks used to validate this CI
setup are retained as historical evidence: the [intentional failure run](https://github.com/Claudfather/Claudlobby/actions/runs/36248906539)
failed on the injected assertion, and the [restored run](https://github.com/Claudfather/Claudlobby/actions/runs/36313493053)
passed all three lanes at `6ff6ddff4133e2d7dda01a69e3003f6e9dcf037d`.
The historical mutation driver remains available in that commit; it is not
part of recurring CI. Each current checkout still runs the complete suite,
including native cleanup regressions and the seven-case JUnit gate, within
the existing 30-minute supported-platform limit.

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

The unified-CLI integration reuses the final net change from
[#1859](https://github.com/Claudfather/Claudlobby/pull/1859) at
`476d33038ae97a2c89f3946467faad99aa785c8b`. Each lane also prepares the selected
editable package's resources and runs with private HOME, TMPDIR and XDG paths
with Plane emission disabled. Native composition fixtures explicitly select
their package resources and put their replacement launcher in a private native
directory. Historical upstream results above do not certify this integration;
its platform jobs must supply fresh evidence.
