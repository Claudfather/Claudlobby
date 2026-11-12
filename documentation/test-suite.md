# Running the test suite

How to get a result you can trust out of `./.venv/bin/pytest`. Moved from the root `CLAUDE.md`
in #2035, which keeps a short summary; the rules are unchanged.

**Plane test isolation.** Run tests from a disposable checkout with its own
editable-install venv and scratch HOME; never from a live fleet root. Pytest's
session and function defaults silence incidental emission, and both shared
child-env builders do the same. New subprocess tests use `constructed_env`;
intentional recording uses `constructed_env(**scratch_plane_env(root))`, with a
pytest-owned root and private socket. Direct Python database writers still need
explicit scratch roots: the silencer is not a database access control. See
[`documentation/testing-plane-isolation.md`](testing-plane-isolation.md)
for recording, standalone-shell, and census conventions.

**Build test resources in the disposable export.** After its editable install, run `.venv/bin/python tests/prepare_resources.py --disposable-checkout "$PWD"`. Index a history-free export with `git init --quiet && git add --all` first. Reprepare after source changes. Compare separate prepared before/after exports; a stash/pop in one prepared tree leaves stale assets and is not a valid comparison.

**Three things about the test suite that will otherwise cost you an hour.**

*Run it unsandboxed — and do not diff sandboxed runs either.* `claudlobby/_runtime_scripts/` scripts call `mktemp -d`
into the real `$TMPDIR`. Under a restrictive agent sandbox those calls return `Operation not
permitted` and roughly **250 phantom failures** appear across every bash-script suite. They are
not real.

It is tempting to assume a *diff* of two sandboxed runs is still sound, since the sandbox
penalises both equally. **It is not.** A test the sandbox already breaks fails in the before run
*and* the after run, so it cancels out of the diff — and any regression you introduce inside it
is invisible. That is not hypothetical: it is exactly how a `claudlobby_cli` regression reached
CI in #947, after a sandboxed diff reported one clean delta. Take the baseline unsandboxed or
not at all.

