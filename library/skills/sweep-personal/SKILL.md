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

**Step 3: Launch the audit subagent**

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
- FILES_FOUND: how many files in {DIR} were in scope for this audit type
- FILES_READ: how many of those you actually opened and read
- FILES_GREPPED: how many you only pattern-matched, never read
- SKIPPED: each path you did not cover, and why — or the single word `none`
- CAP_HIT: the limit that stopped you (tool cap, time, context, unreachable
  source), or the single word `none`
```

**These are counts, not prose, and that is the point.** The parent composes the
tracker's bounds string from them below. A number cannot be produced without
having done the counting, so an engineer who did not do the work cannot fill
this in — where a free-text "bounds" field can always be satisfied with
something plausible-sounding that describes no particular sweep.

`SKIPPED` and `CAP_HIT` still carry free text, and that half is **not**
derivable — a reason for skipping is a judgement. Treat those as unverified
context around the counts, which are the load-bearing part.

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

This is derivation, not validation, and the difference is the whole point.
A bounds statement the engineer *types* can always be satisfied by something
plausible — `"Reviewed the repository structure and did not find significant
issues in the areas examined"` is not `"n/a"`, parses fine, and describes any
sweep of any repo unchanged. A bounds statement **composed from counts the
sweep actually produced** cannot be written without having done the counting.
Banning bad values is a deny-list, and the next bad value is never on it.

Backstop for the free-text halves: **a bounds statement that could describe any
sweep of any repo, unchanged, is not a real one.** It must name at least one
specific file, directory, count, or limit actually hit this pass.

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
