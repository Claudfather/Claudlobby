# Environment Variables Reference

The compositor writes environment variables to each bot's `bot.conf`. These are sourced at startup by `lib/start-bot.sh` and available to all lib/ scripts, hooks, and skills.

## The env contract: names in git, values in `.env`

**An env var NAME is a contract and belongs in git. Only its VALUE is a secret and belongs in the
gitignored `.env`.**

This is the rule for every declaration surface in the shared package — `library/mcp/*.json`
(`${GITHUB_PAT}`, `${NOTION_TOKEN}`), `library/tools/<name>/tool.yaml` (`env: [SIMPLEFIN_ACCESS_URL]`),
and `fleet.defaults.git_credentials`. All of those files are **tracked**, and they name variables
without ever holding a value.

The reason is that a name is what the compositor and validator must both agree on: `validate`
warns when a declared var is unset, `freshbox` audits declarations against `.env` tiers, and a
reader needs to know what to provision. None of that works if names live only in an untracked file.
A value in a tracked file, by contrast, is a leaked credential.

So the split is:

| Lives in | Example | Tracked? |
|---|---|---|
| Declaration (the **name**) | `env: [MYORG_GITHUB_PAT]`, `${GITHUB_PAT}`, `git_credentials: {OrgA: ORGA_GITHUB_PAT}` | **yes** — `library/`, `fleet.yaml.example`, docs |
| Value | `MYORG_GITHUB_PAT=github_pat_xxxxxxxxxxxxxxxxxxxx` | **never** — `.env`, `local/` |

The one thing that is *not* obvious and is worth stating plainly: this holds for **fleet-specific
env var names too**. A name like `MYORG_GITHUB_PAT` is not fleet-specific data even if only
one fleet sets it — it is the interface. Reasoning from "anything credential-adjacent is secret" to
"the name goes in the overlay" is a natural inference and the wrong one.

## Bot Identity

| Variable | Source | Description |
|----------|--------|-------------|
| `BOT_ID` | `bots.<name>` key | Bot identifier (same as fleet.yaml key) |
| `BOT_NAME` | `bots.<name>.name` (defaults to key) | Bot display name (defaults to BOT_ID) |
| `BOT_SERVICE` | Derived | systemd/launchd service name (e.g., `com.example.fleet.botname`) |
| `TMUX_SOCKET` | Derived | Per-bot tmux server socket name (`-L` argument) — equals `BOT_SERVICE`. One tmux server per bot, so one server's death drops only that bot. Peer scripts resolve it via `tmux_socket_for_bot()` (`lib/lib-common.sh`) |
| `BOT_LABEL` | Derived | Human-readable label for the service |
| `BOT_DIR` | Derived | Absolute path to the bot's runtime directory |
| `CLAUDLOBBY_ROOT` | Detected | Absolute path to the claudlobby repository root |

## Fleet Context

| Variable | Source | Description |
|----------|--------|-------------|
| `FLEET_NAME` | `fleet.name` | Fleet identifier |
| `SERVICE_PREFIX` | `fleet.service_prefix` | Service name prefix for systemd/launchd units |
| `FLEET_STATE_PATH` | Derived | Path to `fleet-state.json` for atomic state updates |
| `MANAGER_TMUX` | `teams` config | tmux session name of this bot's manager (if in a team) |
| `MANAGER_TMUX_SOCKET` | Derived | tmux socket (`BOT_SERVICE`) of this bot's manager, or its own socket when this bot is itself a manager. Used for cross-socket sends via `bot_tmux_send()` |
| `TMUX_TMPDIR` | Pinned constant | tmux's tmpdir (`/tmp`), pinned so every script's `tmux -L <socket>` resolves to the same server — drift here would silently spawn a duplicate server for the same socket name |
| `FLEET_MISSION_FILE` | `fleet.mission_file` | Absolute path to the fuller fleet charter file — emitted only when both `mission_file` and `mission` are set |
| `WORKSTREAM_MAX_ACTIVE` | `fleet.workstreams.max_active` | Cap on concurrently active workstreams in the fleet registry (default: 12) |
| `WORKSTREAM_LEASE_DAYS` | `fleet.workstreams.lease_days` | Workstream lease length in days before renewal is needed (default: 14) |

## Telegram

