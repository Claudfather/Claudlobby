# fleet.yaml — schema reference

`fleet.yaml` is the recipe. It tells `claudlobby generate` which bots to compose, which library pieces to assemble, and how to wire them up. One file at the repo root, hand-edited or `claudlobby new-bot`-driven.

## Top-level shape

```yaml
fleet:
  name: <fleet-name>                    # human-readable identifier
  service_prefix: <reverse-domain>      # e.g. "com.example.claudlobby" — used for service unit names
  telegram_group_chat_id: "<chat-id>"   # default group; bots can override per-bot
  human_telegram_id: "<user-id>"        # OPTIONAL — human's Telegram ID for DM allowlisting

  accounts:                             # OPTIONAL — alternate Claude Code config dirs (multi-auth)
    default: ~/.claude
    work: ~/.claude-work

  defaults:                             # applied to every bot unless overridden
    model: opus | sonnet | haiku
    effort: max | default
    account: default
    prompt_suggestions: true | false    # CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION (default: false)
    expertise: [<list>]                 # appended to bot's expertise
    skills: [<list>]
    mcp: [<list>]
    integrations: [<list>]
    guardrails: [<list>]
    protocols: [<list>]
    resources: [<list>]
    lessons: [<list>]
    principles: [<list>]
    permissions: [<list>]
    post_actions: [<list>]
    mission: <string>
    scope: { ... }
    model_strategy: { ... }
    telegram:                           # merged field-by-field with bot telegram
      token_env: <env-var-name>
      require_mention: true | false
    sandbox:                            # sandbox network/filesystem allowlists
      auto_allow_bash: true | false
      network_allowed_domains: [<list>]
      filesystem_allow_write: [<list>]
    tools:                              # tool allow/deny defaults
      deny: [<tool>, ...]
      allow: [<tool>, ...]
    hooks:                              # Claude Code hooks for fleet-wide observability
      <EventName>:
        - command: <shell-command>
          matcher: <tool-pattern>

  teams:                                # OPTIONAL — group bots so managers know their roster
    <team-name>:
      manager: <bot-name>
      workers: [<bot-name>, ...]

  bots:                                 # REQUIRED — one entry per bot
    <bot-name>:
      name: <display-name>              # OPTIONAL — defaults to dict key
      expertise: [<list>]               # REQUIRED — area(s) of expertise from library/expertise/
      voice: voices/<file>.md           # OPTIONAL — personality overlay
      mission: <string>                 # OPTIONAL — one-paragraph charter
      reports_to: <bot-name>            # OPTIONAL — bot_id of manager
      manages: [<bot-name>, ...]        # OPTIONAL — bot_ids this bot manages
      scope:                            # OPTIONAL — operational boundary
        org: <github-org>
        repos: [<repo>, ...]
        snowflake_targets: [<target>, ...]
      model_strategy:                   # OPTIONAL — escalation rules
        base: <model>
        escalate_to: <model>
        escalate_when: <human-readable rule>
        compact_when: <human-readable rule>
        explore: <model>                # subagent model for Explore agents
        plan: <model>                   # subagent model for Plan agents
        general: <model>                # subagent model for general-purpose agents
      account: <account-key>
      model: <model>
      effort: <effort>
      skills: [<list>]                  # appended to defaults.skills
      mcp: [<list>]
      integrations: [<list>]            # auto-paired with mcp; explicit overrides
      guardrails: [<list>]
      protocols: [<list>]
      resources: [<list>]
      lessons: [<list>]
      principles: [<list>]
      permissions: [<list>]
      post_actions: [<list>]
      env: { <KEY>: <value>, ... }      # bot-specific env exports (merged into bot.conf)
      tools:                            # OPTIONAL — tool allow/deny
        deny: [<tool>, ...]
        allow: [<tool>, ...]
      hooks:                            # OPTIONAL — per-bot hooks (appended to fleet defaults)
        <EventName>:
          - command: <shell-command>
            matcher: <tool-pattern>
      mounts:                           # OPTIONAL — symlinks to external host paths
        <name>: /absolute/host/path
      telegram:
        handle: <bot-handle>
        token_env: TELEGRAM_TOKEN_<X>
        require_mention: true | false
        chat_id: "<override>"
      startup_prompt: <string>
      bench: true | false                 # OPTIONAL — benchmarking target (default: false)
      dangerously_skip_permissions: true | false  # OPTIONAL — skip permission prompts (default: true)
      remote_control: true | false        # OPTIONAL — enable --remote-control (default: true)
      prompt_suggestions: true | false    # OPTIONAL — autocomplete suggestions (default: false)
      channels: [<list>]                  # OPTIONAL — channel plugins (default: [plugin:telegram@...])
      extra_flags: [<string>, ...]        # OPTIONAL — additional Claude CLI flags
      claudna_version: <string>           # OPTIONAL — pin clauDNA plugin version
      claudron_vault_path: <string>       # OPTIONAL — Claudron vault path for this bot
      claudosseum_tenant_id: <string>     # OPTIONAL — Claudosseum telemetry tenant
```

