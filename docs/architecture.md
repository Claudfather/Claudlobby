# Architecture

claudlobby is a **compositor**: a thin Python program that reads `fleet.yaml`, assembles bot directories from `library/` building blocks, and writes them to `runtime/bots/<name>/`. Claude Code then runs the assembled bots like normal.

## The four layers

```
1. library/      — composable building blocks (in git, single source of truth)
2. fleet.yaml    — the recipe (which bots, which pieces)
3. runtime/      — what the compositor produces (gitignored)
4. host         — systemd / launchd start the bots from runtime/
```

### 1. `library/` — building blocks

Eleven subdirectories, each a flat collection of small files:

```
library/
├── expertise/          orchestration.md, software-engineering.md, code-review.md, …
├── skills/             dispatch/, lifecycle/, prs/, fleet-status/, sweep/, …
├── mcp/                github.json, notion.json, slack.json, …
├── guardrails/         no-push-main.md, pii-protection.md, no-fabrication.md, …
├── protocols/          report-back.md, dispatch.md, telegram-routing.md, …
├── integrations/       neon.md, railway.md, vercel.md, … (usage docs, auto-paired with mcp)
├── resources/          timezone.md, … (reference docs composed into CLAUDE.md)
├── lessons/            tmux-dispatch-shell-expansion.md, … (learned patterns)
├── principles/         consolidate-dont-fork.md, … (design principles)
├── permissions/        access.json.template (permission templates)
└── post_actions/       notify-human.md, … (post-task workflows)
```

**Expertise** files are role scaffolding — what a manager *does*, not what it sounds like. (Voice goes elsewhere — see below.)

**Skills** are slash-command-style packages. Each skill is a directory with a `SKILL.md` (and optional helpers). The compositor symlinks them into the bot's `.claude/skills/` so edits in `library/skills/` propagate live to all bots that load the skill.

**MCP fragments** are small JSON files containing one (or a few) MCP server definitions, with `${ENV_VAR}` placeholders. The compositor merges them into a single `.mcp.json` per bot.

**Guardrails** are composable rule sets (`no-push-main`, `pii-protection`, `dbt-safety`). Each is appended to the bot's CLAUDE.md as a self-contained section.

**Protocols** are reusable communication and workflow patterns (`report-back`, `dispatch`, `telegram-routing`, `consensus-loop`). Same append model as guardrails.

### 2. `fleet.yaml` — the recipe

```yaml
fleet:
  name: my-fleet
  service_prefix: com.example.claudlobby
  telegram_group_chat_id: "-1001234567890"

  defaults:
    model: opus
    effort: max
    guardrails: [no-push-main, pii-protection]
    protocols: [report-back, telegram-routing]

  teams:
    main:
      manager: lead
      workers: [eng-1, reviewer-1]

  bots:
    lead:
      expertise: [orchestration]
      voice: voices/lead.md
      skills: [dispatch, fleet-status, lifecycle]
      mcp: [github, notion]
      telegram:
        handle: my_lead_bot
        token_env: TELEGRAM_TOKEN_LEAD
        require_mention: false

    eng-1:
      expertise: [software-engineering]
      mcp: [github]
      telegram:
        handle: my_eng_1_bot
        token_env: TELEGRAM_TOKEN_ENG1
        require_mention: true
```

Defaults flow into every bot. Lists (skills, guardrails, protocols) accumulate; scalars (model, effort) override.

`teams` exist so the compositor can inject a "Fleet You Manage" roster table into manager personas — that's how a manager bot knows who its workers are.

See [`fleet-yaml-schema.md`](fleet-yaml-schema.md) for every field.

### 3. `runtime/bots/<name>/` — generated output

For each bot in `fleet.yaml`, `claudlobby generate` writes:

```
runtime/bots/<name>/
├── CLAUDE.md                ← composed from expertise + voice + all library sections
├── .mcp.json                ← merged from library/mcp/ fragments
├── bot.conf                 ← env exports for lib/start-bot.sh
├── .claude/
│   ├── settings.local.json  ← memory dir, permissions, sandbox, hooks
│   └── skills/              ← symlinks → library/skills/<skill>
├── memory/                  ← bot-owned persistent state
├── data/                    ← bot-owned data + scripts
├── projects/                ← git checkouts (gitignored)
├── <name>.service           ← systemd unit (Linux)
└── <name>.plist             ← launchd plist (macOS)
```

#### CLAUDE.md composition order

```
 1. Expertise                  library/expertise/<name>.md (concatenated)
 2. Voice overlay              inserted after H1 (if voice: set)
 3. Mission                    from fleet.yaml mission: field
 4. Scope                      from fleet.yaml scope: field
 5. Model strategy             from fleet.yaml model_strategy: field
 6. Org structure              auto-generated from reports_to/manages
 7. Fleet roster               auto-generated for managers (from teams)
 8. Resources                  library/resources/<each>.md
 9. Integrations               library/integrations/<each>.md (auto-paired with mcp)
10. Principles                 library/principles/<each>.md
11. Protocols                  library/protocols/<each>.md
12. Guardrails                 library/guardrails/<each>.md
13. Lessons                    library/lessons/<each>.md
14. Post-actions               library/post_actions/<each>.md
```

