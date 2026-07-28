#!/bin/bash
# Shared bot start script — called by each bot's systemd service
# Usage: start-bot.sh /path/to/bot/dir
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

BOT_DIR="${1:?Usage: start-bot.sh /path/to/bot/dir}"
install_error_trap "$BOT_DIR"
load_bot_conf "$BOT_DIR"

# --- Boot-mass mitigation -----------------------------------------------------
# When the whole fleet is mass-restarted (e.g. reconcile-fleet --enroll, or
# systemd restarts triggered by keepalive), all bots race to spawn their channel
# plugins simultaneously. Observed failure mode: some bots silently fail to bring
# up their telegram plugin (no bot.pid ever written), so the bot is "alive" in
# tmux but never sees inbound channel messages.
#
# Serialize the first few seconds of each bot's boot with a fleet-wide mkdir
# lock so plugins come up one-at-a-time. mkdir is atomic on every Unix and
# requires no external tools (flock may not be available on all hosts).
# Safe on solo restarts (uncontended → instant).
BOOT_LOCK_DIR="${TMPDIR:-/tmp}/.claudlobby-fleet-boot.lock"
BOOT_LOCK_HOLD_S="${BOOT_LOCK_HOLD_S:-8}"
_bl_waited=0
while ! mkdir "$BOOT_LOCK_DIR" 2>/dev/null; do
    # Stale lock cleanup: if lock dir is older than 60s, force-claim
    if [ -d "$BOOT_LOCK_DIR" ]; then
        _lock_age=$(($(date +%s) - $(stat -c %Y "$BOOT_LOCK_DIR" 2>/dev/null || stat -f %m "$BOOT_LOCK_DIR" 2>/dev/null || date +%s)))
        if [ "$_lock_age" -gt 60 ]; then
            rmdir "$BOOT_LOCK_DIR" 2>/dev/null || true
            continue
        fi
    fi
    sleep 1
    _bl_waited=$((_bl_waited + 1))
    if [ "$_bl_waited" -gt 120 ]; then
        echo "start-bot.sh: boot lock contended >120s, proceeding without it" >&2
        break
    fi
done
# Release the lock after BOOT_LOCK_HOLD_S so the next bot can proceed.
( sleep "$BOOT_LOCK_HOLD_S"; rmdir "$BOOT_LOCK_DIR" 2>/dev/null || true ) &
disown
# --- end boot-mass mitigation -------------------------------------------------

export PATH=/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin${_HOMEBREW:+:${_HOMEBREW}/bin}
export HOME="$HOME"

# 3-tier env sourcing: global -> fleet -> bot (later tiers override)
source_env_tiered

# Resolve the Telegram token from the env-var name declared in fleet.yaml.
# NEVER export an empty TELEGRAM_BOT_TOKEN: the telegram channel plugin server
# treats a defined-but-empty token as authoritative and skips reading its own
# channel-dir .env, silently killing the inbound bridge — the bot boots
# "Listening" but with a dark (non-delivering) poller. Export only a
# non-empty resolved value; unset any empty leftover so the server's channel-dir
# .env fallback still works.
if [ -n "${TELEGRAM_TOKEN_ENV_NAME:-}" ] && [ -n "${!TELEGRAM_TOKEN_ENV_NAME:-}" ]; then
    export TELEGRAM_BOT_TOKEN="${!TELEGRAM_TOKEN_ENV_NAME}"
fi
[ -z "${TELEGRAM_BOT_TOKEN:-}" ] && unset TELEGRAM_BOT_TOKEN

cd "$BOT_DIR"

