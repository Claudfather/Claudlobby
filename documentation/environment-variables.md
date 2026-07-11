# Environment Variables Reference

The compositor writes environment variables to each bot's `bot.conf`. These are sourced at startup by `lib/start-bot.sh` and available to all lib/ scripts, hooks, and skills.

## Bot Identity

| Variable | Source | Description |
|----------|--------|-------------|
| `BOT_ID` | `bots.<name>` key | Bot identifier (same as fleet.yaml key) |
| `BOT_NAME` | `bots.<name>` key | Bot name (same as BOT_ID) |
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

## Telegram

| Variable | Source | Description |
|----------|--------|-------------|
| `TELEGRAM_GROUP_CHAT_ID` | `bots.<name>.telegram.group_chat_id` | Telegram group chat ID for posting |
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
| `OBSERVABILITY_REAP_DAYS` | `bots.<name>.observability.reap_days` | Days to retain event files (default: 7) |
| `OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD` | `bots.<name>.observability.activity_stuck_threshold` | Seconds of inactivity before flagged stuck (default: 1800) |
| `OBSERVABILITY_DISPATCH_DEADLINE` | `bots.<name>.observability.dispatch_deadline` | Seconds after dispatch before flagged overdue (default: 1800) |
| `RC_READY_TIMEOUT_S` | env override (`start-bot.sh`) | Seconds to wait for the `remote-control is active` readiness string before logging TIMEOUT and emitting the `rc_timeout` event (default: 90). Not composed from fleet.yaml — a raw override for slow hosts and the test harness |

## Code-Audit Sweep

Emitted only into the `fleet.sweep.owner_bot`'s `bot.conf` (see `fleet.sweep` in the fleet.yaml schema reference) — the fleet-level nightly selector (`lib/code-audit-sweep.sh`) needs to resolve exactly one owner.

| Variable | Source | Description |
|----------|--------|-------------|
| `SWEEP_OWNER_BOT` | `fleet.sweep.owner_bot` | Bot ID whose session runs the audit (set only in this bot's own `bot.conf`) |
| `SWEEP_REPOS` | `fleet.sweep.repos` | Space-separated repo list to audit — falls back to the owner's `scope.repos` when `sweep.repos` is unset |
| `SWEEP_LABEL` | `fleet.sweep.label` | GitHub label used to track audit staleness (default: `auto-audit`) |
| `SWEEP_AUDIT_TYPES` | `fleet.sweep.audit_types` | Space-separated audit types rotated per run (default: `tech-debt`) |

## Ecosystem

| Variable | Source | Description |
|----------|--------|-------------|
| `CLAUDNA_VERSION` | `bots.<name>.claudna_version` | clauDNA plugin version pin |
| `CLAUDRON_VAULT_PATH` | `bots.<name>.claudron_vault_path` | Claudron vault path for scoped queries |
| `CLAUDOSSEUM_TENANT_ID` | `bots.<name>.claudosseum_tenant_id` | Claudosseum telemetry tenant ID |
| `CLAUDRON_QUERY_BEFORE` | `bots.<name>.env` (manual opt-in) | `1` enables the dispatch query-before preflight: `dispatch-task.sh` prepends fleet-memory pointers (titles + paths from `claudron lookup`) to dispatched tasks. Off by default; needs the claudron CLI on PATH and `CLAUDRON_VAULT_PATH` set |
| `CLAUDRON_QUERY_LIMIT` | `bots.<name>.env` (manual opt-in) | Max fleet-memory pointers injected per dispatch (default 3) |

## Plugins

| Variable | Source | Description |
|----------|--------|-------------|
| `CLAUDE_CODE_SYNC_PLUGIN_INSTALL` | `fleet.plugins` | Semicolon-separated plugin install commands |
| `FLEET_PLUGINS_REQUIRED` | `fleet.plugins.additional` | Space-separated required plugins (`name@marketplace`) |
| `FLEET_PLUGINS_MARKETPLACES` | `fleet.plugins.marketplaces` | Space-separated `name=type:repo` pairs (e.g. `mymarket=github:MyOrg/my-plugins`) |

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
