#!/bin/bash
# lib-common.sh — shared helpers for claudlobby lib/ scripts.
#
# Source this at the top of any lib/ script:
#   LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$LIB_DIR/lib-common.sh"
#
# Provides:
#   detect_os          — sets _OS (Linux|Darwin), _HOMEBREW (macOS prefix)
#   load_bot_conf      — source bot.conf with validation
#   source_env_tiered  — 3-tier env loading: global -> fleet -> bot
#   parse_env_file     — restricted .env parser ([export ]KEY=VALUE only)
#   with_timeout       — run a command under timeout(1) if available, else bare
#   with_lock          — portable mutex (flock if available, else mkdir spinlock)
#   setup_log_dir      — mkdir -p for log file's parent directory
#   safe_mktemp        — mktemp with automatic EXIT cleanup
#   tmux_session_name  — derive tmux session name from bot directory
#   check_tmux_session — tmux has-session wrapper (returns 0/1)
#   bridge_state       — classify a bot's Telegram bridge (up/no_bridge/no_token/...)
#   show_help          — extract comment header from calling script
#   ts_iso             — portable ISO 8601 timestamp
#   date_relative      — portable relative date arithmetic
#   stat_mtime         — portable file mtime (epoch seconds)
#   sed_i              — portable in-place sed
#   df_pcent           — portable disk usage percentage
#   json_escape        — escape backslash + double-quote for JSON values
#   debounce_notify    — fire-once notification with file-based marker
#   debounce_clear     — clear a debounce marker for re-firing
#   resolve_bots_dir   — fleet-aware runtime/bots path resolution
#
# Variables set on source:
#   CLAUDLOBBY_ROOT — repo root (auto-detected from this file's location)
#   _OS          — "Linux" or "Darwin"
#   _HOMEBREW    — Homebrew prefix (macOS only; empty on Linux)
#   _TMUX_BIN    — resolved path to tmux binary
#   _TIMEOUT_BIN — resolved path to timeout/gtimeout (empty if neither exists)
#   _FLOCK_BIN   — resolved path to flock (empty on stock macOS)

set -euo pipefail

# Guard against double-sourcing
[ -n "${_LIB_COMMON_LOADED:-}" ] && return 0
_LIB_COMMON_LOADED=1

# --- Repo root resolution ----------------------------------------------------
# Derive CLAUDLOBBY_ROOT from this file's location ($CLAUDLOBBY_ROOT/lib/lib-common.sh)
# unless already set by the environment or the calling script.
: "${CLAUDLOBBY_ROOT:=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export CLAUDLOBBY_ROOT

# --- OS detection -----------------------------------------------------------

_OS=""
_HOMEBREW=""

detect_os() {
    _OS="$(uname)"
    if [ "$_OS" = "Darwin" ]; then
        # arm64 (Apple Silicon) vs x86_64 (Intel)
        if [ -d /opt/homebrew ]; then
            _HOMEBREW="/opt/homebrew"
        else
            _HOMEBREW="/usr/local"
        fi
    fi
}

# Auto-detect on source
detect_os

# --- tmux binary resolution -------------------------------------------------

_TMUX_BIN="${TMUX_BIN:-}"
if [ -z "$_TMUX_BIN" ]; then
    _TMUX_BIN="$(command -v tmux 2>/dev/null)" || true
    if [ -z "$_TMUX_BIN" ]; then
        # Cron environments have minimal PATH; check common locations
        for _p in /usr/bin/tmux /usr/local/bin/tmux /opt/homebrew/bin/tmux; do
            [ -x "$_p" ] && { _TMUX_BIN="$_p"; break; }
        done
    fi
fi
: "${_TMUX_BIN:=/usr/bin/tmux}"
unset _p

# --- Portable external tools ------------------------------------------------
# timeout(1) and flock(1) are GNU/util-linux — absent on a stock macOS host.
# Resolve GNU variants if present (Homebrew installs them with a `g` prefix).

_TIMEOUT_BIN="$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true)"
_FLOCK_BIN="$(command -v flock 2>/dev/null || true)"

# with_timeout <seconds> <command> [args...]
# Runs the command under a timeout if a timeout binary exists; otherwise runs
# it unguarded (better to risk a hang than to fail outright on macOS).
with_timeout() {
    local secs="${1:?Usage: with_timeout <seconds> <command...>}"; shift
    if [ -n "$_TIMEOUT_BIN" ]; then
        "$_TIMEOUT_BIN" "$secs" "$@"
    else
        "$@"
    fi
}

# with_lock <lockfile> <command> [args...]
# Portable mutex: uses flock if available, else an atomic mkdir-based spinlock
# (mkdir is atomic on every POSIX filesystem). Spins up to ~5s then proceeds
# best-effort. Suitable for the small jq+mv critical sections in this repo.
with_lock() {
    local lockfile="${1:?Usage: with_lock <lockfile> <command...>}"; shift
    if [ -n "$_FLOCK_BIN" ]; then
        ( "$_FLOCK_BIN" -x 200; "$@" ) 200>"$lockfile"
        return $?
    fi
    local lockdir="${lockfile}.d" i=0
    while ! mkdir "$lockdir" 2>/dev/null; do
        i=$((i + 1))
        [ "$i" -ge 100 ] && break
        sleep 0.05
    done
    local rc=0
    "$@" || rc=$?
    rmdir "$lockdir" 2>/dev/null || true
    return $rc
}

# --- Bot conf loading -------------------------------------------------------

load_bot_conf() {
    local bot_dir="${1:?Usage: load_bot_conf /path/to/bot/dir}"
    if [ ! -f "$bot_dir/bot.conf" ]; then
        echo "$(basename "$0"): $bot_dir/bot.conf not found" >&2
        return 1
    fi
    # shellcheck source=/dev/null
    . "$bot_dir/bot.conf"
}

# --- Restricted .env parser -------------------------------------------------

# Safely parse a .env file without executing arbitrary shell code.
# Accepts `[export ]KEY=VALUE` lines; rejects lines with $(), backticks, pipes, semicolons.
parse_env_file() {
    local file="${1:?Usage: parse_env_file /path/to/.env}"
    [ -f "$file" ] || return 0
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip comments and blank lines
        case "$line" in
            ''|\#*) continue ;;
        esac
        # Tolerate a leading `export ` (with any leading whitespace) — many
        # hand-written .env files use it. The value is still treated as data.
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in
            export[[:space:]]*) line="${line#export}"; line="${line#"${line%%[![:space:]]*}"}" ;;
        esac
        # Only accept KEY=VALUE where KEY is a valid shell identifier
        if ! printf '%s' "$line" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*='; then
            echo "parse_env_file: skipping invalid line in $file: ${line:0:40}" >&2
            continue
        fi
        # Reject lines with command substitution, backticks, pipes, semicolons
        if printf '%s' "$line" | grep -qE '(\$\(|`|\||\;)'; then
            echo "parse_env_file: rejecting dangerous line in $file: ${line:0:40}" >&2
            continue
        fi
        key="${line%%=*}"
        value="${line#*=}"
        # Strip optional surrounding quotes (single or double)
        case "$value" in
            \"*\") value="${value#\"}"; value="${value%\"}" ;;
            \'*\') value="${value#\'}"; value="${value%\'}" ;;
        esac
        export "$key=$value"
    done < "$file"
}