## Field reference

### `fleet.name` / `fleet.service_prefix` / `fleet.telegram_group_chat_id`

Cosmetic + service-unit naming + default Telegram chat. Bots may override `chat_id` per-bot.

### `fleet.accounts`

Alternate Claude Code config directories. Useful when some bots authenticate against a different account. Bot stanza references the key (`account: work`); the generator writes `CLAUDE_CONFIG_DIR` into `bot.conf`.

### `fleet.human_telegram_id`

The human operator's Telegram user ID. When set, the compositor writes this into every bot's `access.json` `allowFrom` list, so the human can DM any bot without pending approval.

### `fleet.plugins`

Declares Claude Code plugins that all bots in the fleet should have installed and enabled.

**Defaults:** `claudna@Claudfather` is installed by default on every fleet with its marketplace pre-registered. No `plugins:` section needed for the baseline.

```yaml
# Minimal — defaults apply automatically, just add extras:
plugins:
  additional:
    - telegram@claude-plugins-official

# Full — custom marketplace + disable defaults:
plugins:
  include_defaults: false
  marketplaces:
    MyMarket:
      source: { source: github, repo: MyOrg/my-plugins }
  additional:
    - my-plugin@MyMarket
```

**`additional`** — Plugin identifiers in `name@marketplace` format, appended to the defaults. Deduplicated.

**`marketplaces`** — Third-party marketplace registrations, merged with defaults. The official Anthropic marketplace is built in and doesn't need a declaration. Each entry maps a marketplace name to a source descriptor (the nested `source` key is the Claude Code marketplace schema — `github`, `url`, `git`, or `local` types).

**`include_defaults`** — Boolean (default `true`). Set to `false` to disable the built-in default plugins and marketplaces. Unusual — the validator warns when this is set.

### `fleet.defaults`

Applied to every bot. Merge rules by type:

- **Lists** (skills, expertise, guardrails, protocols, resources, lessons, principles, post_actions, mcp, integrations) — bot-level **appends to** defaults (deduped, order-preserved).
- **Scalars** (model, effort, account, mission) — bot-level **overrides** defaults.
- **Telegram** — merged **field-by-field**. Bot-level fields override individual defaults fields (e.g., a bot can override `require_mention` while inheriting `token_env`).
- **Sandbox** — lists (network_allowed_domains, filesystem_allow_write) are **unioned**; booleans (auto_allow_bash) use bot-level value.
- **Tools** — deny/allow lists are **unioned** across defaults and bot-level.
- **Hooks** — bot-level entries are **appended after** defaults per event. Same-matcher hooks group together.

### `fleet.teams`

Optional grouping. The generator uses team membership to inject a "Fleet You Manage" roster into manager personas.

### `fleet.sweep`

Opt-in rolling code-audit sweep. A fleet-level nightly timer runs a no-LLM selector (`lib/code-audit-sweep.sh`) that picks the **stalest** repo — by the timestamp of its most recent `auto-audit`-labelled GitHub issue — and dispatches an audit into the owner bot's session via `lib/bot-sweep-cron.sh`. The audit's filed issues become the next run's staleness signal, so GitHub is the only ledger (no local tracker, no drift). Presence of the block opts in; omit it and nothing is emitted.

```yaml
fleet:
  sweep:
    owner_bot: astrid              # bot whose session runs the audit (required)
    repos: [acme/api, acme/web]    # optional; defaults to owner_bot's scope.repos
    label: auto-audit              # staleness label (default: auto-audit)
    schedule: "*-*-* 03:00:00"     # systemd OnCalendar (default: nightly 03:00)
    audit_types: [tech-debt, security-audit]  # rotated per run (default: [tech-debt])
    enabled: true                  # default true when the block is present
```

