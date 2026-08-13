---
name: simulate-cold-start
description: "Run the onboarding path exactly as a brand-new user would — export a history-free tree, drive /setup from a blind session, then reap everything it created. Use before merging any change to README, getting-started, the setup skill, setup-system/setup-fleet, fleet.yaml.seed, or .env.seed.example."
argument-hint: "[prepare|reap|report] [--ref REF]"
---

# Simulate cold start

The method behind the **"Validating changes to the onboarding path — MANDATORY"** gate in the
root `CLAUDE.md`. Rationale and the three validation levels live in
[`documentation/validating-cold-start.md`](../../../documentation/validating-cold-start.md); this
skill is the runnable procedure.

Mechanical work is delegated to `lib/coldstart-harness.sh`, which is deterministic and
idempotent. What stays here is the part that needs judgment: who runs the cold arm, what counts
as a finding, and when to stop.

## The one rule that makes this work

**You cannot run the cold arm yourself, and neither can a subagent you spawn.**

Two independent reasons, both fatal:

1. **Contamination.** By the time you have read `CLAUDE.md` and this file you know about the
   venv, PEP 668, `python3 -m pip` vs `pip`, and `fleet.yaml.seed`. You will route around every
   gap without noticing and report success. `documentation/validating-cold-start.md` lists this
   as blind spot #3.
2. **Skill registration is bound at session start.** A subagent inherits *this* session's skill
   registry, so it can only ever read the exported `SKILL.md` as a file — it can never invoke
   `/setup`. Since the invocation is the thing under test, a subagent cannot test it.

So the cold arm is **a new interactive Claude Code session, launched by the human**, whose entire
input is `/setup`. Your job is to prepare it, stay out of it, then reap and analyse.

## Design: instrument and reap, do not fence

The instinct is to fence the cold run so it cannot touch the host. Resist it. You cannot say
"don't enroll supervision" without revealing that supervision exists, which contaminates the
measurement — and the escape is usually where the findings are. A run that enrolled seven
undisclosed launchd agents is exactly how you learn that `/setup` enrolls seven undisclosed
launchd agents.

So: snapshot the host, let the run do whatever it does, diff, and reap deterministically.

`reap` only ever removes units, unit files, sockets and processes **absent from the pre-run
snapshot**. A production fleet on the same host is invisible to it. That is the property that
makes this safe to run on a machine that matters.

Fence only the genuinely irreversible. Tell the human to decline if the cold session asks for:
- `sudo` anything (notably `setup-system` phase 7, which writes to `/Library/Application Support`)
- `pip --break-system-packages`, or a global/pipx install
- writes outside the exported tree and `~/Library/LaunchAgents` (or `~/.config/systemd/user`)

## Procedure

### 1. Prepare

```bash
lib/coldstart-harness.sh prepare            # or: --ref <branch>  --dir <path>
```

This runs a contamination preflight (inherited `CLAUDLOBBY_ROOT` / `CLAUDRON_VAULT_PATH`, a
user-level `~/.claude/CLAUDE.md`), exports `<ref>` with `git archive`, asserts the tree carries
none of `.git local .venv .env fleet.yaml runtime state`, records the host snapshot, and prints
the launch command.

**Export, never clone.** A `.git` carries the commit messages describing the defects you are
trying to rediscover.

### 2. Run the cold arm — human, new terminal

```
cd <printed tree> && claude
```

Then `/setup`, and **nothing else**. No hints, no follow-up questions answered beyond what the
skill itself asks for. If it asks for credentials, decide in advance:

- **Stop at the credential gate** — cheapest, and essentially every onboarding defect lives
  before it.
- **Go past it** — needs a *throwaway* BotFather bot. Never reuse a token a live fleet holds:
  Telegram allows one `getUpdates` consumer per token, so the two hosts silently steal each
  other's messages. Going past the gate is what exercises generate, supervision enrollment and
  first boot — the richest part of the surface.

### 3. Reap

```bash
lib/coldstart-harness.sh status             # what did the run create?
lib/coldstart-harness.sh reap --dry-run     # confirm the plan
lib/coldstart-harness.sh reap               # bootout units, kill sockets, delete the tree
```

Watchdogs are stopped before the things they watch — a 60s keepalive otherwise walks the bot
back up in between. Re-run `status` afterwards; every count should read zero.

Reap is safe to run standalone and long after the fact: its state lives in
`~/.claudlobby-coldstart`, outside the exported tree, precisely because the tree is what gets
deleted.

### 4. Harvest

```bash
lib/coldstart-harness.sh transcript
```

Transcripts survive the reap (they live under `~/.claude/projects/<cwd-with-slashes-as-dashes>/`),
including a separate directory per bot the run booted. Read the cold session's own narrative
rather than reconstructing what you think happened.

## The metric is exploration, not success

A capable agent gets almost anything working eventually, so "did it work?" measures persistence,
not documentation. Count **exploration events** — every moment the run:

- ran a documented command that failed and needed an alternative
- read source to learn how something works
- guessed a value, path or flag
- applied knowledge that is not on the page
- performed a step the docs never mention

Each is a documentation defect whether or not it was solved. Record: what it was trying to do →
what the doc said → what actually happened, **quoted verbatim** → what it did instead.

Also record findings that only a real host can produce: what appeared in Login Items, what
prompted interactively, what was left behind after the run.

## Reporting

Headline the exploration count, then:

1. **Blockers** — stopped the run, or would stop a new user
2. **Escapes** — anything that touched the host outside the tree, disclosed or not
3. **Residue** — what survived the run and whether an uninstall path exists
4. **Doc defects** — each with the verbatim quote that misled

Label harness artifacts explicitly and discount them: agents report their own sandbox failures as
project failures (a blocked pypi host is not an onboarding defect). Verify every claim against
source before acting — findings can be **stale rather than wrong**, half-fixed by an earlier
commit on the same branch.

**Do not fix before you measure.** Patching the skill first destroys the baseline and makes the
improvement unattributable — the failure mode Level 3 exists to prevent. Run, record, then fix,
then re-export to measure the delta.

## Limitations

- Stripping `.git` means neither arm can run a literal `git clone`, so step 1 of the README goes
  untested. Accept it, or stage a real remote.
- Process reaping matches on **command line**, not cwd. Bots are caught by their per-bot tmux
  socket; a stray process whose argv never names the tree is not caught.
- `launchctl list` counts drift on their own — macOS churns per-app agents constantly. Judge by
  the named diff `status` prints, never by a raw count.
- For the attributable version — blind agents on both a treatment and a **control** tree, so an
  improvement is measured rather than assumed — see Level 3 in
  `documentation/validating-cold-start.md`. Without the control arm, a treatment arm that
  succeeds tells you nothing.