# --- Headless consent pre-acceptance -----------------------------------------
# Claude Code prompts for TOS/permission-mode consent on first run. In headless
# (tmux-supervised) mode this blocks indefinitely — no human to click "accept".
# Pre-set the skip flags in ~/.claude/settings.json so the prompt never fires.
CLAUDE_SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
if command -v jq >/dev/null 2>&1; then
    _consent_ok=0
    [ -f "$CLAUDE_SETTINGS" ] && \
        jq -e '.skipAutoPermissionPrompt == true and .skipDangerousModePermissionPrompt == true' \
            "$CLAUDE_SETTINGS" >/dev/null 2>&1 && _consent_ok=1

    if [ "$_consent_ok" -eq 0 ]; then
        # Serialize with other bots — mass restart can race on this file.
        _do_consent_update() {
            mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
            _tmp=$(safe_mktemp)
            if [ -f "$CLAUDE_SETTINGS" ] && jq -e '.' "$CLAUDE_SETTINGS" >/dev/null 2>&1; then
                jq '.skipAutoPermissionPrompt = true | .skipDangerousModePermissionPrompt = true' \
                    "$CLAUDE_SETTINGS" > "$_tmp" && mv "$_tmp" "$CLAUDE_SETTINGS"
            else
                printf '{"skipAutoPermissionPrompt":true,"skipDangerousModePermissionPrompt":true}\n' > "$CLAUDE_SETTINGS"
            fi
            echo "$(ts_iso) CONSENT pre-accepted permission prompts in $CLAUDE_SETTINGS" >> "$BOT_DIR/logs/startup.log" 2>/dev/null || true
        }
        with_lock "${CLAUDE_SETTINGS}.lock" _do_consent_update
    fi
fi
# --- end headless consent ----------------------------------------------------

# --- Pre-trust MCP servers in this bot dev checkouts -------------------------
# Seed each projects/ checkout with this bot composed MCP allowlist so a `claude`
# session rooted there boots clean instead of stalling on the MCP-server-trust
# prompt (see seed_all_checkouts). Re-seeds each start to track allowlist
# changes. Idempotent, fail-closed; never blocks boot.
seed_all_checkouts "$BOT_DIR" || true
# --- end MCP checkout pre-trust ----------------------------------------------

# Tmux session name: use the bot directory slug (always lowercase) rather
# than BOT_NAME (display name, potentially mixed-case from fleet.yaml).
# Dispatch and monitoring scripts target sessions by this slug.
TMUX_SESSION="$(tmux_session_name "$BOT_DIR")"

# Per-bot tmux server socket: this bot runs on its OWN tmux server (its own
# -L <socket>), so one server's death drops only this bot, never the whole
# fleet. Resolved via the SSOT helper (falls back to BOT_SERVICE for an
# un-regenerated bot.conf); a misconfigured empty socket while FLEET_NAME is set
# fails loud rather than silently sharing the default server.
TMUX_SOCKET="$(tmux_socket_for_bot "$BOT_DIR")" || {
    echo "start-bot.sh: cannot resolve tmux socket for $BOT_DIR (check BOT_SERVICE in bot.conf)" >&2
    exit 1
}

# Kill any prior session — expected to fail on first boot or after clean shutdown
bot_tmux "$TMUX_SOCKET" kill-session -t "$TMUX_SESSION" 2>/dev/null || true

SESSION_NAME="$BOT_LABEL-$(date '+%Y%m%d-%H%M')"

# Build claude command. CLAUDE_FLAGS comes from bot.conf (composed by
# `claudlobby generate` from fleet.yaml). It contains all the per-bot
# CLI flags: --channels, --remote-control, --dangerously-skip-permissions,
# --model, --effort, plus any extras. We add only --name here since it
# uses a per-launch timestamp.
#
# IMPORTANT: tmux runs ONE SERVER PER USER. The first bot to start creates
# the server, and the server's environment becomes the inherited default
# for every subsequent session spawned in it. Anything not set explicitly
# leaks from whichever bot created the server — including, critically,
# tokens and identity vars.
#
# Bug pattern: workers can end up posting to Telegram under another
# bot's identity if TELEGRAM_BOT_TOKEN isn't set per-session — they
# inherit whatever TOKEN the tmux server was first started with.
#
# Fix: per-bot env vars are written to a chmod-600 env file and sourced
# by the tmux session's shell. This avoids unquoted tokens in command
# strings and process listings while ensuring per-session isolation.
BOT_ENV_FILE="$BOT_DIR/.tmux-env"
(umask 177; : > "$BOT_ENV_FILE")
chmod 600 "$BOT_ENV_FILE"