# --- 3-tier env sourcing ----------------------------------------------------

# Requires load_bot_conf to have been called first (CLAUDLOBBY_ROOT, FLEET_NAME, BOT_DIR must be set).
source_env_tiered() {
    # Global
    [ -f "$HOME/.env" ] && parse_env_file "$HOME/.env"
    # Backward-compat: source legacy location with deprecation warning
    if [ -n "${CLAUDLOBBY_ROOT:-}" ] && [ -f "$CLAUDLOBBY_ROOT/.env" ]; then
        echo "DEPRECATED: $CLAUDLOBBY_ROOT/.env detected — move secrets to ~/.env or local/<fleet>/.env" >&2
        parse_env_file "$CLAUDLOBBY_ROOT/.env"
    fi
    # Fleet
    if [ -n "${FLEET_NAME:-}" ] && [ -n "${CLAUDLOBBY_ROOT:-}" ]; then
        local fleet_env="$CLAUDLOBBY_ROOT/local/$FLEET_NAME/.env"
        [ -f "$fleet_env" ] && parse_env_file "$fleet_env"
    fi
    # Bot
    if [ -n "${BOT_DIR:-}" ] && [ -f "$BOT_DIR/.env" ]; then
        parse_env_file "$BOT_DIR/.env"
    fi
}

# --- Log directory -----------------------------------------------------------

setup_log_dir() {
    local log_path="${1:?Usage: setup_log_dir /path/to/logfile}"
    mkdir -p "$(dirname "$log_path")"
}

# --- Safe temp files ---------------------------------------------------------
# The temp directory is created eagerly at source time so safe_mktemp works
# correctly inside $(...) command substitution (which runs in a subshell --
# lazy init would set _LC_TMPDIR only in the subshell, orphaning files).
# Cost is negligible: one mktemp -d + rm -rf on tmpfs per script invocation.

_LC_TMPDIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'lib-common')

_lc_cleanup() {
    rm -rf "$_LC_TMPDIR" 2>/dev/null || true
}

# Source lib-common.sh before setting your own EXIT trap — this overwrites
# any existing trap.  If your script needs its own EXIT handler, set it
# after sourcing and call _lc_cleanup explicitly.
trap '_lc_cleanup' EXIT

safe_mktemp() {
    mktemp "$_LC_TMPDIR/tmp.XXXXXXXXXX"
}

# --- JSON helpers ------------------------------------------------------------

# json_escape <string>
# Escape backslashes and double quotes for safe embedding in JSON values.
# Prints the escaped string to stdout.
json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# --- tmux helpers ------------------------------------------------------------

# Strip control chars and escape sequences dangerous in tmux send-keys.
sanitize_tmux_input() {
    local input="$1"
    input=$(printf '%s' "$input" | tr -d '\000-\037\177')
    input=$(printf '%s' "$input" | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g')
    printf '%s' "$input"
}

# Derive tmux session name from bot directory basename (always lowercase).
# BOT_NAME can be mixed-case (display name from fleet.yaml); the directory
# slug is the stable lowercase identifier that dispatch scripts expect.
tmux_session_name() {
    basename "${1:?Usage: tmux_session_name /path/to/bot/dir}"
}

check_tmux_session() {
    local session="${1:?Usage: check_tmux_session <name> [socket]}"
    local socket="${2:-}"
    if [ -n "$socket" ]; then
        bot_tmux "$socket" has-session -t "$session" 2>/dev/null
    else
        "$_TMUX_BIN" has-session -t "$session" 2>/dev/null
    fi
}

# --- Telegram channel bridge state ------------------------------------------

# bridge_state <bot_dir>
# Classify a bot's Telegram inbound bridge — the `bun server.ts` MCP child its
# `claude` spawns from the `--channels` flag. Prints exactly one state; returns
# 0 only for `up`:
#   up        a live `bun server.ts` poller OWNED by this bot (exact, NUL-delimited
#             TELEGRAM_STATE_DIR env match) whose parent is a live `claude`.
#   no_bridge handle + token configured, but no such live owned bridge.
#   no_token  handle configured but the resolved token is empty (the plugin exits
#             before writing bot.pid; a bounce cannot help — escalate, don't heal).
#   no_handle the bot has no Telegram handle (not a channel bot; callers skip).
#   unknown   a candidate poller exists but ownership can't be verified (its
#             /proc/<pid>/environ is unreadable, or this is not Linux). Callers
#             MUST NOT heal on `unknown`.
# The token VALUE is not in bot.conf — only its var NAME (TELEGRAM_TOKEN_ENV_NAME)
# is; the value lives in the .env chain, which keepalive/fleet-pulse never source.
# Resolve it via source_env_tiered IN A SUBSHELL so the caller's env is untouched.
bridge_state() {
    local bot_dir="${1:?Usage: bridge_state /path/to/bot/dir}"
    local handle state_dir token pidfile pid comm ppid pcomm environ args psline _anc _hop

    handle="$(bot_conf_get "$bot_dir" TELEGRAM_BOT_HANDLE "")" || true
    if [ -z "$handle" ]; then printf '%s' "no_handle"; return 1; fi

    # Resolve the token the way start-bot.sh does, but in a subshell: bot.conf
    # names the var (TELEGRAM_TOKEN_ENV_NAME); the value is in the .env tiers.
    token="$(
        load_bot_conf "$bot_dir" >/dev/null 2>&1 || true
        # shellcheck disable=SC2030  # subshell-local by design: never touch the caller's env
        BOT_DIR="$bot_dir"
        source_env_tiered 2>/dev/null || true
        _te="${TELEGRAM_TOKEN_ENV_NAME:-}"
        if [ -n "$_te" ]; then printf '%s' "${!_te:-}"; fi
    )" || true
    if [ -z "$token" ]; then printf '%s' "no_token"; return 1; fi

    state_dir="$(bot_conf_get "$bot_dir" TELEGRAM_STATE_DIR "")" || true
    # shellcheck disable=SC2016  # literal "$HOME" is intended: bot.conf stores it unexpanded
    case "$state_dir" in
        '$HOME'/*) state_dir="$HOME/${state_dir#\$HOME/}" ;;
    esac
    pidfile="$state_dir/bot.pid"
    if [ -z "$state_dir" ] || [ ! -f "$pidfile" ]; then printf '%s' "no_bridge"; return 1; fi

    pid="$(tr -cd '0-9' < "$pidfile" 2>/dev/null)" || true
    if [ -z "$pid" ]; then printf '%s' "no_bridge"; return 1; fi

    # One ps for the target pid (-ww: no arg truncation). A non-empty line proves
    # the pid is alive and yields comm + ppid + full args in a single call.
    psline="$(ps -ww -o comm=,ppid=,args= -p "$pid" 2>/dev/null)" || true
    if [ -z "$psline" ]; then printf '%s' "no_bridge"; return 1; fi
    read -r comm ppid args <<<"$psline"

    # Footgun guard: a real bridge is a `bun` process running server.ts — never a
    # shell that merely has "server.ts" on its command line (kills phantom counts).
    case "$comm" in
        bun | */bun) ;;
        *) printf '%s' "no_bridge"; return 1 ;;
    esac
    case "$args" in
        *server.ts*) ;;
        *) printf '%s' "no_bridge"; return 1 ;;
    esac

    # Ownership: the poller's environ must hold EXACTLY this bot's
    # TELEGRAM_STATE_DIR as a NUL-delimited KEY=VALUE entry — not a substring
    # (telegram-data must not match telegram-data-eng). Unreadable environ
    # (EACCES / non-Linux) → unknown; never treat unprovable ownership as ours.
    environ="/proc/$pid/environ"
    if [ ! -r "$environ" ]; then printf '%s' "unknown"; return 1; fi
    if ! tr '\0' '\n' < "$environ" 2>/dev/null | grep -qxF "TELEGRAM_STATE_DIR=$state_dir"; then
        printf '%s' "no_bridge"; return 1
    fi

    # Lineage: a poller whose `claude` died reparents to the session subreaper
    # (systemd --user / init) and delivers nothing while still holding the
    # single-consumer token slot — a deaf orphan that must NOT read `up`. Require
    # a live `claude` ANCESTOR. The telegram plugin's MCP command is `bun … start`,
    # so the real tree is  claude → bun (`bun … start`) → bun server.ts  — `claude`
    # is the GRANDPARENT, reached THROUGH bun spawn-shims, never the poller's direct
    # parent (a direct-parent-only check read `no_bridge` for every healthy bridge).
    # Walk up: bun/sh/bash are transparent spawn-shims (the known set between claude
    # and the poller — extend if the plugin runtime changes) so keep climbing; the
    # FIRST non-shim ancestor decides — `claude` → owned & live, anything else
    # (systemd, init, an unrelated proc) → no_bridge. Stopping at the first non-shim
    # is what stops us chasing an unrelated `claude` higher up. One ps per hop,
    # ppid-first so a spaced comm (`tmux: server`) can't corrupt the next pid;
    # bounded (real depth 2–3) so a malformed /proc can't spin.
    _anc="$ppid"
    pcomm=""
    for _hop in 1 2 3 4 5 6 7 8; do
        [ -n "$_anc" ] && [ "$_anc" -gt 1 ] || break
        psline="$(ps -o ppid=,comm= -p "$_anc" 2>/dev/null)" || true
        [ -n "$psline" ] || break
        read -r _anc pcomm <<<"$psline"                  # _anc advances to the parent
        case "$pcomm" in
            claude | */claude) break ;;                  # live claude ancestor → owned
            bun | */bun | sh | */sh | bash | */bash) ;;  # spawn shim → keep walking up
            *) pcomm=""; break ;;                         # first real ancestor isn't claude → orphan
        esac
    done
    case "$pcomm" in
        claude | */claude) ;;
        *) printf '%s' "no_bridge"; return 1 ;;
    esac

    printf '%s' "up"
    return 0
}

