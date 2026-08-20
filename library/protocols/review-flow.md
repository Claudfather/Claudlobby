---
title: Review Flow
---

# Review Flow

For every PR:

1. Read the description. What problem? What behavior change?
2. Read the diff with that in mind. Does the code actually do what's claimed?
3. **Mutation-test the assertions.** If the PR claims "fixes bug X," mentally revert the fix — would tests still pass? Yes → tests are decoys.
4. Check for: scope creep, missing tests, dead code, naming clarity, error handling at boundaries.
5. Post a verdict comment with a first-line marker:
   - `**Verdict: Ship it**` — approve
   - `**Verdict: Mechanical fixes**` — small, obvious
   - `**Verdict: Request changes**` — substantive, must address
   - `**Verdict: Architectural concerns**` — flag manager + human

**Same-Identity GitHub Fallback** (when the fleet shares one PAT):

- GitHub blocks `--approve` and `--request-changes` on same-account PRs.
- Try `APPROVE` first; on failure fall back to `gh pr review --comment` with the verdict marker as the first line: `**Verdict: Ship it** (comment-only — same-identity blocks Approve)`. Manager greps the marker.
- Auto-merge on COMMENT-with-ship-it is valid. The COMMENT *is* the review under same-identity constraint.
- Goes away when the fleet graduates to **per-bot** GitHub Apps (#252). A fleet-scope App (App-auth #1270) does not lift it — every bot still commits as one shared `<slug>[bot]`.

**MCP gotcha — `get_pull_request_files` truncates at 30 files.** Returns only the first GitHub API page. PRs with > 30 files silently lose the rest.

Canonical full-file list: `gh pr view <NN> --json files --jq '.files[].path'`. No pagination ceiling. Treat the MCP tool as a small-PR convenience. If it returns exactly 30, assume truncation and re-fetch via `gh`.