# 3-tier env sourcing inside the tmux session so Claude Code (and its MCP
# servers) inherit all env vars. Later tiers override earlier ones.
# These source commands run inside the tmux shell, NOT the parent start-bot.sh.
[ -f "$HOME/.env" ]                                && printf '. %q\n' "$HOME/.env" >> "$BOT_ENV_FILE"
if [ -n "${CLAUDLOBBY_ROOT:-}" ] && [ -f "$CLAUDLOBBY_ROOT/.env" ]; then
    printf '. %q\n' "$CLAUDLOBBY_ROOT/.env" >> "$BOT_ENV_FILE"
fi
if [ -n "${FLEET_NAME:-}" ] && [ -n "${CLAUDLOBBY_ROOT:-}" ]; then
    # Flat local/<fleet> byte-identically, or nested local/<system>/<fleet>.
    _sb_fleet_dir=$(resolve_fleet_dir "$FLEET_NAME") || _sb_fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET_NAME"
    local_fleet_env="$_sb_fleet_dir/.env"
    [ -f "$local_fleet_env" ] && printf '. %q\n' "$local_fleet_env" >> "$BOT_ENV_FILE"
fi
[ -f "$BOT_DIR/.env" ]                             && printf '. %q\n' "$BOT_DIR/.env" >> "$BOT_ENV_FILE"

# Source bot.conf — the SSOT for all compositor-generated config.
# set -a auto-exports every assignment so vars reach `exec claude`.
# This replaces the old per-var cherry-pick block — every var in bot.conf
# automatically propagates to the tmux session.
printf 'set -a\n' >> "$BOT_ENV_FILE"
printf '. %q\n' "$BOT_DIR/bot.conf" >> "$BOT_ENV_FILE"
printf 'set +a\n' >> "$BOT_ENV_FILE"

# Resolve the Telegram token via the indirection var set in bot.conf. .env tiers
# provide the actual secret; TELEGRAM_TOKEN_ENV_NAME names it. Two correctness
# constraints, both required for cross-shell (bash on Linux, zsh on macOS) correctness:
#   1. This block is sourced by the tmux session shell, which is zsh by default on
#      macOS — where bash ${!name} indirection is unavailable. Resolve portably
#      via eval so the same code works under bash (Linux) and zsh (macOS).
#   2. NEVER leave an empty TELEGRAM_BOT_TOKEN in the exported env. The channel
#      plugin server treats a defined-but-empty token as authoritative and skips
#      reading its own channel-dir .env, silently killing the inbound bridge. So
#      export only a non-empty resolved value, then unset any empty leftover
#      (e.g. an unfilled scaffold stub) so the server's .env fallback still works.
cat >> "$BOT_ENV_FILE" <<'TGTOKEOF'
if [ -n "${TELEGRAM_TOKEN_ENV_NAME:-}" ]; then
  eval "_tg_tok=\${${TELEGRAM_TOKEN_ENV_NAME}:-}"
  [ -n "${_tg_tok:-}" ] && export TELEGRAM_BOT_TOKEN="${_tg_tok}"
  unset _tg_tok
fi
[ -z "${TELEGRAM_BOT_TOKEN:-}" ] && unset TELEGRAM_BOT_TOKEN
TGTOKEOF

# The launched binary is overridable via CLAUDE_BIN so the validation harness
# (validate-bot-change.sh) can inject a stub for a hermetic, auth-free start;
# production leaves it unset and `claude` resolves on PATH inside the session.
# The plugin-management calls below honor the same override: the PATH rebuild
# above discards any harness-prepended stub dir, so this seam is the only way
# to drive that block hermetically (without it, tests can only skip the block
# via an empty FLEET_PLUGINS_REQUIRED).
# Sequence with ';' not '&&': the env file ends with a conditional Telegram-token
# export that returns nonzero when no token is configured. A '&&' would skip exec
# on that benign nonzero — the pane process exits, the tmux session dies, and a
# token-less bot lands in a keepalive restart loop. Masked when a prior tmux
# server already exported the token into the new pane; bites on a fresh server.
CLAUDE="${CLAUDE_BIN:-claude}"
# CLAUDE_FLAGS is composed into every bot.conf; the :- guard keeps a minimal or
# hand-written conf that omits it from aborting boot under `set -u`.
CLAUDE_CMD=". '$BOT_ENV_FILE'; exec $CLAUDE ${CLAUDE_FLAGS:-} --name \"$SESSION_NAME\""