# --- Per-bot tmux socket isolation ------------------------------------------
# Each bot runs its own tmux server, reached via a private socket name (the
# `-L` argument), so one server's death can only drop one bot — not the whole
# fleet. The socket name is the third bot-identity axis, resolved from a single
# helper exactly as BOT_SERVICE (unit name) and the dir slug (session name) are.

# tmux_socket_for_bot <bot_dir>
# Resolve a bot's private tmux socket name from its identity (SSOT). The socket
# name is the bot's BOT_SERVICE — host-wide unique and fleet-prefixed — so every
# script that can see a bot's dir agrees on its socket. Prefers the explicit
# TMUX_SOCKET field; falls back to BOT_SERVICE for bots whose bot.conf predates
# the field (un-regenerated).
#
# Production guard: an empty result while FLEET_NAME is set is a
# misconfiguration that would collapse every such bot onto one bare socket and
# reintroduce the shared-server SPOF — so fail fast. The bare-id fallback is
# permitted ONLY for the test harness (FLEET_NAME unset), where bots carry no
# service prefix.
tmux_socket_for_bot() {
    local bot_dir="${1:?Usage: tmux_socket_for_bot <bot_dir>}"
    local sock
    sock=$(bot_conf_get "$bot_dir" TMUX_SOCKET "")
    [ -n "$sock" ] || sock=$(bot_conf_get "$bot_dir" BOT_SERVICE "")
    if [ -n "$sock" ]; then
        printf '%s' "$sock"
        return 0
    fi
    if [ -n "${FLEET_NAME:-}" ]; then
        echo "tmux_socket_for_bot: empty BOT_SERVICE for '$bot_dir' while FLEET_NAME is set — refusing a bare socket name that would collide across fleets and reintroduce the shared-server SPOF" >&2
        return 1
    fi
    # Test-harness fallback (no fleet prefix): unique-enough by dir basename.
    printf 'tmux-%s' "$(basename "$bot_dir")"
}

