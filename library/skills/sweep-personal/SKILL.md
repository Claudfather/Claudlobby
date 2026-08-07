---
name: sweep
description: "Nightly code sweep variant that integrates with a personal `<ASSISTANT_TOOLS_DIR>` of helper scripts (rolling-audit selector + audit-tracker). Feeds a morning briefing. Template — replace placeholders with your own tooling paths."
argument-hint: "[run|status]"
---


# Sweep (personal-assistant variant)

Automated nightly code sweep that rotates through repos. Picks the stalest area via an external suggester script, runs the appropriate planning skill, creates GitHub issues, and logs everything for a morning briefing.

**This skill is a reference.** It assumes you've implemented a set of personal helper scripts under `<ASSISTANT_TOOLS_DIR>`:
- `rolling-audit.sh suggest` — outputs `REPO`, `DIR`, `TYPE`, `STALENESS`, `REPO_PATH` for the stalest area
- `audit-tracker.py` — records sweep history (`log`, `stale`, `history` subcommands). `log` requires `--bounds`; `stale` reads a missing record as "not audited", so a pass that cannot log gets re-selected next run
- `audit-results/latest.md` — the artifact the morning briefing reads

Replace the `<ASSISTANT_TOOLS_DIR>` placeholders with your actual path before installing.

## How it works

The sweep orchestrates three existing skills based on the audit type:

| Type from `rolling-audit.sh` | Skill to run | What it finds |
|------------------------------|-------------|---------------|
| `tech-debt` | `/claudna:audit tech-debt` | Dead code, god modules, deprecated patterns, missing abstractions |
| `security` | `/claudna:audit security` | Credential leaks, injection vectors, auth gaps, TLS issues |
| `enhancement` | `/claudna:product-enhance` | UX gaps, missing features, performance issues, API inconsistencies |

Each skill is run with `--auto --github-issues` flags so it operates non-interactively and creates GitHub issues directly.

## Operations

### 1. Run (default)

Execute a full sweep cycle. This is what the 9pm cron triggers.

**Step 1: Pick target**
```bash
bash <ASSISTANT_TOOLS_DIR>/rolling-audit.sh suggest
```
Outputs: REPO, DIR, TYPE, STALENESS (days), REPO_PATH. The script rotates through repos and directories, always picking the stalest area.

**Step 2: Pull latest code**
```bash
cd <REPO_PATH> && git checkout main && git pull
```
Always sweep against the latest main branch.

**Step 3: Count the in-scope files, then launch the audit subagent**

Count first, in the parent, **before** dispatching:

```bash
FILES_FOUND=$(find <REPO_PATH>/{DIR} -type f {TYPE_FILTER} | wc -l)
```

This is the one coverage number that does not come from the thing being
measured. Everything the subagent reports about its own coverage is a
self-report; this is not, so it is the value the others get checked against.

Pick `{TYPE_FILTER}` to match what the audit skill treats as in scope (e.g.
`-name '*.py'` for a Python tech-debt pass). **The anchor is only as good as
that match** — a filter wider or narrower than the audit's own notion of scope
makes `FILES_FOUND` wrong in a different direction, so keep it aligned with the
skill you are about to run rather than counting every file in the tree.

Spawn a **background** Agent (subagent_type: general-purpose) with this prompt structure:

```
You are running an automated {TYPE} audit.

1. cd to {REPO_PATH}
2. Run the /{SKILL} skill with --auto --github-issues flags, targeting the {DIR} directory
3. Use: Skill tool with skill="{SKILL}" and args="--auto --github-issues {DIR}"

After the skill completes, collect:
- All GitHub issue URLs created
- Key findings summary with severity levels
- Positive notes (what's well-implemented)

Return a structured summary with:
- REPO: {REPO}
- DIR: {DIR}
- TYPE: {TYPE}
- ISSUES: comma-separated list of issue URLs
- FINDINGS: brief summary of key findings
- FILES_READ: how many files you actually opened and read
- FILES_GREPPED: how many you only pattern-matched, never read
- SKIPPED: each path you did not cover, and why — or the single word `none`
- CAP_HIT: the limit that stopped you (tool cap, time, context, unreachable
  source), or the single word `none`
```

Do not ask the subagent for `FILES_FOUND`. The parent counted it in Step 3, and
asking the thing being measured to also report the denominator would remove the
only number in the record that is not a self-report.

**What this does and does not guarantee — read this before trusting a record.**
These four fields are **still self-reported and unverified**. Nothing here
counts real tool calls, so a subagent can report `FILES_READ: 14` without
opening a file, exactly as it could have written a plausible sentence. Numbers
are in some ways *worse* than prose here, because specific figures read as more
rigorous to anyone scanning the tracker.

What structuring actually buys: casual fabrication becomes more effortful, the
fields become checkable against the Step 3 anchor, and there is somewhere for a
real check to attach later. That is a raised floor, not a closed hole. The
honest one-line version is **one anchored field, four self-reported ones.**

**IMPORTANT: The subagent needs full permissions.** It will:
- Read many files across the repo (Glob, Grep, Read)
- Search code patterns (Grep)
- Create GitHub issues (mcp__github__create_issue)
- Run the Skill tool

If the subagent can't create issues due to permissions, the sweep fails silently. Ensure GitHub MCP tools are in the allow list.

**Step 4: Process results**

When the subagent completes, parse its output for REPO, DIR, TYPE, ISSUES,
FINDINGS, and the five coverage fields.

**Build the bounds string from the counts — do not retype it as prose.** Fill
every brace from a reported value:

