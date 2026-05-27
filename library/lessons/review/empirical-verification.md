---
title: Empirical Verification of PR Claims
description: Reproduce locally before approving. "Tests pass" in the PR body is hearsay.
---

A PR description says "all tests pass." The CI status says green. Reviewers approve. Three days later, prod breaks on a path the tests didn't cover, and the postmortem is "the test passed in CI but not in prod."

**Empirical verification:**

- Check out the branch.
- Run the tests yourself, locally.
- Run the new code path with realistic input.
- If the PR claims "no regression," diff the output of the old and new versions on a representative input.
- Cite what you ran in the review verdict: "ran `pytest tests/test_<area>.py -v` against branch <hash> on <date>; all 47 pass; new test fails on `git stash` of the implementation."

**Why this matters even when CI is green:**

- CI runs on a sanitised fixture. Real input has shapes the fixture doesn't.
- CI may use a mock for an external service that prod hits live.
- CI may skip the slow test that's most likely to catch the bug.
- A test environment may differ from prod in ways the PR doesn't acknowledge (Python version, library version, env vars).

**For data PRs:**

- Run the model against a sample of recent prod data (not the test fixture).
- Compare row counts, key invariants, and a checksum across before/after.
- If the PR claims a perf improvement, measure: `query_history` before/after, EXPLAIN plans, warehouse credits.

**For bot-behavior PRs (claudlobby):**

- A change to lib/ scripts, hooks, protocols, guardrails, or composed `bot.conf` changes how a bot *behaves*, not just how it composes. Unit tests prove the env var lands in `bot.conf`; they do not prove the event fires, the alert sends, or the bot acts.
- Require the author to have run the **Deliver → Add config → Recompose → Observe** loop (see CLAUDE.md) and cited the observation: "ran `lib/validate-bot-change.sh` → `activity_stuck` + `overdue_dispatch` fired, manager notified." If the path needs a live bot, require a `spin-up-bot` + `data/events` observation.
- Treat "the composer test passes" as necessary but insufficient. The harness that runs the real sweep is what caught a latent `fleet-pulse.sh` sweep-abort no composer test could see.

The verdict is only as strong as what was actually verified. "Approved based on the PR description" is not a verdict.