# _resolve_cross_fleet_bot_dir <session>
# Find a bot's directory by session name (= dir basename) across EVERY fleet
# under $CLAUDLOBBY_ROOT/local/*/runtime/bots. This is the cross-fleet fallback
# for tmux_socket_for_session: when a manager dispatches to a peer that lives in
# a sibling fleet (e.g. a top-level orchestrator → a worker in another fleet),
# the peer is absent from the caller's own bots_dir and only a fleet-wide search
# locates it. Echoes the resolved bot dir, or nothing if no fleet owns <session>.
#
# Collision (same bot name owned by >1 fleet): prefer the match whose private
# tmux server is LIVE on its socket — the running peer is the real dispatch
# target. If none are live, warn and pick deterministically (sorted) so the
# result is stable across calls rather than filesystem-glob-order dependent.
_resolve_cross_fleet_bot_dir() {
    local session="$1" d matches=()
    for d in "$CLAUDLOBBY_ROOT"/local/*/runtime/bots/"$session"; do
        [ -d "$d" ] && matches+=("$d")
    done
    case ${#matches[@]} in
        0) return 0 ;;
        1) printf '%s' "${matches[0]}"; return 0 ;;
    esac
    local m sock
    for m in "${matches[@]}"; do
        sock=$(tmux_socket_for_bot "$m" 2>/dev/null) || continue
        if [ -n "$sock" ] && bot_tmux "$sock" has-session -t "$session" 2>/dev/null; then
            printf '%s' "$m"
            return 0
        fi
    done
    local pick
    pick=$(printf '%s\n' "${matches[@]}" | sort | head -1)
    echo "_resolve_cross_fleet_bot_dir: session '$session' exists in ${#matches[@]} fleets; none have a live tmux server — picking '$pick' deterministically" >&2
    printf '%s' "$pick"
}

# tmux_socket_for_session <session> [bots_dir]
# Reverse-resolve a socket from a session name (= bot dir basename) for the
# cross-socket call sites that only know the peer's session name (dispatch.sh,
# bot-sweep-cron.sh, the report-back fallback). Resolution order:
#   1. Fast path — the peer in the caller's OWN fleet (sibling dir under
#      [bots_dir], default derived from $BOT_DIR). The overwhelmingly common
#      case; no cross-fleet scan when it hits.
#   2. Cross-fleet fallback — the peer lives in a sibling fleet (cross-fleet
#      dispatch); search every fleet's runtime/bots via _resolve_cross_fleet_bot_dir.
#   3. Unknown peer — preserve original behavior (test-harness socket synthesis
#      when FLEET_NAME is unset, or the production fail-fast on an empty socket).
tmux_socket_for_session() {
    # Empty session degrades to rc 2, never a fatal ${1:?} — the expansion
    # fault would kill the calling script from inside command substitution
    # under set -e (silently, when stderr is redirected), and every caller
    # already handles an empty-socket result.
    local session="${1:-}"
    if [ -z "$session" ]; then
        echo "tmux_socket_for_session: empty session name" >&2
        return 2
    fi
    local bots_dir="${2:-}"
    if [ -z "$bots_dir" ]; then
        # Prefer the caller's own sibling dir; otherwise fall back to the
        # fleet-aware runtime/bots resolution (CLAUDLOBBY_FLEET / FLEET_NAME).
        if [ -n "${BOT_DIR:-}" ]; then
            bots_dir=$(dirname "$BOT_DIR")
        else
            bots_dir=$(resolve_bots_dir)
        fi
    fi
    # Own-fleet fast path by default; if the peer isn't in the caller's fleet,
    # try the cross-fleet fallback; if that also misses, leave the own-fleet path
    # unchanged so the unknown peer hits the original behavior (harness socket
    # synthesis when FLEET_NAME is unset, else the production fail-fast).
    local target="$bots_dir/$session"
    if [ ! -d "$target" ]; then
        local peer_dir
        peer_dir=$(_resolve_cross_fleet_bot_dir "$session")
        [ -n "$peer_dir" ] && target="$peer_dir"
    fi
    tmux_socket_for_bot "$target"
}

# resolve_peer_socket <explicit_socket> <peer_session> [bots_dir]
# Resolve a peer bot's tmux socket for a cross-socket send: prefer an explicit
# value (the composed MANAGER_TMUX_SOCKET field, however the caller read it),
# else reverse-look it up from the peer's session name. The single home for the
# "explicit field, else reverse-lookup" precedence shared by report-back,
# sprint-trigger, fleet-pulse, evening-audit, and emit_failure_alert.
resolve_peer_socket() {
    local explicit="$1" session="$2" bots_dir="${3:-}"
    if [ -n "$explicit" ]; then
        printf '%s' "$explicit"
        return 0
    fi
    tmux_socket_for_session "$session" "$bots_dir" 2>/dev/null || true
}

# bot_tmux <socket> <tmux-args...>
# The single chokepoint for socket-targeted tmux calls: runs a subcommand
# against the per-bot server identified by <socket> (`tmux -L <socket> ...`).
#
# Unset-socket contract: an empty <socket> while FLEET_NAME is set is refused
# (never `tmux -L ""`, which would silently fall back to the shared default
# server and defeat isolation) — returns non-zero with a stderr error. When
# FLEET_NAME is unset (test harness / pre-fleet), an empty socket passes through
# to tmux's default socket for backward compatibility.
bot_tmux() {
    local socket="${1?Usage: bot_tmux <socket> <tmux-args...>}"; shift
    if [ -z "$socket" ]; then
        if [ -n "${FLEET_NAME:-}" ]; then
            echo "bot_tmux: empty socket while FLEET_NAME is set — refusing 'tmux -L \"\"' (would defeat per-bot isolation)" >&2
            return 2
        fi
        "$_TMUX_BIN" "$@"
        return $?
    fi
    "$_TMUX_BIN" -L "$socket" "$@"
}

# _tmux_send_miss <session> <socket> <reason>
# Emit a send_miss event to the CALLER bot's JSONL ledger (best-effort) plus a
# stderr breadcrumb, so a dropped cross-socket send becomes observable instead
# of silently swallowed. Internal to bot_tmux_send. Caller identity comes from
# $BOT_DIR / $BOT_ID (the sender); falls back to the fleet-level ledger.
_tmux_send_miss() {
    local session="$1" socket="$2" reason="$3"
    local bot_dir="${BOT_DIR:-}" bot_id="${BOT_ID:-}" events_dir
    if [ -n "$bot_dir" ] && [ -d "$bot_dir" ]; then
        events_dir="$bot_dir/data/events"
        [ -n "$bot_id" ] || bot_id=$(basename "$bot_dir")
    else
        events_dir="${CLAUDLOBBY_ROOT}/state/events"
        bot_id="${bot_id:-fleet}"
    fi
    mkdir -p "$events_dir" 2>/dev/null || return 0
    local ts today
    ts=$(ts_iso); today=$(date +%Y-%m-%d)
    printf '{"ts":"%s","bot":"%s","type":"send_miss","source":"dispatch","data":{"target":"%s","socket":"%s","session":"%s","caller":"%s","reason":"%s"}}\n' \
        "$ts" "$bot_id" "$(json_escape "$session")" "$(json_escape "$socket")" "$(json_escape "$session")" "$bot_id" "$reason" \
        >> "$events_dir/fleet-${today}.jsonl" 2>/dev/null || true
}

# bot_tmux_send <peer_socket> <session> <text>
# The ONE safe cross-socket send. Prechecks that <session> exists on
# <peer_socket>, then sends <text> followed by Enter as two race-safe steps
# (preserving sanitize_tmux_input). On a miss — empty socket, or session absent
# on that socket — it emits a send_miss event + stderr breadcrumb and returns
# non-zero, replacing the old silent `|| true` at every cross-socket call site.
# Residual TOCTOU (the session dying between precheck and send) surfaces as a
# non-zero send-keys exit, logged best-effort by the caller, never swallowed.
bot_tmux_send() {
    local peer_socket="${1?Usage: bot_tmux_send <peer_socket> <session> <text>}"
    local session="${2:?Usage: bot_tmux_send <peer_socket> <session> <text>}"
    local text="${3:?Usage: bot_tmux_send <peer_socket> <session> <text>}"

    if [ -z "$peer_socket" ]; then
        _tmux_send_miss "$session" "$peer_socket" "no-socket"
        echo "bot_tmux_send: no socket resolved for session '$session' — send dropped (logged)" >&2
        return 1
    fi
    if ! bot_tmux "$peer_socket" has-session -t "$session" 2>/dev/null; then
        _tmux_send_miss "$session" "$peer_socket" "no-session"
        echo "bot_tmux_send: session '$session' not found on socket '$peer_socket' — send dropped (logged)" >&2
        return 1
    fi
    local safe
    safe=$(sanitize_tmux_input "$text")
    bot_tmux "$peer_socket" send-keys -t "$session" "$safe"
    sleep 0.3
    bot_tmux "$peer_socket" send-keys -t "$session" Enter
}

# Base idle-detection regex — single source of truth for keepalive.sh
# classify_pane and fleet-pulse pane_is_idle. Operators extend at runtime
# via KEEPALIVE_IDLE_PATTERNS (appended by both consumers).
#
# Prompt glyph pattern: [>❯].{0,3}$ matches the glyph near end of line
# with up to 3 trailing chars.  Claude Code's TUI puts a non-breaking space
# (U+00A0) after ❯ which \s doesn't match — .{0,3}$ handles it plus any
# other minor decoration.  The short suffix cap avoids false positives on
# lines where > appears mid-content.
_IDLE_PATTERN_BASE='([>❯].{0,3}$|Remote Control act|Enter.*to close|Yes\/No|Allow|Deny|y\/n\b|\$\s*$)'

# pane_is_idle <pane_text>
# Returns 0 if the pane is at a prompt / waiting-for-input, 1 otherwise.
# Used by fleet-pulse to avoid flagging a finished, at-prompt bot as
# activity_stuck. Operators extend via KEEPALIVE_IDLE_PATTERNS.
pane_is_idle() {
    local text="$1"
    local _idle_pattern="$_IDLE_PATTERN_BASE"
    if [ -n "${KEEPALIVE_IDLE_PATTERNS:-}" ]; then
        _idle_pattern="$_idle_pattern|$KEEPALIVE_IDLE_PATTERNS"
    fi
    printf '%s' "$text" | grep -qE "$_idle_pattern"
}

# marker_is_newer <marker_a> <marker_b>
# Returns 0 if marker_a exists AND its mtime >= marker_b's mtime (or marker_b
# is missing). Returns 1 otherwise. Used by fleet-pulse to compare .idle vs
# .last-tool-call without parsing pane text.
marker_is_newer() {
    local a="$1" b="$2"
    [ -f "$a" ] || return 1
    [ -f "$b" ] || return 0
    local a_epoch b_epoch
    a_epoch=$(stat_mtime "$a" 2>/dev/null) || return 1
    b_epoch=$(stat_mtime "$b" 2>/dev/null) || return 0
    [ "$a_epoch" -ge "$b_epoch" ]
}

# marker_age_within <marker> <max_age_s>
# Returns 0 if <marker> exists AND (now - its mtime) <= max_age_s, else 1.
# Treats a recently-touched marker as a rendering-immune liveness signal: e.g.
# data/.last-tool-call (bot-vitals.sh touches it on every tool call) lets a
# supervisor tell a working bot from an idle one without parsing pane glyphs,
# which churn with Claude Code's verb/spinner rendering.
marker_age_within() {
    local marker="$1" max_age="$2"
    [ -f "$marker" ] || return 1
    local m_epoch now_epoch
    m_epoch=$(stat_mtime "$marker" 2>/dev/null) || return 1
    now_epoch=$(date +%s)
    [ "$(( now_epoch - m_epoch ))" -le "$max_age" ]
}

# --- Debounced notification ---------------------------------------------------

# debounce_notify <state_dir> <bot_id> <marker_suffix> <notify_fn> <message>
# Fires the notification only if the marker file does not exist (first
# occurrence). Caller is responsible for clearing the marker via
# debounce_clear when the condition resolves.
debounce_notify() {
    local state_dir="$1" bot_id="$2" suffix="$3" notify_fn="$4" message="$5"
    local marker="$state_dir/${bot_id}.${suffix}"
    if [ ! -f "$marker" ]; then
        "$notify_fn" "$message"
        touch "$marker"
    fi
}

# debounce_clear <state_dir> <bot_id> <marker_suffix>
# Remove the debounce marker so the next occurrence fires again.
debounce_clear() {
    rm -f "$1/${2}.${3}"
}

# --- Help display ------------------------------------------------------------

show_help() {
    # Extract comment block from the caller (line 2 to first non-# line).
    # Strips the leading "# " prefix from each line.
    local script="${1:-$0}"
    awk 'NR == 1 { next }
         /^[^#]/ { exit }
         { sub(/^# ?/, ""); print }' "$script"
}

# --- Portable timestamps -----------------------------------------------------

ts_iso() {
    # ISO 8601 with timezone, e.g. 2026-05-08T14:30:00-04:00
    date -Iseconds 2>/dev/null || date +%Y-%m-%dT%H:%M:%S%z
}

date_relative() {
    # Portable relative date: date_relative "-7 days" [format]
    # Accepts GNU-style offsets: "-7 days", "7 days ago", "+1 month"
    local offset="${1:?Usage: date_relative '<offset>' [format]}"
    local fmt="${2:-%Y-%m-%d}"
    if [ "$_OS" = "Darwin" ]; then
        local sign num unit bsd_unit
        # Normalize "N unit ago" -> "-N unit"
        if printf '%s' "$offset" | grep -qiE '\bago$'; then
            offset="-$(printf '%s' "$offset" | sed -E 's/[[:space:]]*ago$//i')"
        fi
        sign=$(printf '%s' "$offset" | grep -oE '^[+-]' || echo "+")
        num=$(printf '%s' "$offset" | grep -oE '[0-9]+')
        unit=$(printf '%s' "$offset" | grep -oE '[a-zA-Z]+$')
        case "$unit" in
            day|days)       bsd_unit="d" ;;
            month|months)   bsd_unit="m" ;;
            year|years)     bsd_unit="y" ;;
            hour|hours)     bsd_unit="H" ;;
            minute|minutes) bsd_unit="M" ;;
            second|seconds) bsd_unit="S" ;;
            week|weeks)     bsd_unit="w" ;;
            *) echo "date_relative: unknown unit '$unit'" >&2; return 1 ;;
        esac
        date -v"${sign}${num}${bsd_unit}" +"$fmt"
    else
        date -d "$offset" +"$fmt"
    fi
}

# --- Portable stat -----------------------------------------------------------

stat_mtime() {
    # Print file modification time as epoch seconds
    local file="${1:?Usage: stat_mtime <file>}"
    if [ "$_OS" = "Darwin" ]; then
        stat -f %m "$file"
    else
        stat -c %Y "$file"
    fi
}

iso_to_epoch() {
    # Convert an ISO-8601 UTC timestamp (e.g. 2026-05-15T14:30:00Z) to epoch
    # seconds. Portable across GNU date (Linux) and BSD date (Darwin). Prints
    # nothing and returns non-zero if the input is empty or unparseable.
    local iso="${1:-}"
    [ -n "$iso" ] || return 1
    if [ "$_OS" = "Darwin" ]; then
        date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$iso" +%s 2>/dev/null
    else
        date -u -d "$iso" +%s 2>/dev/null
    fi
}

session_md_handoff_epoch() {
    # Echo a session.md's handoff time as epoch seconds. Prefers the doc-level
    # `last_updated:` ISO-8601 UTC frontmatter field (written by /session-handoff
    # and robust to file touches the way mtime is not); falls back to the file
    # mtime for legacy artifacts that predate the field. Returns 1 if absent.
    local file="${1:?Usage: session_md_handoff_epoch <file>}" iso epoch
    [ -f "$file" ] || return 1
    iso=$(grep -m1 '^last_updated:' "$file" 2>/dev/null \
        | sed -E 's/^last_updated:[[:space:]]*//; s/[[:space:]]*$//')
    if [ -n "$iso" ]; then
        epoch=$(iso_to_epoch "$iso") && [ -n "$epoch" ] && { printf '%s' "$epoch"; return 0; }
    fi
    stat_mtime "$file" 2>/dev/null
}

