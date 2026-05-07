# Advanced Patterns

Patterns that extend a running claudlobby fleet beyond basic dispatch and briefings. Each section is self-contained — implement whichever ones fit your setup.

Prerequisites: a working fleet with at least a manager bot and one worker, systemd services, tmux sessions, and the shared `bot-common/` scripts. See the main [README](../README.md) if you're not there yet.

---

## 1. Lifecycle Orchestration (/lifecycle)

A full development pipeline managed by the manager bot: implement, review, iterate, merge, retro, create issues. One slash command kicks off the entire cycle.

### Why

Without this, you manually dispatch the engineer, wait, dispatch the reviewer, wait, read the review, decide what to do, merge, and forget to capture learnings. `/lifecycle` automates the entire chain and only pulls you in when a human judgment call is needed.

### Flow

```
/lifecycle "Add rate limiting to the auth endpoint" --repo org/api --eng eng-a-bot --reviewer code-reviewer-bot

  1. Manager dispatches engineer via tmux
  2. Engineer implements, creates PR, reports back
  3. Manager dispatches code reviewer to the PR
  4. Reviewer posts review:
     a. Approved          → Manager merges the PR
     b. Mechanical fixes  → Manager sends back to engineer automatically
     c. Ambiguous concern → Manager flags the human via Telegram
  5. After merge: Manager runs /development-retro
  6. Manager creates GitHub issues for retro findings
  7. If 3+ review cycles on the same PR → flag human regardless
```

### SKILL.md

```markdown
---
name: lifecycle
description: "Full dev pipeline: implement → review → iterate → merge → retro → issues. Flags human only when needed."
argument-hint: "<task description> --repo <owner/repo> --eng <engineer-bot> --reviewer <reviewer-bot>"
---

# Lifecycle

Orchestrate a complete development cycle.

## Steps

1. **Dispatch engineer**
   - Validate bot is alive: `tmux has-session -t <eng-bot>`
   - Send task: `tmux send-keys -t <eng-bot> '<prompt>' Enter`
   - Prompt must include: task description, target repo, instruction to create PR and report back

2. **Wait for engineer report**
   - Watch for `[BOTREPORT] <eng-bot> | completed | ...` in your tmux pane
   - Extract PR URL from `pr:<url>` field
   - If status is `failed` or `blocked`, notify user and stop

3. **Dispatch code reviewer**
   - Send: `tmux send-keys -t <reviewer-bot> 'Review PR at <url>. Use /review-pr.' Enter`
   - Wait for reviewer report

4. **Route the review**
   - **Approved:** Merge the PR via GitHub MCP. Post confirmation to Telegram.
   - **Mechanical fixes** (formatting, naming, missing tests — things with clear right answers):
     Send engineer back with specific instructions from the review.
   - **Ambiguous concerns** (architecture questions, design tradeoffs, scope debates):
     Post to Telegram: "Review of PR #N raised concerns that need human input: [summary]"
   - Parse this from the reviewer's report or read the PR review comments via GitHub MCP.

5. **Cycle guard**
   - Track review round count. If this is round 3+, flag the human:
     "PR #N has been through 3 review cycles. Intervening."

6. **Post-merge retro**
   - After merge, run `/development-retro` against the PR
   - Create GitHub issues for any findings (tech debt, patterns to extract, docs to update)
   - Post retro summary to Telegram

## Dispatch Prompt Template

For the engineer:
```
Implement the following in <owner/repo>:

<task description>

Work in a git worktree. Branch from main. Create a PR when done.
Report back via: ~/claudlobby/bot-common/report-back.sh "<eng-bot>" "completed" "<summary>" "pr:<pr-url>"
If blocked, report immediately with status "blocked".
```

## Rules

- Never merge without a review
- Never auto-resolve ambiguous feedback
- Always run retro after merge
- If engineer reports blocked, notify human immediately
```

### Wiring

Add `/lifecycle` to the manager bot's project skills:

```
~/claudlobby/manager-bot/.claude/skills/lifecycle/SKILL.md
```

Reference in the manager's CLAUDE.md:

```markdown
- **Full lifecycle** — `/lifecycle` orchestrates implement → review → merge → retro
```

### Gotchas

- The manager needs to be patient between steps. Each dispatch can take 5-30 minutes. The manager should not poll — it waits for the `[BOTREPORT]` message.
- "Mechanical fix" vs "ambiguous concern" is a judgment call the manager makes by reading the review. Bias toward flagging humans early — false escalations are cheaper than bad merges.
- The 3-cycle guard exists because infinite review loops waste bot time. If a PR can't converge in 3 rounds, something is wrong with the task scoping or the code.

---

## 2. Alert Sweep (/data-alert-sweep)

Batch-process alerts from a monitoring channel. Pull alerts, check existing work, investigate, and either approve existing PRs or start new fixes — without creating competing PRs.

### Why

Monitoring channels (Slack, Discord, Datadog, etc.) accumulate alerts faster than anyone triages them. This pattern processes them in bulk: investigate each one, check if someone already filed a PR, and either contribute to the existing fix or start a new one.

### Flow

```
/data-alert-sweep --channel #data-alerts --lookback 24h

  1. Pull recent alerts from the channel
  2. For each alert:
     a. Read the full thread (including bot/human replies)
     b. Check if a PR already exists for this alert
     c. Run investigation AND review existing PR in parallel
     d. Compare findings:
        - Same conclusion → approve PR, reply to thread
        - Different conclusion → post as discussion on existing PR
        - No existing PR → create fix, open new PR, reply to thread
     e. Never create a competing PR if one already exists
  3. Reply to each alert thread: 1-2 line summary + PR link
```

### SKILL.md

