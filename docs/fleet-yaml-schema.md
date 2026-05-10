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
      scope:                            # OPTIONAL — operational boundary
        org: <github-org>
        repos: [<repo>, ...]
        snowflake_targets: [<target>, ...]
      model_strategy:                   # OPTIONAL — escalation rules
        base: <model>
        escalate_to: <model>
        escalate_when: <human-readable rule>
        compact_when: <human-readable rule>
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
```

## Field reference

### `fleet.name` / `fleet.service_prefix` / `fleet.telegram_group_chat_id`

Cosmetic + service-unit naming + default Telegram chat. Bots may override `chat_id` per-bot.

### `fleet.accounts`

Alternate Claude Code config directories. Useful when some bots authenticate against a different account. Bot stanza references the key (`account: work`); the generator writes `CLAUDE_CONFIG_DIR` into `bot.conf`.

### `fleet.human_telegram_id`

The human operator's Telegram user ID. When set, the compositor writes this into every bot's `access.json` `allowFrom` list, so the human can DM any bot without pending approval.

### `fleet.defaults`

Applied to every bot. Merge rules by type:

- **Lists** (skills, expertise, guardrails, protocols, resources, lessons, post_actions, mcp, integrations) — bot-level **appends to** defaults (deduped, order-preserved).
- **Scalars** (model, effort, account, mission) — bot-level **overrides** defaults.
- **Telegram** — merged **field-by-field**. Bot-level fields override individual defaults fields (e.g., a bot can override `require_mention` while inheriting `token_env`).
- **Sandbox** — lists (network_allowed_domains, filesystem_allow_write) are **unioned**; booleans (auto_allow_bash) use bot-level value.
- **Tools** — deny/allow lists are **unioned** across defaults and bot-level.
- **Hooks** — bot-level entries are **appended after** defaults per event. Same-matcher hooks group together.

### `fleet.teams`

Optional grouping. The generator uses team membership to inject a "Fleet You Manage" roster into manager personas.

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

### `bots.<name>.scope`

Operational boundary. Composed into a `## Scope` section. Common fields:

- `org` — GitHub org the bot operates within
- `repos` — list of repos the bot owns/touches
- `snowflake_targets` — dev/prod/etc the bot may use

Any extra fields are passed through verbatim.

### `bots.<name>.model_strategy`

Escalation rules. When you have a bot that runs Sonnet for routine work but should escalate to Opus for architecturally tricky tasks, declare it here. Composed into a `## Model Strategy` section so the bot is aware of its own escalation pattern.

### `bots.<name>.skills`

List of skill basenames from `library/skills/`. Generator symlinks each into `runtime/bots/<name>/.claude/skills/`. Bot accumulates `defaults.skills` + bot-level (deduped, in order).

### `bots.<name>.mcp` and `bots.<name>.integrations`

`mcp:` lists MCP fragments from `library/mcp/`. The generator merges them into `.mcp.json`.

`integrations:` lists usage docs from `library/integrations/`. By default, integrations are **auto-paired with mcp** — listing `mcp: [github]` automatically pulls in `library/integrations/github.md` (when it exists). Override by setting `integrations:` explicitly.

### `bots.<name>.guardrails` / `protocols` / `resources` / `lessons` / `post_actions`

Lists of basenames from the corresponding `library/<dir>/`. Each gets appended to CLAUDE.md as its own section. Bot accumulates `defaults.<list>` + bot-level (deduped, order-preserved).

### `bots.<name>.telegram`

Telegram config for this bot. Fields: `handle` (bot username), `token_env` (env var name holding the token), `require_mention` (whether the bot responds only to @-mentions), `chat_id` (override fleet-level group).

`token_env` names the **env var** that holds the Telegram token (e.g., `TELEGRAM_TOKEN_LEAD`). The actual token lives in `.env`. The generator writes the env-var *name* into `bot.conf` as `TELEGRAM_TOKEN_ENV_NAME`; `lib/start-bot.sh` reads through to the actual token. This indirection lets you commit `fleet.yaml` publicly while keeping tokens in a gitignored `.env`.

Telegram fields support defaults merging — set common values (like `token_env`) in `defaults.telegram` and override per-bot as needed.

### `bots.<name>.tools`

Control which Claude Code tools a bot may use. Two sub-fields:

- `deny: [Write, Edit, NotebookEdit]` — generates permission deny rules like `Write(**)` in `settings.local.json`. The bot cannot call these tools.
- `allow: [Agent, Bash, Read]` — generates permission allow rules. Useful when combined with auto-derived permissions from expertise profiles.

Deny wins over allow at the same layer. The validator warns if denied tools conflict with the bot's expertise (e.g., denying `Write` for a `software-engineering` bot).

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
9. **Protocols** — `## Protocols` section.
10. **Guardrails** — `## Guardrails` section.
11. **Lessons** — `## Lessons` section.
12. **Post-actions** — `## Post-actions` section.

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