After `claudlobby generate`, enroll the timer once per host: `lib/install-code-audit-sweep-systemd.sh <fleet>` (Linux) or `lib/install-code-audit-sweep.sh <fleet>` (macOS). The owner bot needs the `code-audit-sweep` skill (add `code-audit-sweep` to its `skills:`). Audit events (`audit_selected`, `audit_dispatched`, `audit_completed`, …) land in the owner's `data/events/` — see the `fleet-observability` protocol.

### `fleet.auto_deploy`

Opt-in platform self-deploy. A fleet-level nightly timer (`lib/auto-deploy.sh`) keeps the host's claudlobby checkout current with its remote and applies new code **live** via `reload-fleet.sh` (no restart, no context loss). It is **safety-gated** — it refuses rather than risk a bad deploy, and every refusal is a clean no-op the next run retries: it skips when the working tree is dirty, when the host is parked on a feature branch (so it never yanks a host off in-flight WIP), or when CI is red on the deploy branch; it no-ops when already current. Only after every gate passes does it `git pull --ff-only` and reload; a failed reload **rolls the checkout back** to the pre-deploy commit and is loud (a `deploy_failed` event + manager alert via the shared `emit_failure_alert` path). Presence of the block opts in; omit it and nothing is emitted.

```yaml
fleet:
  auto_deploy:
    schedule: "*-*-* 03:15:00"     # systemd OnCalendar (default: 03:15, before reload-fleet)
    enabled: true                  # default true when the block is present
```

The deploy branch, git remote, and CI gate are script-level env knobs (`AUTO_DEPLOY_BRANCH` default `main`, `AUTO_DEPLOY_REMOTE` default `origin`, `AUTO_DEPLOY_CI_GATE` default `1`) — set them in the timer unit's environment if a host needs to track a non-default branch or skip the CI gate. After `claudlobby generate`, enroll the timer once per host: `lib/install-auto-deploy-systemd.sh <fleet>` (Linux) or `lib/install-auto-deploy.sh <fleet>` (macOS). It reuses `reload-fleet.sh` (Mechanism 1) for the apply step, so a host running auto-deploy gets the same idle-gated `/reload` as the daily reload timer.

### `bots.<name>.expertise`

**Required.** A list of area-of-expertise filenames from `library/expertise/`. The first file's H1 titles the bot; subsequent files' H1s are stripped and their bodies append below.

Available out of the box:
- `orchestration` — dispatch, decision frameworks, fleet health
- `software-engineering` — lifecycle, branch/PR, testing
- `code-review` — mutation testing, verdict-first comments
- `frontend-design` — visual audits, Tailwind, screenshots
- `customer-service` — drafting customer replies, refund decisions
- `business-operations` — order management, supplier coordination, daily summaries

Add new ones by dropping markdown files in `library/expertise/`. Combine: `expertise: [software-engineering, data-engineering]` produces a bot fluent in both.

### `bots.<name>.voice`

Optional path to a voice overlay (relative to repo root). Voice content gets injected after the persona's H1 but before the rest of the body. Goal: expertise = capability, voice = personality.

### `bots.<name>.mission`

One-paragraph charter — why this bot exists, what success looks like. Forces every bot to articulate its purpose in `fleet.yaml` itself (kept visible alongside the rest of the config). Composed into a `## Mission` section.

### `bots.<name>.reports_to` / `bots.<name>.manages`

Org structure fields. `reports_to` names the bot_id of this bot's manager. `manages` lists bot_ids this bot manages. Together they generate an `## Org Structure` section in CLAUDE.md showing the reporting hierarchy. Both are optional — bots without either get no org section.

### `bots.<name>.scope`

Operational boundary. Composed into a `## Scope` section. Common fields:

- `org` — GitHub org the bot operates within
- `repos` — list of repos the bot owns/touches
- `snowflake_targets` — dev/prod/etc the bot may use

Any extra fields are passed through verbatim.

### `bots.<name>.model_strategy`