```markdown
---
name: data-alert-sweep
description: "Batch-process alerts from a monitoring channel. Investigate, check for existing PRs, fix or approve, reply to threads."
argument-hint: "--channel <channel-name> [--lookback <duration>]"
---

# Data Alert Sweep

Process monitoring alerts in bulk.

## Steps

1. **Pull alerts**
   - Use Slack MCP (or relevant integration) to read recent messages from the alert channel
   - Default lookback: 24 hours. Configurable via `--lookback`.
   - Filter to alert messages (skip bot responses, reactions, human chatter)

2. **Read thread context**
   - For each alert, read the full thread
   - Note: has a human already acknowledged it? Has a bot already investigated?
   - If fully resolved (human confirmed fix, PR merged), skip

3. **Check for existing PRs**
   - Search GitHub for PRs referencing the alert text, error message, or affected file
   - Also check if any open PRs touch the files/functions mentioned in the alert
   - Record: `existing_pr_url` or `null`

4. **Parallel investigation**
   For each unresolved alert, run two tasks in parallel:
   - **Independent investigation:** Read the codebase, understand the root cause, draft a fix
   - **Review existing PR** (if one exists): Read the PR diff, understand the approach

5. **Compare and act**
   - **Existing PR + same conclusion:** Approve the PR. Add a comment: "Independent investigation confirms this approach."
   - **Existing PR + different conclusion:** Post findings as a discussion comment on the PR. Do NOT create a competing PR.
   - **No existing PR:** Create the fix in a worktree, open a PR, link it to the alert.

6. **Reply to alert threads**
   - Post a 1-2 line summary in each alert thread
   - Include PR link (new or existing)
   - Format: `Investigated — [root cause]. PR: [link]`

## Rules

- NEVER create a competing PR. One PR per alert, always.
- If investigation is inconclusive, reply to the thread with findings and flag for human review.
- Process alerts oldest-first so fixes build on each other.
- If an alert maps to multiple repos, pick the most likely root cause repo.
```

### Wiring

Manager bot skill:

```
~/claudlobby/manager-bot/.claude/skills/data-alert-sweep/SKILL.md
```

The manager can run this directly (if it has Slack + GitHub MCP) or dispatch it to an engineer bot. For complex alerts, the manager can dispatch investigation to an engineer and review to the code reviewer in parallel, then reconcile.

### Cron (optional)

```crontab
# Sweep alerts twice daily
0 10 * * * /home/YOUR_USER/claudlobby/manager-bot/alert-sweep-cron.sh
0 16 * * * /home/YOUR_USER/claudlobby/manager-bot/alert-sweep-cron.sh
```

```bash
#!/bin/bash
# alert-sweep-cron.sh
BOT_SESSION="manager-bot"
if /usr/bin/tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
    /usr/bin/tmux send-keys -t "$BOT_SESSION" "/data-alert-sweep --channel #data-alerts --lookback 12h" Enter
fi
```

### Gotchas

- Thread replies in Slack MCP can be slow for channels with hundreds of alerts. Use `--lookback` to limit scope.
- The "no competing PRs" rule is critical. Two PRs fixing the same thing causes merge conflicts and wasted review time. Always search thoroughly before creating.
- Some alerts are symptoms of the same root cause. The bot should try to group related alerts, but this is hard to get perfect. Err on the side of one PR per alert rather than one PR for a cluster.

---

## 3. Triage (/triage)

Surface untracked items from unstructured inputs (emails, meeting notes, messages) and move them into a structured system (Notion, GitHub Issues, etc.).

### Why

Work hides in inboxes. Emails contain action items that never become tasks. Meeting notes mention follow-ups that nobody tracks. This pattern reads unstructured inputs, enriches them with external data, deduplicates against existing tasks, and creates new tracked items.

### Flow

```
/triage --source email --lookback 48h

  1. Read unstructured inputs (email, meeting notes, Slack DMs)
  2. Extract action items, requests, follow-ups
  3. Enrich with external data:
     - Customer email → look up their order history (Shopify)
     - Bug report → check existing GitHub issues
     - Meeting note → cross-reference calendar for attendees
  4. Deduplicate against existing Notion tasks
  5. Create new tasks with linked contacts/records
  6. Report what was created
```

### SKILL.md

```markdown
---
name: triage
description: "Surface untracked items from emails, meeting notes, or messages. Enrich with external data, deduplicate, create tasks."
argument-hint: "--source <email|meetings|slack> [--lookback <duration>]"
---

# Triage

Find untracked work hiding in unstructured inputs.

## Sources

| Source | MCP Server | What to Extract |
|--------|-----------|-----------------|
| Email | Gmail | Action items, customer requests, follow-ups |
| Meeting notes | Granola / Calendar | Decisions, assigned tasks, follow-ups |
| Slack DMs | Slack | Direct requests, questions needing answers |

## Steps

1. **Read inputs**
   - Email: search for messages in the lookback window, skip newsletters/automated
   - Meetings: read recent meeting transcripts or notes
   - Slack: read DMs and priority channels

2. **Extract items**
   - Look for: explicit requests, questions awaiting response, promised deliverables,
     deadlines mentioned, people waiting on something
   - Each item needs: summary, source (email/meeting/slack), urgency, related people

3. **Enrich**
   - If item mentions a customer → look up in Shopify/CRM for order history, account status
   - If item mentions a bug or feature → search GitHub issues for existing reports
   - If item mentions a person → search contacts database for context
   - Attach enrichment data to the item

4. **Deduplicate**
   - Search Notion tasks database for similar items (by keyword, contact, description)
   - If a matching task already exists and is open → skip (or update with new context)
   - If a matching task exists but is closed → note it, may need reopening

5. **Create tasks**
   - Create Notion task for each new item
   - Link to source (email URL, meeting date, Slack permalink)
   - Link to related contacts/records
   - Set initial status, priority based on urgency signals

6. **Report**
   Post to Telegram:
   ```
   Triage complete (48h email scan):
   - 3 new tasks created
   - 2 duplicates skipped (already tracked)
   - 1 item flagged for review (ambiguous priority)
   ```

## Variants

### Customer Service Triage
Email → order context (Shopify) → task with customer record

### Engineering Triage
Alert/bug report → investigation → GitHub issue with reproduction steps

### Meeting Follow-Up Triage
Meeting notes → action items → tasks assigned to attendees
```

### Wiring

Works best on the manager bot (which has the most MCP integrations) or a business bot (which has Shopify + Gmail + Notion).

```
~/claudlobby/manager-bot/.claude/skills/triage/SKILL.md
```

Can be run on-demand or scheduled:

