# Bot Archetypes

Proven bot configurations for common use cases. Each archetype lists the MCP servers, skills, and Telegram settings that work well together.

## Skill Layers

Skills come from two sources:

1. **Global skills** (`~/.claude/skills/`) — shared by all bots. General-purpose development, review, and workflow tools. Managed by your own global-skills repository (a single repo of common slash-skills synced to `~/.claude/skills/` on every machine).

2. **Project skills** (`~/claudlobby/my-bot/.claude/skills/`) — specific to one bot. Only visible when Claude runs in that bot's directory.

This separation means engineer bots inherit `/review-work pr` and `/worktree` from the global layer without needing their own copies, while a business bot's shop-specific skills (e.g. an order-lookup tool living in that fleet's local library overlay) stay invisible to everyone else.

### Global Skills (via your global-skills repo)

These are available to every bot on the system:

| Skill | Purpose |
|-------|---------|
| `/review-work pr` | Structured PR code review |
| `/commit-push-pr` | Stage, commit, push, and open PR in one step |
| `/quick-commit` | Fast conventional commit |
| `/worktree` | Git worktree management for parallel work |
| `/audit tech-debt` | Find and plan tech debt remediation |
| `/audit security` | Security vulnerability scanning |
| `/audit docs` | Documentation audit against codebase |
| `/audit design` | Visual/UX audit of deployed apps |
| `/audit frontend-perf` | Performance analysis |
| `/product-vision` | Architecture-aware product roadmapping |
| `/implement-plan` | Execute a written plan step-by-step |
| `/session handoff` | Capture context for session continuity |
| `/session resume` | Restore context from a previous session |
| `/investigate-app` | Debug production issues |
| `/audit repo-health` | Cross-repo health overview |
| `/lessons` | Capture and review learnings |
| `/notes` | Persistent notes across sessions |

Infrastructure skills (install based on what you use):
- `/vercel deploy`, `/vercel status`, `/vercel logs`
- `/railway deploy`, `/railway status`, `/railway logs`
- `/neon query`, `/neon branch`, `/neon info`
- `/modal deploy`, `/modal status`, `/modal logs`
- `/snowflake-query`, `/snowflake-cutover`
- `/dbt` — dbt command runner

---

## Manager / Orchestrator Bot

The brain of the fleet. Converses freely, delegates to workers, monitors health.

**Telegram:** `requireMention: false` (hears everything in its group)
**Model:** Opus (needs strong reasoning for orchestration)

### MCP Servers

| Server | Purpose |
|--------|---------|
| GitHub | Oversee PRs, merges, issues across all repos |
| Notion | Personal + work task management |
| Gmail | Email access (personal + work accounts) |
| Google Calendar | Scheduling, meeting prep |
| Slack | Monitor channels, post updates |
| Home Assistant | Smart home (optional) |
| Spotify | Music control (optional) |
| Docker | Container management (optional) |

### Project Skills

| Skill | Purpose |
|-------|---------|
| `/dispatch` | Send tasks to fleet bots via tmux with structured tracking |
| `/lifecycle` | Full pipeline: implement → review → iterate → merge → retro → issues |
| `/fleet-status` | Health check across all bots (alive/dead, reported context-degraded state, idle/working) |
| `/fleet-pulse` | Actionable fleet health — restart dead workers, flag stuck panes, protect WIP |
| `/prs` | PR activity across repos: authored, review requests, CI status |
| `/status` | Readout for the human: what moved, and what is waiting on their decision. Consumes `claudlobby brief`; adds the PR surface brief lacks |
| `/selfcheck` | Self-diagnostic — session health, MCP connectivity, tmux fleet state, fleet-state ledger |
| `/sweep` | Periodic code-quality sweep across fleet repos (cron or on demand) |
| `/restart` | Graceful self-restart |
| `/briefing` | Scheduled daily digests (morning/midday/evening) |
| `/data-alert-sweep` | Batch-process alerts: Slack → investigate → fix → PR → reply |
| `/pi-status` | Full Pi system health (all bots, RAM, disk, temp, crons) |
| `/emails` | Read, search, draft emails |
| `/calendar` | Unified calendar view |
| `/tasks` | Notion task management |
| `/contacts` | Contact/relationship management |
| `/finance` | Portfolio/spending tracking (optional) |
| `/weather` | Location-aware forecasts (optional) |
| `/home` | Smart home control (optional) |
| `/triage` | Surface untracked items from meetings/emails into Notion |

### Crons

```crontab
# Daily briefings
30 8 * * * /path/to/bot/briefing-cron.sh morning
0 13 * * * /path/to/bot/briefing-cron.sh midday
30 18 * * * /path/to/bot/briefing-cron.sh evening
```

---

## Engineer Bot

Focused executor. Takes tasks from the manager, works in worktrees, creates PRs.

**Telegram:** `requireMention: true` (only responds when @mentioned)
**Model:** Opus (needs strong coding ability)

### MCP Servers (lean)