# Update third-party plugins before launch. Handles cold start (fresh
# host with no plugins installed) through full lifecycle: register
# marketplace → install plugin → update plugin. FLEET_PLUGINS_REQUIRED
# and FLEET_PLUGINS_MARKETPLACES come from bot.conf (composed from
# fleet.yaml plugins config). The shared cache at ~/.claude/plugins/cache/
# means only the first bot to start actually fetches; others get a no-op.
# Failures never block bot startup (a network blip must not down the
# fleet) — but marketplace registration is verified against the registry
# and surfaced as a fleet event when it did not land, never swallowed.
if command -v "$CLAUDE" >/dev/null 2>&1 && [ -n "${FLEET_PLUGINS_REQUIRED:-}" ]; then
    LOG="$BOT_DIR/logs/startup.log"
    setup_log_dir "$LOG"

    # Step 1: Register marketplaces if not already known. The add is
    # positional — `claude plugin marketplace add <owner>/<repo>` — and the
    # CLI takes the marketplace NAME from the repo marketplace.json, so the
    # name declared in fleet.yaml must match it: the known-check keys on the
    # declared name, and plugin pins (plugin@name) resolve through it.
    _mp_known() { grep -q "\"$1\"" "$HOME/.claude/plugins/known_marketplaces.json" 2>/dev/null; }
    if [ -n "${FLEET_PLUGINS_MARKETPLACES:-}" ]; then
        for _mp_pair in $FLEET_PLUGINS_MARKETPLACES; do
            _mp_name="${_mp_pair%%=*}"
            _mp_source="${_mp_pair#*=}"
            _mp_repo="${_mp_source#*:}"
            if ! _mp_known "$_mp_name"; then
                echo "$(ts_iso) PLUGIN registering marketplace $_mp_name ($_mp_repo)" >> "$LOG"
                with_timeout 60 "$CLAUDE" plugin marketplace add "$_mp_repo" >> "$LOG" 2>&1 \
                    || echo "$(ts_iso) PLUGIN marketplace add $_mp_repo exited nonzero" >> "$LOG"
                # 60s bounds a cold clone (known marketplaces never
                # reach the add). Verify against the registry, not the exit
                # code: an unregistered marketplace means every plugin pinned
                # to it resolves to whatever matching plugin is already
                # installed — the bot comes up healthy-looking on the wrong plugin.
                if _mp_known "$_mp_name"; then
                    echo "$(ts_iso) PLUGIN marketplace $_mp_name registered" >> "$LOG"
                else
                    echo "$(ts_iso) PLUGIN ERROR marketplace $_mp_name ($_mp_repo) not registered — plugins pinned to $_mp_name will fall back to installed versions" >> "$LOG"
                    _mp_data=$(printf '{"marketplace":"%s","repo":"%s"}' \
                        "$(json_escape "$_mp_name")" "$(json_escape "$_mp_repo")")
                    emit_fleet_event "plugin_marketplace_failed" "startup" "$_mp_data"
                fi
            fi
        done
    fi

    # Step 2: Install or update each required plugin
    for _plugin in $FLEET_PLUGINS_REQUIRED; do
        if [ ! -f "$HOME/.claude/plugins/installed_plugins.json" ] || \
           ! grep -q "\"$_plugin\"" "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null; then
            echo "$(ts_iso) PLUGIN installing $_plugin (cold start)" >> "$LOG"
            with_timeout 30 "$CLAUDE" plugin install "$_plugin" >> "$LOG" 2>&1 || true
        else
            echo "$(ts_iso) PLUGIN updating $_plugin" >> "$LOG"
            with_timeout 30 "$CLAUDE" plugin update "$_plugin" >> "$LOG" 2>&1 || true
        fi
    done
fi

bot_tmux "$TMUX_SOCKET" new-session -d -s "$TMUX_SESSION" "$CLAUDE_CMD"