```crontab
# Triage email every morning before the briefing
0 8 * * * /home/YOUR_USER/claudlobby/manager-bot/triage-cron.sh
```

```bash
#!/bin/bash
# triage-cron.sh
BOT_SESSION="manager-bot"
if /usr/bin/tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
    /usr/bin/tmux send-keys -t "$BOT_SESSION" "/triage --source email --lookback 24h" Enter
fi
```

### Gotchas

- Deduplication is the hardest part. Exact-match on subject line misses rephrased duplicates. Fuzzy matching (searching Notion by keywords) works better but occasionally creates near-duplicates. Accept ~90% accuracy and clean up manually.
- Enrichment calls (Shopify order lookup, GitHub search) add latency. For a 48h email triage with 50+ emails, expect 5-10 minutes.
- Be careful with auto-creating tasks from meeting notes — transcription errors can produce phantom action items. If using meeting transcripts, flag items as "from meeting notes (verify)" rather than treating them as confirmed tasks.

---

## 4. Pre-Stop Handoff

Graceful shutdown with context preservation. When systemd stops a bot, it captures the bot's current context before killing the process, so the next startup can resume seamlessly.

### Why

A raw `systemctl stop` or `systemctl restart` kills the bot mid-thought. Any work in progress, mental context about ongoing tasks, or partial investigations is lost. Pre-stop handoff gives the bot a few seconds to write down what it's doing before it dies.

### How It Works

```
systemctl stop my-bot
  → systemd calls ExecStop=pre-stop-handoff.sh
    → Script checks for recent session-handoff (< 5 min old)
    → If none: sends /session-handoff --auto to bot via tmux, waits
    → Bot writes handoff file to planning/
    → systemd proceeds with SIGTERM

systemctl start my-bot
  → Bot starts, reads CLAUDE.md
  → CLAUDE.md or startup prompt includes: run /context-resume
  → Bot reads handoff file and picks up where it left off
```

### Script: pre-stop-handoff.sh

```bash
#!/bin/bash
# pre-stop-handoff.sh — called by systemd ExecStop before killing the bot
# Usage: pre-stop-handoff.sh /path/to/bot/dir

BOT_DIR="${1:?Usage: pre-stop-handoff.sh /path/to/bot/dir}"
source "$BOT_DIR/bot.conf"

HANDOFF_DIR="$BOT_DIR/planning"
HANDOFF_PATTERN="$HANDOFF_DIR/session-handoff-*.md"
MAX_AGE_SECONDS=300  # 5 minutes

# Check if a recent handoff already exists (e.g., user ran /session-handoff manually)
LATEST=$(ls -t $HANDOFF_PATTERN 2>/dev/null | head -1)
if [ -n "$LATEST" ]; then
    FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST" 2>/dev/null || stat -f %m "$LATEST" 2>/dev/null) ))
    if [ "$FILE_AGE" -lt "$MAX_AGE_SECONDS" ]; then
        echo "Recent handoff exists ($FILE_AGE seconds old), skipping"
        exit 0
    fi
fi

# Check if bot tmux session is alive
if ! /usr/bin/tmux has-session -t "$BOT_NAME" 2>/dev/null; then
    echo "Bot session not running, nothing to hand off"
    exit 0
fi

# Send handoff command
echo "Requesting session handoff..."
/usr/bin/tmux send-keys -t "$BOT_NAME" "/session-handoff --auto" Enter

# Wait for handoff file to appear (up to 45 seconds)
for i in $(seq 1 45); do
    LATEST=$(ls -t $HANDOFF_PATTERN 2>/dev/null | head -1)
    if [ -n "$LATEST" ]; then
        FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST" 2>/dev/null || stat -f %m "$LATEST" 2>/dev/null) ))
        if [ "$FILE_AGE" -lt 60 ]; then
            echo "Handoff captured: $LATEST"
            exit 0
        fi
    fi
    sleep 1
done

echo "WARN: Handoff timed out after 45s, proceeding with stop"
exit 0
```

### Systemd Service Changes

Update the bot's `.service` file to call pre-stop on shutdown:

```ini
[Service]
Type=oneshot
ExecStart=/home/YOUR_USER/claudlobby/bot-common/start-bot.sh /home/YOUR_USER/claudlobby/my-bot
ExecStop=/home/YOUR_USER/claudlobby/bot-common/pre-stop-handoff.sh /home/YOUR_USER/claudlobby/my-bot
RemainAfterExit=yes
TimeoutStopSec=90
```

Note `TimeoutStopSec=90` — enough time for the handoff script (45s max) plus cleanup.

### Startup Resume

Add to the bot's `STARTUP_PROMPT` in `bot.conf`:

```bash
STARTUP_PROMPT="You just restarted. Read your CLAUDE.md. Then run /context-resume to check for a session handoff. If one exists, pick up where you left off. If not, greet the team."
```

Or add it to the bot's CLAUDE.md:

```markdown
## On Startup

After reading this file, always run `/context-resume` to check for pending handoffs.
```

### Gotchas

- `TimeoutStopSec` must be longer than the handoff wait time, or systemd will SIGKILL the bot before the handoff completes.
- The `--auto` flag on `/session-handoff` is important — it runs non-interactively so the bot doesn't prompt for confirmation.
- If the bot is deeply stuck (infinite loop, unresponsive MCP server), the handoff will time out. That's fine — the script exits gracefully and systemd proceeds with the kill.
- `stat -c` vs `stat -f` handles both Linux and macOS. On a Pi you'll only need the Linux variant, but the script is portable.

---

## 5. Inter-Bot Communication Protocol (report-back.sh)

Structured messaging between bots via tmux. Workers call `report-back.sh` when done, and the manager parses the structured message to decide next steps.

### Why

Bots need a reliable way to tell each other "I'm done" with enough context for the receiver to act. Telegram is unreliable for bot-to-bot communication (messages drop). tmux send-keys is instant and deterministic. The structured format makes parsing straightforward.

### Message Format

```
[BOTREPORT] <bot-name> | <status> | <summary> [| pr:<url>] [| issues:<url1>,<url2>]
```

**Statuses:**
- `completed` — task finished successfully
- `failed` — task failed, summary explains why
- `blocked` — task can't proceed, needs human or different bot

