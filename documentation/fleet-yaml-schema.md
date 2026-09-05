# fleet.yaml — schema reference

`fleet.yaml` is the recipe. It tells `claudlobby generate` which bots to compose, which library pieces to assemble, and how to wire them up. One file at the repo root, hand-edited or `claudlobby new-bot`-driven.

## Top-level shape

```yaml
fleet:
  name: <fleet-name>                    # human-readable identifier
  service_prefix: <reverse-domain>      # e.g. "com.example.claudlobby" — used for service unit names
  telegram_group_chat_id: "<chat-id>"   # default group; bots can override per-bot
  human_telegram_id: "<user-id>"        # OPTIONAL — human's Telegram ID for DM allowlisting

  mission: <one paragraph>              # OPTIONAL — fleet-level goal anchor, composed into EVERY bot
  mission_file: <relative-path>         # OPTIONAL — fuller charter (managers compose it in full); REQUIRES mission

  accounts:                             # OPTIONAL — alternate Claude Code config dirs (multi-auth)
    default: ~/.claude
    work: ~/.claude-work

  system_defaults: true | false | { enabled: bool, hooks: bool, timers: bool, observability: bool }
                                         # OPTIONAL — gate system.yaml injection into defaults (default: true)

  defaults:                             # applied to every bot unless overridden
    model: opus | sonnet | haiku | fable   # or a pinned model ID, e.g. claude-opus-4-8
    effort: low | medium | high | max
    account: default
    prompt_suggestions: true | false    # CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION (default: false)
    disable_nonessential_traffic: true | false  # RC-safe headless trim set (default: true)
    spinner_tips_enabled: true | false  # settings.local spinnerTipsEnabled (default: false)
    preferred_notif_channel: <string>   # settings.local preferredNotifChannel (default: notifications_disabled)
    prefers_reduced_motion: true | false # settings.local prefersReducedMotion (default: true)
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
      enabled: true | false             # omit/None = inherit global settings.json
      auto_allow_bash: true | false
      network_allowed_domains: [<list>]
      filesystem_allow_write: [<list>]
    tool_permissions:                   # tool allow/deny defaults
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
      secret_files: { <ENV_VAR>: <fleet-relative-path>, ... }  # OPTIONAL — secret file paths, anchored on FLEET_ROOT
      tools: [<list>]                   # OPTIONAL — library/tools/ refs (composited scripts)
      tool_permissions:                 # OPTIONAL — tool allow/deny
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
      dangerously_skip_permissions: true | false  # OPTIONAL — opt into --dangerously-skip-permissions (default: false → acceptEdits)
      skip_auto_permission_prompt: true | false          # OPTIONAL — settings.local skipAutoPermissionPrompt (default: true)
      skip_dangerous_mode_permission_prompt: true | false # OPTIONAL — settings.local skipDangerousModePermissionPrompt (default: true)
      remote_control: true | false        # OPTIONAL — enable --remote-control (default: true)
      prompt_suggestions: true | false    # OPTIONAL — autocomplete suggestions (default: false)
      channels: [<list>]                  # OPTIONAL — channel plugins (default: [plugin:telegram@...])
      extra_flags: [<string>, ...]        # OPTIONAL — additional Claude CLI flags
      claudna_version: <string>           # OPTIONAL — pin clauDNA plugin version
      claudron_vault_path: <string>       # OPTIONAL — Claudron vault path for this bot
      claudron_session_loop: true | false # OPTIONAL — tri-state; default follows claudron_vault_path presence
      claudosseum_tenant_id: <string>     # OPTIONAL — Claudosseum telemetry tenant
```

## Field reference

### `fleet.name` / `fleet.service_prefix` / `fleet.telegram_group_chat_id`

Cosmetic + service-unit naming + default Telegram chat. Bots may override `chat_id` per-bot.

### `fleet.accounts`

Alternate Claude Code config directories. Useful when some bots authenticate against a different account. Bot stanza references the key (`account: work`); the generator writes `CLAUDE_CONFIG_DIR` into `bot.conf`.

### `fleet.human_telegram_id`

The human operator's Telegram user ID. When set, the compositor writes this into every bot's `access.json` `allowFrom` list, so the human can DM any bot without pending approval.

### `fleet.mission` / `fleet.mission_file`