Escalation rules. When you have a bot that runs Sonnet for routine work but should escalate to Opus for architecturally tricky tasks, declare it here. Composed into a `## Model Strategy` section so the bot is aware of its own escalation pattern. Emitted as `MODEL_STRATEGY_*` env vars in `bot.conf` for skills/protocols to read.

Subagent model preferences (`explore`, `plan`, `general`) control which model subagents use. These are stored in the `raw` dict and emitted as env vars (e.g., `MODEL_STRATEGY_EXPLORE=haiku`).

### `bots.<name>.skills`

List of skill basenames from `library/skills/`. Generator symlinks each into `runtime/bots/<name>/.claude/skills/`. Bot accumulates `defaults.skills` + bot-level (deduped, in order).

### `bots.<name>.mcp` and `bots.<name>.integrations`

`mcp:` lists MCP fragments from `library/mcp/`. The generator merges them into `.mcp.json`.

`integrations:` lists usage docs from `library/integrations/`. By default, integrations are **auto-paired with mcp** — listing `mcp: [github]` automatically pulls in `library/integrations/github.md` (when it exists). Override by setting `integrations:` explicitly.

### `bots.<name>.guardrails` / `protocols` / `resources` / `lessons` / `principles` / `post_actions`

Lists of basenames from the corresponding `library/<dir>/`. Each gets appended to CLAUDE.md as its own section. Bot accumulates `defaults.<list>` + bot-level (deduped, order-preserved).

### `bots.<name>.expertise` (note: `persona` is deprecated)

The `persona:` key is accepted as a backwards-compatible alias for `expertise:` but emits a deprecation warning. Use `expertise:` in all new configs — `persona:` will be removed in a future release.

### `bots.<name>.telegram`

Telegram config for this bot. Fields: `handle` (bot username), `token_env` (env var name holding the token), `require_mention` (whether the bot responds only to @-mentions), `chat_id` (override fleet-level group).

`token_env` names the **env var** that holds the Telegram token (e.g., `TELEGRAM_TOKEN_LEAD`). The actual token lives in `.env`. The generator writes the env-var *name* into `bot.conf` as `TELEGRAM_TOKEN_ENV_NAME`; `lib/start-bot.sh` reads through to the actual token. This indirection lets you commit `fleet.yaml` publicly while keeping tokens in a gitignored `.env`.

Telegram fields support defaults merging — set common values (like `token_env`) in `defaults.telegram` and override per-bot as needed.

### `bots.<name>.tools`

Control which Claude Code tools a bot may use. Two sub-fields:

- `deny: [Write, Edit, NotebookEdit]` — generates permission deny rules like `Write(**)` in `settings.local.json`. The bot cannot call these tools.
- `allow: [Agent, Bash, Read]` — generates permission allow rules. Useful when combined with auto-derived permissions from expertise profiles.

Deny wins over allow at the same layer. The validator warns if denied tools conflict with the bot's expertise (e.g., denying `Write` for a `software-engineering` bot).

### `bots.<name>.observability`

Controls fleet observability thresholds for heartbeat pulses, event retention, and stuck-detection. Emitted as env vars in `bot.conf` for consumption by `lib/fleet-pulse.sh`, `lib/bot-vitals.sh`, and the dispatch watchdog.

```yaml
observability:
  pulse_interval: 300           # seconds between heartbeat pulses (default: 300)
  reap_days: 7                  # days to retain event files before reaping (default: 7)
  activity_stuck_threshold: 1800  # seconds of no tool-call activity before flagged (default: 1800)
  dispatch_deadline: 1800       # seconds after manager dispatch before flagged overdue (default: 1800)
```

All fields are optional integers with sensible defaults. Can be set in `defaults:` to apply fleet-wide; bot-level overrides. The validator warns if `pulse_interval` is less than 30 or `activity_stuck_threshold` is less than 60.

Emitted env vars: `OBSERVABILITY_PULSE_INTERVAL`, `OBSERVABILITY_REAP_DAYS`, `OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD`, `OBSERVABILITY_DISPATCH_DEADLINE`.

### Fleet-pulse escalation (environment overrides)

`lib/fleet-pulse.sh` escalates to Telegram when the same critical event (`service_down`, `session_missing`) affects multiple bots within a short window. These are tuned by environment variables read directly by the script — they are **not** `fleet.yaml` fields. Set them in your fleet's `.env` or the fleet-pulse systemd unit's environment.

