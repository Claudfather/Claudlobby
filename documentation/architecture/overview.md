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

Twelve subdirectories, each a flat collection of small files:

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
├── post_actions/       (post-task lifecycle hooks — e.g. pre-stop handoff, daily wrap-up)
└── tools/               tool.yaml + Jinja template per tool, rendered into <bot_dir>/tools/
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

See [`fleet-yaml-schema.md`](../fleet-yaml-schema.md) for every field.

`fleet.yaml` is the middle of **three config tiers**: package-owned
[`system.yaml`](../system-yaml-schema.md) governs HOW the platform runs (host
jobs, per-fleet defaults), `fleet.yaml` governs WHO the bots are, and optional
[`projects.yaml`](../projects-yaml-schema.md) governs WHAT the work is.

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
├── tools/                   ← composited scripts (0755, generated — never hand-edited)
├── logs/                    ← bot log files
├── projects/                ← git checkouts (gitignored)
├── <service_prefix>.<name>.service   ← systemd unit (Linux)
└── <service_prefix>.<name>.plist     ← launchd plist (macOS)
```

#### CLAUDE.md composition order

```
 1. Voice overlay              inserted after H1 (if voice: set)
 2. Expertise                  library/expertise/<name>.md (concatenated)
 3. Fleet Mission              from fleet.yaml fleet.mission: field
 4. Mission                    from fleet.yaml bots.<name>.mission: field
 5. Autonomous Runner          "Your Continuous Job" section (if bot.autonomous_runner: set)
 6. Scope                      from fleet.yaml scope: field
 7. Shared Documentation       fleet-shared docs index (if shared_docs_path: set)
 8. Model strategy             from fleet.yaml model_strategy: field
 9. Org structure              auto-generated from reports_to/manages
10. Fleet roster               auto-generated for managers (from teams)
11. Projects                   auto-generated for managers (from projects.yaml)
12. Resources                  library/resources/<each>.md
13. Integrations               library/integrations/<each>.md (auto-paired with mcp)
14. Principles                 library/principles/<each>.md
15. Permissions                library/permissions/<each>.md
16. Protocols                  library/protocols/<each>.md
17. Guardrails                 library/guardrails/<each>.md
18. Lessons                    library/lessons/<each>.md
19. Post-actions               library/post_actions/<each>.md
```

Reading the generated file top-to-bottom, you can see exactly which library file each section came from. This matters for trust: bots are configured with composable text, not opaque code.

#### Templating

Expertise / protocol / guardrail files may use `{{BOT_NAME}}`, `{{SERVICE_PREFIX}}`, `{{CLAUDLOBBY_ROOT}}`, `{{TELEGRAM_GROUP_CHAT_ID}}`, `{{FLEET_NAME}}` placeholders. The compositor expands them per bot.

### 4. The host — systemd / launchd

The bot is started by `lib/start-bot.sh`:

```bash
# Reads runtime/bots/<bot>/bot.conf
# Sources $CLAUDLOBBY_ROOT/.env
# Launches `claude --channels plugin:telegram --remote-control` inside a tmux
# session on the bot's OWN tmux server (private `-L <socket>` == BOT_SERVICE),
# so one server's death drops only that bot, never the whole fleet
```

The OS-specific service unit (`<service_prefix>.<bot>.service` on Linux, `<service_prefix>.<bot>.plist` on macOS) wraps that. Both are generated by `claudlobby generate`. The easiest way to enroll and start a bot is `lib/spin-up-bot.sh`:

```bash
lib/spin-up-bot.sh runtime/bots/<bot>     # idempotent: enroll + start
```

This detects the OS, links the service unit, enables it, and starts the bot. For manual control:

```bash
# Linux
ln -sf $PWD/runtime/bots/<bot>/<service_prefix>.<bot>.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now <service_prefix>.<bot>.service

