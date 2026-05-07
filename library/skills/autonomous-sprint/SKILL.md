---
name: autonomous-sprint
description: "Autonomous development cycle: reads PROJECT_MISSION.md, evaluates backlog, runs /product-vision if needed, picks highest-impact issues, dispatches /lifecycle, and reports results. The conductor that orchestrates the fleet."
argument-hint: "<repo> [--max-issues N] [--dry-run] [--focus <area>]"
---

# Autonomous Sprint — Mission-Driven Development Cycle

The conductor skill. Reads a project's north star, evaluates the current backlog, identifies the highest-impact work, and dispatches the fleet to build it — all with Telegram visibility for the user.

## Arguments

Parse `$ARGUMENTS`:
- First word: repo name (required — e.g., `<repo-a>`, `<repo-b>`, `<repo-c>`)
- `--max-issues N`: cap on issues to work in this sprint (default: 3)
- `--dry-run`: plan the sprint but don't dispatch — just show what would be worked
- `--focus <area>`: constrain to a focus area (e.g., `frontend`, `api`, `reliability`)

## Prerequisites

- The repo must have a `PROJECT_MISSION.md` (run `/mission --bootstrap` first if missing)
- At least one engineer bot must be available
- A reviewer bot must be available for reviews

## Procedure

### Phase 1: Assess the Landscape

**Step 1: Read the mission**

Read `PROJECT_MISSION.md` from the target repo. Extract:
- North star (what to optimize for)
- Guiding principles (how to prioritize)
- In bounds / requires approval (safety boundary)
- Success metrics (what matters)

If no mission doc exists, stop and suggest running `/mission --bootstrap` first.

**Step 2: Evaluate the backlog**

Check GitHub Issues for the repo:
```bash
gh issue list --repo <owner/repo> --state open --limit 50 --json number,title,labels,createdAt
```

Categorize:
- **Mission-aligned**: directly moves toward the north star
- **Maintenance**: tech debt, bugs, documentation (always valid)
- **Out of scope**: doesn't align with mission or "in bounds" criteria

**Step 3: Backlog health check**

If fewer than 5 mission-aligned open issues exist:
- Run `/product-vision --auto --output github` on the repo to generate new issues
- Wait for issues to be created, then re-evaluate

If plenty of issues exist, skip to Phase 2.

### Phase 2: Plan the Sprint

**Step 4: Select issues**

Score each issue against the mission:

| Factor | Weight |
|--------|--------|
| Mission alignment | 40% |
| Impact (user-facing value) | 25% |
| Effort (inverse — prefer quick wins) | 20% |
| Dependencies (prefer no blockers) | 15% |

Select top N issues (default 3, configurable via `--max-issues`).

Verify each selected issue is "in bounds" per the mission doc. If any require approval, flag the user and exclude from auto-dispatch.

**Step 5: Determine execution order**

Order by:
1. Dependencies (blockers first)
2. Quick wins before large tasks (build momentum)
3. Related issues adjacent (minimize context switching)

**Step 6: Present the sprint plan**

Post to Telegram:
```
🏃 AUTONOMOUS SPRINT — [repo]
Mission: [north star one-liner]

Selected issues (in order):
1. #N — [title] (1-hop, high impact)
2. #N — [title] (1-hop, quick win)
3. #N — [title] (2-hop, mission-critical)

Estimated: [N] issues, [engineer bot] implementing, [reviewer bot] reviewing.
```

If `--dry-run`, stop here.

### Phase 3: Execute

**Step 7: Dispatch the assembly line**

For each issue, sequentially:

1. **Dispatch engineer** via `/dispatch`:
   - Include issue URL, repo, and relevant context from the mission doc
   - Engineer: acknowledge → branch → implement → test → /simplify → PR → report back

2. **On engineer completion**: immediately dispatch a reviewer
   - If approved: verify CI green → merge → post to Telegram
   - If changes requested: classify mechanical vs ambiguous
     - Mechanical: auto-dispatch back to engineer (max 3 cycles)
     - Ambiguous: flag the user, pause this issue, move to next

3. **After merge**: engineer runs `/compact` before next issue

4. **Between issues**: check engineer's context level
   - If >60%: restart bot before next issue
   - If OK: dispatch next issue

**Step 8: Sprint summary**

After all issues are worked (or max cycles reached), post summary to Telegram:

```
✅ SPRINT COMPLETE — [repo]

Merged:
- #N — [title] (PR #M)
- #N — [title] (PR #M)

Flagged for review:
- #N — [title] (ambiguous review feedback, needs your input)

Skipped:
- #N — [title] (requires approval per mission doc)

Next sprint candidates:
- #N, #N, #N (highest remaining scores)
```

### Phase 4: Learn

**Step 9: Post-sprint retro (optional)**

If any issues were merged, dispatch an engineer to run `/development-retro` on the combined work. Create follow-up issues from findings.

## Bot Selection

Map repo to engineer bot based on your Dispatch Routing rules (see manager `CLAUDE.md`). A typical shape:

- `<FLEET_ORG>` work repos (`<FLEET_REPOS>`) → `<primary-engineer>` (or `<data-engineer>` for data-heavy repos)
- Personal / side-project repos (`<PERSONAL_REPOS>`) → `<personal-engineer>` *(if your fleet has one; otherwise skip)*

Reviewer is whichever reviewer bot is idle and least-context-saturated.

**Safe-merge boundary**: shared / production repos where the fleet doesn't own the merge decision (e.g., `<FLEET_REPOS>` items with team review required) should have PRs created but NOT auto-merged. The sprint summary should note these as queued for human review.

## Safety

- **Never work issues marked "requires approval"** in the mission doc
- **Never merge to shared repos** (configure your fleet's exclusion list) — create PRs only
- **Always verify CI green** before any merge
- **Max 3 review cycles** per issue — flag the user after that
- **Respect context limits** — restart bots proactively
- **Emit everything to Telegram** — the user sees every dispatch, every merge, every decision

## Scheduling

This skill can be triggered:
- Manually: `/autonomous-sprint <repo> --max-issues 5`
- Via cron / launchd: see `bot-common/sprint-trigger.sh`
- By the manager: when the fleet is idle and there's mission-aligned work to do

## Notes

- This skill orchestrates, it doesn't implement. All work is done by the fleet bots.
- The mission doc is the constitution. If it says "reliability over features," a high-impact feature loses to a reliability fix.
- Start with small sprints (2-3 issues). Scale up as confidence in the loop grows.
- The sprint plan is posted BEFORE execution starts — the user can cancel or adjust.