should_resume_session() {
    # F6 age gate: decide whether a handoff checkpoint is fresh enough to resume
    # from. Returns 0 (resume) when the handoff timestamp is within
    # <max_age_seconds> of now; returns 1 (skip — clean start) when older, or
    # when the file is absent/unreadable. A future-dated checkpoint (clock skew)
    # counts as fresh.
    local file="${1:?Usage: should_resume_session <file> <max_age_seconds>}"
    local max_age="${2:?Usage: should_resume_session <file> <max_age_seconds>}"
    local epoch now age
    [ -f "$file" ] || return 1
    epoch=$(session_md_handoff_epoch "$file") || return 1
    [ -n "$epoch" ] || return 1
    now=$(date +%s)
    age=$(( now - epoch ))
    [ "$age" -lt "$max_age" ]
}

# --- Portable sed -i ---------------------------------------------------------

sed_i() {
    # Portable in-place sed. Usage: sed_i 's/foo/bar/g' file.txt
    if [ "$_OS" = "Darwin" ]; then
        sed -i "" "$@"
    else
        sed -i "$@"
    fi
}

# --- Portable df -------------------------------------------------------------

df_pcent() {
    # Print disk usage as a bare number (no %).
    local mount="${1:-/}"
    if df --output=pcent / >/dev/null 2>&1; then
        df --output=pcent "$mount" | tail -1 | tr -dc 0-9
    else
        df -P "$mount" | awk 'NR==2 {gsub("%",""); print $5}'
    fi
}