| Env var | Default | Purpose |
|---------|---------|---------|
| `FLEET_PULSE_ESCALATION_CHAT_ID` | _(fallback)_ | Telegram chat ID for fleet-wide critical alerts. When unset, fleet-pulse uses the first bot in the fleet that declares a non-empty `TELEGRAM_GROUP_CHAT_ID` (bots that omit it are skipped, not blindly trusted). If no bot declares one, escalation is disabled and fleet-pulse logs a warning rather than failing silently. |
| `FLEET_PULSE_ESCALATION_THRESHOLD` | `2` | Number of distinct bots that must hit the same critical event within the window to trigger escalation. |
| `FLEET_PULSE_ESCALATION_WINDOW` | `10` | Lookback window, in minutes, for counting affected bots. |

Set `FLEET_PULSE_ESCALATION_CHAT_ID` explicitly so alert targeting never depends on bot directory ordering.

### `bots.<name>.hooks`

Per-bot Claude Code hooks, appended to fleet defaults. Each event (e.g., `PreToolUse`, `PostToolUse`) contains a list of hook entries:

```yaml
hooks:
  PostToolUse:
    - command: "notify-manager.sh"
      matcher: "Bash"
      timeout: 30
```

Hook entry fields:

| Field | Description |
|-------|-------------|
| `command` | Shell command to run (required for type: command) |
| `matcher` | Tool name filter: `"Bash"`, `"Write\|Edit"`, `"mcp__.*"`, or omit for all tools |
| `type` | Hook type: `command` (default), `http`, `prompt`, `agent` |
| `timeout` | Seconds before timeout (default: 600 for command) |
| `async` | Run in background without blocking (default: false) |

The compositor transforms the flat fleet.yaml format into Claude Code's nested matcher-group format in `settings.local.json`.

### `bots.<name>.sandbox`

Sandbox network and filesystem allowlists, written to `settings.local.json`. Merged with defaults (lists unioned, bools overridden).

- `network_allowed_domains` — hostnames the bot may access (e.g., `api.github.com`, `"*.anthropic.com"`)
- `filesystem_allow_write` — additional writable paths beyond the bot directory
- `auto_allow_bash` — skip Bash tool permission prompts when running in sandbox mode

### `bots.<name>.mounts`

Symlinks to external host paths, created under `<bot-dir>/mounts/<name>`. Useful for giving a bot access to files outside the repo (e.g., Home Assistant config).

```yaml
mounts:
  ha-config: /path/to/homeassistant/config
```

Edits write to the real location via the symlink. Stale symlinks (removed from config) are cleaned up on re-generate.

### `bots.<name>.bench`

Boolean (default `false`). Marks this bot as the fleet's benchmarking target for cold-start timing (`lib/bench-cold-start.sh`). Multi-bot fleets should set `bench: true` on exactly one bot so the benchmarking script knows which bot to measure. The validator warns if a fleet has multiple bots and none has `bench: true`.

### `bots.<name>.permission_mode`

String (default `null`). Sets the `--permission-mode` flag on the Claude Code CLI, providing more granular control than `dangerously_skip_permissions`. When set, this field takes precedence over `dangerously_skip_permissions`.

Valid values:

| Mode | Behavior |
|------|----------|
| `"default"` | Prompt for every tool call |
| `"acceptEdits"` | Auto-approve file edits, prompt for others |
| `"bypassPermissions"` | Skip all permission prompts (equivalent to `dangerously_skip_permissions: true`) |
| `"plan"` | Plan mode — read-only exploration, no writes |
| `"dontAsk"` | Skip tool calls that would require permission instead of prompting |
| `"auto"` | Auto-approve safe operations, prompt for risky ones |

Can be set in `defaults:` to apply fleet-wide; bot-level overrides.

### `bots.<name>.dangerously_skip_permissions`

Boolean (default `true`). Controls whether the bot runs with `--dangerously-skip-permissions`, which skips tool-call permission prompts. Set to `false` for bots that should require human approval before executing tools. Superseded by `permission_mode` when both are set. Can be set in `defaults:` to apply fleet-wide; bot-level overrides.

### `bots.<name>.remote_control`

