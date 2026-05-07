# fleet.yaml — schema reference

`fleet.yaml` is the recipe. It tells `claudlobby generate` which bots to compose, which library pieces to assemble, and how to wire them up. One file at the repo root, hand-edited or `claudlobby new-bot`-driven.

## Top-level shape

```yaml
fleet:
  name: <fleet-name>                    # human-readable identifier
  service_prefix: <reverse-domain>      # e.g. "com.example.claudlobby" — used for service unit names
  telegram_group_chat_id: "<chat-id>"   # default group; bots can override per-bot

  accounts:                             # OPTIONAL — alternate Claude Code config dirs (multi-auth)
    default: ~/.claude
    work: ~/.claude-work

  defaults:                             # applied to every bot unless overridden
    model: opus | sonnet | haiku
    effort: max | default
    account: default
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

  teams:                                # OPTIONAL — group bots so managers know their roster
    <team-name>:
      manager: <bot-name>
      workers: [<bot-name>, ...]

  bots:                                 # REQUIRED — one entry per bot
    <bot-name>:
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

### `fleet.defaults`

Applied to every bot. Bot-level fields **append to** lists (skills, expertise, guardrails, protocols, resources, etc.) and **override** scalars (model, effort, account, mission).

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

### `bots.<name>.telegram.token_env`

Name of the **env var** that holds this bot's Telegram token (e.g., `TELEGRAM_TOKEN_LEAD`). The actual token lives in `.env`. The generator writes the env-var *name* into `bot.conf` as `TELEGRAM_TOKEN_ENV_NAME`; `lib/start-bot.sh` reads through to the actual token.

This indirection lets you commit `fleet.yaml` publicly while keeping tokens in a gitignored `.env`.

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
