# Validating the cold-start path

Sibling of [`validating-bot-changes.md`](validating-bot-changes.md). That one covers changes to
how a *bot* behaves at runtime; this one covers changes to what a *brand-new user* is told to
run. The gate itself is stated in the root `CLAUDE.md` under **"Validating changes to the
onboarding path — MANDATORY"** — this document is the method behind it.

## Why a warm checkout cannot do this

Three independent blind spots, all pointing the same way:

| Blind spot | Consequence |
|---|---|
| The maintainer checkout has a long-standing `.venv`, `local/`, `.env`, and configured `~/.claude` | The documented install is never re-executed |
| CI installs on `ubuntu-latest` via `setup-python` — never externally-managed | PEP 668 cannot fire, so the blocker is invisible to the suite |
| An agent auditing its own project may carry prior knowledge in memory | It routes around gaps silently and reports success |

The observed cost of that combination: `pip install -e .`, the **first command in the README**,
failed on both first-class host families for months, against a fully green test suite (#947).

## The metric is exploration, not success

A capable engineer or agent will get almost anything working eventually. That makes "did it
work?" nearly useless as a signal — it measures persistence, not documentation.

Count **exploration events** instead. An exploration event is any moment you:

- ran a documented command that failed and had to find an alternative
- read source code to learn how something works
- guessed a value, a path, or a flag
- applied knowledge that is not on the page
- performed a step the docs never mentioned

**Every one is a documentation defect, whether or not you solved it.** Record each as: what you
were trying to do → what the doc said → what actually happened, *quoted verbatim* → what you did
instead. Report the total as a headline number.

## Level 1 — the mechanical gate (always runs)

`tests/test_cold_start_contract.py` runs inside the normal `pytest` invocation and enforces:

- no bare `pip` in any onboarding doc (Homebrew ships `pip3` only)
- every doc that installs the package first creates a venv (PEP 668)
- README, getting-started, and the `/setup` skill all name the same first-run template
- the CLI resolver probes a submodule that pulls the deps, and prefers `$CLAUDLOBBY_ROOT/.venv`
- placeholders are errors, not warnings
- `setup-system --dry-run` reports state it verified rather than post-conditions it skipped

This is a **floor, not a substitute.** It catches inconsistency; it cannot tell you a doc is
confusing, circular, or missing a step.

> When you add a gate here, verify it **fails** on the unfixed tree — not just that it passes on
> the fixed one. A gate that only ever passes is decorative. One version of the template-agreement
> test passed against `main`, because the two docs agreed with *each other* and the disagreement
> was with the skill; that only surfaced by checking the failure direction.

## Level 2 — a real cold run (for any onboarding change)

Driven by the **`simulate-cold-start`** skill, which wraps `lib/coldstart-harness.sh`:

```bash
lib/coldstart-harness.sh prepare       # preflight, export, host snapshot, launch command
# ... run the cold arm in a NEW terminal: cd <tree> && claude, then /setup ...
lib/coldstart-harness.sh status        # what did the run create?
lib/coldstart-harness.sh reap          # tear down units, sockets, processes, tree
lib/coldstart-harness.sh transcript    # harvest the session narrative
```

**Export, do not clone.** A `git clone` carries `.git`, and your own commit messages describe the
defects you are trying to rediscover. `prepare` uses `git archive` and refuses a tree that
carries any of `.git local .venv .env fleet.yaml runtime state`.

**Scrub the environment before the run starts, not after.** "Export, do not clone" isolates the
*tree*. Nothing isolates the *environment*, and a reader who follows every step above exactly
still gets a contaminated run if the shell running the exercise is a bot session — which it will
be, if a bot is the one measuring. Measured live (2026-08-27, `t-1787863382-7b06`): the first
`claudlobby validate` inside a correctly-exported test tree silently resolved `fleet.yaml` to
`/home/crog/claudlobby/fleet.yaml` — the shared install, a different checkout entirely — because
the launching bot session still had `CLAUDLOBBY_ROOT` exported. A second round surfaced
`GITHUB_PAT` set-but-empty from that same ambient session, which `validate` read and reported on
as though it belonged to the tree under test.

**The failure is silent in the general case.** Both instances above were caught only because the
tool happened to be loud about them — an error naming a path, a warning naming a var. A quieter
contaminated run produces a clean-looking result that measured the wrong tree entirely, with
nothing in the output to say so. Scrub before the first documented command runs, in a subshell
the exercise never leaves:

```bash
env -u CLAUDLOBBY_ROOT -u FLEET_NAME -u FLEET_ROOT -u BOT_NAME \
    -u TELEGRAM_BOT_TOKEN -u GITHUB_PAT -u STARTUP_PROMPT -u CLAUDE_FLAGS bash
# run the entire exercise inside this shell, including `prepare` itself
```

`FLEET_*`/`BOT_*`/`TELEGRAM_*` cover the rest of a composed bot's env, not just the two variables
that happened to fire loudly this time.

**Deliberately not scrubbed: `~/.env`.** It can hold real tokens on the host running the
exercise, and the right move is to disclose it as a bound, not hide it — a real stranger's cold
start also runs under a real `$HOME` with whatever is already in it. The goal is a *realistic*
cold start, not a *sterile* one: scrub what would misattribute the measurement to the wrong tree
or leak a credential into a report, not everything merely ambient.

**This is not covered by `tests/test_cold_start_contract.py`, and can't be.** That gate runs
inside the maintainer's or CI's own process and either does static analysis of doc text or drives
a subprocess through an environment the test itself constructs — it has no way to see, and no
reason to see, what the *operator's own shell* had exported before they typed the first command.
The contamination is a property of the runner's environment, not of anything in this repo. Level
1 remains a floor for what it does cover; this is why it can't be extended to cover this too.

**The cold arm cannot be a subagent.** Skill registration is bound at session start, so a
subagent inherits the parent's registry and can only read the exported `SKILL.md` as a file — it
can never invoke `/setup`. Since the invocation is what is under test, the arm must be a fresh
interactive session whose entire input is `/setup`.

**Instrument, do not fence.** You cannot tell a blind run "do not enroll supervision" without
revealing that supervision exists. So snapshot the host, let the run do whatever it does, and
diff. `reap` removes only what is *absent from the pre-run snapshot*, which is what makes this
safe to run on a host carrying a production fleet. Fence only the irreversible: no `sudo`, no
`--break-system-packages`, no writes outside the tree and the user unit directory.

Stop at the credential gate. Needing real tokens is not a reason to skip the exercise —
essentially every onboarding defect lives before it. Going *past* it exercises generate,
supervision enrollment and first boot — the richest part of the surface — but requires a
throwaway BotFather bot: Telegram permits one `getUpdates` consumer per token, so reusing a live
fleet's token makes two hosts silently steal each other's messages.

## Level 3 — blind A/B (for a change that claims to *fix* onboarding)

The strongest form, and the one that makes an improvement attributable rather than assumed.

1. Freeze two history-free trees: **treatment** from your branch, **control** from `main`.
2. Dispatch two agents that have not seen the work, with **identical** minimal prompts —
   *"set this up on a fresh machine following its own docs"* — plus an instruction to log every
   exploration event and quote real error output.
3. Fence them: **no `sudo`, no `--break-system-packages`, no pipx, no touching other project
   directories.** Without this, an agent recovering from PEP 668 can pollute the host's system
   python or install real launchd units.
4. Compare exploration counts, first blocker, and verdict across the two arms.

**The control arm is the point.** Without it, a treatment arm that succeeds tells you nothing
about whether your changes caused the success.

### Known limitations

- Stripping `.git` means neither arm can execute a literal `git clone`, so step 1 of the README
  goes untested. Accept it, or stage a real remote.
- Agents report their own sandbox failures as project failures. Ask them to label harness
  artifacts explicitly, and discount those.
- Findings can be **stale rather than wrong** — an agent may report a defect that an earlier
  commit in the same branch already half-fixed. Verify every claim against source before acting
  on it.

## What this found the first time (2026-08-01, PR #947)

Seven defects, two of them blockers at the first command; six further findings from the blind
arms, **two of which were defects the fix itself had just introduced** — a doc promoting
`lib/setup-system` without disclosing that it needs `sudo`, and prose ambiguous enough that a
fresh reader ran `generate` to test it.

That last pair is the argument for Level 3 in one line: **the person fixing the docs is the
person least able to see what the docs still fail to say.**
