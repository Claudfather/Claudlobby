---
title: Claudfather Bot — Consolidated Implementation Spec
type: decision
status: shipped
owner: chris
created: 2026-05-11
tags: [claudfather, seed-fleet, bootstrap, onboarding, setup-assistant]
---

# Claudfather Bot — Consolidated Implementation Spec

Synthesizes Mason's original plan, Greg's engineer plan, Branden's operator plan, and Rajan's adversarial review, filtered through Chris's final design direction.

---

## 1. Architecture

### Concept

Claudfather is a solo bot on a **seed fleet** that ships committed to the claudlobby repo. It is the repo's built-in guide: when a user clones claudlobby, they get a real, runnable bot that helps them understand the system and create their own fleets.

There are no new rendering paths, no second template, no special-case composition logic. Claudfather uses the standard `compose_bot()` pipeline with `claude.md.j2`, exactly like every other bot. (Greg's position, unanimously supported.)

### Seed fleet vs. user fleets

| Property | Seed fleet | User fleet |
|---|---|---|
| Location | `fleet.yaml.seed` at repo root | `local/<name>/fleet.yaml` |
| Committed | Yes (tracked in git) | No (gitignored via `local/`) |
| Bots | 1 (claudfather) | N (user-defined) |
| Teams | None | User-defined |
| Runtime output | `runtime/seed/bots/claudfather/` | `local/<name>/runtime/bots/` |
| Purpose | Bootstrap + repo expert | User's operational fleet |

The seed fleet is **not** `fleet.yaml.example` (the template for user fleets). `fleet.yaml.example` stays as-is. `fleet.yaml.seed` is a separate, real, runnable manifest. (Mason's original proposal, refined by Greg.)

### Directory layout

```
claudlobby/
  fleet.yaml.seed                         # committed seed fleet manifest
  .env.seed.example                       # placeholder credentials template
  runtime/seed/bots/claudfather/          # gitignored generated output
    CLAUDE.md                             # composed from expertise + library
    bot.conf                              # env vars, CLI flags
    .mcp.json                             # MCP server config
    claudfather.service                   # systemd unit
    com.claudlobby.seed.claudfather.plist # launchd plist (service_prefix: com.claudlobby.seed)
    .claude/skills/                       # symlinked skills
    memory/                               # persistent state
    data/                                 # scripts, state files
  library/expertise/setup-assistant.md    # new expertise file
  library/skills/bootstrap/SKILL.md       # new skill
  library/skills/doctor/SKILL.md          # new skill
  lib/setup-host.sh                       # never shipped -- superseded by lib/setup-system + lib/setup-fleet (see §10)
```

### Relationship to user fleets

Claudfather knows how to help users create fleets in `local/<name>/`. It reads the repo's `library/`, `templates/`, `documentation/`, and `lib/` to answer questions. It does not manage, dispatch to, or operate on user fleet bots at runtime -- that is the user's manager bot's job.


## 2. Seed Fleet Configuration

### fleet.yaml.seed

```yaml
fleet:
  name: seed
  service_prefix: com.claudlobby.seed

  accounts:
    default: ~/.claude

  defaults:
    model: sonnet
    effort: default
    account: default
    prompt_suggestions: false
    mcp: [github]
    guardrails: [no-push-main, no-destructive-git, pii-protection, no-fabrication]
    protocols: [context-management, telegram-routing, telegram-formatting]
    sandbox:
      enabled: false
      auto_allow_bash: true
      network_allowed_domains:
        - api.github.com
        - api.telegram.org
        - "*.anthropic.com"

  bots:
    claudfather:
      name: Claudfather
      expertise: [setup-assistant]
      mission: >
        You are the claudlobby repo's built-in guide. Help users set up their host,
        create fleets, validate configurations, diagnose problems, and learn the
        system. You specialize in everything within this repo. You do not dispatch
        work or manage other bots -- you teach users to build their own fleets.
      model: opus
      effort: max
      skills: [bootstrap, doctor, fleet-status]
      mcp: [github]
      guardrails: [no-push-main, no-destructive-git, pii-protection, no-fabrication]
      protocols: [context-management, telegram-routing, telegram-formatting]
      scope:
        org: Claudfather
        repos: [Claudlobby, clauDNA]
      telegram:
        handle: claudfather_bot
        token_env: TELEGRAM_BOT_TOKEN_CLAUDFATHER
        require_mention: false
      startup_prompt: >
        Welcome back, {{ bot_name }}. Read your CLAUDE.md.
        Idle and await Telegram messages.
```

**Design notes:**

- `model: opus`, `effort: max` -- shipped this way since commit `d97d709` (2026-05-12), the day after this spec was written. The original recommendation here was `sonnet` "for cost efficiency" (Branden), on the theory that reading docs, running diagnostics, and guiding config do not require Opus -- but the always-on repo-guide role was judged to warrant Opus-level reasoning, and the fleet config was flipped the next day and never reverted. The fleet-level `defaults.model` remains `sonnet`; claudfather's own bot stanza overrides it. (Superseded -- see §14.)
- No `dangerously_skip_permissions` -- standard permissions by default. The user approves commands during bootstrap. (Greg's position, supported by Rajan.)
- No `teams` -- single-bot fleet, no dispatch hierarchy.
- `require_mention: false` -- claudfather is the only bot in the seed fleet's group, so it listens to everything.

### .env.seed.example

```bash
# Seed fleet secrets -- copy to .env and fill in real values.
# Create a bot via @BotFather on Telegram. Paste the token here.
TELEGRAM_BOT_TOKEN_CLAUDFATHER=8888888:AAAAAAAAAAAAAAAAAAAA

# GitHub PAT (optional, enables repo operations)
GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxx
```

This file is committed. `.env` is gitignored.


## 3. Expertise: setup-assistant

**New file:** `library/expertise/setup-assistant.md`

### Role definition

Claudfather is a **repo expert, bootstrap guide, fleet doctor, and tutor**. It reads documentation, runs diagnostics, guides configuration, and explains concepts.

### Capabilities

- Host readiness checks (deps: tmux, node, claude CLI, plugins, python, jq)
- Fleet.yaml scaffolding (guided or flag-driven via `claudlobby new-bot`)
- Credential validation (`REPLACE_ME` sentinel for seed-fleet fields, prose placeholder-substring check for user-fleet tokens -- see §8)
- Service enrollment guidance (systemd on Linux, launchd on macOS)
- Diagnostics: reconcile-fleet, creds-check, disk-monitor, npx-cache, plugin-cache
- Repo exploration: reads and explains library/, docs/, lib/, templates/, claudlobby/

### Boundaries

- Does NOT dispatch work to other bots
- Does NOT implement code (no engineering tasks)
- Does NOT manage user fleet bots at runtime
- Does NOT have Write/Edit tool permissions on files outside its own directory

### Knowledge scope

Everything committed to the claudlobby repo: `library/`, `documentation/`, `lib/`, `templates/`, `claudlobby/`, `voices/`, `fleet.yaml.example`, `fleet.yaml.seed`.

### Expertise file structure

Frontmatter grants read-heavy permissions (Bash, Agent, Read, Grep, Glob, WebFetch, WebSearch) with bash_allow covering system inspection commands (tmux, git, gh, claude, claudlobby, systemctl, launchctl, node, npm, pip, python3, uname, df, free, uptime). Body sections: "What you do" (bootstrap, diagnose, teach, maintain), "What you do NOT do" (dispatch, implement, manage runtime, push commits), "How you work" (read repo, cite sources, run lib/ scripts).

**Estimated size:** ~80 lines.


## 4. Bootstrap Flow (User Journey)

The user journey has three phases: **prerequisites** (steps 1-2, scripted), **the /setup skill** (steps 3-5, interactive in a terminal Claude session), and **Telegram handoff** (steps 6-8, claudfather guides from Telegram).

### Phase A: Prerequisites (scripted)

**Step 1: Clone + install**
```bash
git clone https://github.com/Claudfather/Claudlobby.git
cd Claudlobby
pip install -e .
```

**Step 2: Host setup**
```bash
lib/setup-system
```
Installs dependencies: tmux, node, jq, gh, claude CLI, Telegram plugin. Idempotent -- detects what is already installed and skips it. Works on both Linux and macOS. **Note:** this spec originally proposed a dedicated `lib/setup-host.sh`; that script was never built. The shipped mechanism is `lib/setup-system` (host prereqs + `system.yaml` host-job enrollment) alongside `lib/setup-fleet` and `lib/setup-fleets` (per-fleet apply/enroll), landed 2026-07-02 via the Phase 3 setup backbone (#464). See section 10 for details.

### Phase B: The /setup skill (interactive terminal)

**Step 3: User starts a Claude session and runs /setup**
```bash
claude
> /setup
```

The `/setup` skill is a **project-level skill** at `claudlobby/.claude/skills/setup/SKILL.md`. It is NOT in clauDNA -- it is specific to the claudlobby repo. Any Claude Code session opened in the claudlobby root directory automatically has access to it.

**Step 4: /setup collects credentials**

The skill guides the user conversationally:

1. "Let's get claudfather running. First, you'll need a Telegram bot."
2. Walks through @BotFather: /newbot, choose a name, get the token
3. "Paste your bot token here."
4. Validates the token via `curl https://api.telegram.org/bot<TOKEN>/getMe`
5. "What's your Telegram user ID?" (guides to @userinfobot if unknown)
6. Writes both values to `.env` (creates from `.env.seed.example` if needed)

**Step 5: /setup spins up claudfather**

The skill runs the mechanical steps:

1. `claudlobby --seed generate` -- composes the seed fleet
2. `lib/spin-up-bot.sh runtime/seed/bots/claudfather` -- enrolls and starts under supervision
3. Waits for claudfather's tmux session to report ready
4. Claudfather messages the user on Telegram: "Hey, I'm claudfather. I'm your claudlobby setup assistant. Let's set up your fleet."

The /setup skill's job is done. It is the **matchmaker** -- it introduces the user to claudfather and hands off.

### Phase C: Telegram handoff (claudfather guides)

**Step 6: User continues on Telegram**

The user can close their terminal session. Everything continues on Telegram via claudfather.

**Step 7: Fleet creation guided by claudfather**

Claudfather runs the `/bootstrap` skill conversationally:
1. Choose a fleet name (e.g., `my-fleet`)
2. Scaffold: `mkdir -p local/my-fleet && cp fleet.yaml.example local/my-fleet/fleet.yaml`
3. Define bots: expertise, model, voice, skills (claudfather explains each concept)
4. Create Telegram bots via @BotFather (one per fleet bot) -- claudfather guides
5. Paste tokens into `local/my-fleet/.env`
6. Generate: `claudlobby --fleet my-fleet generate`
7. Spin up: `lib/spin-up-bot.sh` for each bot

**Step 8: User's fleet is running**

Claudfather confirms all bots are alive, suggests next steps (adding more bots, configuring MCP servers, setting up skills).

### Phase B: Conversational guidance

**Step 6: User says "help me set up my fleet"**
Claudfather runs the `/bootstrap` skill. This is a guided, resume-aware conversation that walks the user through:

**Step 7: Fleet creation guided by claudfather**
1. Choose a fleet name (e.g., `my-fleet`)
2. Scaffold: `mkdir -p local/my-fleet && cp fleet.yaml.example local/my-fleet/fleet.yaml`
3. Define bots: expertise, model, voice, skills (claudfather explains each concept)
4. Create Telegram bots via @BotFather (one per fleet bot) -- claudfather guides
5. Paste tokens into `local/my-fleet/.env`
6. Generate: `claudlobby --fleet my-fleet generate`
7. Spin up: `lib/spin-up-bot.sh` for each bot

**Step 8: User's fleet is running**
Claudfather confirms all bots are alive, suggests next steps (adding more bots, configuring MCP servers, setting up skills).


## 5. Ongoing Value

After bootstrap, claudfather remains useful as a persistent, always-on repo expert.

### /doctor skill

Fleet health sweep that runs a graduated checklist:
- `claudlobby validate` (config correctness)
- `lib/reconcile-fleet.sh` (supervision state: healthy/orphan/missing/unbound)
- `lib/creds-check.sh` (credential validity)
- `lib/disk-monitor.sh` (disk usage)
- `lib/fleet-memory-check.sh` (memory pressure)
- `lib/check-npx-cache.sh` (MCP npx package cache)
- Plugin cache state
- Service unit health (systemd/launchd status)

Output: a structured summary with pass/warn/fail per check, actionable next steps for anything non-green.

### /bootstrap skill

The guided fleet creation flow from section 4. Resume-aware: if the user already has a `local/<name>/fleet.yaml`, claudfather picks up from where they left off instead of starting over. Uses graduated assessment (not binary "done/not done"):

| State detected | Resume point |
|---|---|
| No `local/` directory | Start from scratch |
| `fleet.yaml` exists but no bots generated | Skip scaffolding, go to generate |
| Bots generated but not enrolled | Skip generate, go to spin-up |
| Bots enrolled but some unhealthy | Run /doctor, fix issues |
| Everything healthy | Report clean bill of health |

### Tutor capability

Not a skill -- an inherent capability from the expertise file. Claudfather reads repo files and explains concepts. Examples:
- "What is an expertise file?" -- reads `library/expertise/README.md` and explains
- "How do MCP fragments work?" -- reads `library/mcp/README.md` and a sample fragment
- "Why did validate fail?" -- reads the error, finds the relevant validator code, explains

### Maintenance

- Re-validate + re-generate after config changes
- Plugin updates (checks for new versions)
- Library drift detection (diff between runtime and what generate would produce)
- Service restart guidance


## 6. Skills

### Skills claudfather needs

| Skill | Source | Purpose |
|---|---|---|
| `bootstrap` | **New:** `library/skills/bootstrap/SKILL.md` | Guided fleet creation, resume-aware |
| `doctor` | **New:** `library/skills/doctor/SKILL.md` | Fleet health sweep |
| `fleet-status` | **Existing:** `library/skills/fleet-status/SKILL.md` | Quick health overview |

### Skills claudfather does NOT need

- `dispatch`, `lifecycle`, `delegate` -- claudfather does not manage other bots
- `prs`, `sweep`, `review-status` -- claudfather does not do engineering
- `briefing`, `calendar`, `finance`, etc. -- personal-assistant skills, not bootstrap
- `restart` -- claudfather can guide the user to restart, but does not self-restart via fleet mechanisms (it is the seed fleet's only bot)


## 7. The /setup Skill

### Location

`claudlobby/.claude/skills/setup/SKILL.md` -- a **project-level skill**, not a clauDNA skill. Any Claude Code session opened in the claudlobby root has automatic access to it.

### Responsibilities

The /setup skill is the matchmaker. It runs once, in an ephemeral Claude session, and its only job is:

1. Guide the user through creating a BotFather bot (noob-friendly instructions)
2. Collect the Telegram bot token and validate it
3. Collect the user's Telegram user ID
4. Write credentials to `.env`
5. Run `claudlobby --seed generate`
6. Run `lib/spin-up-bot.sh runtime/seed/bots/claudfather`
7. Confirm claudfather is alive on Telegram
8. Tell the user: "Claudfather is ready. Open Telegram and say hi."

### What /setup does NOT do

- Does not guide fleet creation (that's claudfather's /bootstrap skill)
- Does not install host dependencies (that's `lib/setup-system`, run before `claude` -- see section 10)
- Does not persist -- it runs in the user's ephemeral session and exits

### Implementation

~120 lines of SKILL.md. The skill body contains:
- BotFather walkthrough with step-by-step instructions
- Token validation procedure (curl getMe endpoint)
- Telegram user ID discovery guidance (@userinfobot)
- .env creation logic (from .env.seed.example)
- Generate + spin-up commands
- Readiness verification (tmux session check)
- Handoff message

## 8. CLI Commands

### --seed flag

A new global flag for `claudlobby` (similar to `--fleet`):

```python
parser.add_argument("--seed", action="store_true",
    help="Operate on the seed fleet (fleet.yaml.seed)")
```

When `--seed` is passed, `_resolve_paths()` returns a Paths object with:
- `fleet_yaml` = `<root>/fleet.yaml.seed`
- `runtime_bots` = `<root>/runtime/seed/bots/`
- No overlay library (seed fleet uses base library only)

**Implementation:** ~15 lines modifying `_resolve_paths()` in `__main__.py`.

### claudlobby bootstrap (optional sugar, Phase 2)

```
claudlobby bootstrap [--fleet <name>]
```

Optional convenience command that launches `claude --prompt "/setup"` in the repo root. Deferred to Phase 2 -- users can run `/setup` directly in any Claude session for Phase 1.

### claudlobby doctor (optional sugar, Phase 2)

```
claudlobby doctor [--fleet <name>]
```

Optional convenience command that sends `/doctor` to claudfather's tmux session. Deferred to Phase 2.


## 8. Resume & Recovery

### Graduated assessment

The `/bootstrap` skill does not use a binary "is bootstrap done?" check. Instead, it probes each layer independently (Branden's placeholder-detection pattern). **Note:** this shipped differently than originally specced below -- no literal `is_filled()` function exists anywhere in the repo (grep confirms). Two lighter mechanisms cover the same need instead:

- **Seed-fleet fields** (`telegram_group_chat_id`, `human_telegram_id`, the claudfather bot's `telegram.handle`) ship in `fleet.yaml.seed` as a literal `REPLACE_ME` sentinel. The `/setup` skill (`.claude/skills/setup/SKILL.md`) patches these once real values are collected -- a field still reading `REPLACE_ME` means that step hasn't run yet.
- **User-fleet credentials** (`.env` token values) are assessed by the `/bootstrap` skill's prose placeholder-substring check (`library/skills/bootstrap/SKILL.md`): a value counts as a placeholder if it contains `xxxx`, `AAAA`, `your_token_here`, `REPLACE`, `ghp_xxxxxxxxxxxxxxxxxxxx`, or `8888888:AAAAAAAAAAAAAAAAAAAA`.

Originally proposed (never implemented as a literal function):

```python
def is_filled(env_var_value: str) -> bool:
    """Detect placeholder vs. real credential."""
    if not env_var_value:
        return False
    placeholders = [
        "xxxx", "AAAA", "your_token_here",
        "ghp_xxxxxxxxxxxxxxxxxxxx",
        "8888888:AAAAAAAAAAAAAAAAAAAA",
        "secret_xxxxxxxxxxxxxxxxxxxx",
    ]
    return not any(p in env_var_value for p in placeholders)
```

### State detection

| Check | Command | Healthy signal |
|---|---|---|
| Repo cloned | `test -d $CLAUDLOBBY_ROOT` | Directory exists |
| Pip installed | `claudlobby --version` | Returns version |
| Host deps | `command -v tmux node claude jq` | All found |
| .env populated | Telegram token env var does not contain a known placeholder substring (see above) | Real token |
| Fleet generated | `test -f runtime/seed/bots/claudfather/bot.conf` | File exists |
| Bot enrolled | `systemctl --user is-active claudfather` or launchd equivalent | Active |
| Bot responsive | `tmux has-session -t claudfather` | Session exists |

### SD card recovery

If the Pi's SD card dies and the user re-images:
1. Clone repo (fleet.yaml.seed is committed)
2. Restore `.env` from backup (claudfather guides `.env` backup during initial setup)
3. `lib/setup-host.sh && claudlobby --seed generate && lib/spin-up-bot.sh ...`
4. Claudfather is back. User's fleet config is lost unless they backed up `local/`.

Claudfather should advise during bootstrap: "Your .env and local/ directory are not committed to git. Back them up separately."

### .env backup guidance

Claudfather proactively suggests:
- `cp .env ~/.env.backup` after initial setup
- Mention that `local/<fleet>/fleet.yaml` should also be backed up
- Do NOT suggest committing .env to git


## 9. Security Model

### Standard permissions by default

Claudfather runs with standard Claude Code permission prompts. The user approves bash commands, file writes, and MCP tool calls during bootstrap. This is intentional:
- New users learn what the tools do by approving them
- No ambient authority for a bot that reads system state (Rajan's concern)
- Experienced users can add `dangerously_skip_permissions: true` to fleet.yaml.seed

### --fast flag

For power users who want to skip permission prompts:

```bash
claudlobby bootstrap --fast
```

This adds `--dangerously-skip-permissions` to claudfather's CLAUDE_FLAGS for that session only. It does NOT modify fleet.yaml.seed. (Greg's proposal.)

### Credential handling

- Tokens never appear in bash history: use temp files for curl-based validation, not inline args
- `.env` is gitignored at every level (repo root, local/, runtime/)
- Claudfather never echoes back token values -- confirms "token set" or "token missing"
- Placeholder-substring checks (see §8) validate token format without logging the value

### Supply chain

- `fleet.yaml.seed` is committed and code-reviewed -- changes go through PR
- `library/expertise/setup-assistant.md` is committed and code-reviewed
- Skills (`bootstrap/`, `doctor/`) are committed and code-reviewed
- No dependency on external resources during bootstrap (claudfather works offline once cloned)


## 10. Implementation Phases

### Phase 1: Ship first

Everything needed for "git clone to claudfather alive on Telegram."

| File | Action | Est. lines |
|---|---|---|
| `.claude/skills/setup/SKILL.md` | Create (project-level /setup skill) | ~120 |
| `library/expertise/setup-assistant.md` | Create | ~80 |
| `library/skills/bootstrap/SKILL.md` | Create | ~120 |
| `library/skills/doctor/SKILL.md` | Create | ~100 |
| `fleet.yaml.seed` | Create | ~50 |
| `.env.seed.example` | Create | ~8 |
| `lib/setup-host.sh` | Create (Linux + macOS generalization of setup-mac-mini.sh) | ~300 |
| `claudlobby/__main__.py` | Modify: add `--seed` flag | ~30 net new |
| `claudlobby/paths.py` | Modify: handle seed fleet path resolution | ~20 net new |
| `.gitignore` | Modify: add `runtime/seed/` | ~2 |
| `tests/test_seed_fleet.py` | Create | ~150 |
| `tests/test_setup_host.py` | Create | ~80 |
| **Total** | | **~1060** |

#### setup-host.sh details (superseded)

**This script as specced was never built.** `lib/setup-host.sh` does not exist in the repo. Instead, a later and more comprehensive mechanism shipped: `lib/setup-system` (host prereqs + `system.yaml` host-job enrollment) plus `lib/setup-fleet` (idempotent per-fleet apply + enroll -- default jobs, atomic legacy-keepalive swap, bot enrollment, reconcile) and `lib/setup-fleets` (runs `setup-fleet` for every fleet on the host). These landed via the Phase 3 setup backbone (#464, 2026-07-02) -- five weeks after this spec -- and own considerably more scope than this section's original sketch (see root `CLAUDE.md`'s `lib/` table for the current command set). `setup-mac-mini.sh` still exists but is now an unused stub (124 bytes). `tests/test_setup_host.py` was never created; `tests/test_setup_backbone.py` covers the shipped backbone instead.

Original plan (kept for historical record):

Generalizes `setup-mac-mini.sh` for both Linux and macOS. Phases:

1. **Preflight** -- OS detection (Linux or Darwin), repo root check
2. **Package manager** -- apt on Linux, brew on macOS; install core tools: tmux, node, jq, gh
3. **Python** -- ensure python3 3.10+, pip
4. **Claude CLI** -- npm install @anthropic-ai/claude-code, verify `claude` in PATH
5. **Telegram plugin** -- `claude plugin install telegram@claude-plugins-official`
6. **Env check** -- verify .env exists, warn if missing
7. **Report** -- summary matrix of what was installed vs. already present

Key differences from setup-mac-mini.sh:
- No brew on Linux (uses apt-get or equivalent)
- No launchd phases (those are handled by spin-up-bot.sh)
- No --with-data (data CLIs are fleet-specific, not bootstrap)
- Simpler: fewer phases, focused on minimum viable host

Required for Pi users (Branden's position, supported by Rajan) in the original plan. Not deferred to Phase 2 -- but in practice it landed later than any other Phase 1 item, superseded by the Phase 3 setup backbone described above.

#### Tests

**Unit tests (~10):**
- `test_seed_fleet_loads` -- fleet.yaml.seed parses without error
- `test_seed_fleet_validates` -- passes claudlobby validate
- `test_seed_fleet_single_bot` -- exactly one bot named "claudfather"
- `test_seed_bot_model_sonnet` -- model is sonnet
- `test_seed_bot_no_skip_permissions` -- dangerously_skip_permissions is false
- `test_seed_paths_resolution` -- --seed flag resolves correct paths
- `test_is_filled_real_token` -- is_filled returns True for real-format tokens
- `test_is_filled_placeholder` -- is_filled returns False for placeholder tokens
- `test_expertise_setup_assistant_parses` -- expertise file loads via parse_expertise_file()
- `test_compose_seed_bot` -- compose_bot produces valid CLAUDE.md

**Integration tests (~3):**
- `test_bootstrap_end_to_end` -- generate seed fleet + verify all output files exist (no Claude session)
- `test_setup_host_dry_run` -- setup-host.sh --dry-run completes on current OS
- `test_doctor_checks_exist` -- each diagnostic script referenced by /doctor exists and is executable

### Phase 2: CLI sugar + Telegram enhancement

| Deliverable | Est. lines |
|---|---|
| `claudlobby bootstrap` CLI command (launches `claude --prompt "/setup"`) | ~40 |
| `claudlobby doctor` CLI command (sends /doctor to claudfather tmux) | ~30 |
| `/bootstrap` gains Telegram liveness check | ~30 |
| Per-bot token guidance for user fleets | ~50 in bootstrap skill |
| `/add-bot` conversational wrapper around `claudlobby new-bot` | ~80 skill |

### Phase 3: CI + docs

| Deliverable | Est. lines |
|---|---|
| CI harness: `claudlobby --seed generate --dry-run` in GitHub Actions | ~30 (workflow YAML) |
| `documentation/getting-started.md` updated with seed fleet path | ~40 |
| Root `CLAUDE.md` updated with claudfather section | ~20 |


## 11. Testing Strategy

### Unit tests

Cover the config layer (does fleet.yaml.seed parse?), the composition layer (does compose_bot produce valid output?), and the credential detection logic (is_filled). Estimated count: ~10 tests.

All unit tests run without network, without Claude CLI, without tmux. They exercise the Python compositor only.

### Integration tests

Cover the end-to-end generate pipeline and the shell scripts. Estimated count: ~3 tests.

- Generate test: `claudlobby --seed generate` produces all expected files
- setup-host.sh test: `--dry-run` mode completes without errors
- Doctor references test: all scripts referenced by the /doctor skill exist

### Manual test protocol

Before merge, manually test on:

1. **Raspberry Pi 5 (Linux/arm64):** Full flow from clone to Telegram message
2. **macOS (arm64):** Full flow from clone to Telegram message
3. **Fresh environment:** Verify setup-host.sh installs all deps from scratch

Checklist:
- [ ] `pip install -e .` succeeds
- [ ] `lib/setup-host.sh --dry-run` reports expected deps
- [ ] `claudlobby --seed validate` passes
- [ ] `claudlobby --seed generate` produces runtime/seed/bots/claudfather/
- [ ] Generated CLAUDE.md contains setup-assistant expertise content
- [ ] Generated bot.conf has `model sonnet`, no `--dangerously-skip-permissions`
- [ ] `lib/spin-up-bot.sh runtime/seed/bots/claudfather` creates tmux session
- [ ] Telegram message to @claudfather_bot gets a response
- [ ] `/doctor` returns health summary
- [ ] `/bootstrap` starts guided flow


## 12. Success Criteria

### Phase 1: Done when

- A user can go from `git clone` to claudfather responding on Telegram in under 20 minutes (revised from Mason's 15-min target -- the BotFather step and pip install add time on Pi hardware)
- `claudlobby --seed generate` produces a valid, runnable bot with no manual file creation beyond .env
- setup-host.sh works on both Raspberry Pi 5 (Bookworm) and macOS (Sonoma+)
- All 13 tests pass
- PR reviewed and merged

### Phase 2: Done when

- Claudfather posts a Telegram liveness confirmation during bootstrap
- `/add-bot` guides through Telegram bot creation for user fleet bots
- Per-bot token naming convention documented and enforced

### Phase 3: Done when

- CI runs `claudlobby --seed generate` on every PR and fails on breakage
- getting-started.md includes the seed fleet path alongside the existing manual path
- Root CLAUDE.md references claudfather


## 13. Open Questions

1. **Seed fleet name:** Currently `seed` in this spec. Alternatives: `claudfather`, `bootstrap`. "seed" is neutral and avoids conflating the fleet name with the bot name. Awaiting human decision.

2. ~~**Service prefix:** Currently `com.claudfather.seed`. Could be `com.claudlobby.seed` to match the repo name. Minor -- affects systemd unit names and launchd labels only.~~ **RESOLVED:** shipped as `service_prefix: com.claudlobby.seed` (matches the repo name -- confirmed in `fleet.yaml.seed`). See §14.

3. **Claudfather in user's Telegram group:** Should claudfather be added to the user's fleet Telegram group too, or only DM? DM-only is simpler and avoids noise. Group presence could be useful for cross-fleet diagnostics. Recommendation: DM-only for Phase 1; group access as opt-in later.

4. **Keepalive timer offset:** If the user runs both the seed fleet and their own fleet on the same host, the keepalive timers could fire simultaneously. Recommend staggering: seed fleet keepalive at :05 past the interval, user fleets at :00. Low priority -- the keepalive script is idempotent and concurrent runs are harmless.

5. **--seed vs. dedicated seed path:** This spec proposes `--seed` as a CLI flag. An alternative is to make the seed fleet a regular overlay at `local/seed/` and ship a `claudlobby init-seed` command that copies fleet.yaml.seed there. The `--seed` flag is simpler and keeps the seed fleet visibly separate from user content. Awaiting human decision.


## 14. Resolved Questions

| Question | Resolution | Attribution |
|---|---|---|
| Is claudfather a fleet bot or a separate entity? | Fleet bot. Uses standard compose_bot() pipeline. No second template. | Greg (unanimously supported) |
| Where does the seed fleet.yaml live? | `fleet.yaml.seed` at repo root, committed. Separate from fleet.yaml.example. | Mason (original), Greg (refined) |
| Service prefix: `com.claudfather.seed` or `com.claudlobby.seed`? | `com.claudlobby.seed` -- matches the repo name. Affects systemd unit names and launchd labels. (Listed as an open question in §13 above; already resolved in shipped `fleet.yaml.seed`.) | Implementation (unattributed in spec) |
| Telegram from day one or deferred? | Day one. User creates BotFather bot, pastes token, claudfather is on Telegram. User fleet Telegram setup is guided conversationally. | Chris (design direction) |
| Supervised or run-and-forget? | Supervised. systemd/launchd unit, keepalive timer. Always listening. | Branden |
| One claudfather per org or per clone? | Per clone. Each host gets its own seed fleet instance. | Chris (design direction) |
| --dangerously-skip-permissions default? | Off. Standard permissions. User approves commands. --fast flag for power users. | Greg (position), Rajan (security review) |
| setup-host.sh in Phase 1 or deferred? | Phase 1, as planned -- but the script itself was never built under this name. Superseded by the `lib/setup-system` / `lib/setup-fleet` / `lib/setup-fleets` backbone (landed 2026-07-02, #464; see §10). | Branden (position), Rajan (supported) |
| Cost model for always-on? | Reversed one day after this was written: shipped as `model: opus`, `effort: max` since commit `d97d709` (2026-05-12) and never reverted. Branden's original sonnet recommendation did not hold in practice. | Branden (superseded) |
| New expertise type or reuse orchestration? | New: `setup-assistant`. Orchestration expertise includes dispatch/tmux mechanics that do not apply. | Mason (original), Greg (refined) |
| Should claudfather auto-merge or auto-push? | No. It has no-push-main and no-destructive-git guardrails. It teaches, not implements. | Rajan (adversarial review) |
| How does bootstrap resume from partial state? | Graduated assessment. Probe each layer independently, resume from the first incomplete layer. | Branden (is_filled pattern), Greg (implementation) |
| Token validation approach? | Placeholder detection by substring match -- `REPLACE_ME` sentinel for seed-fleet fields, prose substring list for user-fleet tokens (see §8). No network calls to validate tokens during assessment (network validation happens in /doctor). Shipped as described, not as a literal `is_filled()` function. | Branden |
| Bootstrap entry point: CLI command or skill? | Project-level /setup skill at `.claude/skills/setup/SKILL.md`. User runs `claude` in repo root, then `/setup`. CLI `claudlobby bootstrap` deferred to Phase 2 as sugar. | Chris (design direction) |
| Where does /setup live: clauDNA or claudlobby? | Project-level in claudlobby. It is repo-specific, not a general-purpose clauDNA skill. | Chris (explicit) |