# Spawn marker — its mtime is this bot's last session (re)start. fleet-pulse
# reads it to grace the Telegram bridge poller while it spins up, so a (re)start
# — including a fleet-wide restart — doesn't trip a spurious bridge_down alert.
mkdir -p "$BOT_DIR/data" 2>/dev/null || true
touch "$BOT_DIR/data/.spawn" 2>/dev/null || true

# Wait for initialization with observability. The readiness ceiling is
# RC_READY_TIMEOUT_S (default 90s, polled every 0.5s) — overridable; see
# documentation/environment-variables.md.
LOG="$BOT_DIR/logs/startup.log"
setup_log_dir "$LOG"
_rc_timeout_s="${RC_READY_TIMEOUT_S:-90}"
# Coerce a non-numeric/empty override to the default: a bad value would crash
# start-bot under `set -u` ($(( abc * 2 )) → "abc: unbound variable"), crash-
# looping the bot. Fail safe to 90s.
case "$_rc_timeout_s" in ""|*[!0-9]*) _rc_timeout_s=90 ;; esac
_rc_iters=$(( _rc_timeout_s * 2 ))
_poll_start=$(date +%s)
echo "$(ts_iso) POLL_START — waiting for Telegram poller readiness (bridge_state, ceiling ${_rc_timeout_s}s)" >> "$LOG"
_ready=0
# Resolve the token ONCE here, not inside every bridge_state poll below: the token
# is static .env config, so re-sourcing the .env chain each 0.5s iteration (up to
# ~180x on a cold start) is wasted I/O — costly when bots start in parallel on a
# few cores. Threaded into bridge_state so the loop only re-checks the changing
# part (the live poller pid) (#756).
_pretoken="$(resolve_bot_telegram_token "$BOT_DIR" 2>/dev/null || true)"
for _i in $(seq 1 "$_rc_iters"); do
    if ! check_tmux_session "$TMUX_SESSION" "$TMUX_SOCKET"; then
        echo "$(ts_iso) CRASH — tmux session died during startup (after ${_i}s)" >> "$LOG"
        exit 1
    fi
    # Assert ground truth, not a bring-up pane string: a grepped readiness line
    # drifts across claude builds and silently stops matching, timing out on every
    # healthy start and firing a false rc_timeout FLEET ALERT (#751). The Telegram
    # poller writing a live bot.pid — proven by bridge_state == up — confirms Claude
    # Code initialized far enough to spawn its MCP plugin, and is immune to string
    # drift (the #710/#741 bridge-truth family). Bots with no channel (no_handle) or
    # a declared tokenless canary have no poller to await: ready at once, no alert.
    case "$(bridge_state "$BOT_DIR" "$_pretoken" 2>/dev/null || true)" in
        up)
            _elapsed=$(( $(date +%s) - _poll_start ))
            echo "$(ts_iso) READY — Telegram poller up after ${_elapsed}s" >> "$LOG"
            _ready=1
            break
            ;;
        no_handle)
            echo "$(ts_iso) READY — non-channel bot, no poller to await" >> "$LOG"
            _ready=1
            break
            ;;
        no_token)
            if bot_expects_no_token "$BOT_DIR"; then
                echo "$(ts_iso) READY — declared tokenless canary, no poller to await" >> "$LOG"
                _ready=1
                break
            fi
            ;;
    esac
    sleep 0.5
done
if [ "$_ready" -eq 0 ]; then
    echo "$(ts_iso) TIMEOUT — ${_rc_timeout_s}s elapsed, Telegram poller never reached bridge_state=up, proceeding anyway" >> "$LOG"
    # Emit a fleet event so a genuine readiness regression reaches fleet-pulse's
    # escalation instead of just appending to a log. Now gated on bridge ground
    # truth, so this fires only when the poller really never came up — a true
    # positive worth paging, not the #751 string-drift false alarm. A fleet-wide
    # TIMEOUT must page: the #533 outage sat in every startup.log for a week with
    # nothing alerting.
    emit_fleet_event "rc_timeout" "startup" "{\"timeout_s\":${_rc_timeout_s}}"
fi

# No sleep here: the bridge-readiness wait loop above already confirms Claude Code
# is fully initialized (MCP servers connected, channel plugin active). A fixed
# sleep after that point is wasted CPU time — especially costly when 8 bots
# start in parallel on a 4-core machine. See documentation/runbooks/audit-cold-start-timing.md.