# --- Fleet path resolution ---------------------------------------------------

# shellcheck disable=SC2120  # fleet arg is optional by design (env fallback);
# tmux_socket_for_session calls it argless, other-file callers pass a fleet.
resolve_bots_dir() {
    # Resolve the runtime/bots directory for a fleet.
    # Usage: BOTS_DIR=$(resolve_bots_dir [fleet-name])
    # Falls back to CLAUDLOBBY_FLEET / FLEET_NAME env vars, then root-mode runtime/bots.
    local fleet="${1:-${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}}"
    if [ -n "$fleet" ]; then
        printf '%s' "$CLAUDLOBBY_ROOT/local/$fleet/runtime/bots"
    else
        printf '%s' "$CLAUDLOBBY_ROOT/runtime/bots"
    fi
}

# host_bots_dirs
# Emit every bots dir on this host (one per line): root-mode runtime/bots when
# present, plus each local/<fleet>/runtime/bots. The named enumerator for
# host-scope scripts that report across every fleet (disk-monitor,
# fleet-memory-check). Signal routing's first_bot_with_conf_any_fleet keeps
# its own caller-dir-first loop — a different contract (the caller's dir may
# be any path, and root-mode is the caller's dir there, not a fallback).
host_bots_dirs() {
    local d
    [ -d "$CLAUDLOBBY_ROOT/runtime/bots" ] && printf '%s\n' "$CLAUDLOBBY_ROOT/runtime/bots"
    for d in "$CLAUDLOBBY_ROOT"/local/*/runtime/bots; do
        [ -d "$d" ] && printf '%s\n' "$d"
    done
    return 0
}

parse_fleet_bots() {
    # Emit the bot names declared in a fleet.yaml (one per line) — the single
    # source of truth for "which bots does this fleet own". Supervision scripts
    # (fleet-pulse, keepalive-all, reconcile-fleet) filter their runtime-dir glob
    # through this so stale/cross-fleet residue dirs are never health-checked.
    # Mirrors claudlobby's documented schema: `bots:` at 2-space indent, bot keys
    # at 4-space indent. Missing/unreadable file → no output, so callers fall back
    # to scanning every dir (preserves root-mode and pre-fleet.yaml behavior).
    local fleet_yaml="$1"
    [ -f "$fleet_yaml" ] || return 0
    awk '
        /^  bots:[ \t]*$/ {in_bots=1; next}
        in_bots && /^    [a-zA-Z_][a-zA-Z0-9_-]*:[ \t]*$/ {
            gsub(/[ \t:]/, "", $0); print
        }
        in_bots && /^  [a-zA-Z_]/ && !/^    / {in_bots=0}
    ' "$fleet_yaml"
}

bot_in_fleet() {
    # Membership predicate for the declared-bots list from parse_fleet_bots.
    # Usage: bot_in_fleet <bot-name> <newline-separated-declared-list>
    # Stateless (the list is passed, not captured) so fleet-pulse, keepalive-all,
    # and any future supervision script share ONE filter — no per-script drift.
    # Empty list (no/unreadable fleet.yaml → root-mode) → 0, i.e. "declared":
    # callers then scan every dir, preserving pre-fleet.yaml behavior.
    [ -z "$2" ] && return 0
    printf '%s\n' "$2" | grep -qx "$1"
}

# fleet_service_prefix <fleet.yaml-path>
# Emit the fleet's service_prefix (composer default "claudlobby" when unset).
# Mirrors claudlobby's documented schema — `service_prefix:` at 2-space indent
# under `fleet:` — the same assumption parse_fleet_bots codifies. Reading
# fleet.yaml directly (instead of a composed bot.conf) is what lets timer
# enrollment run on a cold host before any bot output exists.
fleet_service_prefix() {
    local fleet_yaml="$1" val=""
    if [ -f "$fleet_yaml" ]; then
        val=$(sed -n 's/^  service_prefix:[[:space:]]*//p' "$fleet_yaml" | head -1)
        val=${val%%#*}
        val=$(printf '%s' "$val" | tr -d '\042\047' | sed 's/[[:space:]]*$//')
    fi
    printf '%s' "${val:-claudlobby}"
}

# bot_unit_present <bot-name> <bot_dir>
# True when the bot's host service unit exists (systemd unit file / launchd
# plist), under BOT_SERVICE from bot.conf or the bare bot name (pre-generate
# fallback). The unit-presence half of the fleet's "healthy" definition —
# reconcile-fleet (audit) and setup-fleet (skip-healthy) share this ONE
# predicate so the two can never drift.
bot_unit_present() {
    local bot="$1" bot_dir="$2" svc
    svc=$(bot_conf_get "$bot_dir" BOT_SERVICE "$bot")
    case "$_OS" in
    Linux)
        [ -f "$HOME/.config/systemd/user/$svc.service" ] ||
            [ -f "$HOME/.config/systemd/user/$bot.service" ]
        ;;
    Darwin)
        [ -f "$HOME/Library/LaunchAgents/$svc.plist" ] ||
            [ -f "$HOME/Library/LaunchAgents/$bot.plist" ]
        ;;
    *)
        return 1
        ;;
    esac
}

