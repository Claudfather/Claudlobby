---
title: Verify every rollout
description: Every PR names, before it merges, the production check that proves it works. Once the change is live, the merger runs that check and posts PASS, FAIL or PENDING on the PR. A FAIL stops merges into that repo until a fix or revert lands.
---

# Verify every rollout

CI, a green deploy and a healthy service prove that a system still runs. None of them proves that a merged change does what it was merged to do, where it runs. This rule closes that gap (#2111).

**Where it sits.** `ab-gating-rollout` decides before the merge whether an effect claim may land. `canary-rollout` decides how a framework release rolls out, and proves that the rollout is safe. This rule proves that each merged change works once it is live. None of the three substitutes for another.

## Author: write the check before review

Every PR body has a `## Rollout check` section with four lines. `.github/pull_request_template.md` carries them, but `gh pr create --body` and `--body-file` skip the template, so write them yourself:

```markdown
## Rollout check
- **Observe:** what to read in production (a command, endpoint, job, event or record) and the exact value or shape expected
- **Control:** what the same check shows today, or on a case this change must not touch, so that a pass cannot be vacuous
- **When:** right after the change is live, or after a named live event (a timer run, real traffic), with the date by which it will have happened
- **Who:** any bot with the fleet's credentials, or operator only, and why
```

A change with nothing to observe in production carries one line instead: `N/A: docs-only` or `N/A: tests-only`. CI accepts it only when every changed path is under the docs or tests paths that the repo's caller file declares on its default branch.

## Reviewer

A missing or vacuous check is a request-changes item. CI fails these:

- a missing section;
- a section that is only in a fenced block or an HTML comment;
- an empty field, or one that says only `N/A`, `none` or `TBD`;
- an exemption that the changed paths do not bear out.

Only you can judge whether the check would actually prove the change.

**A PR that changes anything under `.github/workflows/` can rename or replace the job that checks it**, so its rollout check proves nothing for it. CI flags such a PR. Review the workflow change itself and name it in your verdict. Without that, the merger refuses.

## Merger: run the check once the change is live

1. **Wait for the target's own record.** Never infer it from page freshness or a green deploy job.
   - **Framework:** in `claudlobby --json host releases`, the selected activation is `active`, and `git merge-base --is-ancestor <merge sha> <source_revision>` holds for the selected release. Anything a bot reads at start, such as its composed CLAUDE.md or env, is live only after that bot restarts.
   - **Product:** the deploy provider's record for the merge commit says ready.
2. **Run the check and its control.**
3. **Post the verdict on the PR.** It is a comment headed `Rollout check: PASS`, `FAIL` or `PENDING`. It names the record that showed the commit live, and gives the command and the output of the check and of its control, redacted.

### PENDING: a task on the plane

When the commit is not live yet, or the live event has not happened, admit one task per check:

```bash
claudlobby --json task admit --title "Rollout check pending: <owner/repo>#<n>, <event>, by <date>" --repo <owner/repo> --body-file <file> --request-id UUID
```

1. **The body** carries the PR URL, the check, its control and the date. The manager's brief lists the task under open work until it closes.
2. **Leave it unassigned while it waits.** A bot-assigned task is re-asked after 48 hours, whatever its deadline.
3. **Assign it once the check can run**, with `task assign TASK_ID --bot <runner> --expected-by <RFC3339>`. After each activation, assign every pending task whose merge commit the new release contains.
4. **The assignee posts the verdict** on the PR and completes with `--artifact <verdict comment URL>`.

### Checks only the operator can run: one per message

For `Who: operator only`, escalate that check's task once the commit is live, with exactly one question:

```bash
claudlobby --json task escalate TASK_ID --question "<repo>#<n> rollout check: look at <X>. PASS looks like <Y>. Reply PASS or FAIL with what you saw." --request-id UUID
```

fleet-pulse pages each escalation once. One check per task and one question per escalation keep it to one item per message. When the answer comes, post it on the PR as the verdict, then close the task.

### FAIL: stop the merge train

The merge train is the queue of merges into one repo's default branch. A FAIL stops that repo's train; other repos keep merging.

1. **Post `Rollout check: FAIL`** with the evidence.
2. **Open the hold.** Open an issue in that repo labelled `rollout-hold` that cites the PR and the verdict. Create the label first if the repo has none. The issue is the recorded stop. The merge guardrails refuse every other merge into the repo while it is open.
3. **Tell the operator:** `claudlobby --json fleet notify --level alert --event rollout_check_failed --message "<repo>#<n>: <what failed>; hold #<issue>"`. This records a critical alert, which the brief shows, and tries the Telegram alert channel.
4. **Open the fix or the revert PR**, with `Closes #<hold issue>` in its body. Its merge lifts the hold, or the operator closes the issue. The fix then gets its own rollout check.

Rolling a framework release back is the operator's call, under `canary-rollout`. This rule does not repeat it.

## Adopting it in a repo

1. **Add the caller file.** Copy this repo's `.github/workflows/rollout-check.yml` and pin `uses:` to `Claudfather/Claudlobby/.github/workflows/verify-rollout.yml@<full commit sha>`. Keep the job id `rollout-check`: with the reusable job's name, it makes the check `rollout-check / Rollout check` in a PR's status rollup, which is the name the merge guardrails read.
2. **Set the paths.** Set `docs-paths` and `tests-paths` to what an exempt PR may change. They are read from the default branch, so a PR cannot widen its own exemption. Anything composed into a running system is not documentation, whatever its file type.
3. **Add the PR template.** Copy `.github/pull_request_template.md`.
4. **Add a required check where you can.** Where the repo's merges do not go through a ruleset bypass, also list `rollout-check / Rollout check` in a ruleset's `required_status_checks`.