# Resume prior context (lossless restart) before STARTUP_PROMPT. Every start —
# intentional, crash, or weekly bounce — injects /claudna:session resume --auto
# as a first keystroke so the bot picks up its last handoff. Age-gated: resume
# only from a checkpoint fresher than RESUME_MAX_AGE_S; a stale one (e.g. a bot
# that crashed days ago) clean-starts rather than replaying dead state. Sent
# BARE — no 'set +H;' prefix, unlike the STARTUP_PROMPT prose send below (see
# pane_send_verified for why a slash command must reach the pane untouched).
# Sent first so it settles before STARTUP_PROMPT and the two never collide.
_SESSION_MD="$BOT_DIR/.claude/session.md"
_RESUME_MAX_AGE_S="${RESUME_MAX_AGE_S:-86400}"
if should_resume_session "$_SESSION_MD" "$_RESUME_MAX_AGE_S"; then
    echo "$(ts_iso) RESUME — injecting /claudna:session resume --auto (fresh checkpoint)" >> "$LOG"
    pane_send_verified "$TMUX_SOCKET" "$TMUX_SESSION" '/claudna:session resume --auto'
elif [ -f "$_SESSION_MD" ]; then
    echo "$(ts_iso) RESUME SKIP — checkpoint older than ${_RESUME_MAX_AGE_S}s, clean-starting" >> "$LOG"
fi

if [ -n "${STARTUP_PROMPT:-}" ]; then
    # Prose, so it keeps the 'set +H; ' history-expansion guard the resume send
    # above must omit. The TUI's input buffer can race on cold start, leaving the
    # prompt typed but unsubmitted; pane_send_verified owns the catch.
    pane_send_verified "$TMUX_SOCKET" "$TMUX_SESSION" "set +H; $STARTUP_PROMPT"
fi

# Mark bot as idle in fleet-state — non-fatal if helper is missing or fails
[ -x "$LIB_DIR/fleet-state-update.sh" ] && "$LIB_DIR/fleet-state-update.sh" "$BOT_NAME" "idle" || true

# Verify the Telegram inbound bridge actually spawned (Phase 3b/3c). The readiness
# wait above (poll 1) already blocked up to RC_READY_TIMEOUT_S on the SAME
# bridge_state==up predicate, so this verify does NOT re-wait — it runs a single
# check (timeout 0) to produce the verdict, drop/clear the durable .bridge-down
# marker, and escalate tmux-first. Its bridge_down alert is DISTINCT from poll 1's
# rc_timeout, not redundant (#756): different detection paths driving different
# responses — rc_timeout burst-pages via fleet-pulse (#533), bridge_down feeds
# keepalive's heal ladder. Both are intentional. Done AFTER the bot is dispatch-
# ready (resumed + prompted + marked idle) so it never delays the bridge-
# INDEPENDENT tmux-dispatch path. start-bot does NOT bounce — keepalive owns the
# heal ladder (Fork F1=b). No second wait-knob here: a slow host raises poll 1's
# RC_READY_TIMEOUT_S (which owns the wait); this verify is a single check (0) by
# construction, never a redundant re-wait past an already-declared timeout.
_bridge_verdict="$(bridge_bringup_verify "$BOT_DIR" "$(dirname "$BOT_DIR")" 0)"
case "$_bridge_verdict" in
    ready)     echo "$(ts_iso) BRIDGE_READY — Telegram poller up" >> "$LOG" ;;
    expected:no_token) echo "$(ts_iso) BRIDGE_SKIP — no token by design (EXPECT_NO_TOKEN); canary/throwaway, no alert" >> "$LOG" ;;
    missing:*) echo "$(ts_iso) BRIDGE_MISSING ${_bridge_verdict#missing:} — escalated tmux-first; keepalive owns heal" >> "$LOG" ;;
    unknown)   echo "$(ts_iso) BRIDGE_UNKNOWN — ownership unprovable; not actionable" >> "$LOG" ;;
    no_handle) : ;; # not a channel bot — nothing to verify
esac

echo "$BOT_LABEL started"