Boolean (default `true`). Controls whether the bot runs with `--remote-control`, which allows dispatch via `tmux send-keys`. Disable for standalone bots that should only respond to Telegram messages. Can be set in `defaults:`.

### `bots.<name>.prompt_suggestions`

Boolean (default `false`). Controls `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION` env var. When true, Claude Code shows autocomplete prompt suggestions. Headless bots don't need them — only enable for interactive use. Can be set in `defaults:`.

### `bots.<name>.channels`

List of channel plugin identifiers (default `["plugin:telegram@claude-plugins-official"]`). Each entry generates a `--channels <name>` CLI flag. Override to use a different messaging plugin or disable channels entirely with an empty list. Can be set in `defaults:`.

### `bots.<name>.extra_flags`

List of additional CLI flags appended to `CLAUDE_FLAGS` in `bot.conf`. Use for any Claude Code flags not covered by dedicated fields. Bot-level list appends to defaults (deduped). Example: `["--verbose"]`.

### `bots.<name>.claudna_version` / `claudron_vault_path` / `claudosseum_tenant_id`

Ecosystem-aware fields connecting bots to clauDNA, Claudron, and Claudosseum. All optional; emitted as env vars in `bot.conf` when set.

| Field | Env var | Purpose |
|-------|---------|---------|
| `claudna_version` | `CLAUDNA_VERSION` | Pin the clauDNA plugin version (e.g., `"0.2.0"`). Skills/hooks can read this to gate behavior by version. |
| `claudron_vault_path` | `CLAUDRON_VAULT_PATH` | Path to the bot's Claudron vault (e.g., `"vaults/my-fleet/eng-1"`). The Claudron MCP server reads this to scope queries. |
| `claudosseum_tenant_id` | `CLAUDOSSEUM_TENANT_ID` | Tenant identifier for Claudosseum telemetry (e.g., `"tenant_abc123"`). Bots emit structured signal to this tenant when configured. |

Can be set in `defaults:` (fleet-wide) or per-bot (bot overrides default). The validator warns if `claudron_vault_path` is set but no `claudron` MCP server is configured.

### `bots.<name>.autonomous_runner`

Configures a bot to run a skill autonomously on a schedule. When set, the compositor generates autonomous runner instructions in the bot's `CLAUDE.md`.

```yaml
autonomous_runner:
  skill: "sweep"                # required — skill name to run
  cadence: "every 4h"           # required — execution frequency
  target_repo: "my-org/my-repo" # required — target repository
  args: "--label bug"           # optional — additional arguments passed to the skill
  picker:                       # optional — how to select work items
    type: "github_issues"       # picker type (default: "github_issues")
    label: "bug"                # filter label (default: null)
    state: "open"               # issue state filter (default: "open")
    score_by: "recency"         # scoring strategy (default: "recency")
  bypass:                       # optional — risk-based bypass config
    risk_classifier: "structural_vs_mechanical"  # classifier name (default)
    block_on: ["structural"]    # risk levels that block execution (default)
    on_bypass: "comment_and_label"  # action when bypassed (default)
  pre_hooks: []                 # optional — commands to run before skill execution
  post_hooks: []                # optional — commands to run after skill execution
  on_outcome:                   # optional — routing based on skill outcome
    success: "report-back completed"
    failure: "report-back failed"
```