**Optional fields:**
- `pr:<url>` — PR created or reviewed
- `issues:<url1>,<url2>` — GitHub issues created

### Script: report-back.sh

This already exists in `bot-common/`. Here's how bots use it:

```bash
# Engineer finished a task, created a PR
~/claudlobby/bot-common/report-back.sh "eng-a" "completed" "Added rate limiting to auth endpoint" "pr:https://github.com/org/api/pull/87"

# Engineer hit a blocker
~/claudlobby/bot-common/report-back.sh "eng-a" "blocked" "Need DB migration permissions — cannot alter production schema"

# Reviewer finished a review, also filed issues
~/claudlobby/bot-common/report-back.sh "code-reviewer" "completed" "Reviewed PR #87 — approved with minor comments" "pr:https://github.com/org/api/pull/87" "issues:https://github.com/org/api/issues/88"
```

### How the Manager Processes Reports

The manager bot sees `[BOTREPORT]` messages arrive in its tmux pane. Its CLAUDE.md should include instructions for handling them:

```markdown
## Bot Reports

When you see a message starting with `[BOTREPORT]`, parse it:

Format: `[BOTREPORT] <bot> | <status> | <summary> [| pr:<url>] [| issues:<urls>]`

### Actions by status:

**completed:**
- If this was part of a /lifecycle cycle, proceed to the next step (dispatch reviewer, merge, etc.)
- If standalone dispatch, post the summary to Telegram
- If a PR was created, note it for the next briefing

**failed:**
- Post failure to Telegram with the summary
- Suggest next steps (retry, different approach, escalate)

**blocked:**
- Post to Telegram immediately — this needs human attention
- Include the blocker reason so the human can unblock without context-switching

### Example

You see: `[BOTREPORT] eng-a | completed | Added rate limiting to auth endpoint | pr:https://github.com/org/api/pull/87`

Action: Dispatch code-reviewer-bot to review PR #87. Post to Telegram: "eng-a completed rate limiting. PR #87 — dispatching review."
```

### Configuring the Manager Session Name

Workers need to know which tmux session to send reports to. Set `MANAGER_BOT_NAME` in each worker's `bot.conf`:

```bash
# In eng-a-bot/bot.conf
MANAGER_BOT_NAME="manager-bot"
```

Or export it in the worker's `.env`:

```bash
export MANAGER_BOT_NAME="manager-bot"
```

The `report-back.sh` script reads this variable (defaulting to `claude-bot` if unset).

### Gotchas

- The pipe (`|`) delimiter means summaries must not contain pipes. Keep summaries to one sentence.
- tmux send-keys has a practical length limit. Keep the total message under ~500 characters. If you need to convey more detail, include a link (PR URL, issue URL) and let the manager read the details via GitHub MCP.
- Reports arrive as text in the manager's pane. If the manager is mid-task, it may not process the report immediately. This is fine — the report sits in the pane buffer and the manager handles it when it reaches a natural pause point.

---

## 6. Git Pull Scheduler

Keep cloned repos fresh across all bots without manual intervention.

### Why

Bots work from cloned repos on the Pi. If those repos are stale, bots create PRs against old code, miss recently-merged changes, and produce merge conflicts. A scheduled git pull keeps everything current.

### Script: git-pull-all.sh

```bash
#!/bin/bash
# git-pull-all.sh — pull all repos in a directory using --ff-only
# Usage: git-pull-all.sh /path/to/repos/
#
# Only uses --ff-only so it fails safely if there are local changes.
# Intended to run via cron.

REPOS_DIR="${1:?Usage: git-pull-all.sh /path/to/repos/}"
LOG="${REPOS_DIR}/git-pull.log"

echo "$(date -Iseconds) === Starting git pull sweep ===" >> "$LOG"

