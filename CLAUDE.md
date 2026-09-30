# claudlobby

Compositor for Claude Code agent fleets. Transforms `fleet.yaml` + `library/` into runnable bot directories with isolated identities, MCP servers, skills, and systemd/launchd supervision.

**North star:** Trivial to run a fleet of distinct, cooperating bots on cheap hardware — and to point that fleet at a goal (fleets know the mission they serve, pick work that advances it, and close it at each project's declared rigor).

**What that is for:** specialist agent teams that take on the work a team of people would otherwise do. Such a team **communicates in plain terms to the human while keeping the rigor behind it intact** — simplified for the reader, never simplified in the doing (`library/principles/simple-outside-rigorous-inside.md`). It works toward a business goal with enough autonomy to parallelize its own workstreams and fill its own idle time, and sustains that over days rather than a session. **Long-running is the precondition for the rest**: a fleet that cannot survive a restart with its context intact cannot hold a goal. Tracked as #974.

**New here?** See [`documentation/getting-started.md`](documentation/getting-started.md) for the clone-to-fleet walkthrough and [`documentation/fleet-yaml-schema.md`](documentation/fleet-yaml-schema.md) for every config field.

**This file is an index.** Detail lives beside what it describes: [`lib/CLAUDE.md`](lib/CLAUDE.md) (each `lib/` script in depth, and the shell authoring rules), [`claudlobby/CLAUDE.md`](claudlobby/CLAUDE.md) (the Python module map), the other nested `CLAUDE.md` files, script headers, and `documentation/`. Read "Instruction files" below before adding to this one.

## Ecosystem boundary

Claudlobby is the stack's **composition system**: it turns `fleet.yaml` into wired bots —
identities, plugins, env, permission grants, supervision — and owns fleet *policy* (what each bot
may do, including vault writer topology). Engineering-workflow behavior is clauDNA's; the durable
knowledge corpus is Claudron's. The local rules:

- **Skills here are fleet operations.** `library/skills/` operate the fleet itself (dispatch,
  restart, pulse) — runtime content that happens to use the skill format. Engineering-workflow
  skills belong in clauDNA.
- **Durable knowledge lives in the vault.** `library/` composes context into bots; it is not the
  corpus. Learned-the-hard-way content accrues through the Claudron door (`/claudna:capture`),
  not as new library files (see `library/CLAUDE.md`).
- **Consume siblings by contract, never by assertion.** Wire what is shipped (the `claudron` CLI
  door; the `claudron_compat.py` floor); never validate or warn about a sibling surface that does
  not exist at the pinned version. The canonical vault env var is `CLAUDRON_VAULT_PATH` (Claudron
  CLI contract).
- **Placement test** (one line): does it *wire, grant, or supervise*? → here. Behavior → clauDNA;
  knowledge → the vault. Full algorithm: Claudron repo,
  `documentation/plans/2026-07-20-claudfather-boundary-separation.md` §10.3.

## Architecture

```
fleet.yaml          →  claudlobby generate  →  runtime/bots/<name>/
library/                                         ├── CLAUDE.md      (composed instructions)
  expertise/                                     ├── bot.conf       (env vars, sourced at startup)
  skills/                                        ├── .mcp.json      (MCP server config)
  mcp/                                           ├── .claude/       (settings.local.json, skills/ symlinks)
  guardrails/                                    ├── *.service      (systemd unit, Linux)
  protocols/                                     ├── *.plist        (launchd unit, macOS)
  integrations/                                  ├── memory/        (bot-owned persistent state)
  resources/                                     ├── data/          (bot-owned data — mutable, never regenerated)
  lessons/                                       ├── tools/         (composited scripts — generated, never hand-edited)
  principles/                                    ├── logs/          (bot log files)
  permissions/                                   └── projects/      (git checkouts, gitignored)
  post_actions/
  tools/
voices/
templates/claude.md.j2
```

The compositor reads `fleet.yaml` (which declares bots, their expertise, skills, MCP servers, guardrails, etc.) and assembles each bot's directory from the shared `library/`. The template `claude.md.j2` owns all top-level structure — library files supply slot content.

### Key concepts

- **Expertise** — Role definitions (e.g. `software-engineering`, `orchestration`, `code-review`). Each bot gets one or more. This is who the bot is.
- **Skills** — Composable slash-command packages in `library/skills/<name>/SKILL.md`. What the bot can do.
- **MCP fragments** — JSON wire configs in `library/mcp/` with `${ENV_VAR}` placeholders. Never real tokens.
- **Guardrails** — Safety rules composed per-bot (e.g. `no-push-main`, `snowflake-read-only`).
- **Protocols** — Reusable workflow patterns (dispatch, review-flow, context-management).
- **Tools** — Composited bot scripts in `library/tools/<name>/` (`tool.yaml` + Jinja template), rendered per-bot into `<bot_dir>/tools/` (0755) with compose-time params; secrets stay runtime env reads. See `library/tools/README.md`.
- **Plugins** — Claude Code plugins installed fleet-wide. `claudna@Claudfather` is a built-in default; extras via `fleet.plugins.additional`. Auto-installed on bot start.
- **Voices** — Optional personality overlays from `voices/`.

### Runtime model

Bots run as supervised processes: systemd user units on Linux, launchd LaunchAgents on macOS. Each bot lives in its own tmux session on its **own** tmux server (a private `-L <socket>` == `BOT_SERVICE`), so one server's death drops only that bot, never the whole fleet. The manager dispatches work via the socket-aware `lib/dispatch.sh` helper (which resolves the worker's socket); workers report back via `lib/report-back.sh`.