# unit_is_dormant <timers-dir> <unit-basename>
# True when the composed DORMANT manifest lists the unit (an enroll: false
# job — composed-but-dormant, opt-in via fleet.yaml). One predicate shared by
# setup-fleet and reconcile-fleet so enrollment and audit can never drift.
# Missing manifest → nothing is dormant; -x keeps comment lines inert.
unit_is_dormant() {
    grep -qxF "${2:?unit basename required}" "${1:?timers dir required}/DORMANT" 2>/dev/null
}

# resolve_timer_unit <caller-name> <timer-name> [<fleet-name>]
# Shared resolution for the generic timer enrollers (systemd + launchd):
# honors the setup-backbone env overrides (TIMER_DIR / UNIT_NAME /
# SERVICE_PREFIX), else resolves the fleet's composed-timers dir and the
# <service_prefix>.<timer> basename. On success sets:
#   TIMER_DIR      — source dir of composed units
#   UNIT_BASENAME  — unit basename (systemd unit name / launchd Label)
resolve_timer_unit() {
    local caller="$1" timer="$2" fleet="${3:-${CLAUDLOBBY_FLEET:-}}"
    local fleet_dir=""
    if [ -z "${TIMER_DIR:-}" ]; then
        if [ -z "$fleet" ]; then
            echo "$caller: pass a fleet name, set CLAUDLOBBY_FLEET, or set TIMER_DIR" >&2
            return 2
        fi
        fleet_dir="$CLAUDLOBBY_ROOT/local/$fleet"
        TIMER_DIR="$fleet_dir/runtime/fleet/timers"
    fi
    if [ ! -d "$TIMER_DIR" ]; then
        echo "Error: $TIMER_DIR not found — run 'claudlobby generate' first." >&2
        return 1
    fi
    if [ -n "${UNIT_NAME:-}" ]; then
        UNIT_BASENAME="$UNIT_NAME"
        return 0
    fi
    # Derive service prefix from bot.conf (all bots share the same
    # SERVICE_PREFIX). setup-fleet passes SERVICE_PREFIX from fleet.yaml
    # instead, so a cold start (no bot.conf composed yet) still enrolls.
    if [ -z "${SERVICE_PREFIX:-}" ] && [ -n "$fleet_dir" ]; then
        local _first_conf
        _first_conf="$(find "$fleet_dir/runtime/bots" -name bot.conf -print -quit 2>/dev/null)"
        if [ -n "$_first_conf" ]; then
            SERVICE_PREFIX="$(extract_bot_conf_var "$_first_conf" SERVICE_PREFIX)"
        fi
    fi
    if [ -z "${SERVICE_PREFIX:-}" ]; then
        echo "$caller: SERVICE_PREFIX not set and no bot.conf found." >&2
        return 2
    fi
    UNIT_BASENAME="$SERVICE_PREFIX.$timer"
}

# extract_bot_conf_var FILE VAR_NAME
# Extract a variable's value from a bot.conf file (strips 'export' prefix and quotes).
# Usage: SERVICE_PREFIX="$(extract_bot_conf_var "$conf_file" SERVICE_PREFIX)"
extract_bot_conf_var() {
    local conf_file="$1" var_name="$2"
    grep -m1 "^export ${var_name}=" "$conf_file" | cut -d= -f2- | tr -d "'"
}

# --- Script error events ------------------------------------------------------

# emit_script_error <bot_dir> <script_name> <exit_code> <message>
# Write a script_error event to the bot's JSONL event log.
# For scripts that run outside a bot context, pass "" for bot_dir and
# the event is written to $CLAUDLOBBY_ROOT/state/events/.
emit_script_error() {
    local bot_dir="$1" script_name="$2" exit_code="$3" message="$4"
    local events_dir bot_id

    if [ -n "$bot_dir" ] && [ -d "$bot_dir" ]; then
        events_dir="$bot_dir/data/events"
        bot_id=$(basename "$bot_dir")
    else
        events_dir="${CLAUDLOBBY_ROOT}/state/events"
        bot_id="fleet"
    fi
    mkdir -p "$events_dir"

    local ts today escaped_msg
    ts=$(ts_iso)
    today=$(date +%Y-%m-%d)
    escaped_msg=$(json_escape "$message")
    printf '{"ts":"%s","bot":"%s","type":"script_error","source":"lib","data":{"script":"%s","exit_code":%d,"message":"%s"}}\n' \
        "$ts" "$bot_id" "$script_name" "$exit_code" "$escaped_msg" \
        >> "$events_dir/fleet-${today}.jsonl"
}

# _emit_fleet_signal <bots_dir> <event_type> <reason> <ev_source> <WORD>
# Shared body for emit_failure_alert / emit_fleet_notice. It
#   1. emits a fleet-observability event {type:<event_type>, source:<ev_source>,
#      data.reason} to $CLAUDLOBBY_ROOT/state/events/fleet-<date>.jsonl, and
#   2. signals the fleet manager via a tmux nudge AND the Telegram channel
#      (chat id resolved like fleet-pulse: env override, else the first bot that
#      declares TELEGRAM_GROUP_CHAT_ID — falling back across every fleet on the
#      host, since host-scope callers run fleet-less).
# <WORD> is the uppercase framing word (ALERT/NOTICE) the delivery prefixes
# derive from. Both delivery channels are best-effort and never abort the caller.
_emit_fleet_signal() {
    local bots_dir="$1" event_type="$2" reason="$3" ev_source="$4" word="$5"
    local tmux_prefix="[FLEET-${word}]" tg_prefix="FLEET ${word}"

    local events_dir="${CLAUDLOBBY_ROOT}/state/events"
    mkdir -p "$events_dir"
    local ts today escaped
    ts=$(ts_iso); today=$(date +%Y-%m-%d); escaped=$(json_escape "$reason")
    printf '{"ts":"%s","bot":"fleet","type":"%s","source":"%s","data":{"reason":"%s"}}\n' \
        "$ts" "$event_type" "$ev_source" "$escaped" >> "$events_dir/fleet-${today}.jsonl"

    # manager tmux nudge (resolve from whichever bot declares MANAGER_TMUX) — on
    # the manager's OWN socket (per-bot servers); a default-socket send would
    # silently miss the manager post-migration. Routed through the one safe-send
    # primitive so a miss is logged, not swallowed. No manager anywhere →
    # nothing to nudge. Socket reverse-lookup uses the resolved manager's own
    # bots dir, which may be a fallback fleet's.
    local mgr_bot mgr mgr_socket
    mgr_bot=$(first_bot_with_conf_any_fleet "$bots_dir" MANAGER_TMUX || true)
    mgr=$(bot_conf_get "$mgr_bot" MANAGER_TMUX "")
    if [ -n "$mgr" ]; then
        mgr_socket=$(resolve_peer_socket "$(bot_conf_get "$mgr_bot" MANAGER_TMUX_SOCKET "")" "$mgr" "$(dirname "$mgr_bot")")
        if check_tmux_session "$mgr" "$mgr_socket"; then
            bot_tmux_send "$mgr_socket" "$mgr" "$tmux_prefix $event_type: $reason" || true
        fi
    fi

    # Telegram (loudest channel) — mirror fleet-pulse chat-id resolution
    local chat_bot chat_id state_dir
    chat_id="${FLEET_PULSE_ESCALATION_CHAT_ID:-}"
    if [ -z "$chat_id" ]; then
        chat_bot=$(first_bot_with_conf_any_fleet "$bots_dir" TELEGRAM_GROUP_CHAT_ID || true)
        if [ -n "$chat_bot" ]; then
            chat_id=$(bot_conf_get "$chat_bot" TELEGRAM_GROUP_CHAT_ID "")
            state_dir=$(bot_conf_get_path "$chat_bot" TELEGRAM_STATE_DIR "")
        fi
    fi
    if [ -n "$chat_id" ]; then
        TELEGRAM_GROUP_CHAT_ID="$chat_id" TELEGRAM_STATE_DIR="${state_dir:-}" \
            "${CLAUDLOBBY_ROOT}/lib/tg-post.sh" "$tg_prefix [$event_type]: $reason" >/dev/null 2>&1 || true
    fi
}

