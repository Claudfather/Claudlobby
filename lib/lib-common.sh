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

# tmux_socket_for_session <session> [bots_dir]
# Reverse-resolve a socket from a session name (= bot dir basename) for the
# cross-socket call sites that only know the peer's session name (dispatch.sh,
# bot-sweep-cron.sh, the report-back fallback). Locates the sibling bot dir
# under [bots_dir] — default: the caller's own runtime/bots dir, derived from
# $BOT_DIR — and resolves its socket via tmux_socket_for_bot.
tmux_socket_for_session() {
    local session="${1:?Usage: tmux_socket_for_session <session> [bots_dir]}"
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
    tmux_socket_for_bot "$bots_dir/$session"
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

# emit_failure_alert <bots_dir> <event_type> <reason>
# LOUD, never-silent failure path shared by the fleet update mechanisms
# (reload-fleet.sh = Mechanism 1; update-claude-code.sh = Mechanism 2). It
#   1. emits a fleet-observability event {type:<event_type>, source:"alert",
#      data.reason} to $CLAUDLOBBY_ROOT/state/events/fleet-<date>.jsonl, and
#   2. alerts the fleet manager via a tmux nudge AND the Telegram escalation
#      (chat id resolved like fleet-pulse: env override, else the first bot that
#      declares TELEGRAM_GROUP_CHAT_ID).
# Both alert channels are best-effort and never abort the caller.
emit_failure_alert() {
    local bots_dir="$1" event_type="$2" reason="$3"

    local events_dir="${CLAUDLOBBY_ROOT}/state/events"
    mkdir -p "$events_dir"
    local ts today escaped
    ts=$(ts_iso); today=$(date +%Y-%m-%d); escaped=$(json_escape "$reason")
    printf '{"ts":"%s","bot":"fleet","type":"%s","source":"alert","data":{"reason":"%s"}}\n' \
        "$ts" "$event_type" "$escaped" >> "$events_dir/fleet-${today}.jsonl"

    # manager tmux nudge (resolve from whichever bot declares MANAGER_TMUX) — on
    # the manager's OWN socket (per-bot servers); a default-socket send would
    # silently miss the manager post-migration. Routed through the one safe-send
    # primitive so a miss is logged, not swallowed.
    local mgr_bot mgr mgr_socket
    mgr_bot=$(first_bot_with_conf "$bots_dir" MANAGER_TMUX || true)
    mgr=$(bot_conf_get "$mgr_bot" MANAGER_TMUX "")
    mgr_socket=$(resolve_peer_socket "$(bot_conf_get "$mgr_bot" MANAGER_TMUX_SOCKET "")" "$mgr" "$bots_dir")
    if [ -n "$mgr" ] && check_tmux_session "$mgr" "$mgr_socket"; then
        bot_tmux_send "$mgr_socket" "$mgr" "[FLEET-ALERT] $event_type: $reason" || true
    fi

    # Telegram escalation (loudest channel) — mirror fleet-pulse chat-id resolution
    local chat_bot chat_id state_dir
    chat_id="${FLEET_PULSE_ESCALATION_CHAT_ID:-}"
    if [ -z "$chat_id" ]; then
        chat_bot=$(first_bot_with_conf "$bots_dir" TELEGRAM_GROUP_CHAT_ID || true)
        if [ -n "$chat_bot" ]; then
            chat_id=$(bot_conf_get "$chat_bot" TELEGRAM_GROUP_CHAT_ID "")
            state_dir=$(bot_conf_get "$chat_bot" TELEGRAM_STATE_DIR "")
        fi
    fi
    if [ -n "$chat_id" ]; then
        TELEGRAM_GROUP_CHAT_ID="$chat_id" TELEGRAM_STATE_DIR="${state_dir:-}" \
            "${CLAUDLOBBY_ROOT}/lib/tg-post.sh" "FLEET ALERT [$event_type]: $reason" >/dev/null 2>&1 || true
    fi
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