| Variable | Source | Description |
|----------|--------|-------------|
| `TELEGRAM_GROUP_CHAT_ID` | `bots.<name>.telegram.chat_id` (falls back to `fleet.telegram_group_chat_id`) | Telegram group chat ID for posting |
| `TELEGRAM_TOKEN_ENV_NAME` | `bots.<name>.telegram.token_env` | Name of the env var holding the bot's Telegram token |
| `TELEGRAM_REQUIRE_MENTION` | `bots.<name>.telegram.require_mention` | Whether the bot requires @-mention to respond in groups |
| `TELEGRAM_BOT_HANDLE` | `bots.<name>.telegram.handle` | Bot's Telegram username (without @) |
| `TELEGRAM_STATE_DIR` | Derived | Path to the Telegram channel plugin state directory |

## Claude Code Flags

| Variable | Source | Description |
|----------|--------|-------------|
| `CLAUDE_FLAGS` | Multiple fields | Composed CLI flags string (--remote-control, --permission-mode, --model, --channels, etc.) |
| `CLAUDE_CONFIG_DIR` | `bots.<name>.account` | Custom Claude config directory (only set for non-default accounts) |
| `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION` | `bots.<name>.prompt_suggestions` | Whether to show prompt suggestions (true/false) |
| `DISABLE_AUTOUPDATER`, `DISABLE_ERROR_REPORTING`, `DISABLE_BUG_COMMAND`, `DISABLE_FEEDBACK_SURVEY`, `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY` | `bots.<name>.disable_nonessential_traffic` | When true (default), emits this granular RC-safe set to suppress the built-in auto-updater (claudlobby self-manages updates), Sentry error reporting, `/bug`, and the satisfaction survey. Deliberately **not** the `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` umbrella nor `DISABLE_TELEMETRY` — those also disable feature-flag evaluation and with it `--remote-control`, silently dropping channel replies (#533). Presence flags — a false override omits them, never `=0` |
| `STARTUP_PROMPT` | `bots.<name>.startup_prompt` | Initial prompt sent to bot on startup |

## Model Strategy

| Variable | Source | Description |
|----------|--------|-------------|
| `MODEL_STRATEGY_BASE` | `bots.<name>.model_strategy.base` | Base model for the bot |
| `MODEL_STRATEGY_ESCALATE_TO` | `bots.<name>.model_strategy.escalate_to` | Model to escalate to for complex tasks |
| `MODEL_STRATEGY_ESCALATE_WHEN` | `bots.<name>.model_strategy.escalate_when` | Condition for escalation |
| `MODEL_STRATEGY_COMPACT_WHEN` | `bots.<name>.model_strategy.compact_when` | Condition for context compaction |
| `MODEL_STRATEGY_EXPLORE` | `bots.<name>.model_strategy.explore` | Model override for Explore subagents |
| `MODEL_STRATEGY_PLAN` | `bots.<name>.model_strategy.plan` | Model override for Plan subagents |
| `MODEL_STRATEGY_GENERAL` | `bots.<name>.model_strategy.general` | Model override for general subagents |

## Observability

| Variable | Source | Description |
|----------|--------|-------------|
| `OBSERVABILITY_PULSE_INTERVAL` | `bots.<name>.observability.pulse_interval` | Seconds between heartbeat pulses (default: 300) |
| `OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD` | `bots.<name>.observability.activity_stuck_threshold` | Seconds of inactivity before flagged stuck (default: 1800) |
| `OBSERVABILITY_DISPATCH_DEADLINE` | `bots.<name>.observability.dispatch_deadline` | Seconds after dispatch before flagged overdue. Composed for every bot since #1481; 86400 (24h) when the fleet declares none, `0` = open-ended. SECONDS — 24h is 1440 minutes, and only `--deadline-min` speaks minutes |
| `RC_READY_TIMEOUT_S` | env override (`start-bot.sh`) | Seconds to wait for the `remote-control is active` readiness string before logging TIMEOUT and emitting the `rc_timeout` event (default: 90). Not composed from fleet.yaml — a raw override for slow hosts and the test harness |
| `KEEPALIVE_BOOT_GRACE_S` | env override (`lib-common.sh` `service_is_starting`) | Seconds a unit may stay mid-start before keepalive stops treating it as booting and restarts it, and fleet-pulse resumes alarming (default: 300). Budgets ONE phase — `ExecStart`, bounded by `RC_READY_TIMEOUT_S` — so the composed boot stagger never eats it. Raise it only on a host where cold starts genuinely exceed it; the cap is what stops a wedged `start-bot.sh` suppressing the watchdog forever (#1002). Not composed from fleet.yaml |

## Code-Audit Sweep

Emitted only into the `fleet.sweep.owner_bot`'s `bot.conf` (see `fleet.sweep` in the fleet.yaml schema reference) — the fleet-level nightly selector (`lib/code-audit-sweep.sh`) needs to resolve exactly one owner.

| Variable | Source | Description |
|----------|--------|-------------|
| `SWEEP_OWNER_BOT` | `fleet.sweep.owner_bot` | Bot ID whose session runs the audit (set only in this bot's own `bot.conf`) |
| `SWEEP_REPOS` | `fleet.sweep.repos` | Space-separated repo list to audit — falls back to the owner's `scope.repos` when `sweep.repos` is unset |
| `SWEEP_LABEL` | `fleet.sweep.label` | GitHub label used to track audit staleness (default: `auto-audit`) |
| `SWEEP_AUDIT_TYPES` | `fleet.sweep.audit_types` | Space-separated audit types rotated per run (default: `tech-debt`) |

## Projects

Emitted into **every** bot's `bot.conf` from `projects.yaml` — one pair per project — so any sprint/runner bot can resolve a working repo's closure bar locally (there is no "sprint owner" concept). `<SLUG>` is the uppercased project key.

| Variable | Source | Description |
|----------|--------|-------------|
| `PROJECT_TIER_<SLUG>` | `projects.<key>.validation.tier` | Validation/closure tier for the project (`auto` / `review` / `preview` / `human`) |
| `PROJECT_REPOS_<SLUG>` | `projects.<key>.repos` | Space-separated list of repos belonging to the project |

## Ecosystem

| Variable | Source | Description |
|----------|--------|-------------|
| `CLAUDNA_VERSION` | `bots.<name>.claudna_version` | clauDNA plugin version pin |
| `CLAUDRON_VAULT_PATH` | `bots.<name>.claudron_vault_path` | Claudron vault root; the bot's `claudron` CLI resolves the vault from it (Claudron `docs/CLI_CONTRACT.md` §Environment) |
| `CLAUDOSSEUM_TENANT_ID` | `bots.<name>.claudosseum_tenant_id` | Claudosseum telemetry tenant ID |
| `CLAUDRON_QUERY_BEFORE` | `bots.<name>.env` (manual opt-in) | `1` enables the dispatch query-before preflight: `dispatch-task.sh` prepends fleet-memory pointers (titles + paths from `claudron lookup`) to dispatched tasks. Off by default; needs the claudron CLI on PATH and `CLAUDRON_VAULT_PATH` set |
| `CLAUDRON_QUERY_LIMIT` | `bots.<name>.env` (manual opt-in) | Max fleet-memory pointers injected per dispatch (default 3) |

## Opt-in Feature Flags

A handful of shared `lib/` hooks and scripts compose into **every** bot on **every** fleet
(via `system.yaml` `defaults.hooks` or a shared `lib/` script), but ship **dormant by default** (the plane hooks are the exception since F18 R1: always on, `PLANE_EMIT_DISABLED=1` the one silencer) —
each is a no-op until the specific var below is set to `"1"` under the relevant bot's
`bots.<name>.env` (which lands in that bot's `bot.conf` and is inherited by hooks/scripts running
in its session). This is the equippable-dormant pattern: a shared install cannot be staged
per-bot, so rollout is gated per-fleet (or per-bot) instead of going live estate-wide on the next
`generate` or daily reload.

| Variable | Consumer | Description |
|----------|----------|--------------|
| `SESSION_DIGEST_ENABLED` | `lib/transcript-digest.sh` (SessionEnd hook) | `"1"` arms per-session Haiku transcript digesting for this bot. Default `0` (dormant) |
| `PLANE_EMIT_ENABLED` | `claudlobby generate` (`registry_emit.py`) | `"1"` in the fleet-tier `.env` arms the generate-time registry keyframe scan. Not a runtime door gate — every door is always on since F18 R1 |
| `PLANE_EMIT_DISABLED` | `lib/plane-emit.sh`, every hook | `"1"` silences every plane door — the harness/test exemption, the one silencer. Opposite polarity from the other flags on this list |
| `SPINDOWN_RECEIPT_ENABLED` | `lib/spin-down-bot.sh` | `"1"` arms the `bot_teardown_started` receipt on teardown (on the plane, anchored on this bot's fleet). Default `0` (dormant) |

## Plugins

| Variable | Source | Description |
|----------|--------|-------------|
| `CLAUDE_CODE_SYNC_PLUGIN_INSTALL` | `fleet.plugins` | Semicolon-separated plugin install commands |
| `FLEET_PLUGINS_REQUIRED` | `fleet.plugins.additional` | Space-separated required plugins (`name@marketplace`) |
| `FLEET_PLUGINS_MARKETPLACES` | `fleet.plugins.marketplaces` | Space-separated `name=type:repo` pairs (e.g. `mymarket=github:MyOrg/my-plugins`) |

## Git

| Variable | Source | Description |
|----------|--------|-------------|
| `GIT_CONFIG_GLOBAL` | `git_credentials` or `github_app` (fleet or bot) | Path to the composed `<bot_dir>/.gitconfig` that routes git credentials per GitHub org and/or through the GitHub App helper. Emitted when the bot declares either surface. The composed file `include`s the operator's `~/.gitconfig` first; App mode with `slug`+`bot_user_id` overrides the commit identity AFTER the include. See [`fleet-yaml-schema.md`](fleet-yaml-schema.md#fleetdefaultsgit_credentials--botsnamegit_credentials) |
| `GITHUB_APP_ID` / `GITHUB_APP_INSTALLATION_ID` / `GITHUB_APP_PRIVATE_KEY_PATH` | `github_app` or `mcp: [github-app]` | The three App-auth inputs (fleet tier; values from `lib/setup-github-app.sh`). Consumed at USE time by `lib/git-credential-github-app` — never resolved into the boot env (F9). Note the composed `cache --timeout=3000` layer: git `approve` re-stores a token with a fresh TTL on every successful auth, so a cached token can outlive the ~1h `ghs_` lifetime under continuous pushing — self-healing (the next 401 erases and re-mints) at the cost of one failed round trip (D5) |

Two operational notes:

**Adding `git_credentials` needs a restart, not a reload.** `bot.conf` is sourced once when the
pane is created, so a live session keeps whatever `GIT_CONFIG_GLOBAL` it started with (usually
none). `lib/reload-fleet.sh` deliberately does not restart, so waiting for the daily reload leaves
a composed `.gitconfig` that nothing points at.

**A `403` here has two causes that look identical.** Either the routing is not active (no
`GIT_CONFIG_GLOBAL` in the session) or the token itself is invalid. To tell them apart, `POST
/git/refs` with an existing ref: a valid token returns `422 Reference already exists`, an invalid
one returns `403`.

Note the consequence of `GIT_CONFIG_GLOBAL`: inside a bot session, `git config --global` writes to
the **composed** file, not `~/.gitconfig` — and the composed file is regenerated, so such a write is
lost on the next `claudlobby generate`. Change credential routing in `fleet.yaml`, not with
`git config`.

## Bot-Specific Env

Any key-value pairs in `bots.<name>.env` are exported directly:

```yaml
bots:
  my-bot:
    env:
      MY_CUSTOM_VAR: "value"
      DATABASE_URL: "${NEON_CONNECTION_STRING}"
```

These are emitted as `export MY_CUSTOM_VAR="value"` in `bot.conf`.

### Bot-Specific Secret Files

`bots.<name>.secret_files` is the fleet-relative-path sibling of `env:` — it maps an env var name
to a secret file's path *inside the fleet overlay* (service-account keys, credential files) rather
than a literal value:

```yaml
bots:
  my-bot:
    secret_files:
      GOOGLE_SERVICE_ACCOUNT_JSON: secrets/my-bot-service-account.json
```

Composes into `bot.conf` as `export GOOGLE_SERVICE_ACCOUNT_JSON="$FLEET_ROOT/secrets/my-bot-service-account.json"`
— fleet-relative and anchored on `$FLEET_ROOT` so the path is derived rather than hand-typed
absolute; an absolute or `~`-relative value, or one containing `..`, is rejected at `generate`. See
[`fleet-yaml-schema.md`](fleet-yaml-schema.md#botsnamesecret_files).
