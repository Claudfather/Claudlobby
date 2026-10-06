---
title: Same-Identity Comment Fallback
description: GitHub blocks approve/request-changes when reviewer shares identity with author — fall back to a verdict-bearing comment.
---

# Same-Identity Comment Fallback

When the fleet shares a single GitHub PAT (every bot commits as the same identity), the GitHub API rejects `gh pr review --approve` and `gh pr review --request-changes` on PRs the same identity authored. A verdict you couldn't post is not a verdict.

**Fallback:**

1. If `--approve` or `--request-changes` returns "Can not approve your own pull request" or equivalent — post a `COMMENT` review instead, through the API, because that call returns the review's URL:

   ```bash
   jq -Rs '{event: "COMMENT", body: .}' verdict.md > review.json
   gh api -X POST repos/OWNER/REPO/pulls/N/reviews --input review.json --jq .html_url
   ```

   Run them as two separate commands. The at-mention guard reads `review.json` before the post runs, so it refuses a file the same command writes, one that does not exist yet, `--input -`, and a body that would @-mention anyone (#1019). On success the post prints `https://github.com/OWNER/REPO/pull/N#pullrequestreview-…`. A nonzero exit means nothing was posted, and what it printed is gh's error, not a URL. Build the body as JSON with jq and `--input`, never `-F body=@<file>`, which can post the file's name instead of its text (#2178).
2. Lead the comment body with the verdict: `**[<bot>] [VERDICT] approve** — reviewed at <sha>`, `**[<bot>] [VERDICT] request changes** — reviewed at <sha>`, or `**Comment**` (no verdict) — leave a plain `**Comment**` bare, never bracket-tag it: `claudlobby task reviews` treats an unrecognized bracket-tagged word as vocabulary drift, not as a neutral note. Record the review-role fleet report for authoritative actor attribution, and pass the review's URL as `--artifact`:

   ```bash
   claudlobby --json assignment complete ASSIGNMENT_ID --summary "Approve on #N; detail in the review" --pr https://github.com/OWNER/REPO/pull/N --pr-role reviewed --artifact https://github.com/OWNER/REPO/pull/N#pullrequestreview-123 --request-id UUID
   ```

   `task reviews` joins a verdict to the report that names its URL, however long the write-up took. A report that names none is matched only by time, from 10 s before the verdict to 120 s after it, which misses a careful review, a corrected verdict and two reviewers posting seconds apart. A corrected verdict is a new review, so it needs its own report naming its own URL. A verdict posted as an issue comment passes its comment URL the same way: `gh api -X POST repos/OWNER/REPO/issues/N/comments --input comment.json --jq .html_url`, with `comment.json` built by `jq -Rs '{body: .}'`.
3. The verdict line is the contract — the merge gate reads it programmatically, and counts it only with its recorded review-role report: anyone can post a comment that starts with a verdict line.

**Do not:**

- Treat merging as a substitute for the verdict. The block is on the review *state*; it does not remove the obligation to post one.
- Silently skip the review. Always post the verdict, even as comment.
- Edit the PR description to record the verdict — comments are searchable; descriptions get rewritten.

**Why this matters:** the verdict is signal for whatever gates the merge — a person, or a manager acting under the fleet's merge guardrail — not for GitHub's branch-protection. That is why a comment carries the same signal as a formal review state: the gate reads the verdict line, not GitHub's approval field.