**Changing a running fleet:** when a change reaches a bot depends on **when the artifact is read, not what type of file it is** (#1310). Read **once at session start** (composed `CLAUDE.md` text, `bot.conf` env, `.mcp.json`): a running bot sees it from its next restart, so a canary window exists. Read **on demand, per use** (skill symlinks, hook scripts, `lib/`): live on every bot the moment `generate` writes it or the root is pulled, with **no canary window**. Composed `settings.local.json` permissions apply from the next tool call. See [`documentation/fleet-update-lifecycle.md`](documentation/fleet-update-lifecycle.md) before assuming a merged change is in force.

**Defaults (ruled 2026-09-07):** a job or door is **ON by default** unless it *deletes data, spends money, mutates operator source, sends outbound to people at scale, or has no deployment gate*. The registry, not this sentence, is the list: `claudlobby/switches.py` gives each opt-in's reason, and `claudlobby doctor --switches` prints the live table with the line that arms each. A door turned off no-ops **loudly**.

### `lib/` scripts

One line per script, for routing. **Before changing a script, read [`lib/CLAUDE.md`](lib/CLAUDE.md)**: the shell authoring rules and each script's full reference (contracts, exit codes, the incidents behind them).

**Bot lifecycle and supervision**
- `start-bot.sh` — launch a bot's tmux session (own socket, env from `bot.conf`) and wait until ready
- `spin-up-bot.sh` — enroll a bot as a supervised service, then start it (idempotent)
- `spin-down-bot.sh` — full teardown for canary/throwaway bots; `--purge` also deletes the bot dir
- `pre-stop-handoff.sh` — graceful context handoff before a service stop
- `keepalive.sh` — per-bot watchdog: restarts a dead session and records heartbeat samples
- `keepalive-all.sh` — run keepalive for every bot
- `reconcile-fleet.sh` — audit supervision state: healthy, orphan, missing, unsupervised-down, unbound
- `supervisor.sh` — systemd/launchd adapter; the only `lib/` file allowed to call `systemctl`/`launchctl`
- `rolling-restart.sh` — restart bots one at a time, each gated on a fresh Telegram `BRIDGE_READY`
- `weekly-worker-restart.sh` — weekly lossless restart of workers (not managers) to apply a staged binary
- `reload-fleet.sh` — daily plugin update + `generate`, then a `/reload` of running bots (no restart)
- `migrate-fleet-to-system.sh` — move `local/<fleet>/` into `local/<system>/<fleet>/`, re-pointing units (reversible)

**Shared helpers and query doors**
- `lib-common.sh` — shared helpers; the one door for pane sends (`pane_send_verified`), fleet events (`emit_fleet_event`), switches (`switch_is_on`) and the roster (`declared_bots_strict`)
- `env-tiers.sh` — print the `.env` tier cascade (host, root, fleet, bot) in runtime order
- `claude-version.sh` — print the Claude Code version, or refuse at rc 3
- `claude-session-pid.sh` — which Claude Code session the caller runs inside (walks its own ancestry)
- `fleet-state-update.sh` — atomic, flock-locked updates to `state/fleet-state.json`
- `tg-post.sh` — post a message to Telegram
- `mcp-package-grammar.py` — how an MCP server's `command` + `args` name a package, in one place

**Dispatch, reporting and the task loop**
- `dispatch.sh` — manager → worker dispatch helper; resolves the worker's tmux socket
- `dispatch-task.sh` — task dispatch: records it on the plane, then sends it
- `report-back.sh` — worker → manager structured report; closes the matching dispatch
- `task-act.sh` — a manager's acts on one open task: `withdraw` (close it) or `escalate` (ask a human)
- `task-recheck.sh` — timer: sends each manager one list of their stale open tasks
- `dispatch-overdue.py` — the plane matcher behind fleet-pulse: overdue, orphaned, open, unassigned rows
- `dispatch-supersede-hint.py` — at dispatch time, flags an open task the new one may supersede
- `workstream-update.sh` — single writer for the fleet's workstream registry
- `briefing-trigger.sh` — fire a bot's scheduled briefing as a slash command
- `manager-checkin.sh` — the check-in beat: prompts an idle manager to pick its next move (dormant)
- `checkin-record.sh` — write door for a manager's check-in decision
- `checkin-contract.py` — validates a check-in decision record
- `sprint-selection-record.py` — records the sprint's full candidate set and scores, not just winners
- `who-reviewed.py` — which bot wrote a PR review (the shared GitHub account hides it)
- `pr-review-state.py` — whether a PR's blocking review verdict is still live

**Observable plane (the fleet's record)**
- `plane-emit.sh` — the recording shim every door uses: daemon socket → cold CLI → spool
- `plane-socket-client.py` — stdlib socket leg of `plane-emit.sh`
- `plane-daemon.sh` — launcher for the ingest-only plane daemon
- `plane-view.sh` — launcher for the read-only operator plane UI (localhost)
- `plane-prune.sh` — timer: deletes metric samples past 30 days, never the ledger
- `plane-expire.sh` — timer: expires assignments 7+ days past deadline
- `plane-host-probe.sh` — per-minute host samples: load, RAM, disk, swap, iowait, thermal
- `plane-lookup.py` — read-only plane lookups by task id, assignment, escalation, event
- `plane-readers.py` — stdlib list readers the bash doors share (open, overdue, escalations, workstreams)
- `plane-parity.py` — reconcile an archived JSONL ledger against the plane
- `plane-session-start.sh` — SessionStart hook: session and process ids for the plane
- `plane-dispatch-in.sh` — UserPromptSubmit hook: records what the receiving bot actually got
- `plane-telegram-in.sh` — UserPromptSubmit hook: records the operator's Telegram messages
- `plane-telegram-out.sh` — PostToolUse hook on the Telegram reply tool: records the reply
- `plane-rc-relay-out.sh` — Stop hook: records a Telegram answer sent without the reply tool

**Monitoring and alerts**
- `fleet-pulse.sh` — fleet watchdog: overdue dispatches, idle workers, critical events, escalations
- `bot-vitals.sh` — Pre/PostToolUse hook: records tool calls and session events
- `fleet-utilization.sh` — per-bot busy/idle % from heartbeat samples
- `tail-fleet.sh` — tail and grep every bot's logs
- `disk-monitor.sh` — daily disk check; FLEET ALERT past threshold
- `fleet-memory-check.sh` — daily fleet RSS vs available RAM; FLEET ALERT past the reserve floor
- `host-health-check.sh` — alert on Pi under-voltage/throttling and SD/MMC storage stalls
- `creds-check.sh` — validate fleet credentials, including GitHub App mode
- `orphan-browser-reaper.sh` — daily reap of browser processes orphaned by dead automation
- `notify-behind.sh` — daily report of framework checkouts behind their newest release
- `transcript-digest.sh` — SessionEnd hook: a model-written digest per session (opt-in: spends)
- `selfstart-snapshot.sh` — after an unplanned reboot, counts which bots self-started; run it before any rescue
- `boot-capture.sh` — records every declared bot at each host boot (dormant)

**Setup and enrollment**
- `setup-system` — host prerequisites + `system.yaml` host-job enrollment
- `setup-fleet` — per-fleet apply and enroll: default jobs, bots, reconcile
- `setup-fleets` — run `setup-fleet` for every fleet on the host
- `install-bot.sh` — bot service enrollment (launchd)
- `install-bot-systemd.sh` — bot service enrollment (systemd)
- `install_fleet_timer.sh` — fleet/host timer enrollment (systemd)
- `install_fleet_timer_launchd.sh` — fleet/host timer enrollment (launchd)
- `install-host-service-systemd.sh` — host service enrollment, e.g. the plane daemon (systemd)
- `install-code-audit-sweep.sh` — code-audit-sweep timer enrollment (launchd)
- `install-code-audit-sweep-systemd.sh` — code-audit-sweep timer enrollment (systemd)

**Maintenance and updates**
- `log-rotate.sh` — rotate one bot's logs
- `log-rotate-fleet.sh` — rotate every bot's logs
- `git-pull-all.sh` — pull every repo in a bot's `projects/`
- `data-sweep.sh` — weekly purge of vetted ephemeral files in bots' `data/`
- `check-npx-cache.sh` — check that MCP server packages are cached
- `update-claude-code.sh` — daily staged Claude Code binary download (no fleet bounce)
- `update-siblings.sh` — weekly fast-forward of sibling checkouts to their newest release (opt-in)
- `pull-root.sh` — daily fast-forward of `$CLAUDLOBBY_ROOT` itself (opt-in)
- `bot-sweep-cron.sh` — periodic bot sweep via cron
- `code-audit-sweep.sh` — picks the stalest repo for a code audit, hands it to its owner (opt-in)
- `vault-sync.sh` — scheduled vault sync that reports what happened (dormant)

**GitHub identity and guards**
- `git-credential-github-app` — git credential helper that mints GitHub App installation tokens
- `mint-github-token.sh` — print a fresh App token for one command; never export it at boot
- `github-app-mcp-wrapper.py` — runs the GitHub MCP server with auto-refreshed App tokens
- `setup-github-app.sh` — one-time App validation and config write
- `gh-mention-guard.sh` — PreToolUse hook: rewrites `@<botname>` out of GitHub-bound text
- `mention-rewrite.py` — the rewriter behind `gh-mention-guard.sh`
- `vault-git-guard.sh` — PreToolUse hook: stops bots rewriting git state inside the vault
- `vault-git-decide.py` — the decision half of `vault-git-guard.sh`
- `vault-git-base-rate.py` — how often state-changing git reaches that guard directly

**Validation, canaries and measurement**
- `validate-bot-change.sh` — end-to-end harness for bot behavior changes
- `coldstart-harness.sh` — prepare/reap for the cold-start onboarding simulation
- `freshbox-boot-gate.sh` — boots a bot on a fresh config dir; checks composed permissions hold
- `naked-bot-observe.py` — records what a bot gets from a fleet that declares nothing
- `boot-strand-sampler.sh` — real-boot sampler for startup prompts left unsubmitted
- `boot-strand-summary.py` — statistics for `boot-strand-sampler.sh`
- `send-size-probe.sh` — how much of a large pane send reaches the reader
- `rehearse-env-cascade.sh` — canary for the `.env` tier cascade on a throwaway bot
- `rehearse-staged-claude-update.sh` — canary for the staged Claude Code update
- `rehearse-plane-durability.sh` — canary gating changes to plane daemon checkpointing
- `plane-durability-driver.py` — that canary's client and loss witness
- `plane-canary-sampler.py` — that canary's passive daemon sampler
- `plane-canary-compare.py` — that canary's comparator (self-tested)
- `rehearse-debounce-recipient.sh` — proves a debounced page survives a manager restart
- `rehearse-briefing-timer.sh` — rehearses the briefing-timer chain on a throwaway fleet
- `rehearse-permissions-ladder.sh` — single-factor permissions ladder on a disposable bot
- `rehearse-vault-sync.sh` — proves `vault-sync.sh`'s outcomes against a real plane
- `transcript-usage.py` — per-session token accounting from transcripts
- `ab-comms-eval.sh` — A/B harness for the token-efficiency comms eval
- `ab-comms-verdict.py` — pass bar and verdict for `ab-comms-eval.sh`
- `ab-coverage-verdict.py` — coverage-honesty A/B analysis
- `ab-channel-brevity-verdict.py` — channel-brevity A/B analysis
- `ab-recoverability-scorer.py` — scores whether compressed-out detail is recoverable in full
- `ab-recoverability-judge.py` — semantic judge for that scorer

## Repository Hygiene — MANDATORY

### What goes in git (shared, reusable, generalized)

Everything in these top-level directories is committed and shared:

- `library/` — All composable building blocks (see Architecture above)
- `voices/` — Personality overlays
- `templates/` — Jinja2 templates for CLAUDE.md generation
- `lib/` — Lifecycle and utility scripts
- `claudlobby/` — Python compositor source
- `documentation/` — Architecture docs, schema reference, setup guides
- `fleet.yaml.example` — Template manifest (committed; `fleet.yaml` is NOT)

### What stays local (gitignored, fleet-specific, secret)

These are ALL gitignored — never commit them:

- `fleet.yaml` — Your active fleet config. Copy from `fleet.yaml.example`.
- `local/` — Fleet overlays. Each `local/<fleet>/` contains fleet.yaml, local library overrides, voices, and runtime output. **All fleet-specific content lives here.**
- `local/<fleet>/runtime/bots/` — Generated bot directories
- `local/<fleet>/library/` — Fleet-specific library content not general enough for shared
- `.env` — Secrets (tokens, PATs, OAuth credentials). Never committed.
- `runtime/` — Root-mode generated output (if running without fleet overlays)
- `*/projects/` — Git checkouts in bot directories

### The bright line

**If it contains a real token, API key, credential, org ID, database UUID, or fleet-specific path → it goes in `local/` or `.env`.** If it's a reusable pattern that any fleet could benefit from → it goes in `library/`.

When in doubt: would another person running claudlobby find this useful? Yes → library. No → local overlay.

### No PII in committed assets

No personally identifiable information in any checked-in file. This includes:

- Real email addresses, phone numbers, physical addresses
- Real Telegram chat IDs, user IDs, or bot tokens
- Real API keys, OAuth tokens, or credentials
- Real database UUIDs, org IDs, or project IDs
- Real names tied to personal details (author names in pyproject.toml are fine)
- Real IP addresses (localhost/examples are fine)
- Financial account numbers or identifiers

Documentation and examples must use obviously fake placeholders (`ghp_xxxxxxxxxxxxxxxxxxxx`, `"-1001234567890"`, `8888888:AAAAAAAAAAAAAAAAAAAA`). If you need to reference a real service, use generic descriptions, not real account details.

### Before committing, always verify

```bash
git status           # nothing from local/, runtime/, .env should appear
git diff --cached    # no secrets, no fleet-specific UUIDs, no hardcoded paths
```

## Working on This Repo

### Instruction files: CLAUDE.md and AGENTS.md

Claude Code reads `CLAUDE.md`; Codex reads `AGENTS.md`. Every `AGENTS.md` here is a **committed symlink to the `CLAUDE.md` beside it**, and every `.agents/skills/<name>` a directory symlink to `.claude/skills/<name>` (Codex skips a symlinked `SKILL.md` file). Edit the Claude-side file only; never copy or reword it into the other name.

This file loads whole at the start of every session in this checkout, and in every bot under it (`local/<fleet>/runtime/bots/<bot>/`), since Claude Code also reads each parent directory's `CLAUDE.md`. Nested files load only on use: Claude Code reads one when a session first opens a file in its folder, Codex when it starts in that folder. `tests/test_instruction_budget.py` enforces the index above and two budgets:

- **This file: 32 KiB**, Codex's default `project_doc_max_bytes` (it reads no further). One line per thing.
- **Each nested file: under 150k characters**, where Claude Code starts warning. Past that, move detail into the script's or module's own header.

### Adding library content

Each library category has its own format. Check the category's `README.md` for specifics. General rules:

1. Create the file in the appropriate `library/<category>/` directory
2. Use YAML frontmatter with `title:` and `description:` fields
3. Add an H1 heading (`# Title`) matching the frontmatter title — the loader strips it to avoid duplication in composed output
4. Use `{{BOT_NAME}}`, `{{FLEET_NAME}}`, `{{CLAUDLOBBY_ROOT}}` Jinja2 placeholders where appropriate
5. Test: `claudlobby --fleet <your-fleet> generate` and verify the content appears in the right bot's CLAUDE.md
6. Commit to a branch, PR, review

**Heading levels matter.** The template renders library content inside `##`/`###` sections. The loader runs `_demote_headings` to shift all headings down. An H1 (`#`) in your file becomes H2 in the output. If you start with H3, it becomes H4 — which may be too deep.

### Adding compositor features

1. Edit Python source in `claudlobby/` (module map: [`claudlobby/CLAUDE.md`](claudlobby/CLAUDE.md))
2. Run tests: `python3 -m venv .venv && ./.venv/bin/python -m pip install -e '.[dev]'`, then `./.venv/bin/pytest`
3. Test against your local fleet: `claudlobby --fleet <name> validate` then `generate`
4. Run `claudlobby --fleet <name> diff` to verify no unintended drift
5. Commit to a branch, PR, review

**Read [`documentation/test-suite.md`](documentation/test-suite.md) before trusting a test run.** Run it **unsandboxed**; the suite is **not green** on macOS, so compare a before and an after run on failing test **names**, the **counts** line and the **exit code**; never pipe pytest into grep; quarantine a flaky test (`@pytest.mark.quarantine(issue=<N>)`), never deselect it.

### Adding or modifying lib/ scripts

Read [`lib/CLAUDE.md`](lib/CLAUDE.md) first: the authoring rules (the bash 3.2 one also covers `library/**/*.sh`, gated by `tests/test_bash_parse.py`) and each script's full reference. A new script gets one line in the index above; its detail goes in its own header.

### Validating changes to how a bot behaves — MANDATORY

Any change that affects **how a bot behaves at runtime** (lib/ supervision & observability scripts, hooks, skills, protocols, guardrails, principles, composed `bot.conf` env) must be **empirically validated** before merge: unit tests prove composition, only running the code proves behavior. **Deliver** → **add config** in `fleet.yaml` → **recompose** (`claudlobby --fleet <fleet> generate`, and confirm it landed) → **observe** the real behavior (`bash lib/validate-bot-change.sh` for observability events; otherwise `lib/spin-up-bot.sh`, drive the path, watch `claudlobby events --bot <bot>`). **Cite the observation in the PR body**: claimed evidence is not evidence. Ground a fixture for an externally produced shape in a **live capture**, committing its shape but never its identifiers. This gate proves the code, not the rollout: canary a fleet-wide framework change on one production bot first (the `canary-rollout` protocol). Full text: [`documentation/validating-bot-changes.md`](documentation/validating-bot-changes.md).

### Validating changes to the onboarding path — MANDATORY

Any change to **what a brand-new user is told to run** — `README.md`, `documentation/getting-started.md`, `.claude/skills/setup/SKILL.md`, `lib/setup-system`, `lib/setup-fleet`, `fleet.yaml.seed`, `.env.seed.example` — must be validated **on a cold host**, not from your checkout (#947): **export, do not clone**; **scrub the environment first**; **run the documented commands verbatim**; **log every exploration event** and report the count; **stop at the credential gate**. `tests/test_cold_start_contract.py` is a floor, not a substitute. **Cite the cold run in the PR body.** Procedure: [`documentation/validating-cold-start.md`](documentation/validating-cold-start.md).

### Never hand-edit generated output

Files in `runtime/bots/<name>/` are generated by `claudlobby generate`. Hand-edits will be overwritten on the next generate. To change a bot's config:

1. Edit `fleet.yaml` (fleet-level config) or `library/<category>/` (content)
2. Re-run `claudlobby generate`
3. If the bot drifted during a session (`claudlobby diff` shows changes), use `claudlobby promote` to extract the drift back into library

## Key Commands

```bash
# Composition
claudlobby validate                    # check fleet.yaml against library
claudlobby generate [--bot <name>]     # compose runtime/bots/ from fleet.yaml (or one bot)
claudlobby host-timers                 # compose host-global timer units from system.yaml
claudlobby diff                        # show drift between runtime and generate
claudlobby promote <name>              # extract bot drift back into library
claudlobby list-library                # show available building blocks

# Operations
claudlobby status [--bot <name>]       # fleet health dashboard, or one bot in detail
claudlobby doctor [--switches]         # pre-flight diagnostic; --switches: every switch and the line that flips it
claudlobby creds-reconcile             # declared vs stored vs equipped credentials
claudlobby freshbox                    # fresh-box self-containment audit (--strict, --bot, --reap)
claudlobby report-back [--since 24h]   # the fleet's reports, from the plane
claudlobby uptime                      # per-bot uptime, MTBR, restart-rate
claudlobby events                      # the fleet's events from the plane (rc 3 when it cannot answer)
claudlobby workstreams [list|show <id>] # the fleet's workstream registry, from the plane
claudlobby task nudge <task-id> ["why"] # record a nudge on one open task and ask its manager to act
claudlobby task recheck --fleet <F>    # ask each manager to act on their stale rows (--dry-run)
claudlobby checkins [--summary] [--since 7d] [--json]  # check-in decisions and their dispatch outcomes
claudlobby brief --bot <name> [--json|--ack]  # one read door over fleet state for a bot; --ack advances its report cursor
claudlobby warm-cache                  # pre-download npx + uvx packages for MCP servers
claudlobby move-bot <bot> --to <fleet> # move a bot between fleets

# Scaffolding
claudlobby new-bot                     # interactive bot scaffolding
claudlobby new-skill                   # scaffold a new skill directory
claudlobby new-guardrail               # scaffold a new guardrail file

# One-time migrations from legacy layouts
claudlobby env-migrate                 # .env files into fleet structure
claudlobby data-migrate                # bot data directories
claudlobby cron-migrate                # crontab entries to new paths
claudlobby memory-migrate              # ~/.claude/projects/ memory into per-bot dirs
claudlobby lessons-migrate             # library/lessons/ into the Claudron vault (dry-run by default)

# Testing (the venv is required — PEP 668 refuses a bare install on Homebrew/Debian)
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/pytest                     # run test suite (unsandboxed; baseline is not green)
```

Use `--fleet <name>` for overlay mode: `claudlobby --fleet <your-fleet> generate`

### Fleet operations (lib/ scripts)

```bash
# Bot lifecycle
lib/spin-up-bot.sh <bot-dir>           # enroll + start (idempotent)
lib/reconcile-fleet.sh <fleet>         # audit fleet supervision state
lib/reconcile-fleet.sh <fleet> --enroll # fix orphan bots

# A manager's acts on ONE open task (#1481)
lib/task-act.sh withdraw <task-id> --reason "…"   # retire a dispatch nobody will answer (terminal)
lib/task-act.sh escalate <task-id> "<question>"   # raise it for a human (NON-terminal: the task stays open)

# Maintenance
lib/log-rotate-fleet.sh --fleet <name> # rotate all bot logs
lib/git-pull-all.sh <projects-dir>     # pull all repos in a directory
lib/disk-monitor.sh                    # check disk usage, alert if high
lib/fleet-memory-check.sh              # fleet memory planning and monitoring
lib/check-npx-cache.sh                # verify npx cache state

# After an unplanned reboot — RUN THIS BEFORE RESCUING ANYTHING
lib/selfstart-snapshot.sh             # how many bots self-started (#1002 measurement)
```

## Python Package Structure

The module map is in [`claudlobby/CLAUDE.md`](claudlobby/CLAUDE.md). Start at `__main__.py` → `commands/` (the CLI), `config.py` (`fleet.yaml`), `composer.py` (generation), `loader.py`, `validator.py`, `paths.py`, `switches.py` and `plane/` (reference: `documentation/architecture/observable-plane.md`).