All sub-fields except `skill`, `cadence`, and `target_repo` are optional with sensible defaults. Cannot be set in `defaults:` — must be per-bot (each bot's autonomous schedule is unique).

### `bots.<name>.startup_prompt`

Jinja2-templated string sent to the bot on startup. Available placeholders: `{{ bot_name }}`, `{{ fleet_name }}`, `{{ telegram_group_chat_id }}`, `{{ telegram_handle }}`. Written to `bot.conf` as `STARTUP_PROMPT`. Use to give the bot initial instructions (e.g., "read your CLAUDE.md and idle").

## Auto-derived permissions

The compositor auto-derives permission entries in `settings.local.json` from several sources. These don't require explicit `tools:` config — they're generated automatically.

| Source | What it generates |
|--------|-------------------|
| **Expertise profiles** | `library/expertise/<name>.md` frontmatter can declare `permissions: { allow_all, allow, deny, bash_allow }`. Merged across all expertise files. |
| **MCP permission contracts** | `library/mcp/<name>.json` `_permissions_contract.tools` field lists tool names → generates `mcp__<server>__<tool>` allow patterns. |
| **Channel plugins** | Bots with a Telegram handle get allow rules for `mcp__plugin_telegram_telegram__*` tools. |
| **Skills** | Each skill generates `Skill(<name>)` and `Skill(<name>:*)` allow patterns. |
| **Base tools** | `Read`, `Grep`, `Glob` are always allowed when an allow list is non-empty. |

Layering order (later layers win on conflict): expertise → MCP contracts → channels → skills → fleet defaults → bot-level. Bot-level deny always wins.

## Composition order (per bot)

The generator assembles `runtime/bots/<name>/CLAUDE.md` in this exact order:

1. **Expertise** — concatenated. First file's H1 titles the bot; subsequent files' H1s are stripped and bodies append.
2. **Voice overlay** — injected after the H1 line and the first blank line.
3. **Mission** — `## Mission` section with the paragraph from `fleet.yaml`.
4. **Scope** — `## Scope` section if `scope:` is set.
5. **Model strategy** — `## Model Strategy` section if `model_strategy:` is set.
6. **Team roster** — `## Fleet You Manage` table for managers (auto-generated from `teams`).
7. **Resources** — `## Resources` section, each `library/resources/<name>.md` concatenated.
8. **Integrations** — `## Integrations` section (auto-paired with mcp by default).
9. **Principles** — `## Principles` section.
10. **Protocols** — `## Protocols` section.
11. **Guardrails** — `## Guardrails` section.
12. **Lessons** — `## Lessons` section.
13. **Post-actions** — `## Post-actions` section.

The result is a single CLAUDE.md you can read top-to-bottom. Each section's origin is obvious from the markdown headers.

In addition to CLAUDE.md, the generator produces:

- **bot.conf** — env vars sourced at startup (model flags, Telegram config, model strategy, mounts)
- **.mcp.json** — merged MCP server configs with env-var placeholders
- **.claude/settings.local.json** — memory dir, sibling isolation, tool permissions, sandbox config, hooks
- **access.json** — Telegram channel config (requireMention, DM policy, human allowlist)
- **\<bot\>.service / .plist** — systemd / launchd supervision units
- **.claude/skills/** — symlinked skill directories

## Validation rules

`claudlobby validate` checks:

- **Hard fail** — bot's `expertise:` list is empty or references missing files
- **Hard fail** — `fleet.yaml` itself is invalid YAML or missing required keys
- **Warn** — bot references a `skill` / `mcp` / `guardrail` / `protocol` / `resource` / `lesson` / `post_action` that doesn't exist (skipped during generate)
- **Warn** — MCP fragment references an env var (`${FOO}`) that's not set in the current environment
- **Warn** — `voice:` path doesn't resolve
- **Warn** — `telegram.token_env` env var not set
- **Warn** — `teams.<X>.workers` references a bot not defined in `bots:`
- **Warn** — fleet has multiple bots but none has `bench: true` — benchmarking won't know which bot to measure
- **Warn** — `claudron_vault_path` is set but no `claudron` MCP server is configured

Generate proceeds through warnings. Pass `--strict` to make warnings errors (CI use).

## Example: minimal 2-bot fleet

```yaml
fleet:
  name: starter
  service_prefix: com.example.starter
  telegram_group_chat_id: "-1001234567890"

  defaults:
    model: opus
    effort: max
    guardrails: [no-push-main, no-destructive-git, pii-protection]
    protocols: [report-back, context-management, telegram-routing]

  teams:
    main:
      manager: lead
      workers: [eng-1]

  bots:
    lead:
      expertise: [orchestration]
      mission: "Run the fleet day-to-day; flag anything that needs the human within 5 minutes."
      skills: [dispatch, fleet-status, lifecycle, prs]
      mcp: [github, notion]
      protocols: [dispatch]
      telegram:
        handle: my_lead_bot
        token_env: TELEGRAM_TOKEN_LEAD
        require_mention: false

    eng-1:
      expertise: [software-engineering]
      mission: "Implement features and fix bugs. Branch + PR; tests pass before report-back."
      mcp: [github]
      telegram:
        handle: my_eng_1_bot
        token_env: TELEGRAM_TOKEN_ENG1
        require_mention: true
```
