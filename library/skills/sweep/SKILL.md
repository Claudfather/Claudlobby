---
name: sweep
description: "Periodic code-quality sweep across fleet repos. Picks the stalest repo, runs the appropriate audit skill via a subagent, records findings. Scheduled via cron or manually."
argument-hint: "[<repo-name>] [--type tech-debt|security-audit|docs-review|data-model-audit] [run|status]"
---


# Sweep

A scheduled maintenance pass over the fleet's repos. Run on a cadence (e.g., weekly via cron) to keep repositories from accumulating silent rot.

## How it works

The sweep orchestrates clauDNA planning skills. Pick a repo → pick an audit type → dispatch to a subagent so it doesn't pollute your main context.

| Audit type | Skill to run | What it finds |
|-----------|-------------|---------------|
| tech-debt | `/tech-debt` | Dead code, god modules, deprecated patterns |
| security | `/security-audit` | Credential leaks, injection vectors, auth gaps |
| docs | `/docs-review` | Stale or missing documentation |
| data-model | `/data-model-audit` | Schema / app mismatches (if applicable) |
| enhancement | `/product-enhance` | UX gaps, missing features, inconsistencies |

Each is run with `--auto --output github` so it operates non-interactively and creates GitHub issues directly.

## Operations

### 1. Run (default)

A full sweep cycle. This is what a scheduled cron triggers.

**Step 1: Pick target**

Select the stalest repo — either from a fleet-maintained tracker or by inspecting git recency:

```bash
# Option A: fleet-state ledger, if you maintain last_swept per repo
jq -r '.sweeps | to_entries | sort_by(.value.last_swept) | .[0].key' \
  ~/claudlobby/state/fleet-state.json

# Option B: pick the repo with the most days since last commit to main
for repo in <FLEET_REPOS>; do
  ts=$(git -C "<REPOS_ROOT>/$repo" log -1 --format=%ct main 2>/dev/null || echo 0)
  echo "$ts $repo"
done | sort -n | head -1 | cut -d' ' -f2
```

Override by passing a repo name as the first argument.

**Step 2: Pick an audit type**

Default rotation per repo (so every repo sees every audit over time):

| Week | Audit |
|------|-------|
| 1 | `tech-debt` |
| 2 | `security-audit` |
| 3 | `docs-review` |
| 4 | `data-model-audit` *(skip if not applicable)* |

Override with `--type`.

**Step 3: Pull latest code**

```bash
cd <REPOS_ROOT>/<repo> && git checkout main && git pull
```

Always sweep against the latest main.

**Step 4: Launch the audit subagent**

Spawn a **background** Agent (subagent_type: general-purpose) with this prompt structure:

```
You are running an automated <TYPE> audit on <REPO>.

1. cd to <REPOS_ROOT>/<REPO>
2. Run the /<SKILL> skill with --auto --output github, scoped to the highest-impact directory
3. Use: Skill tool with skill="<SKILL>" and args="--auto --output github <DIR>"

After the skill completes, collect:
- All GitHub issue URLs created
- Key findings summary with severity levels
- Positive notes (what's well-implemented)

Return a structured summary:
- REPO: <REPO>
- DIR: <DIR>
- TYPE: <TYPE>
- ISSUES: comma-separated list of issue URLs
- FINDINGS: brief summary
```

**IMPORTANT: The subagent needs full permissions.** It will:
- Read many files (Glob, Grep, Read)
- Create GitHub issues (`mcp__github__create_issue`)
- Invoke another skill via the Skill tool

If issue creation fails due to permissions, the sweep fails silently. Ensure GitHub MCP tools are in the allow list.

**Step 5: Record the sweep**

Update your sweep tracker (whatever you use — a JSON file, a Notion DB, a dedicated log):

- Repo swept
- Audit type run
- Timestamp
- Issue URLs created
- Count of findings

If you maintain per-repo `last_swept` in fleet-state.json, update it here.

**Step 6: Report**

Post a concise Telegram summary (`parseMode: "Markdown"`) with:
- Repo swept
- Audit type
- Count of findings
- Top 3 GitHub issue URLs (if any)

Or, if this sweep feeds a daily briefing, **don't post** — let the briefing pick up the latest report.

### 2. Status

Show sweep health without running anything:

- Last swept repo + when
- List of repos and their last-swept timestamps (identify stalest)
- Most recent sweep's findings

## Failure handling

- Target picker returns nothing → all repos recently audited. Emit "all repos current" and exit.
- Subagent fails / times out → log the failure and move on. Don't block future sweeps.
- Target directory doesn't exist → let the planning skill discover the right paths automatically.
- Issue creation fails → still log findings locally; flag the permission problem.

## Rules

- **Never cross repo boundaries.** Each sweep targets exactly one repo.
- **Always `--auto`** — sweeps are unattended.
- **Subagent, not main context** — preserve your context for orchestration.
- **Cap findings** at ~10 new issues per sweep to avoid flooding.
- **Always pull latest `main`** before auditing.

## Cron Integration (optional)

```
# Example: nightly weekday sweep at 21:00
0 21 * * 1-5 <BOT_DIR>/evening-audit.sh        # points this skill at /sweep
```

## Instructions

1. Always use the target picker — never choose manually unless the user explicitly passes a repo.
2. Always pull latest main before auditing.
3. Always run via background subagent.
4. Always log the sweep after completion, even on failure.
5. If the suggested directory doesn't exist, let the planning skill discover the right paths.

$ARGUMENTS