| Server | Purpose |
|--------|---------|
| GitHub | Repos, PRs, branches |
| Slack | Read-only context from alert/discussion channels (optional) |
| Notion | Update task status after completing work (optional) |

### Project Skills

| Skill | Purpose |
|-------|---------|
| `/restart` | Graceful self-restart |
| `/eng-status` | Self-diagnostic (uptime, memory, session) |

**Inherits from global:** `/review-work pr`, `/worktree`, `/commit-push-pr`, `/audit tech-debt`, `/audit security`, `/implement-plan`

### CLAUDE.md Essentials

```markdown
## How You Work

- You don't initiate work — you execute what's assigned
- Work in git worktrees for isolation
- Always branch, always PR, never push to main
- When done, report back via report-back.sh
- If blocked, report back immediately. Don't spin.
```

### No Crons

Engineer bots are reactive — they only work when dispatched. No scheduled briefings or automated tasks.

---

## Code Reviewer Bot

Dedicated PR reviewer. Fast, thorough, runs on Sonnet to save cost.

**Telegram:** `requireMention: true`
**Model:** Sonnet (`model: sonnet` in `fleet.yaml`)

### MCP Servers (minimal)

| Server | Purpose |
|--------|---------|
| GitHub | Read PRs, files, comments; post reviews |
| Notion | Update task status after review (optional) |

### Project Skills

| Skill | Purpose |
|-------|---------|
| `/restart` | Graceful self-restart |
| `/review-status` | Self-diagnostic |

**Inherits from global:** `/review-work pr` (the core skill)

### CLAUDE.md Essentials

```markdown
## How You Work

- You only do code reviews — nothing else
- When given a PR, use /review-work pr
- Focus on correctness, clean design, test coverage, maintainability
- Be constructive, not pedantic — flag what matters
- Approve, request changes, or comment — always give a clear verdict
- Never merge PRs — only review them
```

### Why Sonnet?

Code review is read-heavy and pattern-matching — Sonnet handles it well at lower cost and faster speed. Reserve Opus for bots that need complex reasoning (orchestration, multi-step engineering).

---

## Designer / Visual QA Bot

Visual quality auditor. Crawls deployed apps, screenshots at multiple viewports, compares against design tokens, files issues.

**Telegram:** `requireMention: true`
**Model:** Opus (needs visual reasoning)

### MCP Servers

| Server | Purpose |
|--------|---------|
| GitHub | File issues, read repos for design tokens |
| Notion | Track design debt (optional) |

### Project Skills

| Skill | Purpose |
|-------|---------|
| `/visual-crawl` | Autonomous crawl + screenshot + issue-filing |
| `/restart` | Graceful self-restart |

**Inherits from global:** `/audit design`, `/audit frontend-perf`

### CLAUDE.md Essentials

```markdown
## How You Work

- Crawl deployed apps at 3 viewports (mobile, tablet, desktop)
- Compare against design system tokens and norms
- File GitHub issues for every finding with screenshots
- Focus on visual consistency, accessibility, responsive behavior
```

---

## Business / E-Commerce Bot

Customer-facing bot with personality. Handles orders, emails, fulfillment, task tracking.

**Telegram:** `requireMention: false` (conversational in its own group)
**Model:** Opus (needs personality + multi-tool coordination)

### MCP Servers

| Server | Purpose |
|--------|---------|
| Shopify | Orders, products, customers, inventory |
| Printify | Print fulfillment, production status (if drop-shipping) |
| Gmail | Customer email (one or more accounts) |
| Notion | Task tracker, contacts, content calendar |
| GitHub | Codebase for the business site/app |

### Project Skills

| Skill | Purpose |
|-------|---------|
| `/emails` | Customer email inbox, drafting, sending |
| `/tasks` | Notion task/issue management |
| `/triage` | Surface issues from email → enrich with order data → create tasks |
| `/restart` | Graceful self-restart |
| `/selfcheck` | Self-diagnostic |

### Crons

```crontab
# Daily briefings
0 9 * * * /path/to/bot/briefing-cron.sh morning
0 14 * * * /path/to/bot/briefing-cron.sh afternoon
```

### Persona

The business bot is the one place where personality matters. Write the CLAUDE.md persona section to match your brand voice. The bot should be in-character in customer emails, team chat, and internal work.

---

## Fleet Composition Examples

### Solo Developer

```
Manager/Assistant (Opus) — personal + work, all integrations
Code Reviewer (Sonnet) — PR reviews on demand
```

### Small Business + Dev

```
Manager/Assistant (Opus) — personal life, orchestration
Business Bot (Opus) — customer service, orders, email
Engineer (Opus) — code work
Code Reviewer (Sonnet) — PR reviews
```

### Team with Multiple Domains

```
Manager/Orchestrator (Opus) — fleet management, briefings
Business Bot A (Opus) — company A operations
Business Bot B (Opus) — company B operations
Work Engineer (Opus) — day job code
Personal Engineer (Opus) — side projects
Code Reviewer (Sonnet) — shared across all
Designer (Opus) — visual QA across all
```