# emit_failure_alert <bots_dir> <event_type> <reason>
# LOUD, never-silent path for actionable incident signals — conditions an
# operator must act on (failed update, failed restart, disk/memory pressure).
# Routine informational nudges use emit_fleet_notice instead.
emit_failure_alert() {
    _emit_fleet_signal "$1" "$2" "$3" "alert" "ALERT"
}

# emit_fleet_notice <bots_dir> <event_type> <message>
# Informational sibling of emit_failure_alert: same channels, same durability,
# but framed as a notice so routine nudges (e.g. notify-behind's "N commits
# behind") never read as incidents or train operators to ignore FLEET ALERT.
emit_fleet_notice() {
    _emit_fleet_signal "$1" "$2" "$3" "notice" "NOTICE"
}

# install_error_trap <bot_dir>
# Set an ERR trap that emits a script_error event on non-zero exit.
# Call after sourcing lib-common.sh and resolving the bot directory.
# Pass "" for fleet-level scripts that run outside a bot context.
# NOTE: does NOT replace existing EXIT traps — only fires on ERR.
install_error_trap() {
    local _err_bot_dir="$1"
    local _err_script
    _err_script=$(basename "$0")
    trap 'emit_script_error "'"$_err_bot_dir"'" "'"$_err_script"'" "$?" "non-zero exit at line $LINENO"' ERR
}

# bot_conf_get <bot_dir> <key> <default>
# Read a single variable from a bot's bot.conf without sourcing the file
# (no side effects on the caller's environment). Handles both `export VAR=val`
# and plain `VAR=val` forms. Strips surrounding double quotes from values.
# Returns <default> if the file is missing or the key isn't found.
bot_conf_get() {
    local bot_dir="$1" key="$2" default="$3" val=""
    if [ -f "$bot_dir/bot.conf" ]; then
        val=$(grep "^\(export \)\?$key=" "$bot_dir/bot.conf" | head -1 \
            | sed -E "s/^(export )?$key=//" | tr -d '"' || true)
    fi
    printf '%s' "${val:-$default}"
}

# bot_conf_get_path <bot_dir> <key> <default>
# bot_conf_get for path-valued keys. The composer writes bot.conf to be
# SOURCED, so path values keep $HOME / $CLAUDLOBBY_ROOT as shell refs; a raw
# grep read hands back the literal string. Expand the two composed prefixes
# here, beside the contract they belong to, so every raw reader of a path key
# gets a usable path.
bot_conf_get_path() {
    local val
    val=$(bot_conf_get "$1" "$2" "$3")
    val="${val/#\$HOME/$HOME}"
    val="${val/#\$CLAUDLOBBY_ROOT/$CLAUDLOBBY_ROOT}"
    printf '%s' "$val"
}

# first_bot_with_conf <bots_dir> <key>
# Echo the first bot directory (alphabetical) whose bot.conf declares a
# non-empty value for <key>; return 1 with no output if none qualify.
# Use this to resolve a fleet-wide fallback (e.g. an escalation Telegram chat
# ID) from whichever bot actually defines it, rather than trusting the first
# directory blindly — the first bot may lack the key and silently mute the
# fallback. Reading multiple keys from the returned dir keeps them consistent
# (all drawn from the same bot).
first_bot_with_conf() {
    local bots_dir="$1" key="$2" d
    [ -d "$bots_dir" ] || return 1
    for d in "$bots_dir"/*/; do
        [ -d "$d" ] || continue
        if [ -n "$(bot_conf_get "$d" "$key" "")" ]; then
            printf '%s' "$d"
            return 0
        fi
    done
    return 1
}

# first_bot_with_conf_any_fleet <bots_dir> <key>
# first_bot_with_conf, falling back across every local/<fleet>/runtime/bots on
# the host when <bots_dir> has no declaring bot. Host-scope scripts (system.yaml
# host.jobs) run fleet-less, so their resolve_bots_dir lands on the root-mode
# runtime/bots — empty on multi-fleet hosts. Signal routing uses this so a
# fleet event is delivered *somewhere* rather than silently dropped; a
# fleet-scoped caller only reaches the fallback when its own fleet declares no
# receiver at all.
first_bot_with_conf_any_fleet() {
    local bots_dir="$1" key="$2" d
    if first_bot_with_conf "$bots_dir" "$key"; then
        return 0
    fi
    for d in "$CLAUDLOBBY_ROOT"/local/*/runtime/bots; do
        [ "$d" = "$bots_dir" ] && continue
        if first_bot_with_conf "$d" "$key"; then
            return 0
        fi
    done
    return 1
}

# bot_is_manager <bot_dir>
# True (0) if <bot_dir> is a team manager, false (1) otherwise. The composer
# sets a manager's MANAGER_TMUX to its own BOT_ID with an inline
# `# this bot is a manager` comment; a worker's MANAGER_TMUX points at a
# different bot. bot_conf_get does not strip that inline comment, so normalize
# it (and surrounding whitespace) away before comparing MANAGER_TMUX == BOT_ID.
bot_is_manager() {
    local bot_dir="${1:?Usage: bot_is_manager <bot_dir>}" mgr bid
    mgr=$(bot_conf_get "$bot_dir" MANAGER_TMUX "")
    bid=$(bot_conf_get "$bot_dir" BOT_ID "$(basename "$bot_dir")")
    mgr=${mgr%%#*}; mgr=${mgr//[[:space:]]/}
    bid=${bid%%#*}; bid=${bid//[[:space:]]/}
    [ -n "$bid" ] && [ "$mgr" = "$bid" ]
}
