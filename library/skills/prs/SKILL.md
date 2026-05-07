---
name: prs
description: "Use when the user asks about pull requests, code reviews, or wants an overview of PR activity across repos. Shows authored PRs, review requests, and CI status."
argument-hint: "[mine|review|<repo-name>] [--personal]"
---

# PRs

Unified pull request overview across all GitHub repos you care about.

## Tools

| Tool | Purpose |
|------|---------|
| `mcp__github__list_pull_requests` | List PRs for a repo |
| `mcp__github__get_pull_request` | Get PR details |
| `mcp__github__get_pull_request_status` | Get CI/check status |
| `mcp__github__get_pull_request_reviews` | Get review status |
| `mcp__github__get_pull_request_comments` | Get review comments |
| `mcp__github__get_pull_request_files` | Get changed files |

## Repos to Check

**Default: Work repos only (`<FLEET_ORG>` org):**
`<FLEET_REPOS>` *(populate with your repo list, e.g. `repo-a, repo-b, repo-c`)*

**With `--personal` flag, also check (`<USER_GITHUB>`):**
`<PERSONAL_REPOS>` *(populate with your personal repo list, or leave empty if the fleet only touches the org)*

Only check personal repos when `--personal` is explicitly passed. This avoids GitHub API rate limits and keeps the fleet focused on its scope.

## Operations

### 1. Overview (default)

Check for PRs authored by `<USER_GITHUB>` and PRs requesting review across all repos. Run repo checks in parallel using multiple tool calls.

For each repo, call:
```
mcp__github__list_pull_requests
owner: "<FLEET_ORG>" (or "<USER_GITHUB>" for personal)
repo: "<repo-name>"
state: "open"
```

Then categorize results:

**Needs your action:**
- PRs with changes requested on your authored PRs
- PRs where you're requested as reviewer
- PRs with failing CI

**Waiting on others:**
- Your PRs awaiting review
- Your PRs with CI running

**Recently merged:**
- Your PRs merged in last 24h

### 2. My PRs

Filter to only PRs authored by `<USER_GITHUB>`:
```
mcp__github__list_pull_requests
owner: "<FLEET_ORG>"
repo: "<repo>"
state: "open"
```
Then filter results where author is `<USER_GITHUB>`.

### 3. Review Requests

PRs where review is requested from `<USER_GITHUB>`.

### 4. Specific Repo

When user says `/prs <repo>` — check only that repo:
```
mcp__github__list_pull_requests
owner: "<FLEET_ORG>"
repo: "<repo>"
state: "open"
```

### 5. PR Details

When user asks about a specific PR number:
```
mcp__github__get_pull_request
mcp__github__get_pull_request_status
mcp__github__get_pull_request_reviews
```
Run all three in parallel, then summarize.

## Output Formatting

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

```
PRS OVERVIEW

Needs your action:
- <repo-a> #NNN — changes requested by @reviewer
- <repo-b> #NNN — CI failing (test_name)
- <repo-c> #NNN — review requested

Waiting on others:
- <repo-a> #NNN — awaiting review (opened 2h ago)

Merged today:
- <repo-a> #NNN — <title> (merged 4h ago)

No open PRs: <list of quiet repos>
```

## Instructions

1. For **overview**: check all repos in parallel, group results by action needed
2. For **specific repo**: show all open PRs with status
3. Skip repos with no open PRs — just list them at the bottom
4. Always show CI status (passing/failing/pending) for open PRs
5. Prioritize PRs needing action at the top
6. When checking work repos, owner is `<FLEET_ORG>`. For personal repos, owner is `<USER_GITHUB>`

$ARGUMENTS
