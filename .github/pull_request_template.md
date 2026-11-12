<!-- What changed, why, and how it was verified. -->

<!-- Changelog: add changelog.d/<head branch, each / as ->.md (changelog.d/README.md), or write the line "Changelog: none — <why>". -->

## Harness
<!-- What ran for this PR before review: library/guardrails/verify-rollout.md, and harness/CLAUDE.md for the instruments. Fill in either line or both. A change that no CI scenario, instrument or live test exercises replaces them with one line, N/A: and why none applies. -->
- **CI:** <!-- what CI ran that exercises this change: the harness job's scenarios, and any test that runs an instrument -->
- **Live:** <!-- each instrument or live test run for this PR: the command, where it ran, and its result -->

## Rollout check
<!-- What proves this change works once it is live: library/guardrails/verify-rollout.md. Fill in all four lines. A change with nothing to observe in production replaces them with one line, N/A: docs-only or N/A: tests-only, which CI checks against the paths the PR changes. -->
- **Observe:** <!-- what to read in production (a command, endpoint, job, event or record) and the exact value or shape expected -->
- **Control:** <!-- what the same check shows today, or on a case this change must not touch, so that a pass cannot be vacuous -->
- **When:** <!-- right after the change is live, or after a named live event (a timer run, real traffic), with the date by which it will have happened -->
- **Who:** <!-- any bot with the fleet's credentials, or operator only, and why -->