The top of the goal hierarchy (fleet mission → project mission → the work itself). `mission` is **one paragraph** (no newlines — validator-enforced, since it renders into every bot's composed instructions): the goal anchor EVERY bot receives as a `## Fleet Mission` section, above per-bot `## Mission`. `mission_file` optionally points at a fuller charter, relative to `fleet.yaml` — managers compose its full body; workers get the paragraph plus the path (and `FLEET_MISSION_FILE` in `bot.conf`) to read on demand. `mission_file` **requires** `mission`, so a file-only config can never leave workers goal-blind. Keep real mission content in the fleet overlay (`local/<fleet>/`) — it is operator-specific and never belongs in the committed repo.

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

**`marketplaces`** — Third-party marketplace registrations, merged with defaults. The official Anthropic marketplace is built in and doesn't need a declaration. Each entry maps a marketplace name to a source descriptor (the nested `source` key is the Claude Code marketplace schema — `github`, `url`, `git`, or `local` types). The declared name must match the `name` field in the repo's `.claude-plugin/marketplace.json`: Claude Code registers marketplaces under that name, and both the start-time known-check and `plugin@name` pins in `additional` resolve through it.

**`include_defaults`** — Boolean (default `true`). Set to `false` to disable the built-in default plugins and marketplaces. Unusual — the validator warns when this is set.

### `fleet.system_defaults`

Gates how much of the package's [`system.yaml`](system-yaml-schema.md) (shared hooks, observability defaults, and job timers merged into every fleet) gets folded into this fleet's `defaults`. A value set directly in `fleet.defaults` always wins over the system tier for the same key — this field only controls whether the system tier is consulted at all.

```yaml
fleet:
  system_defaults: false   # kill switch — no system.yaml injection at all

# Per-category opt-out (all default true):
fleet:
  system_defaults:
    enabled: true          # false here == the bare `false` shorthand
    hooks: true            # merge system.yaml hooks
    timers: true           # merge system.yaml job timers
    observability: true    # merge system.yaml observability defaults
```

Omit the field entirely for the common case — everything defaults to `true`. Useful for a fleet that wants to supply its own hooks/observability tuning without the package defaults layered underneath.

### `fleet.defaults`

Applied to every bot. Merge rules by type:

- **Lists** (skills, expertise, guardrails, protocols, resources, lessons, principles, permissions, post_actions, mcp, integrations) — bot-level **appends to** defaults (deduped, order-preserved).
- **Scalars** (model, effort, account, mission) — bot-level **overrides** defaults.
- **Telegram** — merged **field-by-field**. Bot-level fields override individual defaults fields (e.g., a bot can override `require_mention` while inheriting `token_env`).
- **Sandbox** — lists (network_allowed_domains, filesystem_allow_write) are **unioned**; booleans (auto_allow_bash) use bot-level value.
- **Tools** — deny/allow lists are **unioned** across defaults and bot-level.
- **Hooks** — bot-level entries are **appended after** defaults per event. Same-matcher hooks group together.
- **Jobs** — `defaults.jobs` merges by job name over the system defaults (system.yaml → fleet.yaml, shallow per-entry spread; sibling jobs are preserved). Drives the composed fleet timer units.

#### `fleet.defaults.jobs.<name>.enroll`

System jobs flagged `enroll: false` (e.g. `weekly-worker-restart` — bouncing workers is disruptive) are **composed-but-dormant**: their units are generated and listed in the timers/ `DORMANT` manifest, but `setup-fleet` does not enroll them and reconcile's job-drift audit ignores them. Opt a fleet in per job:

```yaml
fleet:
  defaults:
    jobs:
      weekly-worker-restart: { enroll: true }
```

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

### `bots.<bot>.briefing`

Equippable scheduled briefing (#627). A bot turns briefings on in `fleet.yaml` alone: each **slot** becomes a composed per-(bot,slot) `OnCalendar` timer (`<service_prefix>.briefing-<bot>-<slot>`) whose `ExecStart` runs `lib/briefing-trigger.sh <fleet> <bot> <slot>` — a committed, never-swept trigger that delivers `/briefing <slot>` into the bot's own session through the slash-aware `lib/dispatch.sh`, so the skill actually fires (never hand-install a briefing cron). Presence of the block opts in; omit it and nothing is emitted for that bot.

```yaml
fleet:
  bots:
    kev:
      brief:                                # SessionStart boot brief (#904 M1 / #1102 R3) — default OFF
        on_start: true                      # STRICT bool (a typo string is a parse error, never an arming).
                                            # Composes a SessionStart hook (matchers: startup, compact) running
                                            # `claudlobby brief --bot <id> --boot`: own dispatch lines (open/
                                            # overdue/orphaned, <=3 + disclosed overflow) + an empty-state line
                                            # with provenance (never a bare zero) + the door line. Token-capped
                                            # ~250, fail-open one-liner on door failure, explicit 10s timeout.
                                            # Compose-time gate: generate REFUSES if the installed CLI lacks
                                            # `brief --boot` (composed settings outlive installs). Rollout is
                                            # operator-held: single-bot canary until cost numbers are ratified.
      briefing:
        slots:                              # slot name -> systemd OnCalendar (NOT 5-field cron)
          morning: "*-*-* 08:30:00"         # daily 08:30
          analytics: "Sun *-*-* 17:00:00"   # weekly, Sundays 17:00 (custom slot)
        sections:                           # optional per-slot section list
          morning: [overnight, calendar, overdue]
        sources: [github, gmail]            # optional data sources the /briefing skill reads
```

- **Slot names must be shell identifiers** (`[A-Za-z_][A-Za-z0-9_]*`) — they become the `BRIEFING_SECTIONS_<SLOT>` env-var suffix. A non-identifier name (`week-end`, `9am`) is a **hard parse error**.
- **Slot values are systemd `OnCalendar`**, the same dialect as `fleet.sweep.schedule` — not 5-field cron. A 5-field cron value (`30 8 * * *`) is a **hard parse error** (the chain has no cron-translation layer).
- Composed into the equipped bot's `bot.conf`: `BRIEFING_SLOTS` (space-separated slot names), `BRIEFING_SOURCES`, and one `BRIEFING_SECTIONS_<SLOT>` per slot that declares sections — **`<SLOT>` is upper-cased** (shell-var convention; the skill upper-cases the dispatched slot to read it). The `/briefing` skill falls back to sensible per-slot defaults when a var is unset, so equipping with zero personalization works.
- **Enrollment** is automatic: `setup-fleet`'s generic `install_fleet_timer[_launchd].sh` glob picks up the composed `<prefix>.briefing-*` units — no per-timer installer. `setup-fleet` also **reconciles** the dynamic family: `reconcile_briefing_timers` disables live enrolled briefing timers with no composed counterpart (a renamed/removed slot), glob-bounded to `<prefix>.briefing-*` and dry-run-logged first; `generate` prunes the corresponding unit files. Both sides carry an **abort-on-degenerate guard** — a composition that yields an empty briefing set while units exist is refused rather than allowed to wholesale-delete live timers.
- The validator **warns** when a briefing-equipped bot has no `integrations`/`mcp` source coverage (sections that read external data would be empty).

### `fleet.workstreams`

Bounds for the per-fleet workstream registry (materialized from the plane; there is no file) — the fleet's bounded portfolio of concurrent work across unrelated repos. Optional; the defaults apply when the block is omitted.

```yaml
fleet:
  workstreams:
    max_active: 12    # cap on concurrently active workstreams — `open` refuses past it (default: 12)
    lease_days: 14    # lease length in days before a workstream needs renewal (default: 14)
```

Both values emit into every bot's `bot.conf` (`WORKSTREAM_MAX_ACTIVE`, `WORKSTREAM_LEASE_DAYS`) and are read by the single-writer helper `lib/workstream-update.sh` at open/renew time. Parsed by `config.py` (`_coerce_workstreams`); the validator warns on non-positive values or unknown keys. Reads go through the read-only `claudlobby workstreams` CLI; see `advanced-patterns.md` for the workstream lifecycle.

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

`scope.org` is the bot's primary work boundary, **not** a credential binding — a bot may push to
an org it does not "scope" to (its product org plus the framework repo it contributes back to).
Credentials are declared separately, below.

### `fleet.defaults.credential_sources` / `bots.<name>.credential_sources`

```yaml
fleet:
  defaults:
    credential_sources:
      GITHUB_PAT: cli:gh-token      # this fleet takes the host CLI identity
  bots:
    ravi:
      credential_sources:
        GITHUB_PAT: literal          # ...except ravi, which uses its own .env value
```

Maps an env var **name** to where its value comes from, overriding the default the
library contract declares. Fleet-then-bot merged, bot winning — the same merge
`git_credentials` uses.

Values are members of a **closed, framework-owned registry**
(`known_values.KNOWN_CREDENTIAL_SOURCES`: `literal`, `cli:gh-token`, and
`mint:github-app` reserved). An unregistered value is a `validate` **error**, held to
exactly the same rule as a contract's own `source`: the resolver dispatches on the
whole identifier through a fixed `case` arm, so a value arriving from `fleet.yaml`
must be no more admissible than one arriving from a library fragment. A second,
laxer door into the same resolver would void the injection guarantee for both.

**This is schema only today.** Nothing resolves these yet — declaring one records
intent and appears in the credential register; it does not fetch anything. Supply
the value in a `.env` tier as usual. `mint:github-app` additionally warns, because
it is registered and deliberately unresolvable.

Why per-scope rather than one contract-wide answer: a var is resolvable at **any**
`.env` tier, so "where does this value come from" is a question each scope can answer
differently — a fleet equipping its own GitHub App, a single bot declining automatic
resolution.

### `fleet.defaults.git_credentials` / `bots.<name>.git_credentials`

Maps a **GitHub org** to the **name of the env var** holding that org's token. Declarable
fleet-wide under `defaults:` and overridable per bot; the two are merged per-org (a bot adds an
org without restating the fleet's).

```yaml
fleet:
  defaults:
    git_credentials:
      OrgA: ORGA_GITHUB_PAT       # org -> env var NAME, never a token
  bots:
    somebot:
      git_credentials:
        OrgB: ORGB_GITHUB_PAT     # merges with the fleet default, per-org
```

**Why this exists.** GitHub fine-grained PATs are **single-resource-owner**: one token cannot
carry write access to two orgs. A host whose bots contribute to more than one org therefore needs
one token per org — and a single git credential helper cannot serve both. Worse, `gh auth
setup-git` installs a helper for `https://github.com` that answers first for *every* org, so a
push to the second org presents the first org's token and fails with a `403` that reads like a
permissions problem and is not one.

**What it composes.** A `<bot_dir>/.gitconfig` that routes per org, plus
`GIT_CONFIG_GLOBAL` in `bot.conf` pointing at it. That file `include`s the operator's
`~/.gitconfig` first, so `user.name` / `user.email` are preserved (per the
`git-identity-no-overrides` guardrail) — it extends global config rather than replacing it. Orgs
you do not declare keep the host default helper.

**Values are never composed.** The generated helper references `$YOUR_VAR`; git reads the value
from the process env at push time, so the composed file carries no secret. See the env-name
contract in [`environment-variables.md`](environment-variables.md).

### `fleet.defaults.github_app` / `bots.<name>.github_app`

GitHub App git-auth routing (App-auth P3, epic #1270 — dormant by default; declaring nothing
composes nothing, byte-identical to before the feature existed). Declarable fleet-wide and
overridable per bot; the merge is per-field, and a bot may opt out of a fleet default with
`enabled: false`.

```yaml
fleet:
  defaults:
    github_app:
      slug: my-fleet-app          # App URL slug — with bot_user_id, commits become <slug>[bot]
      bot_user_id: 1234567        # the App BOT USER id (not the App id); both from setup-github-app.sh
      # orgs: [MyOrg]             # optional: route only these orgs via the App
```

**What it composes.** An App section in the per-bot `.gitconfig` (`GIT_CONFIG_GLOBAL`): a
`cache --timeout=3000` layer, the `lib/git-credential-github-app` helper by absolute path, the
ssh→https `insteadOf` rewrite (App tokens are HTTPS-only), the `<slug>[bot]` commit identity when
both identity fields are set, and a composed `tools/gh` shim (on PATH ahead of system `gh`) so
per-call App minting is mechanical.

**Commit identity is PER-ORG when `orgs:` is scoped** (#1300). With `orgs:` declared, the
`<slug>[bot]` identity applies only in repos whose **remote** is one of those orgs (via a
`includeIf "hasconfig:remote.*.url:…"` pulling in a sibling `.gitconfig-github-app-id` fragment,
git ≥ 2.36); every other repo keeps the operator identity from the include. So a bot that
commits to *both* the App's org and other orgs authors as `<slug>[bot]` on the App's repos and
as the operator elsewhere — and the App never needs access to those other orgs (their pushes use
the host default helper). A host-generic App (no `orgs:`) sets the identity globally instead.
The match is on the raw remote URL across all three spellings (https, `git@github.com:`,
`ssh://`) and is **case-sensitive** on the org segment — a remote cloned with non-canonical
casing (`orga` for a declared `OrgA`) authors as the operator, same as the `insteadOf` rewrite. Credential VALUES ride `GITHUB_APP_ID` /
`GITHUB_APP_INSTALLATION_ID` / `GITHUB_APP_PRIVATE_KEY_PATH` in the fleet `.env` — fleet-tier in
v1 (all bots on a host share one git credential cache, so a per-bot installation override could
cross-serve cached tokens; the validator warns).

**Precedence is per-DECLARATION, by section order.** An org with an explicit `git_credentials`
PAT wins over the App for that org — even when the PAT var is EMPTY (the org helper answers with
an empty password, git presents it, GitHub 401s; the App helper is never consulted). The gh
fallback survives only for org-scoped App declarations; a host-generic App suppresses it, because
its only remaining role would be silently substituting the operator identity after an App-helper
failure.

**Restart-not-reload applies** — the composed `.gitconfig`, `bot.conf` exports, and `tools/gh`
reach a running bot only at its next restart.

Keys must be org names — a key containing `/` is rejected, because repo-scoped routing would
silently never match. A declared var that is unset in every `.env` tier is a `validate` **warning**
(not an error): a missing token is an operator gap, and generation still succeeds.

Full root cause and the open design forks:
[`plans/2026-07-27-per-org-git-credential-routing.md`](plans/2026-07-27-per-org-git-credential-routing.md).

### `bots.<name>.model_strategy`

Escalation rules. When you have a bot that runs Sonnet for routine work but should escalate to Opus for architecturally tricky tasks, declare it here. Composed into a `## Model Strategy` section so the bot is aware of its own escalation pattern. Emitted as `MODEL_STRATEGY_*` env vars in `bot.conf` for skills/protocols to read.

Subagent model preferences (`explore`, `plan`, `general`) control which model subagents use. These are stored in the `raw` dict and emitted as env vars (e.g., `MODEL_STRATEGY_EXPLORE=haiku`).

### `bots.<name>.skills`

List of skill basenames from `library/skills/`. Generator symlinks each into `runtime/bots/<name>/.claude/skills/`. Bot accumulates `defaults.skills` + bot-level (deduped, in order).

### `bots.<name>.mcp` and `bots.<name>.integrations`

`mcp:` lists MCP fragments from `library/mcp/`. The generator merges them into `.mcp.json`.

For a bot that needs **multiple instances** of the same server (e.g. two Notion workspaces), use the mapping form with an `instances:` list instead of a bare string:

```yaml
mcp:
  - notion:
      instances: [default, work]
```

This emits one `.mcp.json` server per instance — `notion` (the `default`) and `notion-work` — and namespaces each instance's env-var placeholders by an uppercased prefix: the `default` instance keeps `NOTION_` (so it reads `NOTION_TOKEN`), while `work` becomes `NOTION_WORK_` (so it reads `NOTION_WORK_TOKEN`). Set one env var per instance in `.env`. Parsed by `config.py` (`_parse_mcp_list` → `McpEntry`); placeholder resolution lives in `claudlobby/mcp_resolve.py`. See the Notion integration guide for a worked example.

`integrations:` lists usage docs from `library/integrations/`. By default, integrations are **auto-paired with mcp** — listing `mcp: [github]` automatically pulls in `library/integrations/github.md` (when it exists). Override by setting `integrations:` explicitly.

### `bots.<name>.guardrails` / `protocols` / `resources` / `lessons` / `principles` / `permissions` / `post_actions`

Lists of basenames from the corresponding `library/<dir>/`. Each gets appended to CLAUDE.md as its own section. Bot accumulates `defaults.<list>` + bot-level (deduped, order-preserved).

### `bots.<name>.expertise` (note: `persona` is deprecated)

The `persona:` key is accepted as a backwards-compatible alias for `expertise:` but emits a deprecation warning. Use `expertise:` in all new configs — `persona:` will be removed in a future release.

### `bots.<name>.telegram`

Telegram config for this bot. Fields: `handle` (bot username), `token_env` (env var name holding the token), `require_mention` (whether the bot responds only to @-mentions), `chat_id` (override fleet-level group).

`token_env` names the **env var** that holds the Telegram token (e.g., `TELEGRAM_TOKEN_LEAD`). The actual token lives in `.env`. The generator writes the env-var *name* into `bot.conf` as `TELEGRAM_TOKEN_ENV_NAME`; `lib/start-bot.sh` reads through to the actual token. This indirection lets you commit `fleet.yaml` publicly while keeping tokens in a gitignored `.env`.

Telegram fields support defaults merging — set common values (like `token_env`) in `defaults.telegram` and override per-bot as needed.

### `bots.<name>.tools`

Attach library tools — composited scripts rendered into `<bot_dir>/tools/`
(0755) at generate time. Each entry is a `library/tools/<name>/` (or fleet
overlay) directory ref; see `library/tools/README.md` for authoring.

```yaml
tools:
  - audit-tracker                  # bare ref — manifest param defaults
  - portfolio-snapshot:            # per-bot param overrides
      params:
        lookback_days: 30
```

- Params are compose-time structure (paths, cadence) baked into the rendered
  script; per-bot values override manifest defaults per key. Unknown param
  names and missing required params are validation **errors**.
- Secrets never pass through params — a tool declares runtime env vars under
  `env:` in its `tool.yaml` and reads them via `os.environ`; the validator
  warns when one is unset (same contract as MCP fragments).
- `tools/` in the bot dir is compositor-owned: hand-edits are overwritten and
  files for detached tools are removed on every generate. Tool runtime
  outputs belong in `data/`.
- Note: this key previously held the tool allow/deny permissions block, which
  is now `tool_permissions:` (below). The old dict shape fails with a
  migration error.

### `bots.<name>.tool_permissions`

Control which Claude Code tools a bot may use. Two sub-fields:

- `deny: [Write, Edit, NotebookEdit]` — generates permission deny rules like `Write(**)` in `settings.local.json`. The bot cannot call these tools.
- `allow: [Agent, Bash, Read]` — generates permission allow rules. Useful when combined with auto-derived permissions from expertise profiles.

Deny wins over allow at the same layer. The validator warns if denied tools conflict with the bot's expertise (e.g., denying `Write` for a `software-engineering` bot).

### `bots.<name>.observability`

Controls fleet observability thresholds for heartbeat pulses and stuck-detection. Emitted as env vars in `bot.conf` for consumption by `lib/fleet-pulse.sh`, `lib/bot-vitals.sh`, and the dispatch watchdog.

```yaml
observability:
  pulse_interval: 300           # seconds between heartbeat pulses (default: 300)
  activity_stuck_threshold: 1800  # seconds of no tool-call activity before flagged (default: 1800)
  dispatch_deadline: 1800       # seconds after manager dispatch before flagged overdue (default: 1800)
  bridge_heal: true             # enable the keepalive Telegram-bridge auto-heal ladder (default: off)
  bridge_heal_max_attempts: 3   # heal bounce cap before escalation (keepalive default: 3)
  unassigned_check: true        # enable the reported-but-never-re-dispatched watchdog (default: off)
  unassigned_threshold: 7200    # seconds since the terminal report before flagging (default: 7200)
  unassigned_max_age: 86400     # stop reporting a strand past this age (default: 86400; <= 0 never stops)
```

`observability.reap_days` is retired (F18 closure, #1467): the event files it aged are gone and the plane's `plane prune` retention replaced them — a manifest that still sets it loads, and `claudlobby validate` warns, naming the key.

The three threshold fields are optional integers with sensible defaults. `bridge_heal` is a boolean. Can be set in `defaults:` to apply fleet-wide; bot-level overrides (a per-bot `bridge_heal: false` opts a bot out of a fleet default-on). The validator warns if `pulse_interval` is `<= 0` or greater than `3600` (1 hour), if `reap_days` is `<= 0` or greater than `365`, and if `bridge_heal_max_attempts` is outside `1..10`. There is currently no validation on `activity_stuck_threshold` or `dispatch_deadline`.

**`bridge_heal` must be set here, not via a `.env` tier.** The keepalive watchdog (`lib/keepalive.sh`) loads `bot.conf` only — it never sources the fleet `.env` tiers (those reach the bot's `claude` session via `start-bot.sh`, not the supervisor). Setting `OBSERVABILITY_BRIDGE_HEAL` in `defaults.env` (silently dropped) or a fleet `.env` file leaves keepalive's gate closed and the heal a no-op. This structured field is the one path that composes into every `bot.conf`, where keepalive's per-tick read picks it up. `bridge_heal` emits as the shell boolean `1`/`0` that the gate (`[ "${OBSERVABILITY_BRIDGE_HEAL:-0}" = "1" ]`) expects.

**`unassigned_check` is the mirror of the overdue-dispatch watchdog** (#1024). `overdue_dispatch` answers "work was sent and never came back"; this answers "work came back and nothing was sent" — a worker that reported terminal and was then forgotten. `activity_stuck` cannot cover it: a genuinely idle bot *is* idle, so keepalive re-stamps `.idle` and that branch never fires. The check emits `worker_unassigned` and pushes a debounced `[FLEET-PULSE]` line, exactly like `overdue_dispatch`.

It is **off by default** because it is the only pulse check whose subject is the *assignment loop* rather than a process: it reports that a human or manager stopped assigning, which a fleet with nobody to act on it can only read as noise. Managers are excluded automatically (a manager has no assigner, so reported-and-not-re-tasked is its resting state).

**`unassigned_max_age` is a trade in both directions, and the second one matters here.** Past the cap the check stops reporting a strand *and clears its debounce state*, so the emitted signal becomes indistinguishable from "the strand resolved" — a worker idle longer than the window goes quiet again. That is bounded rather than immediate (roughly 3–4 pushes at the default 6h renotify cadence before it lapses) and it is the same expiry `overdue_dispatch` already applies via `DISPATCH_OVERDUE_MAX_AGE_S`, so it is a deliberate symmetry rather than a gap unique to this check. But a check that exists to close a silent failure does reopen a narrower one at the far end: set `unassigned_max_age: 0` to refuse the trade and keep reporting indefinitely.

**These three must be set here, not via a `.env` tier** — the same constraint as `bridge_heal`, for a different reason. The composed fleet-pulse unit carries a fixed set of `Environment=` lines (`CLAUDLOBBY_ROOT`, `PATH`, `CLAUDLOBBY_FLEET`, `TELEGRAM_GROUP_CHAT_ID`, plus any `fleet_pulse:` knobs — see below) and `lib/fleet-pulse.sh` sources no `.env` file, so a fleet-tier `.env` setting never reaches it. `bot.conf` is the one path that does, and the per-bot granularity is useful in its own right: a deliberately parked bot can set `unassigned_check: false` and stop tripping the alarm without disarming the fleet.

Emitted env vars: `OBSERVABILITY_PULSE_INTERVAL`, `OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD`, `OBSERVABILITY_DISPATCH_DEADLINE`, `OBSERVABILITY_BRIDGE_HEAL`, `BRIDGE_HEAL_MAX_ATTEMPTS`, `OBSERVABILITY_UNASSIGNED_CHECK`, `OBSERVABILITY_UNASSIGNED_THRESHOLD`, `OBSERVABILITY_UNASSIGNED_MAX_AGE`.

### Fleet-pulse escalation (environment overrides)

`lib/fleet-pulse.sh` escalates to Telegram when the same critical event (`service_down`, `session_missing`) affects multiple bots within a short window.

Set these in the fleet-level `fleet_pulse:` block. The composer emits them as `Environment=` lines on the fleet-pulse timer unit, which is the only tier the script can read:

```yaml
fleet_pulse:
  escalation_threshold: 3
  escalation_window: 15
  escalation_chat_id: "-1001234567890"
  renotify_after_s: 21600
  rearm_window_s: 0
```

Omit a key to keep the script's own default; the composer emits only what is set, so a default lives in exactly one place.

> **Do not put these in a `.env` file.** Earlier revisions of this section said to
> use the fleet `.env` or to edit the unit's environment. **Neither worked** (#1120):
> the timer unit sources no `.env` at any tier, and the unit is generated — a
> hand-edit is reverted by the next `generate`, and its first line says so. Both
> paths failed silently, which is why they are now a `claudlobby freshbox` FAIL
> (`fleet_pulse_env_inert`) rather than something an audit finds weeks later.

The composed env vars, and what each does:

| Env var | Default | Purpose |
|---------|---------|---------|
| `FLEET_PULSE_ESCALATION_CHAT_ID` | _(fallback)_ | Operator override for the fleet-wide alert chat ID, honored by **all** env-less alert paths (fleet-pulse escalation, creds-check, and lib-common `_emit_fleet_signal`) via the shared `resolve_alert_target` resolver. Full precedence: this override → the composed `TELEGRAM_GROUP_CHAT_ID` (baked into every fleet timer unit) → a scan of the fleet's bots for the first non-empty `TELEGRAM_GROUP_CHAT_ID` in bot.conf (bots that omit it are skipped). If none resolves, fleet-pulse escalation is disabled and logs a warning rather than failing silently. |
| `FLEET_PULSE_ESCALATION_THRESHOLD` | `2` | Number of distinct bots that must hit the same critical event within the window to trigger escalation. |
| `FLEET_PULSE_ESCALATION_WINDOW` | `10` | Lookback window, in minutes, for counting affected bots. |
| `FLEET_PULSE_RENOTIFY_AFTER_S` | `21600` (6h) | Age at which a debounce marker re-fires, so an unresolved episode is not announced once and then silent forever (#831). `0` disables the re-fire. |
| `FLEET_PULSE_REARM_WINDOW_S` | _(lib-common default)_ | Bounds debounce re-arming during a known crashloop. `0` disables the bound. |

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

- `enabled` — turn the sandbox layer on or off for this bot. Omitting it (or `None`) inherits the global `settings.json` sandbox setting; `true`/`false` overrides it per-bot.
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

### `bots.<name>.external_paths`

Declarations that bless absolute paths **outside** the fleet overlay so a compose source may reference them. The compositor runs a **deny-by-default source guard**: any absolute path written into a compose source (a bot's `env`, an MCP fragment's `args`, a permission grant, a timer script, …) fails `generate` unless it is either **anchored** on a composer path or **declared** here.

Prefer anchoring. A path *inside* the fleet is written against one of the three composer anchors, and the compositor derives its real, migration-safe location:

| Anchor | Resolves to |
|--------|-------------|
| `${FLEET_ROOT}` | the fleet overlay root |
| `${BOT_DIR}` | this bot's runtime dir |
| `${CLAUDLOBBY_ROOT}` | the install root |

```yaml
env:
  PRINTIFY_MCP_ENTRY: "${FLEET_ROOT}/runtime/bots/kev/data/printify-mcp/dist/index.js"
```

`external_paths` is only for a genuine dependency that truly lives outside the fleet — a host mount source, a system tool tree. Each entry is a `{path, purpose}` mapping; `purpose` is required (it is the verifiable justification — a YAML comment is invisible to the guard):

```yaml
external_paths:
  - path: /var/lib/printify/data       # exact path
    purpose: printify persistent data volume
  - path: /opt/vendor-tool/**          # or a subtree, trailing /** only
    purpose: vendored CLI the bot shells out to
```

Rules enforced at parse time: the path must be absolute (declare the expanded form — `~` and relative paths are rejected); no `..`; a trailing `/**` blesses a whole subtree but only on a segment boundary and only below a breadth floor of two leading path segments (never `/**`, `/opt/**`, or another root-adjacent width). A `/**` declaration matches the prefix and anything below it (`/var/lib/printify/**` blesses `/var/lib/printify/data/x` but never the sibling `/var/lib/printify-secret`). Set in `defaults:` to apply fleet-wide; bot-level entries union with the defaults.

For a host path a bot should *read and write through the bot dir*, prefer `mounts:` (a managed symlink) over a raw `external_paths` grant.

`claudlobby freshbox` reports the surface these declarations bless: an INFO line per declaration with the source values it actually covers (over-broad `/**` grants become visible this way), a `WARN` for a declaration that covers nothing (declaration rot), and a `FAIL` for a path-classified value that is **undeclared and unanchored in a fleet-tier `.env`** (`<bot_dir>/.env`, `local/<fleet>/.env`) — the runtime-sourced surface `generate` cannot see (`.env` values are masked in the report; a host-tier `root/.env` / `~/.env` value WARNs). Rendered `tools/` scripts are scanned for improper fleet paths on the same footing as the other emitted wiring.

### `bots.<name>.secret_files`

Maps an env var **name** to a **fleet-relative path** of a secret file — service-account keys,
credentials that live inside the fleet overlay rather than as a single `.env` value. Fleet-relative
by contract: an absolute path, a `~`-relative path, or a path containing `..` is rejected at
`generate`, so the composed line is always derived from `${FLEET_ROOT}` rather than hand-typed.

```yaml
bots:
  kev:
    secret_files:
      GOOGLE_SERVICE_ACCOUNT_JSON: secrets/kev-service-account.json
```

Composes into `bot.conf` as `export GOOGLE_SERVICE_ACCOUNT_JSON="$FLEET_ROOT/secrets/kev-service-account.json"`
— the path is derived and migration-safe, but the *file itself* (like any fleet secret) belongs in
`local/<fleet>/` and is never committed. Fleet-then-bot merged, bot winning, same shape as `env:`.

### `bots.<name>.bench`

Boolean (default `false`). Marks this bot as the fleet's benchmarking target for cold-start timing (`lib/bench-cold-start.sh`). Multi-bot fleets should set `bench: true` on exactly one bot so the benchmarking script knows which bot to measure. The validator warns if a fleet has multiple bots and none has `bench: true`.

### `bots.<name>.permission_mode`

String (default `null`). Sets the `--permission-mode` flag on the Claude Code CLI, providing more granular control than `dangerously_skip_permissions`. When set, this field takes precedence over `dangerously_skip_permissions`. When **neither** this nor `dangerously_skip_permissions` is set, the compositor emits a conservative default of `--permission-mode acceptEdits` — edits are auto-accepted while the composed allow/deny lists are still enforced.

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

Boolean (default `false`). Set to `true` to run the bot with `--dangerously-skip-permissions`, which bypasses all tool-call permission prompts **and** the composed allow/deny lists. This is an explicit opt-in: when neither this nor `permission_mode` is set, the compositor's conservative default is `--permission-mode acceptEdits`, which is headless-safe without bypassing the permission lists. Superseded by `permission_mode` when both are set. Can be set in `defaults:` to apply fleet-wide; bot-level overrides.

### `bots.<name>.skip_auto_permission_prompt` / `bots.<name>.skip_dangerous_mode_permission_prompt`

Booleans (default `true` for both). Set `settings.local.json`'s `skipAutoPermissionPrompt` /
`skipDangerousModePermissionPrompt` — the first-run interactive consent prompts Claude Code would
otherwise show before auto-accepting edits or entering a dangerous permission mode. Distinct from
the `--dangerously-skip-permissions` CLI flag above: these suppress the one-time interactive
first-run prompts a headless, supervised bot would otherwise hang on with no terminal to answer
them. Can be set in `defaults:`; bot-level overrides.

### `bots.<name>.remote_control`

Boolean (default `true`). Controls whether the bot runs with `--remote-control`, which allows dispatch via `tmux send-keys`. Disable for standalone bots that should only respond to Telegram messages. Can be set in `defaults:`.

### `bots.<name>.prompt_suggestions`

Boolean (default `false`). Controls `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION` env var. When true, Claude Code shows autocomplete prompt suggestions. Headless bots don't need them — only enable for interactive use. Can be set in `defaults:`.

### `bots.<name>.disable_nonessential_traffic`

Boolean (default `true`). When true, emits the **RC-safe granular trim set** into `bot.conf` — `DISABLE_AUTOUPDATER` (claudlobby manages Claude Code updates itself), `DISABLE_ERROR_REPORTING`, `DISABLE_BUG_COMMAND`, and both feedback-survey spellings — suppressing the interactive prompts and background jobs a supervised bot must never see. It deliberately does **not** emit `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` or `DISABLE_TELEMETRY`: Claude Code gates remote-control behind feature-flag evaluation that rides the telemetry channel, so either of those silently disables `--remote-control` and every channel reply with it (the July 2026 fleet-wide Telegram outage — #533). Telemetry therefore stays enabled on channel/RC bots; the validator errors if either RC-killing var is reintroduced via `env:` on a bot that uses remote-control or channels. Set false to omit the trim set entirely. Can be set in `defaults:`.

### `bots.<name>.spinner_tips_enabled`

Boolean (default `false`). Sets `spinnerTipsEnabled` in `settings.local.json`. When false, Claude Code omits the "Tip:" lines shown while working. Can be set in `defaults:`.

### `bots.<name>.preferred_notif_channel`

String (default `notifications_disabled`). Sets `preferredNotifChannel` in `settings.local.json` — the OS notification channel. A supervised headless bot has no desktop, so notifications are disabled by default. Can be set in `defaults:`.

### `bots.<name>.prefers_reduced_motion`

Boolean (default `true`). Sets `prefersReducedMotion` in `settings.local.json`, stopping Claude Code from drawing the animated spinner. Safe for supervised bots because keepalive liveness is marker-based (`data/.last-tool-call`), not spinner-based. Can be set in `defaults:`.

### `bots.<name>.channels`

List of channel plugin identifiers (default `["plugin:telegram@claude-plugins-official"]`). Each entry generates a `--channels <name>` CLI flag. Override to use a different messaging plugin or disable channels entirely with an empty list — an explicit `channels: []` wins over the default (presence-based override, fixed alongside #533; previously the empty list was silently ignored). Can be set in `defaults:`.

### `bots.<name>.extra_flags`

List of additional CLI flags appended to `CLAUDE_FLAGS` in `bot.conf`. Use for any Claude Code flags not covered by dedicated fields. Bot-level list appends to defaults (deduped). Example: `["--verbose"]`.

### `bots.<name>.claudna_version` / `claudron_vault_path` / `claudosseum_tenant_id`

Ecosystem-aware fields connecting bots to clauDNA, Claudron, and Claudosseum. All optional; emitted as env vars in `bot.conf` when set.

| Field | Env var | Purpose |
|-------|---------|---------|
| `claudna_version` | `CLAUDNA_VERSION` | Pin the clauDNA plugin version (e.g., `"0.2.0"`). Skills/hooks can read this to gate behavior by version. |
| `claudron_vault_path` | `CLAUDRON_VAULT_PATH` | Pointer to the Claudron **vault root** (e.g., `"vaults/my-fleet"`) — not a per-bot sub-path; one vault is one tenant. The bot's `claudron` CLI resolves the vault from this env var (Claudron `docs/CLI_CONTRACT.md` §Environment). |
| `claudron_session_loop` | _(none — gates hook/grant composition, not an env var)_ | Wires the Claudron session loop: composed SessionStart/PreCompact/SessionEnd hooks plus a narrow allowlist of `claudron` CLI verb grants. Tri-state boolean — unset defaults to `true` exactly when `claudron_vault_path` is set, `false` otherwise; set explicitly only to override (e.g. `false` on a vault-wired bot meant to reach the vault by hand-run CLI alone). `true` with no `claudron_vault_path` is a `claudlobby validate` error. See `documentation/integrations/claudron-integration.md`. |
| `claudosseum_tenant_id` | `CLAUDOSSEUM_TENANT_ID` | Tenant identifier for Claudosseum telemetry (e.g., `"tenant_abc123"`). Bots emit structured signal to this tenant when configured. |

Can be set in `defaults:` (fleet-wide) or per-bot (bot overrides default). The validator warns if `claudron_vault_path` is set but the **`claudron` CLI is not reachable**, or the path does not resolve to a vault — the CLI is the fleet-consumption door (Claudron `docs/INTEGRATION.md`). It does *not* check for an MCP server: that door is demand-gated and unbuilt (decision C).

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

The compositor auto-derives permission entries in `settings.local.json` from several sources. These don't require explicit `tool_permissions:` config — they're generated automatically.

| Source | What it generates |
|--------|-------------------|
| **Guardrails** | `library/guardrails/<name>.md` frontmatter can declare the same `permissions: { allow_all, allow, deny, bash_allow }` shape as expertise — guardrails are usually deny-only (their rare allows merge alongside expertise's). |
| **Expertise profiles** | `library/expertise/<name>.md` frontmatter can declare `permissions: { allow_all, allow, deny, bash_allow }`. Merged across all expertise files. |
| **MCP permission contracts** | `library/mcp/<name>.json` `_permissions_contract.tools` field lists tool names → generates `mcp__<server>__<tool>` allow patterns. |
| **Channel plugins** | Bots with a Telegram handle get allow rules for `mcp__plugin_telegram_telegram__*` tools. |
| **Skills** | Each skill generates `Skill(<name>)` and `Skill(<name>:*)` allow patterns, plus any `tool_grants` (Bash/MCP/bare tools) the skill's own frontmatter declares. |
| **Claudron session loop** | When `claudron_session_loop` resolves true, a narrow allowlist of `claudron` CLI verb grants (never a `Bash(claudron *)` wildcard). |
| **Base tools** | `Read`, `Grep`, `Glob` are always allowed when an allow list is non-empty. |

Layering order (later layers win on conflict): guardrails → expertise → MCP contracts → channels → skills → fleet defaults → bot-level. Bot-level deny always wins. (Sibling-bot directory isolation is a separate, always-on deny layer beneath all of these — see `permissions-model.md`.)

## Composition order (per bot)

The generator assembles `runtime/bots/<name>/CLAUDE.md` in this exact order:

1. **Expertise** — concatenated. First file's H1 titles the bot; subsequent files' H1s are stripped and bodies append.
2. **Voice overlay** — injected after the H1 line and the first blank line.
3. **Mission** — `## Mission` section with the paragraph from `fleet.yaml`.
4. **Autonomous Runner** — `## Autonomous Runner — Your Continuous Job` section if `autonomous_runner:` is set.
5. **Scope** — `## Scope` section if `scope:` is set.
6. **Shared Documentation** — `## Shared Documentation` section when the fleet has a shared docs directory configured.
7. **Model strategy** — `## Model Strategy` section if `model_strategy:` is set.
8. **Org Structure** — `## Org Structure` section if `reports_to` or `manages` is set.
9. **Team roster** — `## Fleet You Manage` table for managers (auto-generated from `teams`).
10. **Resources** — `## Resources` section, each `library/resources/<name>.md` concatenated.
11. **Integrations** — `## Integrations` section (auto-paired with mcp by default).
12. **Principles** — `## Principles` section.
13. **Permissions** — `## Permissions` section.
14. **Protocols** — `## Protocols` section.
15. **Guardrails** — `## Guardrails` section.
16. **Lessons** — `## Lessons` section.
17. **Post-actions** — `## Post-actions` section.

The result is a single CLAUDE.md you can read top-to-bottom. Each section's origin is obvious from the markdown headers.

In addition to CLAUDE.md, the generator produces:

- **bot.conf** — env vars sourced at startup (model flags, Telegram config, model strategy)
- **.mcp.json** — merged MCP server configs with env-var placeholders
- **.claude/settings.local.json** — memory dir, sibling isolation, tool permissions, sandbox config, hooks
- **access.json** — Telegram channel config (requireMention, DM policy, human allowlist)
- **\<bot\>.service / .plist** — systemd / launchd supervision units
- **.claude/skills/** — symlinked skill directories
- **mounts/** — symlinks to external host paths under `<bot-dir>/mounts/<name>`

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
- **Warn** — `claudron_vault_path` is set but the `claudron` CLI is not on PATH, or the path does not resolve to a vault

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


## `fleet.github.mention_allowlist`

Handles a bot may `@`-mention on GitHub. **Everything else is rewritten** —
`@name` becomes `` `name` `` — because the harm class is any `@word` that
happens to be a real account, which is unbounded (#1019).

```yaml
fleet:
  github:
    mention_allowlist:
      - acme-dev
```

Optional; **empty by default, and that default is deliberate**: no mention
notifies anyone until someone declares it in a manifest. Unioned across every
fleet on the host, since a handle worth notifying from one fleet is worth
notifying from all.

A composed **bot name always wins over this list** and cannot be allowlisted.
Without that, someone eventually adds a bot's name here meaning *our* bot and
silently re-arms the original bug.
