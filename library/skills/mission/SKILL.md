---
name: mission
description: "Create or update a PROJECT_MISSION.md for a repo — defines the north star, guiding principles, and what success looks like. Used by /product-vision and /autonomous-sprint to prioritize work."
argument-hint: "[--bootstrap] [--edit]"
---

# Mission — Define a Project's North Star

Create or update a `PROJECT_MISSION.md` file that grounds all autonomous work in a clear direction. This doc is read by `/product-vision` and `/autonomous-sprint` to score and prioritize features.

## Arguments

Parse `$ARGUMENTS`:
- `--bootstrap`: Auto-generate a mission from codebase exploration (no interactive questions). Good for first run.
- `--edit`: Open existing PROJECT_MISSION.md for refinement. Present current mission, ask what's changed.
- No flags: interactive — ask questions, then draft.

## When to use

- Setting up a new repo for autonomous work
- After a major pivot or scope change
- When `/product-vision` notes "no PROJECT_MISSION.md found"
- Periodically (quarterly) to reassess direction

## Procedure

### If --bootstrap:

1. Launch Explore subagents to understand the codebase:
   - Architecture and capabilities
   - Recent git log (last 30 days) — what direction is development moving?
   - README, CLAUDE.md — stated goals
   - Open GitHub Issues — what's being asked for?
   - Package.json / pyproject.toml — project metadata

2. Synthesize findings into a mission doc. Present to user for approval.

### If --edit:

1. Read existing PROJECT_MISSION.md
2. Ask: "What's changed? Any shifts in direction, users, or priorities?"
3. Update based on input

### If interactive (default):

Ask these questions one group at a time:

**Identity:**
- What does this project do in one sentence?
- Who uses it and why?

**Direction:**
- What is this project becoming? (Not what it is today — what's the trajectory?)
- What would make you say "this project succeeded"?

**Principles:**
- When you have to choose between two features, what wins? (Speed vs quality? Breadth vs depth? Users vs developers?)
- What should this project never become?

**Scope:**
- What's in bounds for autonomous work? (Bug fixes? New features? Refactoring? Documentation?)
- What requires your explicit approval before building?

### Output

Write `PROJECT_MISSION.md` to the repo root:

```markdown
# Project Mission — [Project Name]

## What this project is
[One paragraph — what it does today and who it serves]

## What it's becoming
[One paragraph — the trajectory, the vision]

## North star
[One sentence — the ultimate value proposition. This is what every feature should move toward.]

## Guiding principles
[3-5 bullets — decision-making framework for prioritization]
- e.g., "Reliability over new features"
- e.g., "Push notifications, not pull dashboards"
- e.g., "Every insight should be actionable"

## In bounds for autonomous work
[What the fleet can build without asking]
- Bug fixes and tech debt
- Features scored 🟢 by /product-vision
- Documentation and test coverage
- Performance improvements

## Requires approval
[What needs the user's sign-off]
- New integrations with external services
- Breaking API changes
- Features that change the user-facing workflow
- Anything touching auth or billing

## Success metrics
[How to know if we're moving in the right direction]
- e.g., "Daily active usage increases"
- e.g., "Time to first insight decreases"
- e.g., "Zero manual steps in the pipeline"
```

Present the draft. Iterate with user until approved. Then write to disk.

## Notes

- Keep it short. This is a compass, not a business plan.
- The "in bounds" / "requires approval" sections are critical for autonomous operation — they define the safety boundary.
- Update this doc when direction changes, not every session.
