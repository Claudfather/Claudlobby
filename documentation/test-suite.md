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

*Know the baseline.* The suite is **not fully green** on macOS — as of 2026-08-01 it is
**34 failed / 2125 passed**, 24 of them the `tests/test_setup_backbone.py` cluster (#951). Do not
assume your change caused a failure, and do not assume it didn't because the *count* matched —
compare the failing test **names**:

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
   Re-read the baseline three paragraphs up: **34 failed / 2125 passed.** Our suite is not
   green, so `rc = 1` is the **normal, expected state of both the before run and the after
   run**. That makes the gate structurally incapable of telling a healthy 34-failure
   baseline from a 38-failure baseline-plus-four-regressions — both are `rc 1`. The gate can
   only ever discriminate *did not complete* (rc 2 / 5 / 127). On a suite that is already
   red, counts are not a hedge; they are **the** signal on the only axis that changes.

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

**CI's lanes: quarantine a flaky test, never deselect it.** Each test runs in exactly one CI
lane, chosen by its markers (pinned by `tests/test_ci_lanes.py`). `test.yml`'s `pytest` job
runs `-m "not quarantine and not harness"`; its `harness` job runs the validation harness
(`@pytest.mark.harness`) in parallel; and `quarantine.yml` runs every test marked
`@pytest.mark.quarantine(issue=<N>)`. Fix a flaky test when its cause is clear and small.
Otherwise quarantine it: it leaves the required lanes (`pytest`, `harness`, conformance's
`vault-tests`) but still runs on every PR and push to main, in a check nothing should require.
Collection refuses a quarantine that names no tracking issue. Never `--deselect` or `-k` a test
out of a workflow: a node id that stops matching deselects nothing and says so nowhere. The
suite still runs everything locally, and `pytest -m "not quarantine and not harness"` mirrors
the required `pytest` lane. A quarantined test that shows up in your before/after name diff is
a known flake, so rerun it on both arms before attributing it.