# macOS
ln -sf $PWD/runtime/bots/<bot>/<service_prefix>.<bot>.plist ~/Library/LaunchAgents/<service_prefix>.<bot>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<service_prefix>.<bot>.plist
```

## The sync-back loop

Bots can edit themselves in `runtime/bots/<name>/` during a session. `runtime/bots/` is gitignored, so the host filesystem is the workspace.

- **Skills auto-sync** because they're symlinks: editing `runtime/bots/X/.claude/skills/foo/SKILL.md` is actually editing `library/skills/foo/SKILL.md`. The change propagates to every bot using `foo`.
- **Composed CLAUDE.md doesn't auto-sync** — the next `generate` would overwrite it. The flow is:

  1. Bot edits its CLAUDE.md mid-session (e.g., learns a new pattern, codifies a rule)
  2. `claudlobby diff <bot>` shows the drift vs what `generate` would produce
  3. `claudlobby promote <bot>` — **v1 is a pointer only, not a mover**: it prints which `library/` file each category of drift belongs in (`library/expertise/<role>.md`, `voices/<voice>.md`, a brand-new `library/guardrails/<name>.md` or `library/protocols/<name>.md`, etc.); no content is copied automatically, and the bot's composed `CLAUDE.md` is not read back — you hand-edit the named file yourself. An interactive picker that performs the move is planned for v2.
  4. After hand-editing `library/` per the pointer, re-running `generate` produces a CLAUDE.md consistent with the new library state.

This is the foundation for the future ML / self-learning layer: drift becomes training data. When the same drift shows up across multiple bots, that's a signal a guardrail or protocol should exist.

## The observable plane

Full reference: [`observable-plane.md`](observable-plane.md) — the families and
the envelope, identity, the write spine, every door and its arming carrier, the
read side, the F18 cutover state machine, migrations and retention.

The four layers above answer "how does a bot get *composed*." A fifth concern sits orthogonal to
all of them: how does anyone find out what a *running* fleet actually did — which dispatch went
where, whether it was acknowledged, what a workstream's state is — after the fact, across
restarts, without grepping tmux panes. That's the **observable plane** (`claudlobby/plane/`, a
subpackage of the compositor; landed 2026-08-26/27).

It's an append-only, typed event kernel — not a new generation layer. Nothing in `fleet.yaml` or
`runtime/bots/` changes shape because of it; it's a recording substrate that the existing lib/
scripts write into, from the side.

```
claudlobby/plane/     — the kernel: contracts (typed event envelope), minted ids, canonical
                         serialization, SQLite storage (state/plane/plane.db), an ingest function,
                         queries, migrations
claudlobby/commands/plane.py
                       — the CLI surface: `claudlobby plane {status,doctor,serve,schema,spool}`,
                         `claudlobby emit` / `emit-batch`
lib/plane-emit.sh     — the shim every writing door calls: unix-socket daemon → cold CLI →
                         local spool, each fallback disclosed, never blocks the door's real action
lib/plane-daemon.sh   — launches `claudlobby plane serve`, a long-lived socket ingest daemon
lib/plane-session-start.sh
                       — SessionStart hook; mints the transcript-stable session_uid attached to
                         everything a session reports
```

**Five existing doors dual-write into it**: `dispatch-task.sh`, `report-back.sh`, `tg-post.sh`,
`workstream-update.sh`, `briefing-trigger.sh`. Each keeps writing its legacy JSONL ledger exactly
as before — that stays authoritative — and *additionally* emits a plane event alongside it. Dual
write, not migration.

**Everything about it is dormant by default**, the same pattern as `SESSION_DIGEST_ENABLED`
elsewhere in this codebase: a fleet that never sets `PLANE_EMIT_ENABLED=1` pays zero cost and
behaves exactly as it did before this existed. The daemon itself needs a *second*, host-level
arm (`system.yaml` `host.jobs.plane-daemon.enroll: true`) — a fleet can dual-write to the cold
CLI/spool path without ever running the daemon. See
[`system-yaml-schema.md`](../system-yaml-schema.md#unit-service--resident-host-services) for the
full `host.jobs`/`unit: service` shape and its arm/disarm recipe.

**It's write-side only, for now.** `claudlobby events`, `report-back`, and `brief` still read the
legacy JSONL ledgers — the plane kernel is a flight recorder being built ahead of the read/query
layer that will eventually consume it. Full model, activation semantics, and the fleet-review
history behind the design: `documentation/plans/2026-08-18-observable-plane-design-v2.md`.

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

The compositor logic has grown to ~24,000 lines of Python (`claudlobby/`, including the newer `plane/` observable-plane event kernel) — but the *future* of the system is larger still:

- A self-learning layer behind `library/` (embeddings, similarity search for "I need a bot that…", suggested skills based on observed drift)
- A knowledge graph linking guardrails ↔ incidents that motivated them
- Versioned skill evolution with compatibility tracking

Python's data/ML ecosystem (sentence-transformers, chroma, scikit-learn) is the dominant story for those. Bash can't get there; Node could but the SDKs lag; bun is great but adds a runtime dep on every host. Python ships with macOS, Debian, Pi OS — zero install friction.

The bash lifecycle scripts (`start-bot.sh`, `keepalive.sh`, `report-back.sh`, `tg-post.sh`) stay bash. They're tightly coupled to tmux, launchd, systemd, and `gh` — bash is the right tool there.

## Why YAML (not TOML, not JSON)

`fleet.yaml` is hand-edited. YAML's lists and nested maps make a 17-bot fleet readable. TOML's flat-table model gets awkward fast (every bot needs its own `[bots.X]` table; lists nested in tables are painful). JSON has no comments, which makes commenting a fleet manifest harder.

The compositor consumes the parsed structure; if you'd rather generate `fleet.yaml` from a UI/API later, that's straightforward.