*Know the baseline, and measure it.* On 2026-10-25 the whole suite was **green** on macOS:
`10294 passed, 116 skipped`, rc 0, at `c7529c7`. That was a full unsandboxed run of a prepared
export on Python 3.11, #2517's before run. It has been red before: 34 failed / 2125 passed on
2026-08-01, 24 of them the `tests/test_setup_backbone.py` cluster (#951). It can go red again,
so take the baseline from a fresh before run, never from this paragraph. Do not assume your
change caused a failure, and do not assume it didn't because the *count* matched — compare the
failing test **names**:

Prepare two separate disposable exports, one at the base commit and one with
the candidate bytes. Give each its own editable-install venv, run
`tests/prepare_resources.py --disposable-checkout "$PWD"` in each, then compare
the failing test names and counts. Reusing one tree with `git stash -u` removes
the prepared assets from the before run and hides regressions.

*An empty diff is not the same as a clean one.* The naive `pytest | grep ^FAILED` this
recipe used to print was wrong in **two independent directions**, and it could also fail
*as a mechanism* while looking fine. **Three checks, all load-bearing, each covering what
the other two cannot** (#1035). Drop any one and a live hole reopens.

| rc | situation | what the naive check did |
|---|---|---|
| 0 / 1 | passed / real failures | missed every failure — `addopts = "-rs"` **replaces** pytest's default `fE` reportchars (`-r` is store, not append), so no `FAILED` line is ever printed |
| 2 | collection error, suite aborted | printed `ERROR`, not `FAILED` — and the count line reads `1 error` rather than `N failed, M passed`, so counts do not catch this one either; the rc gate does |
| 4 | bad path or flag | **invented a failure** — `ERROR: file or directory not found:` matches, so a typo in the *after* run fabricates a regression |
| 5 | nothing collected | nothing to match — reads clean |
| 127 | no `.venv` (e.g. run from the shared install, not your checkout) | nothing to match — reads clean |

1. **The `rc` gate — catches *the run did not complete*.** `rc` not in {0,1} → the diff is
   not evidence. Only this sees rc 2 / 5 / 127, where zero or partial tests ran. Scoping
   cannot: those emit no summary block to scope to.
2. **Scoping to the summary block — catches *phantoms inside the evidence band*.** Captured
   logs and `log_cli` output appear *above* that banner; only pytest's verdict lines appear
   inside it. Only this sees the rc 0 / rc 1 phantoms; the gate cannot, because they exit
   inside the band it certifies. Also makes `--tb=no`, verbosity and `log_cli` irrelevant
   by construction.
3. **The count line — catches *how many*, the only axis that moves on an already-red suite.**
   When the before run is red, `rc = 1` is the **normal state of both the before run and the
   after run**. The gate then cannot tell a healthy 34-failure baseline (2026-08-01's) from a
   38-failure baseline plus four regressions: both are `rc 1`. It can only discriminate *did
   not complete* (rc 2 / 5 / 127). On a suite that is already red, counts are not a hedge;
   they are **the** signal on the only axis that changes. When the before run is green,
   `rc 1` in the after run is a regression signal by itself, but read the names and counts
   anyway.

   They are also the last survivor when the names mechanism itself breaks — which is exactly
   what #1012 did here. Measured on this suite: `1 failed, 20 passed` printed correctly
   while `grep "^FAILED"` returned **0 lines** and no summary banner was emitted at all.
   Names dead, `rc` still 1, diff lands inside the evidence band and reads clean. **A count
   change with an empty name diff is evidence the names mechanism is broken, not a clean
   run.** Counts are a cross-check rather than a substitute — they miss rc 2, where the gate
   covers. Ravi validated counts-plus-names on ai-platform under this same bug, catching
   **four** real regressions the old recipe called clean; clog established the
   baseline-is-red reasoning.

**Never pipe pytest into grep.** You would capture *grep's* status, which is only ever 0 or
1 — every broken mode laundered into "this is evidence" and the gate passes silently.
Redirect, read `$?`, then grep the file. Same `${PIPESTATUS[0]}` trap as
`gh api ... | head`.

**CI's lanes: quarantine a flaky test, never deselect it.** On each platform, each test runs in
exactly one CI lane, chosen by its markers (pinned by `tests/test_ci_lanes.py`). On a pull
request, `test.yml` runs `-m "not quarantine and not harness"` on each supported platform, Linux
with Python 3.10 and macOS with Python 3.11, in shards (below); a `harness` job runs the
validation harness (`@pytest.mark.harness`) beside them, on Linux with Python 3.11 and on macOS;
and `quarantine.yml` runs every test marked `@pytest.mark.quarantine(issue=<N>)`. The `pytest`
job runs the shards' selection on Linux with Python 3.11, on push to main only. The `[vault]`
tests skip in the shards and in `pytest`, and run in conformance's `vault-tests`. Fix a flaky
test when its cause is clear and small. Otherwise quarantine it: it leaves the required lanes
(the shards, both `harness` jobs, `pytest`, conformance's `vault-tests`) but still runs on every
PR and push to main, in a check nothing should require. Collection refuses a quarantine that
names no tracking issue. Never `--deselect` or `-k` a test out of a workflow: a node id that
stops matching deselects nothing and says so nowhere. The suite still runs everything locally,
and `pytest -m "not quarantine and not harness"` mirrors the required lanes. A quarantined test
that shows up in your before/after name diff is a known flake, so rerun it on both arms before
attributing it.

**CI's shards.** Each supported platform's suite runs in `TEST_SPLITS` shard jobs (`test.yml`),
`supported (ubuntu-latest, py3.10) shard 1` to `shard 4` and the same for macOS, and pytest-split
hands each one a share by the recorded times in `.test_durations`. Each shard first lists the
whole suite it collected, in the step and environment it runs it in. The platform's gate job
carries its check name, `supported (ubuntu-latest, py3.10)` or `supported (macos-latest,
py3.11)`, and passes only when every shard passed, every shard listed the same suite, the shards'
JUnit reports hold each listed test exactly once, and the platform skipped no more tests,
counted across all its shards, than its ceiling in `tests/ci_shards.py` (`SKIP_CEILINGS`). A
change that adds a skip on a platform raises its ceiling in the same change and says why. A test
id must not change between runs: a parametrize value that embeds something random (a fresh
uuid, the time) needs explicit `ids=`, or the shards' listings disagree. To run what one shard
ran, with the `dev` extra installed: `PYTHONHASHSEED=0 pytest -m "not quarantine and not
harness" --splits 4 --group 3`.

**Refreshing `.test_durations`.** A test missing from it gets the average time, so stale times
make the shards uneven but never skip a test. Its times are macOS's: the slowest platform's
shards decide how long a pull request waits. Judge drift over several green runs, not one:
runner speed alone moves a shard a long way (with the same tests, one macOS shard took 10
minutes in one run and 16 in the next). When the macOS shards stay more than about a quarter
apart run after run, take the `test-durations` artifact of a green `tests` run (the newest on
main, say) and commit it at the repository root: `rm .test_durations`, since `gh run download`
will not overwrite it, then `gh run download <run-id> -n test-durations`.
