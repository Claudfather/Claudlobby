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
#   show_help          — extract comment header from calling script
#   ts_iso             — portable ISO 8601 timestamp
#   date_relative      — portable relative date arithmetic
#   stat_mtime         — portable file mtime (epoch seconds)
#   sed_i              — portable in-place sed
#   df_pcent           — portable disk usage percentage
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
    local session="${1:?Usage: check_tmux_session <name>}"
    "$_TMUX_BIN" has-session -t "$session" 2>/dev/null
}

# pane_is_idle <pane_text>
# Returns 0 if the pane is at a prompt / waiting-for-input, 1 otherwise.
# Mirrors keepalive.sh classify_pane's IDLE branch — used by fleet-pulse to
# avoid flagging a finished, at-prompt bot as activity_stuck (a legitimately
# idle bot makes no tool calls). Operators extend via KEEPALIVE_IDLE_PATTERNS.
pane_is_idle() {
    local text="$1"
    local _idle_pattern='(^\s*[>❯]\s*$|Remote Control active|Enter\/Esc to close|Yes\/No|Allow|Deny|y\/n\b)'
    if [ -n "${KEEPALIVE_IDLE_PATTERNS:-}" ]; then
        _idle_pattern="$_idle_pattern|$KEEPALIVE_IDLE_PATTERNS"
    fi
    printf '%s' "$text" | grep -qE "$_idle_pattern"
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

# extract_bot_conf_var FILE VAR_NAME
# Extract a variable's value from a bot.conf file (strips 'export' prefix and quotes).
# Usage: SERVICE_PREFIX="$(extract_bot_conf_var "$conf_file" SERVICE_PREFIX)"
extract_bot_conf_var() {
    local conf_file="$1" var_name="$2"
    grep -m1 "^export ${var_name}=" "$conf_file" | cut -d= -f2- | tr -d "'"
}