```bash
python3 <ASSISTANT_TOOLS_DIR>/audit-tracker.py log --repo {REPO} --directory {DIR} --type {TYPE} --issues {ISSUE_URLS} \
  --bounds "read {FILES_READ} of {FILES_FOUND} in-scope files in {DIR}; {FILES_GREPPED} pattern-matched only; skipped: {SKIPPED}; cap: {CAP_HIT}"
```

`{FILES_FOUND}` is the parent's count from Step 3, never the subagent's.

**Sanity-check the self-reported counts against the anchor before logging.**
`FILES_READ + FILES_GREPPED` cannot exceed `FILES_FOUND` — the subagent cannot
have covered more files than exist in scope. If it does, the report is
internally inconsistent and the counts are not usable:

```bash
python3 <ASSISTANT_TOOLS_DIR>/audit-tracker.py log --repo {REPO} --directory {DIR} --type {TYPE} --issues {ISSUE_URLS} \
  --bounds "coverage unreliable: subagent reported {FILES_READ} read + {FILES_GREPPED} grepped against {FILES_FOUND} in-scope files, which is impossible; treat this area as unaudited"
```

This catches an inconsistent report, not a plausible one. A subagent reporting
`FILES_READ` equal to the real `FILES_FOUND` passes every check here and may
still have read nothing — see the honest-limits note in Step 3.

Backstop, and it now covers the numbers too: **anything that could describe any
sweep of any repo, unchanged, is not a real bounds statement.** It must name at
least one specific file, directory, count or limit actually hit this pass. Apply
the same suspicion to the figures — counts that never vary between sweeps, or a
`FILES_READ` that always equals `FILES_FOUND`, deserve exactly the doubt a vague
sentence would get. A run of suspiciously round, suspiciously complete records
is the signal that the self-reported half has stopped meaning anything.

**If the subagent did not return the counts**, that is itself the honest bounds.
Do not estimate them — you did not do the scanning, and a fabricated count is
worse than an admitted gap because it looks like evidence:

```bash
python3 <ASSISTANT_TOOLS_DIR>/audit-tracker.py log --repo {REPO} --directory {DIR} --type {TYPE} --issues {ISSUE_URLS} \
  --bounds "coverage unknown: subagent completed and reported findings but no file counts; treat this area as unaudited"
```

**Step 5: Write summary**

Overwrite `<ASSISTANT_TOOLS_DIR>/audit-results/latest.md` with:
```markdown
# Audit Results — {DATE} (Evening)

**Repo:** {REPO}
**Directory:** {DIR}
**Type:** {TYPE}
**Issues Created:** {COUNT}
**Coverage:** read {FILES_READ} of {FILES_FOUND} in-scope files; {FILES_GREPPED} pattern-matched only; skipped: {SKIPPED}; cap: {CAP_HIT}

## High Priority ({N})
- [#{NUM}](URL) — one-line description

## Medium Priority ({N})
- [#{NUM}](URL) — one-line description

## Key Findings
- bullet points

## Positive Notes
- what's well-implemented
```

**Step 6: No Telegram**

Do NOT send a Telegram message. The `/briefing morning` skill reads `latest.md` automatically and includes it in the morning briefing.

### 2. Status

Show sweep health without running anything:

```bash
python3 <ASSISTANT_TOOLS_DIR>/audit-tracker.py stale
```
```bash
python3 <ASSISTANT_TOOLS_DIR>/audit-tracker.py history
```
```bash
cat <ASSISTANT_TOOLS_DIR>/audit-results/latest.md
```

Reports: last audit date/repo, stalest repos needing attention, full audit history, and the most recent findings.

## Failure Handling

- If `rolling-audit.sh suggest` returns no suggestion → all repos recently audited. Write "All repos current" to latest.md.
- If the subagent fails or times out → log the failure with `audit-tracker.py`, write an error summary to latest.md noting the repo and failure reason.

  A died sweep has bounds too, and they are the most valuable ones on the page:
  nothing was covered, and the record has to say so or the area looks audited.
  Write what **you** observed from the parent session — which step it reached,
  whether it returned anything before dying, how long it ran:

  ```bash
  python3 <ASSISTANT_TOOLS_DIR>/audit-tracker.py log --repo {REPO} --directory {DIR} --type {TYPE} --issues "" \
    --bounds "SWEEP FAILED — no coverage. Subagent {failed|timed out} at {step}; {returned partial findings for X | returned nothing} after {N} min. Nothing in {DIR} was audited; re-run before treating this area as covered."
  ```

  Fill in the braces from what actually happened. A generic "sweep failed" is
  the same defeat as `"n/a"` on the success path — it parses, it records, and it
  tells the next run nothing about whether the area still needs a pass.
- If the suggested directory doesn't exist in the repo → the planning skill will scan the repo and find the right directories automatically. Don't fail on this.
- If GitHub issue creation fails (permissions) → still log findings to latest.md without issue links.

## Cron Integration

The 9pm weekday cron (`evening-audit.sh`) sends `/sweep` to the tmux session. The skill handles everything from there.

```
# In crontab:
0 21 * * 1-5 <BOT_DIR>/evening-audit.sh
```

## Instructions

1. Always use `rolling-audit.sh suggest` to pick the target — never choose manually
2. Always pull latest code before auditing
3. Always run via background subagent — don't block the main session
4. Always log with `audit-tracker.py` after completion, even on failure — and always build `--bounds` from values the run actually produced: the subagent's file counts on the success path, what you observed on the failure path. A bounds string that could describe any sweep of any repo unchanged is not one, however plausible it reads
5. Always overwrite `latest.md` with fresh results
6. The directory suggestion is a hint — if it doesn't exist, let the planning skill discover the right paths

$ARGUMENTS
