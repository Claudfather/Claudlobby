---
name: sweep
description: "Periodic code-quality sweep across fleet repos. Picks the stalest repo, runs the appropriate audit skill via a subagent, records findings. Scheduled via cron or manually."
argument-hint: "[<repo-name>] [--type tech-debt|security-audit|docs-review|data-model-audit] [run|status]"
---


# Sweep

A scheduled maintenance pass over the fleet's repos. Run on a cadence (e.g., weekly via cron) to keep repositories from accumulating silent rot.

## How it works

The sweep orchestrates whatever code-audit skills the bot has available. Pick a repo → pick an audit type → dispatch to a subagent so it doesn't pollute your main context.

| Audit type | What it finds |
|-----------|---------------|
| tech-debt | Dead code, god modules, deprecated patterns |
| security | Credential leaks, injection vectors, auth gaps |
| docs | Stale or missing documentation |
| data-model | Schema / app mismatches (if applicable) |
| enhancement | UX gaps, missing features, inconsistencies |

**Which skill runs each one:** use the audit skills you actually have. clauDNA
provides these as `/claudna:audit <type>` and `/claudna:product-enhance` for the
enhancement row — check your own skill list rather than assuming. If no audit
skill is installed, do the audit directly: read the repo against the "what it
finds" column and file the same issues.

Run non-interactively and file GitHub issues directly — with clauDNA that is
`--auto --output github`; with another provider, use its equivalent flags.

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

**Step 4: Count the in-scope files, then launch the audit subagent**

Count first, in the parent, **before** dispatching:

```bash
FILES_FOUND=$(find <REPOS_ROOT>/<REPO>/<DIR> -type f <TYPE_FILTER> | wc -l)
```

This is the only coverage number that does not come from the thing being
measured, so it is what the subagent's self-reported counts get checked
against. Match `<TYPE_FILTER>` to what the audit skill treats as in scope — the
anchor is only as good as that match.

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
- FILES_READ: how many files you actually opened and read
- FILES_GREPPED: how many you only pattern-matched, never read
- SKIPPED: each path you did not cover, and why — or the single word `none`
- CAP_HIT: the limit that stopped you (tool cap, time, context, unreachable
  source), or the single word `none`
```

Do not ask the subagent for `FILES_FOUND` — the parent counted it in Step 4,
and asking the thing being measured for the denominator would remove the only
number in the record that is not a self-report.

**These four are still self-reported and unverified.** Nothing counts real tool
calls, so a subagent can report `FILES_READ: 14` without opening a file, just as
it could have written a plausible sentence — and specific figures read as *more*
rigorous to whoever scans the record, which makes them worse in that respect.
Structuring buys effort and a checkable shape, not a guarantee. One anchored
field, four self-reported ones.

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
- **Coverage bounds**, composed from the subagent's counts:
  `read <FILES_READ> of <FILES_FOUND> in-scope files; <FILES_GREPPED> pattern-matched only; skipped: <SKIPPED>; cap: <CAP_HIT>`

The bounds field is not optional bookkeeping. A record of "0 findings" without
it is indistinguishable from a thorough all-clear, and whatever selects the
next target reads that as "audited, park it". Record what was NOT covered or
the count means nothing.

`<FILES_FOUND>` is the parent's Step 4 count, never the subagent's. Check the
self-reported pair against it before recording: `FILES_READ + FILES_GREPPED`
cannot exceed `FILES_FOUND`. If it does, the report is internally inconsistent
— record "coverage unreliable" rather than the numbers.

That catches an inconsistent report, not a plausible one, and the difference is
worth being clear about: a subagent reporting a `FILES_READ` equal to the real
`FILES_FOUND` passes every check available here and may still have read nothing.

Apply the smell test to the figures as well as the prose: **anything that could
describe any sweep of any repo, unchanged, is not a coverage statement.** Counts
that never vary between sweeps, or a `FILES_READ` that always equals
`FILES_FOUND`, deserve the same doubt as a vague sentence. If your tracker has no bounds field, add one before
recording; if the sweep died, record that nothing was covered rather than
omitting the entry — a missing record usually reads as "never audited", which
is right by accident, but an entry with a timestamp and no bounds reads as
audited, which is wrong on purpose.

If you maintain per-repo `last_swept` in fleet-state.json, update it here — but
only for a pass that actually covered something. Stamping `last_swept` after a
failed sweep parks the repo for a full cycle on work that never happened.

**Step 6: Report**

Post a concise Telegram summary (`parseMode: "Markdown"`) with:
- Repo swept
- Audit type
- Count of findings
- What the pass did not cover (one line from BOUNDS) — a bare finding count
  reads as exhaustive to whoever sees it
- Top 3 GitHub issue URLs (if any)

Or, if this sweep feeds a daily briefing, **don't post** — let the briefing pick up the latest report.

### 2. Status

Show sweep health without running anything:

- Last swept repo + when
- List of repos and their last-swept timestamps (identify stalest)
- Most recent sweep's findings

## Failure handling

- Target picker returns nothing → all repos recently audited. Emit "all repos current" and exit.
- Subagent fails / times out → log the failure and move on. Don't block future sweeps. Record it as zero coverage ("sweep failed at <step>, nothing in <dir> audited"), not as an entry with no bounds — and do not stamp `last_swept`.
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
