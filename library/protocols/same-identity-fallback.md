---
title: Same-Identity Comment Fallback
description: GitHub blocks approve/request-changes when reviewer shares identity with author — fall back to a verdict-bearing comment.
---

# Same-Identity Comment Fallback

A verdict you couldn't post is not a verdict.

**Fallback:**

1. If `--approve` or `--request-changes` returns "Can not approve your own pull request" or equivalent — post a `COMMENT` review instead, through the API, because that call returns the review's URL:

   ```bash
   jq -Rs '{event: "COMMENT", body: .}' verdict.md > review.json
   gh api -X POST repos/OWNER/REPO/pulls/N/reviews --input review.json --jq .html_url
   ```

   Run them as two separate commands, the post with nothing before or after `gh`. The at-mention guard reads `review.json` before the post runs, from the directory the command starts in, so it refuses a file the same command writes, one that does not exist yet, `--input -`, a path the shell builds as the command runs (`$VAR`, `~`, a glob), a relative path when anything else shares the command (a `cd` there would make gh send another file; pass an absolute path instead), and a body that would @-mention anyone (#1019). On success the post prints `https://github.com/OWNER/REPO/pull/N#pullrequestreview-…`. A nonzero exit means nothing was posted, and what it printed is gh's error, not a URL. Build the body as JSON with jq and `--input`, never `-F body=@<file>`, which can post the file's name instead of its text (#2178).
2. Lead the comment body with the verdict: `**[<bot>] [VERDICT] approve** — reviewed at <full sha>`, `**[<bot>] [VERDICT] request changes** — reviewed at <full sha>`, or `**Comment**` (no verdict) — leave a plain `**Comment**` bare, never bracket-tag it: `claudlobby task reviews` treats an unrecognized bracket-tagged word as vocabulary drift, not as a neutral note. Record the review-role fleet report for authoritative actor attribution, and pass the review's URL as `--artifact`, as `report-back` shows. A match by time alone misses a careful review, a corrected verdict and two reviewers posting seconds apart. A corrected verdict is a new review, so it needs its own report naming its own URL. A verdict posted as an issue comment passes its comment URL the same way: `gh api -X POST repos/OWNER/REPO/issues/N/comments --input comment.json --jq .html_url`, with `comment.json` built by `jq -Rs '{body: .}'`.
3. The verdict line is the contract — the merge gate reads it programmatically, and counts it only with its recorded review-role report: anyone can post a comment that starts with a verdict line.

**Do not:**

- Treat merging as a substitute for the verdict. The block is on the review *state*; it does not remove the obligation to post one.
- Silently skip the review. Always post the verdict, even as comment.
- Edit the PR description to record the verdict — comments are searchable; descriptions get rewritten.

**Why this matters:** the verdict is signal for whatever gates the merge — a person, or a manager acting under the fleet's merge guardrail — not for GitHub's branch-protection. That is why a comment carries the same signal as a formal review state: the gate reads the verdict line, not GitHub's approval field.