for repo in "$REPOS_DIR"/*/; do
    [ -d "$repo/.git" ] || continue
    REPO_NAME=$(basename "$repo")

    cd "$repo"
    BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)

    if [ "$BRANCH" != "main" ] && [ "$BRANCH" != "master" ]; then
        echo "$(date -Iseconds) SKIP $REPO_NAME — on branch $BRANCH (not main/master)" >> "$LOG"
        continue
    fi

    OUTPUT=$(git pull --ff-only 2>&1)
    STATUS=$?

    if [ $STATUS -eq 0 ]; then
        if echo "$OUTPUT" | grep -q "Already up to date"; then
            echo "$(date -Iseconds) OK   $REPO_NAME — up to date" >> "$LOG"
        else
            echo "$(date -Iseconds) PULL $REPO_NAME — updated" >> "$LOG"
        fi
    else
        echo "$(date -Iseconds) FAIL $REPO_NAME — ff-only failed (local changes?)" >> "$LOG"
        echo "  $OUTPUT" >> "$LOG"
    fi
done

echo "$(date -Iseconds) === Sweep complete ===" >> "$LOG"
```

### Cron Setup

```crontab
# Daily pull for less active repos (4 AM)
0 4 * * * /home/YOUR_USER/claudlobby/bot-common/git-pull-all.sh /home/YOUR_USER/repos

# 3x/day for active repos (8 AM, 1 PM, 6 PM)
0 8,13,18 * * * /home/YOUR_USER/claudlobby/bot-common/git-pull-all.sh /home/YOUR_USER/active-repos
```

### Gotchas

- `--ff-only` is the key safety mechanism. If a bot has local uncommitted changes or is on a feature branch, the pull fails harmlessly rather than creating a merge commit.
- The script skips repos not on main/master. Bots working in worktrees won't be affected — worktrees are separate directories.
- Don't run git pull while a bot is actively working in a repo. Schedule pulls during off-hours or check bot status first. The staggered cron times (4 AM, 8 AM) help avoid conflicts.
- If a pull fails repeatedly, check the log. Common causes: bot left uncommitted changes, force-pushed remote, or network issues.

---

## 7. Automated Code Audits

Scheduled code reviews without human initiation. The fleet systematically audits repos for tech debt, security issues, and staleness.

### Why

Manual audits happen when someone remembers to do them — which means they don't happen. Automated audits run on a schedule, track what's been audited, and ensure no part of the codebase goes stale.

### Components

1. **Evening audit cron** — triggers the manager bot to run audit skills
2. **Rolling audit script** — suggests which repo/directory to audit next
3. **Audit tracker** — logs completed audits with timestamps and issue URLs

### Cron: audit-cron.sh

```bash
#!/bin/bash
# audit-cron.sh — trigger a code audit on the next stale area
# Called by cron, runs in the manager bot's tmux session

BOT_SESSION="manager-bot"
AUDIT_TRACKER="/home/YOUR_USER/claudlobby/manager-bot/planning/audit-tracker.json"
REPOS_DIR="/home/YOUR_USER/repos"

if ! /usr/bin/tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
    exit 0
fi

# Get the next audit target from the rolling script
TARGET=$(python3 /home/YOUR_USER/claudlobby/bot-common/next-audit-target.py \
    --tracker "$AUDIT_TRACKER" \
    --repos "$REPOS_DIR")

if [ -z "$TARGET" ]; then
    exit 0
fi

REPO=$(echo "$TARGET" | cut -d'|' -f1)
AREA=$(echo "$TARGET" | cut -d'|' -f2)
AUDIT_TYPE=$(echo "$TARGET" | cut -d'|' -f3)

/usr/bin/tmux send-keys -t "$BOT_SESSION" \
    "Run /$AUDIT_TYPE on $REPO (focus: $AREA). Create GitHub issues for findings. Then update the audit tracker at $AUDIT_TRACKER." Enter
```

### Rolling Target Selector: next-audit-target.py

```python
#!/usr/bin/env python3
"""
Suggest the next repo/directory to audit based on staleness.

Reads the audit tracker to find what hasn't been audited recently,
then picks the stalest target.
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def load_tracker(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"audits": []}


def get_repos(repos_dir):
    """List repos and their top-level directories as audit targets."""
    targets = []
    for repo in sorted(Path(repos_dir).iterdir()):
        if not (repo / ".git").exists():
            continue
        repo_name = repo.name
        # Add repo-level targets
        targets.append({"repo": repo_name, "area": ".", "types": ["tech-debt", "security-audit"]})
        # Add directory-level targets for larger repos
        for subdir in sorted(repo.iterdir()):
            if subdir.is_dir() and not subdir.name.startswith("."):
                targets.append({"repo": repo_name, "area": subdir.name, "types": ["tech-debt"]})
    return targets


def find_stalest(targets, tracker):
    """Find the target that was audited longest ago (or never)."""
    audit_map = {}
    for audit in tracker.get("audits", []):
        key = f"{audit['repo']}|{audit['area']}|{audit['type']}"
        audit_map[key] = audit["timestamp"]

    stalest = None
    stalest_time = None

    for target in targets:
        for audit_type in target["types"]:
            key = f"{target['repo']}|{target['area']}|{audit_type}"
            last_audit = audit_map.get(key)

            if last_audit is None:
                # Never audited — highest priority
                return f"{target['repo']}|{target['area']}|{audit_type}"

            if stalest_time is None or last_audit < stalest_time:
                stalest_time = last_audit
                stalest = f"{target['repo']}|{target['area']}|{audit_type}"

    return stalest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracker", required=True, help="Path to audit-tracker.json")
    parser.add_argument("--repos", required=True, help="Path to repos directory")
    args = parser.parse_args()

    tracker = load_tracker(args.tracker)
    targets = get_repos(args.repos)

    if not targets:
        return

    result = find_stalest(targets, tracker)
    if result:
        print(result)


if __name__ == "__main__":
    main()
```

### Audit Tracker Format: audit-tracker.json

```json
{
  "audits": [
    {
      "repo": "my-api",
      "area": "src/auth",
      "type": "security-audit",
      "timestamp": "2025-01-15T22:00:00Z",
      "issues": [
        "https://github.com/org/my-api/issues/91",
        "https://github.com/org/my-api/issues/92"
      ]
    },
    {
      "repo": "my-api",
      "area": ".",
      "type": "tech-debt",
      "timestamp": "2025-01-10T22:00:00Z",
      "issues": []
    }
  ]
}
```

The manager bot updates this file after each audit. Include instructions in the manager's CLAUDE.md:

```markdown
## Audit Tracking

After completing a code audit, update the audit tracker at `planning/audit-tracker.json`:
- Add an entry with repo, area, type, timestamp (ISO 8601), and issue URLs
- This file is read by the rolling audit scheduler to determine what to audit next
```

### Cron

```crontab
# Evening audit — runs one audit per night (Mon-Fri, 9 PM)
0 21 * * 1-5 /home/YOUR_USER/claudlobby/manager-bot/audit-cron.sh
```

### Querying Audit Status

The manager bot can check what needs attention:

```markdown
## Checking Audit Coverage

To see what areas haven't been audited recently, read `planning/audit-tracker.json`
and identify entries older than 30 days or repos/areas with no entries at all.
Post the findings when asked about audit coverage.
```

### Gotchas

- Run audits during off-hours (evenings, weekends) so they don't compete with daytime engineering work for bot time.
- One audit per night is a good cadence. Running multiple audits back-to-back risks overwhelming the issue tracker with noise.
- The tracker file can grow large over time. Periodically prune entries older than 90 days — the issues they created are the permanent record.
- Security audits should run against the full repo (`.`), not subdirectories — vulnerabilities often span module boundaries.

---

## 8. Telegram Formatting Guide

Shared formatting guidance for consistent output across all bots.

### Why

Telegram's markdown support is limited and different from GitHub/standard markdown. Bots that format messages assuming full markdown produce broken output. A shared reference ensures every bot formats consistently.

### What Works in Telegram

| Format | Syntax | Renders? |
|--------|--------|----------|
| **Bold** | `*bold*` | Yes |
| *Italic* | `_italic_` | Yes |
| `Code` | `` `code` `` | Yes |
| Code block | ` ```code``` ` | Yes |
| [Link](url) | `[text](url)` | Yes |
| # Header | `# Header` | No |
| - Bullet | `- item` | Renders as plain text with dash |
| > Quote | `> text` | Partial (some clients) |
| Tables | `\| col \|` | No |

### Reference File: _telegram-formatting.md

Place this in each bot's directory or in a shared location and reference it from CLAUDE.md:

```markdown
# Telegram Formatting Reference

## Rules

1. NO markdown headers — they render as literal `#` characters
2. Use *bold* for section labels instead of headers
3. Use `code blocks` for any structured data, numbers, or tables
4. Keep messages SHORT — Telegram is a mobile-first platform
5. One message per topic — don't combine unrelated updates
6. Use line breaks to separate sections (blank line between blocks)

## Patterns

### Status Update
```
*Fleet Status*

eng-a-bot: ALIVE (idle)
code-reviewer: ALIVE (working)

RAM: 4.2G / 16G | Temp: 58C
```

### Task Report
```
*Task Completed*

Bot: eng-a-bot
PR: https://github.com/org/repo/pull/42
Summary: Added rate limiting to auth endpoint
```

### Briefing Section
```
*Morning Briefing*

_Calendar_
- 10:00 Standup
- 14:00 Design review

_Email_
3 unread, 1 action needed

_GitHub_
2 PRs awaiting review
```

### Data Table (use code block)
```
*Revenue Summary*

` ` `
Period     Revenue    Orders
Today      $1,234     18
Yesterday  $987       14
This Week  $5,421     72
` ` `
```
(Remove spaces between backticks — shown here for escaping.)

## Anti-Patterns

- Don't use headers: `## Summary` renders as `## Summary`
- Don't use numbered lists with periods: `1.` can trigger unexpected formatting
- Don't embed images — Telegram can display them but the bot can't send them inline via text
- Don't write walls of text — break into multiple messages if needed
```

### Wiring

Reference in each bot's CLAUDE.md:

```markdown
## Telegram Formatting

When writing Telegram messages, follow the formatting guide in `_telegram-formatting.md`.
Key rule: no markdown headers, use *bold* for labels, keep messages concise for mobile.
```

Or symlink a shared copy:

```bash
# Shared formatting reference
cp _telegram-formatting.md ~/claudlobby/bot-common/
# Symlink into each bot
ln -s ~/claudlobby/bot-common/_telegram-formatting.md ~/claudlobby/my-bot/_telegram-formatting.md
```

### Gotchas

- Telegram's markdown parser varies slightly between clients (desktop, iOS, Android). Test formatting on your primary device.
- Code blocks with triple backticks work, but nested code blocks do not.
- Very long messages (4096+ characters) get truncated. Split long outputs into multiple messages.
- The bot can send messages with formatting, but when reading user messages, formatting markers may or may not be present depending on the client.

---

## 9. Visual Crawl (Designer Bot)

Autonomous frontend quality assurance. A bot crawls a deployed web app, screenshots every page at multiple viewports, compares against design system tokens, tests basic interactions, and files GitHub issues for findings.

### Why

Frontend QA is tedious and gets skipped. A designer bot can systematically check every page at every viewport, catch regressions that humans miss, and file issues with screenshot evidence. Run it on-demand after deploys or on a schedule.

### Flow

```
/visual-crawl --url https://staging.example.com --repo org/frontend

  1. Discover all routes (crawl links from the homepage, read sitemap, or use a route list)
  2. For each route:
     a. Load at 3 viewports: mobile (375px), tablet (768px), desktop (1440px)
     b. Screenshot each viewport
     c. Compare against design system tokens (colors, spacing, typography)
     d. Test basic interactions (click buttons, hover states, form inputs)
     e. Check accessibility (contrast, alt text, focus states)
  3. File GitHub issues for every finding:
     - Include screenshot evidence
     - Tag with viewport, severity, affected component
  4. Post summary to Telegram
```

### SKILL.md

```markdown
---
name: visual-crawl
description: "Crawl a deployed web app, screenshot all pages at 3 viewports, compare against design tokens, file issues for findings."
argument-hint: "--url <base-url> --repo <owner/repo> [--routes <file>]"
---

# Visual Crawl

Autonomous frontend quality audit.

## Setup

This skill requires a browser automation tool. Options:
- Chrome MCP server (if available on the bot)
- Playwright via Bash (`npx playwright`)
- Puppeteer via Bash (`npx puppeteer`)

The bot also needs the design system tokens — either as a file in the repo
(e.g., `tokens.json`, `theme.ts`) or loaded via the `/design-norms` skill.

## Steps

### 1. Route Discovery

Start from the base URL and discover pages:

```bash
# Option A: crawl links from homepage
# The bot reads the page, extracts all internal links, builds a route list

# Option B: read from sitemap
curl -s https://staging.example.com/sitemap.xml

# Option C: provide a route list file
# --routes routes.txt (one path per line)
```

### 2. Screenshot Capture

For each route, capture at three viewports:

| Viewport | Width | Use Case |
|----------|-------|----------|
| Mobile | 375px | iPhone SE / small phones |
| Tablet | 768px | iPad / mid-size |
| Desktop | 1440px | Standard laptop |

Save screenshots to a temp directory with naming:
`<route-slug>-<viewport>.png`

### 3. Design Token Comparison

Load design system tokens (from repo or `/design-norms`):
- **Colors:** Check that backgrounds, text, and borders use approved palette
- **Typography:** Verify font families, sizes, weights, line heights
- **Spacing:** Check margins and padding against the spacing scale
- **Components:** Compare common elements (buttons, inputs, cards) against specs

### 4. Interaction Testing

For each page, test:
- Clickable elements respond (buttons, links, nav items)
- Hover states appear where expected
- Form inputs accept text, show focus states
- Modals/dropdowns open and close
- No console errors during interaction

### 5. Issue Filing

For each finding, create a GitHub issue:

Title: `[Visual QA] <component/page> — <issue description>`
Body:
```
**Page:** /about
**Viewport:** Mobile (375px)
**Severity:** Medium

**Finding:**
Button text overflows container at mobile width.

**Expected:** Text wraps or truncates within button bounds
**Actual:** Text extends beyond button, overlaps adjacent content

**Screenshot:**
[attached screenshot]

**Design Token Reference:**
Button max-width should be 100% at mobile per tokens.spacing.mobile
```

### 6. Summary

Post to Telegram:
```
*Visual Crawl Complete*

URL: staging.example.com
Pages: 12 crawled
Findings: 7 issues filed

Critical: 1 (broken layout on /checkout mobile)
Medium: 4 (color mismatches, spacing issues)
Low: 2 (minor alignment, hover states)

Issues: https://github.com/org/frontend/issues?q=label:visual-qa
```

## Rules

- Always include screenshot evidence in issues
- Tag issues with `visual-qa` label for easy filtering
- Don't file issues for known/intentional deviations (check existing issues first)
- Group related findings (e.g., same component broken across pages = one issue, not N issues)
```

### Wiring

Designer bot skill:

```
~/claudlobby/designer-bot/.claude/skills/visual-crawl/SKILL.md
```

**On-demand** (manager dispatches):
```bash
tmux send-keys -t designer-bot '/visual-crawl --url https://staging.example.com --repo org/frontend' Enter
```

**Scheduled** (post-deploy or nightly):
```crontab
# Nightly visual audit of staging (11 PM)
0 23 * * * /home/YOUR_USER/claudlobby/designer-bot/visual-crawl-cron.sh
```

```bash
#!/bin/bash
# visual-crawl-cron.sh
BOT_SESSION="designer-bot"
if /usr/bin/tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
    /usr/bin/tmux send-keys -t "$BOT_SESSION" \
        "/visual-crawl --url https://staging.example.com --repo org/frontend" Enter
fi
```

### Gotchas

- Browser automation on a Pi is memory-intensive. Chromium alone uses 300-500 MB. Run visual crawls when other bots are idle, or on a separate Pi.
- Screenshots need to be uploaded to GitHub issues. The bot can do this via the GitHub API (create issue with image attachment) or by committing screenshots to a branch and linking them.
- Crawling can discover hundreds of routes on large apps. Use `--routes` with a curated list for targeted audits, or let the full crawl run overnight.
- Design token comparison is only as good as the tokens file. If the tokens are outdated or incomplete, findings will be noisy. Keep the design tokens in the repo and up to date.

---

## 10. Multi-Account Setup (CLAUDE_CONFIG_DIR)

Running bots under different Claude subscriptions. Useful when you need more concurrent sessions than one account provides, or when work and personal bots should use separate billing.

### Why

A single Claude account has concurrency limits. If your fleet has 5+ bots, you may hit session caps. Running some bots under a second account doubles your capacity. It also provides clean separation — your employer's account pays for work bots, your personal account pays for personal bots.

### How It Works

Each Claude Code installation stores its auth, plugins, and channel state in a config directory (default: `~/.claude/`). Setting `CLAUDE_CONFIG_DIR` to a different path gives a bot its own identity.

### Step-by-Step Setup

#### 1. Create the Config Directory

```bash
mkdir -p ~/.claude-work
```

#### 2. Authenticate

```bash
CLAUDE_CONFIG_DIR=~/.claude-work claude auth login
# Complete OAuth flow — this creates auth tokens in ~/.claude-work/
```

#### 3. Install Plugins

```bash
CLAUDE_CONFIG_DIR=~/.claude-work claude plugin install telegram@claude-plugins-official
```

#### 4. Symlink Shared Skills

Global skills should be available to both accounts:

```bash
ln -s ~/.claude/skills ~/.claude-work/skills
```

This means skills installed to `~/.claude/skills/` (by clauDNA or manually) are visible to bots using either config dir.

#### 5. Set Up Telegram State

When using a separate `CLAUDE_CONFIG_DIR`, Telegram state goes into that config dir's `channels/` directory:

```bash
mkdir -p ~/.claude-work/channels/telegram-work-bot/{approved,inbox}

# Write bot token
echo "TELEGRAM_BOT_TOKEN=your_work_bot_token" > ~/.claude-work/channels/telegram-work-bot/.env
chmod 600 ~/.claude-work/channels/telegram-work-bot/.env

# Write access config
cat > ~/.claude-work/channels/telegram-work-bot/access.json << 'EOF'
{
  "dmPolicy": "allowlist",
  "allowFrom": ["YOUR_TELEGRAM_USER_ID"],
  "groups": {
    "YOUR_GROUP_CHAT_ID": {
      "requireMention": false,
      "allowFrom": []
    }
  },
  "pending": {}
}
EOF
```

When using `CLAUDE_CONFIG_DIR`, you may not need to set `TELEGRAM_STATE_DIR` separately — the Telegram plugin looks for state relative to the config dir. Test this: if the bot finds its token without `TELEGRAM_STATE_DIR`, you can skip it. If not, set both.

#### 6. Configure bot.conf

```bash
BOT_NAME="work-bot"
BOT_SERVICE="work-bot"
BOT_LABEL="WORK-BOT"
BOT_DIR="/home/YOUR_USER/claudlobby/work-bot"
CLAUDE_CONFIG_DIR="/home/YOUR_USER/.claude-work"
TELEGRAM_STATE_DIR="/home/YOUR_USER/.claude-work/channels/telegram-work-bot"
```

#### 7. Verify

```bash
# Test that the bot starts with the right account
CLAUDE_CONFIG_DIR=~/.claude-work claude --version
# Should work without re-authenticating

# Check that skills are visible
ls ~/.claude-work/skills/
# Should show symlinked skills
```

### Directory Layout

```
~/.claude/                        # Account 1 (personal)
├── auth/                         # Personal account auth
├── skills/                       # Global skills (canonical copy)
├── channels/
│   ├── telegram-assistant/       # Personal assistant bot
│   └── telegram-engineer/        # Personal engineer bot
└── ...

~/.claude-work/                   # Account 2 (work)
├── auth/                         # Work account auth
├── skills -> ~/.claude/skills    # Symlink to shared skills
├── channels/
│   ├── telegram-work-bot/        # Work manager bot
│   └── telegram-work-eng/        # Work engineer bot
└── ...
```

### Gotchas

- Each account needs its own `claude auth login`. If auth expires, you need to re-auth with the correct `CLAUDE_CONFIG_DIR` set.
- Symlinking skills means changes to one are visible to both. If you need account-specific skills, use a separate directory instead of a symlink.
- Plugin installation is per-config-dir. If you install a new plugin on your default account, you need to install it again with `CLAUDE_CONFIG_DIR` set for the other account.
- `start-bot.sh` already handles `CLAUDE_CONFIG_DIR` — it reads it from `bot.conf` and exports it before launching Claude. No script changes needed.

---

## 11. Finance/Data Pre-Sync Pattern

Pre-fetch data before scheduled briefings so they run fast.

### Why

Briefings that make API calls in real-time (portfolio data, order totals, analytics) are slow and sometimes fail due to rate limits or timeouts. Pre-syncing fetches the data ahead of time and saves a snapshot. When the briefing runs, it reads the snapshot instead of making live calls.

### How It Works

```
7:00 AM — Cron runs data-sync.sh
           → Fetches portfolio data, order summaries, weather, etc.
           → Saves JSON snapshot to a known location

7:30 AM — Cron triggers /briefing morning
           → Bot reads the pre-synced snapshot
           → Briefing completes in seconds instead of minutes
```

### Script: data-sync.sh

```bash
#!/bin/bash
# data-sync.sh — pre-fetch data for upcoming briefing
# Called by cron ~30 min before each scheduled briefing

SYNC_DIR="/home/YOUR_USER/claudlobby/manager-bot/planning/data-sync"
mkdir -p "$SYNC_DIR"
TIMESTAMP=$(date -Iseconds)

# --- Portfolio data (example: fetch from a finance API) ---
# Replace with your actual data source
PORTFOLIO_DATA=$(curl -s "https://api.example.com/portfolio" \
    -H "Authorization: Bearer $FINANCE_API_KEY" 2>/dev/null)

if [ $? -eq 0 ] && [ -n "$PORTFOLIO_DATA" ]; then
    echo "$PORTFOLIO_DATA" > "$SYNC_DIR/portfolio.json"
    echo "$(date -Iseconds) OK portfolio" >> "$SYNC_DIR/sync.log"
else
    echo "$(date -Iseconds) FAIL portfolio" >> "$SYNC_DIR/sync.log"
fi

# --- Order summary (example: aggregate from Shopify API) ---
ORDER_DATA=$(curl -s "https://your-store.myshopify.com/admin/api/2024-01/orders.json?status=any&limit=50" \
    -H "X-Shopify-Access-Token: $SHOPIFY_TOKEN" 2>/dev/null)

if [ $? -eq 0 ] && [ -n "$ORDER_DATA" ]; then
    echo "$ORDER_DATA" > "$SYNC_DIR/orders.json"
    echo "$(date -Iseconds) OK orders" >> "$SYNC_DIR/sync.log"
else
    echo "$(date -Iseconds) FAIL orders" >> "$SYNC_DIR/sync.log"
fi

# --- Weather (example) ---
WEATHER=$(curl -s "https://api.weather.example.com/forecast?location=YOUR_CITY" 2>/dev/null)

if [ $? -eq 0 ] && [ -n "$WEATHER" ]; then
    echo "$WEATHER" > "$SYNC_DIR/weather.json"
    echo "$(date -Iseconds) OK weather" >> "$SYNC_DIR/sync.log"
else
    echo "$(date -Iseconds) FAIL weather" >> "$SYNC_DIR/sync.log"
fi

# --- Write sync metadata ---
cat > "$SYNC_DIR/sync-meta.json" << EOF
{
  "timestamp": "$TIMESTAMP",
  "files": ["portfolio.json", "orders.json", "weather.json"]
}
EOF

echo "$(date -Iseconds) === Sync complete ===" >> "$SYNC_DIR/sync.log"
```

### CLAUDE.md Instructions

Tell the manager bot to use pre-synced data in briefings:

```markdown
## Data Pre-Sync

Before composing briefings, check for pre-synced data at `planning/data-sync/`.

Read `sync-meta.json` to see when data was last fetched and which files are available.
If the sync is recent (< 1 hour old), use the snapshot files instead of making live API calls.
If the sync is stale or missing, fall back to live data via MCP servers.

Available snapshots:
- `portfolio.json` — investment portfolio positions and values
- `orders.json` — recent Shopify orders
- `weather.json` — local weather forecast
```

### Briefing Skill Integration

Update the briefing SKILL.md to reference pre-synced data:

```markdown
## Data Sources (updated)

1. **Pre-synced data** (preferred) — read `planning/data-sync/*.json` if fresh
2. **Calendar** — always live (via Google Calendar MCP)
3. **Email** — always live (via Gmail MCP)
4. **Notion** — always live (via Notion MCP)
5. **GitHub** — always live (via GitHub MCP)

Use pre-synced data for anything that involves external API calls with rate limits or slow responses.
Use live MCP data for things that change minute-to-minute (calendar, email, tasks).
```

### Cron Setup

```crontab
# Pre-sync data 30 min before each briefing
0 8 * * * /home/YOUR_USER/claudlobby/manager-bot/data-sync.sh
30 12 * * * /home/YOUR_USER/claudlobby/manager-bot/data-sync.sh
0 18 * * * /home/YOUR_USER/claudlobby/manager-bot/data-sync.sh

# Briefings (30 min after sync)
30 8 * * * /home/YOUR_USER/claudlobby/manager-bot/briefing-cron.sh morning
0 13 * * * /home/YOUR_USER/claudlobby/manager-bot/briefing-cron.sh midday
30 18 * * * /home/YOUR_USER/claudlobby/manager-bot/briefing-cron.sh evening
```

### Gotchas

- Secrets for external APIs (`FINANCE_API_KEY`, `SHOPIFY_TOKEN`) need to be available to the cron environment. Source them from the bot's `.env` file at the top of `data-sync.sh`: `source /home/YOUR_USER/claudlobby/manager-bot/.env`
- Snapshot files can contain sensitive data (portfolio values, customer orders). Set permissions: `chmod 600 planning/data-sync/*.json`
- The sync script runs outside Claude — it's a plain bash script with curl calls. This means it doesn't use MCP servers. You need direct API access (tokens, endpoints) for each data source.
- If a data source fails, the bot should still produce a briefing using whatever data succeeded. The `sync.log` tells the bot which sources are available.
- Clean up old snapshots. They're overwritten each run, but the log file grows. Add log rotation to your maintenance cron.