Reading the generated file top-to-bottom, you can see exactly which library file each section came from. This matters for trust: bots are configured with composable text, not opaque code.

#### Templating

Expertise / protocol / guardrail files may use `{{BOT_NAME}}`, `{{SERVICE_PREFIX}}`, `{{CLAUDLOBBY_ROOT}}`, `{{TELEGRAM_GROUP_CHAT_ID}}`, `{{FLEET_NAME}}` placeholders. The compositor expands them per bot.

### 4. The host — systemd / launchd

The bot is started by `lib/start-bot.sh`:

```bash
# Reads runtime/bots/<bot>/bot.conf
# Sources $CLAUDLOBBY_ROOT/.env
# Launches `claude --channels plugin:telegram --remote-control` inside a tmux session
```

The OS-specific service unit (`<bot>.service` on Linux, `<bot>.plist` on macOS) wraps that. Both are generated by `claudlobby generate`. The easiest way to enroll and start a bot is `lib/spin-up-bot.sh`:

```bash
lib/spin-up-bot.sh runtime/bots/<bot>     # idempotent: enroll + start
```

This detects the OS, links the service unit, enables it, and starts the bot. For manual control:

```bash
# Linux
ln -sf $PWD/runtime/bots/<bot>/<bot>.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now <bot>.service

# macOS
ln -sf $PWD/runtime/bots/<bot>/<bot>.plist ~/Library/LaunchAgents/<service_prefix>.<bot>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<service_prefix>.<bot>.plist
```

## The sync-back loop

Bots can edit themselves in `runtime/bots/<name>/` during a session. `runtime/bots/` is gitignored, so the host filesystem is the workspace.

- **Skills auto-sync** because they're symlinks: editing `runtime/bots/X/.claude/skills/foo/SKILL.md` is actually editing `library/skills/foo/SKILL.md`. The change propagates to every bot using `foo`.
- **Composed CLAUDE.md doesn't auto-sync** — the next `generate` would overwrite it. The flow is:

  1. Bot edits its CLAUDE.md mid-session (e.g., learns a new pattern, codifies a rule)
  2. `claudlobby diff <bot>` shows the drift vs what `generate` would produce
  3. `claudlobby promote <bot>` (interactive — v1 is manual; v2 will have a picker) moves drifted content back to `library/expertise/<role>.md`, `library/voices/<voice>.md`, or a brand-new `library/guardrails/<name>.md` or `library/protocols/<name>.md`
  4. After promotion, `library/` reflects the learned change. Re-running `generate` produces a CLAUDE.md consistent with the new library state.

This is the foundation for the future ML / self-learning layer: drift becomes training data. When the same drift shows up across multiple bots, that's a signal a guardrail or protocol should exist.

## Validation

`claudlobby validate` is permissive by default:

- **Hard error** — bot references an `expertise` that doesn't exist (can't generate without a base)
- **Hard error** — `fleet.yaml` is invalid YAML or missing required keys
- **Warn** — bot references a skill / mcp / guardrail / protocol that doesn't exist (skipped)
- **Warn** — MCP fragment uses `${FOO}` but `FOO` is unset (bot boots; MCP server fails loudly at runtime)
- **Warn** — `voice:` path doesn't exist (bot uses persona's default tone)
- **Warn** — `telegram.token_env` env var is unset (bot won't connect to Telegram)

Pass `--strict` to make warnings into errors (CI use).

## Why Python (not Bash, not Node, not Bun)

The compositor logic is small (~400 lines of Python) but the *future* of the system is large:

- A self-learning layer behind `library/` (embeddings, similarity search for "I need a bot that…", suggested skills based on observed drift)
- A knowledge graph linking guardrails ↔ incidents that motivated them
- Versioned skill evolution with compatibility tracking

Python's data/ML ecosystem (sentence-transformers, chroma, scikit-learn) is the dominant story for those. Bash can't get there; Node could but the SDKs lag; bun is great but adds a runtime dep on every host. Python ships with macOS, Debian, Pi OS — zero install friction.

The bash lifecycle scripts (`start-bot.sh`, `keepalive.sh`, `report-back.sh`, `tg-post.sh`) stay bash. They're tightly coupled to tmux, launchd, systemd, and `gh` — bash is the right tool there.

## Why YAML (not TOML, not JSON)

`fleet.yaml` is hand-edited. YAML's lists and nested maps make a 17-bot fleet readable. TOML's flat-table model gets awkward fast (every bot needs its own `[bots.X]` table; lists nested in tables are painful). JSON has no comments, which makes commenting a fleet manifest harder.

The compositor consumes the parsed structure; if you'd rather generate `fleet.yaml` from a UI/API later, that's straightforward.
