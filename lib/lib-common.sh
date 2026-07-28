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
#   own_tool_path      — prepend this repo's tool prefixes (timer PATH is minimal)
#   claudlobby_cli     — run the claudlobby CLI across every install shape
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
#   proc_rss_kb        — portable RSS (KB) of a pid + its direct children
#   json_escape        — JSON-string escaping incl. control chars (#530)
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

# --- Timer-environment tool resolution --------------------------------------
# systemd, launchd and cron hand a script a MINIMAL PATH (launchd gives bare
# /usr/bin:/bin:/usr/sbin:/sbin), so a lib/ script a timer invokes resolves none
# of the tools an interactive shell would. Any such script must own its PATH
# explicitly or die with a bare "command not found" (#805: reload-fleet.sh did,
# silently, for two days).
#
# Division of labour with the composer: composer.py's _scheduler_tool_path bakes
# a PATH into every composed timer unit (#798/#802) and is the primary fix. This
# helper is the belt-and-braces floor for the cases that PATH cannot reach — a
# unit composed before #802 and not yet re-enrolled, cron, or the documented
# on-demand `bash lib/reload-fleet.sh` from a shell (where `claudlobby` is on no
# PATH at all unless the venv is active).
#
# own_tool_path APPENDS every prefix this repo installs tools into:
#   $_HOMEBREW/bin          — Homebrew (macOS node/claude)
#   $CLAUDLOBBY_ROOT/.venv/bin — repo-local venv (`pip install -e .` inside one)
#   $HOME/.local/bin        — pip install --user console scripts
#   $HOME/.bun/bin          — bun global bin (mirrors start-bot.sh:49)
#   $HOME/.npm-global/bin   — npm global prefix (claude)
#
# APPEND, deliberately, not prepend. These are FALLBACKS for an environment that
# resolves nothing, so whatever PATH the caller already set must keep winning:
# an operator pinning a binary, a test stubbing one, and the system dirs that
# start-bot.sh:49 puts FIRST all stay authoritative. Prepending would silently
# re-point reload-fleet at a shadow user copy of claude while the fleet runs the
# system one — the exact class of bug #635 fixed in update-claude-code.sh.
#
# CLAUDLOBBY_TOOL_PREFIXES (colon-separated) substitutes the fallback list, so a
# test or an unusual host can pin resolution — the same seam shape as
# update-claude-code.sh's CLAUDE_UPDATE_FLEET_PATH. Set it empty to add nothing.
own_tool_path() {
    # An unset HOME (a bare timer env) yields "/.local/bin" etc, which the -d
    # test below discards — so no set -u guard is needed on top.
    local d IFS=:
    # shellcheck disable=SC2086  # deliberate split on the colon-separated list
    set -- ${CLAUDLOBBY_TOOL_PREFIXES-${_HOMEBREW:+$_HOMEBREW/bin:}$CLAUDLOBBY_ROOT/.venv/bin:${HOME:-}/.local/bin:${HOME:-}/.bun/bin:${HOME:-}/.npm-global/bin}
    for d; do
        [ -d "$d" ] || continue          # absent prefix — resolves nothing
        case ":$PATH:" in
            *":$d:"*) ;;                 # already present — do not duplicate
            *) PATH="$PATH:$d" ;;
        esac
    done
    export PATH
}

# claudlobby_cli <args...>
# Run the claudlobby CLI across every install shape getting-started.md supports.
# `pip install -e .` yields a console script whose location depends entirely on
# which python did the installing, so PATH alone is not a reliable contract:
#   1. `claudlobby` on PATH — pipx, or a --user/venv install own_tool_path found.
#   2. `python3 -m claudlobby` — the documented equivalent invocation.
# Rung 2 runs from $CLAUDLOBBY_ROOT so an editable/uninstalled checkout resolves
# on sys.path regardless of the caller's cwd — never rely on cwd being the repo
# (the launchd plists set WorkingDirectory, the systemd units do not).
# Returns 127 with a diagnosable message when neither rung resolves.
claudlobby_cli() {
    if command -v claudlobby >/dev/null 2>&1; then
        claudlobby "$@"
    elif ( cd "$CLAUDLOBBY_ROOT" && python3 -c 'import claudlobby' ) >/dev/null 2>&1; then
        ( cd "$CLAUDLOBBY_ROOT" && python3 -m claudlobby "$@" )
    else
        printf 'claudlobby CLI unresolvable: not on PATH (%s) and not importable from %s. Fix: pip install -e %s\n' \
            "$PATH" "$CLAUDLOBBY_ROOT" "$CLAUDLOBBY_ROOT" >&2
        return 127
    fi
}

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
    # Fleet — flat local/<fleet>/.env byte-identically, or the nested fleet dir.
    if [ -n "${FLEET_NAME:-}" ] && [ -n "${CLAUDLOBBY_ROOT:-}" ]; then
        local fleet_dir fleet_env
        fleet_dir=$(resolve_fleet_dir "$FLEET_NAME") || fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET_NAME"
        fleet_env="$fleet_dir/.env"
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
# Escape a value for safe embedding in a JSON string. Prints to stdout.
# Fast path (the overwhelmingly common shape): backslash + double-quote via
# sed. Values containing ANY control character take the python3 path — JSON
# forbids raw chars below 0x20 in strings, sed is line-oriented and cannot
# escape the newline it never sees, and a raw newline splits a single-line
# JSONL ledger row, which the line-oriented rotation then truncates into
# permanently invalid JSON (#530). json.dumps produces exact JSON string
# escaping for every control character; [[:cntrl:]] (POSIX class, bash 3.2
# case-glob safe) routes them all, not just the common \n\r\t.
json_escape() {
    case "$1" in
        *[[:cntrl:]]*)
            printf '%s' "$1" | python3 -c 'import json, sys; sys.stdout.write(json.dumps(sys.stdin.read())[1:-1])'
            ;;
        *)
            printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
            ;;
    esac
}

# --- MCP trust seeding (dev checkouts) ---------------------------------------

# _home_mcp_allowlist <bot_home>
# Echo the bot composed enabledMcpjsonServers allowlist (a JSON array) from its
# home settings.local.json, or return non-zero when there is nothing to trust
# (no home settings, or an empty/absent allowlist). The composer is the SOLE
# deriver of this set (fleet-config-driven, fail-closed); callers only PROPAGATE
# the value it wrote, so they can never drift from the composer rules.
_home_mcp_allowlist() {
    local home_settings="$1/.claude/settings.local.json" allowlist
    [ -f "$home_settings" ] || return 1
    allowlist="$(jq -c '.enabledMcpjsonServers // empty' "$home_settings" 2>/dev/null || true)"
    case "$allowlist" in
        ""|"null"|"[]") return 1 ;;
    esac
    printf '%s' "$allowlist"
}

# seed_checkout_mcp_trust <checkout_dir> <allowlist_json>
# Write the given MCP-trust allowlist into a projects/ checkout so an interactive
# `claude` session rooted there pre-trusts the same fleet-configured MCP servers
# and never stalls on the MCP-server-trust prompt (which no --permission-mode
# answers). Idempotent and non-destructive (merges into any existing
# settings.local.json). Callers derive <allowlist_json> once via
# _home_mcp_allowlist and pass it in. Plain projects/ checkouts only.
seed_checkout_mcp_trust() {
    local checkout_dir="$1" allowlist="$2"
    [ -d "$checkout_dir" ] || return 0
    case "$allowlist" in
        ""|"null"|"[]") return 0 ;;
    esac

    local checkout_settings="$checkout_dir/.claude/settings.local.json" tmp created=0
    mkdir -p "$checkout_dir/.claude"
    tmp="$(safe_mktemp)"
    # Merge into an existing valid file to preserve any developer keys; a missing
    # or unparseable file is (re)written fresh.
    if [ ! -f "$checkout_settings" ] \
       || ! jq --argjson al "$allowlist" '.enabledMcpjsonServers = $al' "$checkout_settings" > "$tmp" 2>/dev/null; then
        printf '{"enabledMcpjsonServers":%s}\n' "$allowlist" > "$tmp"
        created=1
    fi
    mv "$tmp" "$checkout_settings"

    # A file we created is invisible to git only if excluded; an existing file
    # was already visible to whoever wrote it. The per-clone info/exclude never
    # touches tracked files and stops an accidental `git add -A` from staging the
    # dev-context seed. (The callers gate on a real .git directory.)
    if [ "$created" = 1 ]; then
        local exclude="$checkout_dir/.git/info/exclude"
        grep -qxF ".claude/settings.local.json" "$exclude" 2>/dev/null \
            || printf '%s\n' ".claude/settings.local.json" >> "$exclude"
    fi
}

# seed_all_checkouts <bot_dir>
# Seed every projects/ checkout under a bot home with the bot composed MCP-trust
# allowlist (see seed_checkout_mcp_trust). Reads the allowlist once. No-ops
# cleanly when the bot has no projects/ dir, no checkouts, or nothing to trust.
seed_all_checkouts() {
    local bot_dir="$1" repo allowlist
    [ -d "$bot_dir/projects" ] || return 0
    allowlist="$(_home_mcp_allowlist "$bot_dir")" || return 0
    for repo in "$bot_dir"/projects/*/; do
        [ -d "$repo/.git" ] || continue
        seed_checkout_mcp_trust "$repo" "$allowlist" || true
    done
}

# --- Task identity -------------------------------------------------------------

# mint_task_id
# THE task-id mint, shared by every work-admission entry point (dispatch-task,
# and later the runner/manager admission paths). Format t-<epochsecs>-<4hex> —
# pinned grammar ^t-[0-9]+-[0-9a-f]{4}$: collision-safe without coordination,
# mintable offline, greppable in panes and ledgers, and survives
# sanitize_tmux_input untouched (plain [a-z0-9-]). The ledger row that records
# the id is the SSOT; the id is echoed through [BOTCOMMAND] -> [BOTREPORT].
# No fallback branch: /dev/urandom is POSIX-guaranteed on target platforms,
# and under set -e a pipeline failure would abort the caller loudly anyway
# (a fallback line after a failed assignment is unreachable under -e).
mint_task_id() {
    printf 't-%s-%s\n' "$(date +%s)" "$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
}

# rotate_jsonl_by_ts <ledger>
# Shared self-rotation for ts-keyed JSONL ledgers (dispatch-log.jsonl,
# report-back.jsonl): keep entries newer than OBSERVABILITY_REAP_DAYS
# (default 7). Call inside the caller's with_lock critical section.
# CONSTRAINT: keep DISPATCH_OVERDUE_MAX_AGE_S (default 24h) BELOW this
# window — a max_age raised past it would let rotation silently prune a
# still-alerting dispatch row.
rotate_jsonl_by_ts() {
    local ledger="$1"
    local reap_days="${OBSERVABILITY_REAP_DAYS:-7}"
    local cutoff
    cutoff=$(date_relative "-${reap_days} days" "%Y-%m-%dT%H:%M:%SZ") || return 0
    local tmp
    tmp=$(safe_mktemp)
    awk -F'"ts":"' -v cutoff="$cutoff" 'NF>1 { split($2, a, "\"") ; if (a[1] >= cutoff) print }' "$ledger" > "$tmp" \
        && mv "$tmp" "$ledger"
}

# --- tmux helpers ------------------------------------------------------------

# Strip control chars and escape sequences dangerous in tmux send-keys.
# Mirrors the clean() policy in dispatch-task.sh so the dispatch ledger
# records what the worker receives — widen both together (parity test:
# tests/test_claudron_query_wedge.py fixed-point case).
sanitize_tmux_input() {
    local input="$1" _esc
    # Strip whole plain-CSI sequences FIRST — once the ESC byte is converted
    # below, only the printable remainder ("[31m") would be left to leak into
    # the sent text. Other escape families (OSC, charset, private-marker CSI)
    # still degrade to space + printable residue; controls are neutralized
    # either way. Literal ESC byte via printf: BSD sed has no \x escapes.
    _esc=$(printf '\033')
    input=$(printf '%s' "$input" | sed "s/${_esc}\[[0-9;]*[a-zA-Z]//g")
    # The two substitutions must stay separate: the outer capture swallows
    # the newline BSD sed appends to an unterminated last line.
    # Two-operand tr -s: controls become spaces rather than being deleted
    # (deletion merges words across newlines: "do x<NL>then" -> "do xthen"),
    # and the resulting runs squeeze to one space.
    input=$(printf '%s' "$input" | tr -s '\000-\037\177' ' ')
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

# bridge_state <bot_dir> [pretoken]
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
# resolve_bot_telegram_token <bot_dir>
# Print the bot's Telegram token, resolved the way start-bot.sh resolves it:
# bot.conf names the var (TELEGRAM_TOKEN_ENV_NAME); the value lives in the
# tiered .env chain. Runs in a subshell so the calling env is untouched.
# Empty output = no token reaches this bot. The ONE resolution shared by
# bridge_state and creds-check — if they resolved differently, creds-check
# would validate a token no bot actually runs with.
resolve_bot_telegram_token() {
    local bot_dir="${1:?Usage: resolve_bot_telegram_token /path/to/bot/dir}"
    (
        load_bot_conf "$bot_dir" >/dev/null 2>&1 || true
        # shellcheck disable=SC2030  # subshell-local by design: never touch the calling env
        # NO apostrophes in comments inside subshells scanned by bash 3.2 —
        # see the warning at bridge_state; gate: tests/test_bash_parse.py
        BOT_DIR="$bot_dir"
        source_env_tiered 2>/dev/null || true
        _te="${TELEGRAM_TOKEN_ENV_NAME:-}"
        if [ -n "$_te" ]; then printf '%s' "${!_te:-}"; fi
    ) || true
}

# bot_expects_no_token <bot_dir>
# True when a bot intentionally runs WITHOUT a Telegram token — a canary or
# throwaway spun to exercise a boot/reaper path, not a real channel bot. Marked
# by EXPECT_NO_TOKEN=1 in its bot.conf. A missing token is a genuine fault for a
# real bot (unmarked — the no_token alert still fires) but the declared, expected
# state for a throwaway, where that same alert is pure bring-up noise. Read from
# bot.conf via bot_conf_get (not the process env) so the verdict is identical in
# fleet-pulse, which classifies bots without sourcing any bot.conf.
bot_expects_no_token() {
    local bot_dir="${1:?Usage: bot_expects_no_token /path/to/bot/dir}"
    [ "$(bot_conf_get "$bot_dir" EXPECT_NO_TOKEN "")" = "1" ]
}

bridge_state() {
    local bot_dir="${1:?Usage: bridge_state /path/to/bot/dir}"
    local handle state_dir token pidfile pid comm ppid pcomm environ environ_lines args psline _anc _hop

    handle="$(bot_conf_get "$bot_dir" TELEGRAM_BOT_HANDLE "")" || true
    if [ -z "$handle" ]; then printf '%s' "no_handle"; return 1; fi

    # A hot-loop caller (start-bot readiness) may pass a pre-resolved token as $2
    # to skip re-sourcing the .env chain on every poll (#756); every other caller
    # omits it and resolves here. The token is static .env config, so resolving it
    # once in the caller and threading it in is safe.
    if [ "$#" -ge 2 ]; then token="$2"; else token="$(resolve_bot_telegram_token "$bot_dir")" || true; fi
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

    # Ownership: the poller's environment must hold EXACTLY this bot's
    # TELEGRAM_STATE_DIR as a KEY=VALUE entry — never a substring (telegram-data
    # must not match telegram-data-eng). Only the SOURCE of the environment is
    # per-OS; the exact match below is shared. Linux reads the NUL-delimited
    # /proc environ. macOS/BSD has no /proc, so `ps eww` surfaces the environment
    # appended to the command — the portable read that keeps deaf-orphan
    # detection alive for channel bots there (#710), the exact case the lineage
    # walk below exists to catch. Either source unreadable (EACCES, no /proc, a
    # ps with no env support) → unknown; never treat unprovable ownership as ours.
    if [ "$_OS" = "Linux" ]; then
        environ="/proc/$pid/environ"
        if [ ! -r "$environ" ]; then printf '%s' "unknown"; return 1; fi
        environ_lines="$(tr '\0' '\n' < "$environ" 2>/dev/null)"
    else
        # `e` shows the environment, `ww` prevents truncation; split on whitespace
        # so each KEY=VALUE is its own line for the exact match (mirrors the Linux
        # NUL split). A space-bearing value would split — macOS home dirs carry
        # none. Empty (a ps with no env support, or the pid gone) → unknown.
        environ_lines="$(ps eww -p "$pid" 2>/dev/null | tr '[:space:]' '\n')"
        if [ -z "$environ_lines" ]; then printf '%s' "unknown"; return 1; fi
    fi
    if ! grep -qxF "TELEGRAM_STATE_DIR=$state_dir" <<<"$environ_lines"; then
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

# bridge_down_state <bot_dir> [grace_seconds]
# Decide whether an up-bot's Telegram bridge is actionably DOWN. Wraps
# bridge_state with a post-(re)start grace window so a freshly-restarted poller
# (e.g. right after a fleet-wide restart) isn't flagged before it can spin up.
#
# Collapses bridge_state's five states into one verdict:
#   no_bridge | no_token      -> print the state, return 0  (caller alerts)
#   up | no_handle | unknown  -> print nothing, return 1    (no alert)
# `unknown` is never actionable: bridge_state emits it when ownership is
# unprovable (an unreadable environment source — EACCES on /proc, or a ps with
# no env support) and must not be healed OR alerted. macOS is no longer unknown
# by default: its `ps eww` ownership read resolves up/no_bridge like Linux.
# no_token is likewise not actionable for a bot that declares EXPECT_NO_TOKEN=1
# (a canary/throwaway with no token by design) — it collapses to no-alert.
#
# Grace is measured from the data/.spawn marker (touched on every start-bot.sh
# (re)start). A missing marker means no grace — a long-lived bot with a genuinely
# dead bridge still alerts.
bridge_down_state() {
    local bot_dir="${1:?Usage: bridge_down_state <bot_dir> [grace_seconds]}"
    local grace="${2:-300}"
    local spawn_marker="$bot_dir/data/.spawn" spawn_epoch now state

    spawn_epoch="$(stat_mtime "$spawn_marker" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    if [ "$spawn_epoch" -gt 0 ] && [ "$((now - spawn_epoch))" -lt "$grace" ]; then
        return 1 # within post-(re)start grace; poller may still be coming up
    fi

    state="$(bridge_state "$bot_dir" 2>/dev/null || true)"
    case "$state" in
        no_token)
            # Exempt a declared throwaway (EXPECT_NO_TOKEN=1, per the header note);
            # real bots (no marker) still surface as no_token for the caller to alert.
            bot_expects_no_token "$bot_dir" && return 1
            printf '%s' "$state"; return 0 ;;
        no_bridge) printf '%s' "$state"; return 0 ;;
        *) return 1 ;; # up / no_handle / unknown -> not an actionable bridge_down
    esac
}

# bridge_bringup_verify <bot_dir> <bots_dir> [timeout_seconds]
# One-shot post-boot Telegram-bridge verification for start-bot.sh (Phase 3b/3c).
# "remote-control is active" is a SEPARATE subsystem from the channel poller, so a
# bot can come up ready with a dark bridge — this proves the `bun server.ts` poller
# actually spawned. Polls bridge_state up to <timeout>s (default 45) for `up`; on a
# terminal non-up state it marks + escalates so a silently-dark bridge is loud at
# bring-up.
#
# It NEVER bounces — keepalive owns the heal ladder (Fork F1=b); start-bot only
# verifies, marks, and escalates. The durable data/.bridge-down marker is dropped on
# a verified-missing bridge and cleared whenever bridge_state reads `up` (here, or
# later by any consumer). Escalation is tmux-first via emit_failure_alert (the
# manager nudge is bridge-independent — reporting a dead Telegram over Telegram is
# the circular-escalation hazard). no_token escalates once but drops no marker (a
# bounce can't heal a missing token). Non-channel bots (no_handle) and indeterminate
# ownership (unknown) are silent — never actionable.
#
# Prints one status token for the caller to log:
#   ready | missing:no_bridge | missing:no_token | unknown | no_handle
bridge_bringup_verify() {
    local bot_dir="${1:?Usage: bridge_bringup_verify <bot_dir> <bots_dir> [timeout]}"
    local bots_dir="${2:?Usage: bridge_bringup_verify <bot_dir> <bots_dir> [timeout]}"
    local timeout="${3:-45}"
    local marker="$bot_dir/data/.bridge-down" state="" bot_id elapsed=0 heal_note=""
    bot_id="$(basename "$bot_dir")"

    # Poll until `up`, a terminal state, or <timeout>s elapse (always checks at
    # least once). up is success; no_token (token unset) and no_handle (not a
    # channel bot) won't change by waiting, so don't burn the window on them.
    # no_bridge keeps polling — a cold `bun install` may still be writing bot.pid.
    # Count elapsed sleeps rather than reading the clock so the rare dark-bridge
    # poll spawns no per-iteration `date`.
    while :; do
        state="$(bridge_state "$bot_dir" 2>/dev/null || true)"
        case "$state" in
            up | no_token | no_handle) break ;;
        esac
        if [ "$elapsed" -ge "$timeout" ]; then break; fi
        sleep 1
        elapsed=$((elapsed + 1))
    done

    case "$state" in
        up)
            rm -f "$marker" 2>/dev/null || true
            printf '%s' "ready" ;;
        no_handle)
            printf '%s' "no_handle" ;;
        no_token)
            if bot_expects_no_token "$bot_dir"; then
                # Declared throwaway/canary (EXPECT_NO_TOKEN=1): a missing token is
                # its intended state, so this alert would be pure bring-up noise.
                # A real bot never carries the marker and still escalates below.
                printf '%s' "expected:no_token"
            else
                emit_failure_alert "$bots_dir" "bridge_down" \
                    "$bot_id Telegram bridge no_token at bring-up — token unset; escalate, cannot heal" || true
                printf '%s' "missing:no_token"
            fi ;;
        unknown)
            printf '%s' "unknown" ;;
        *) # no_bridge (or an empty read) — a verified-dark bridge
            mkdir -p "$bot_dir/data" 2>/dev/null || true
            : > "$marker" 2>/dev/null || true
            # Promise only the heal keepalive will actually attempt. Its
            # _bridge_heal is gated on OBSERVABILITY_BRIDGE_HEAL (off fleet-wide),
            # and even when armed the poller is an MCP stdio child of claude — so
            # the sole lever is a full bot bounce, never a gentle in-place respawn.
            # Gate off: inbound stays dark until a restart. Mirror the no_token arm
            # above, which escalates rather than promising a heal it cannot deliver.
            if [ "${OBSERVABILITY_BRIDGE_HEAL:-0}" = "1" ]; then
                heal_note="keepalive will bounce the bot to recover"
            else
                heal_note="inbound dark until restart, manager decides"
            fi
            emit_failure_alert "$bots_dir" "bridge_down" \
                "$bot_id Telegram bridge down at bring-up — poller not delivering (tmux dispatch still works; $heal_note)" || true
            printf '%s' "missing:no_bridge" ;;
    esac
}

# bridge_fence_write <bot_dir>
# Append a UNIQUE restart-fence marker to the bot's startup.log and echo the
# token. Call this immediately BEFORE the restart, then pass the token to
# wait_bridge_ready — only a BRIDGE_READY written after the marker counts as
# fresh. The token embeds this run's pid + the bot, so it is unique per
# (run, bot) and never collides with a prior run's leftover marker (a new run
# carries a fresh pid). One fence write per bot per run — if a same-run retry is
# ever added, the token needs a per-write discriminator (pid+bot alone cannot
# tell two same-run writes apart).
bridge_fence_write() {
    local bot_dir="${1:?Usage: bridge_fence_write <bot_dir>}"
    local log="$bot_dir/logs/startup.log" token
    token="RR_FENCE_$$_$(basename "$bot_dir")"
    mkdir -p "$bot_dir/logs" 2>/dev/null || true
    printf '%s %s\n' "$(ts_iso)" "$token" >> "$log" 2>/dev/null || true
    printf '%s' "$token"
}

# wait_bridge_ready <bot_dir> <ceiling_s> <fence_token>
# Block until a BRIDGE_READY is appended to the bot's startup.log AFTER
# <fence_token> — the unique marker bridge_fence_write wrote just before the
# restart. Only a BRIDGE_READY that follows the marker counts, so a stale one
# from a prior boot can never pass the gate. This is the per-bot gate for a
# serial rolling restart (#689); the caller serializes / halts rather than
# proceed-anyway across the fleet (the #688/#689 mass-restart outage).
#
# Why a marker, not a byte offset (#696 review, finding 1): log-rotate.sh keeps
# only a line-count tail, so a rotation mid-restart-window invalidates a byte
# offset AND can leave a prior boot's POLL_START…BRIDGE_READY as the "newest"
# pair — a stale line the old byte/last-POLL_START fallback would accept. The
# marker rides the rotated tail with the new lines; if it is ever rotated away
# entirely, nothing matches and the gate fails CLOSED (a timeout, never a
# false-ready). Poll-count, not clock, so a slow bridge spawns no date fork.
#
# Limitation (#696 finding 2, narrow): the marker is written just before the
# restart, so a bot that is ITSELF still mid-boot when rolled (has not reached
# its own BRIDGE_READY) can have its old process append a real BRIDGE_READY after
# the marker before `systemctl restart` reaps it. This is marker-fresh, not
# process-generation-fresh — tracked as a #696 follow-up.
wait_bridge_ready() {
    local bot_dir="${1:?Usage: wait_bridge_ready <bot_dir> <ceiling_s> <fence_token>}"
    local ceiling="${2:-180}" token="${3:?wait_bridge_ready needs a fence token}"
    local log="$bot_dir/logs/startup.log" waited=0 step=3 after
    while :; do
        # Everything after the LAST occurrence of the fence token. A prior boot's
        # lines precede the marker; only what follows it is this restart's.
        # POSIX awk only (index/ORS/printf) — runs on mawk, no GNU extensions.
        after="$(awk -v tok="$token" 'index($0, tok){after=""; seen=1; next} seen{after = after $0 ORS} END{printf "%s", after}' "$log" 2>/dev/null || true)"
        case "$after" in *BRIDGE_READY*) return 0 ;; esac
        [ "$waited" -ge "$ceiling" ] && return 1
        sleep "$step"
        waited=$((waited + step))
    done
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
    # Nested vault: a fleet under a system container is one level deeper. The
    # [ -d ] guard drops the literal glob when nothing matches (flat = additive).
    for d in "$CLAUDLOBBY_ROOT"/local/*/*/runtime/bots/"$session"; do
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
    local target
    target=$(_session_candidate_dir tmux_socket_for_session "$@") || return $?
    tmux_socket_for_bot "$target"
}

# _session_candidate_dir <caller> <session> [bots_dir]
# The ONE session-name -> candidate-bot-dir resolution, shared by
# tmux_socket_for_session and bot_dir_for_session. Always prints the
# candidate path — even when it does not exist — so each caller keeps its
# own miss contract (socket synthesis / fail-fast vs. rc-1 empty).
_session_candidate_dir() {
    local caller="$1"
    # Empty session degrades to rc 2, never a fatal ${2:?} — the expansion
    # fault would kill the calling script from inside command substitution
    # under set -e (silently, when stderr is redirected), and every caller
    # already handles an empty result.
    local session="${2:-}"
    if [ -z "$session" ]; then
        echo "$caller: empty session name" >&2
        return 2
    fi
    local bots_dir="${3:-}"
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
    # try the cross-fleet fallback; if that also misses, print the own-fleet
    # path unchanged so an unknown peer hits the caller's original behavior.
    local target="$bots_dir/$session"
    if [ ! -d "$target" ]; then
        local peer_dir
        peer_dir=$(_resolve_cross_fleet_bot_dir "$session")
        [ -n "$peer_dir" ] && target="$peer_dir"
    fi
    printf '%s' "$target"
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

# Append one event to a bot's JSONL ledger (data/events/fleet-YYYY-MM-DD.jsonl)
# — the SAME ledger fleet-pulse reads and escalates. Best-effort: never fails
# the caller, because startup/observability paths must not abort on a log write.
# Identity: explicit bot_dir/bot_id win, else ambient $BOT_DIR/$BOT_ID. An
# explicitly EMPTY bot_dir ("") forces the fleet-level ledger (state/events,
# bot:"fleet") and ignores ambient identity — used by _emit_fleet_signal and by
# emit_script_error's host-context (no bot dir) path. data_json must be a valid
# JSON value (default {}).
# Usage: emit_fleet_event <type> <source> [data_json] [bot_dir] [bot_id]
# The shared per-source event write behind fleet-pulse / code-audit-sweep's
# checks and _tmux_send_miss below; each passes its own <source> and emits here.
emit_fleet_event() {
    local event_type="${1:?emit_fleet_event: <type> required}"
    local event_source="${2:-unknown}"
    local data_json="${3:-}"
    # No-colon ${4-…}: an explicitly EMPTY bot_dir stays empty (forcing the
    # fleet-level branch) instead of falling back to ambient $BOT_DIR.
    local bot_dir="${4-${BOT_DIR:-}}"
    local bot_id="${5-}"
    [ -n "$data_json" ] || data_json='{}'
    local events_dir
    if [ -n "$bot_dir" ] && [ -d "$bot_dir" ]; then
        events_dir="$bot_dir/data/events"
        [ -n "$bot_id" ] || bot_id="${BOT_ID:-$(basename "$bot_dir")}"
    else
        # Fleet-level ledger: identity is the explicit bot_id or "fleet" — never
        # ambient $BOT_ID, so a host job's alert is not misattributed to a bot.
        events_dir="${CLAUDLOBBY_ROOT:-}/state/events"
        [ -n "$bot_id" ] || bot_id="fleet"
    fi
    mkdir -p "$events_dir" 2>/dev/null || return 0
    local ts today
    ts=$(ts_iso); today=$(date +%Y-%m-%d)
    printf '{"ts":"%s","bot":"%s","type":"%s","source":"%s","data":%s}\n' \
        "$ts" "$bot_id" "$event_type" "$event_source" "$data_json" \
        >> "$events_dir/fleet-${today}.jsonl" 2>/dev/null || true
}

# _tmux_send_miss <session> <socket> <reason>
# Emit a send_miss event to the caller bot's JSONL ledger (best-effort) plus a
# stderr breadcrumb, so a dropped cross-socket send becomes observable instead
# of silently swallowed. Internal to bot_tmux_send. The sending bot is the
# event's top-level "bot", resolved by emit_fleet_event from BOT_DIR / BOT_ID.
_tmux_send_miss() {
    local session="$1" socket="$2" reason="$3"
    local data
    data=$(printf '{"socket":"%s","session":"%s","reason":"%s"}' \
        "$(json_escape "$socket")" "$(json_escape "$session")" "$reason")
    emit_fleet_event send_miss dispatch "$data"
}

# bot_tmux_send <peer_socket> <session> <text>
# The ONE safe cross-socket send. Prechecks that <session> exists on
# <peer_socket>, sanitizes (sanitize_tmux_input), then hands off to
# pane_send_verified for the settle / Enter / verify-retry. On a miss — empty
# socket, or session absent
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
    pane_send_verified "$peer_socket" "$session" "$safe"
}

# --- verified pane send -------------------------------------------------------
#
# Default settle window (seconds) between the text keystroke and the Enter, so
# the TUI input buffer drains before the submit lands on top of it. Operators
# override at runtime via PANE_SEND_SETTLE_S (read per call, as the KEEPALIVE_*
# knobs are — never frozen at source time).
#
# 0.3, the value three of the four call sites used, rather than STARTUP_PROMPT's
# 0.5. A settle too short for a big payload is now RECOVERABLE — that is what the
# verify-retry below is for — where before it was silent and permanent, so the
# longer window has stopped earning its cost on every other send.
_PANE_SEND_SETTLE_DEFAULT=0.3
# Verify budget: how long to let the input box clear on its own before
# concluding the Enter was swallowed, as a poll interval x a tick count (both
# named, so the resulting budget is readable here rather than only derivable
# from the loop). Polled, not slept: a submit that lands on the first tick costs
# 0.2s and one capture-pane, where a fixed post-Enter sleep pays its full length
# every time. Override via PANE_SEND_VERIFY_TICKS.
#
# Honest accounting, since one number does not cover every caller: this is a net
# WIN on cold start (start-bot's two sends drop from 2.1s to 1.0s) and a small
# LOSS on the cross-socket dispatch path, which previously did not capture the
# pane at all (0.3s and 0 captures, now 0.5s and 1). That cost buys dispatch the
# retry — the stuck-payload failure that motivated this was observed on exactly
# that path, so exempting it to save 0.2s would exempt the reported bug.
_PANE_VERIFY_POLL_S=0.2
_PANE_SEND_VERIFY_TICKS_DEFAULT=5
# A send past a few hundred characters is rendered as a collapsed placeholder
# instead of the literal text, so no text probe can see an unsubmitted large
# payload. Matching the placeholder is what lets a stuck dispatch — the failure
# this helper exists for — be detected at all.
_PANE_PASTE_COLLAPSE_MARKER='[Pasted text'
# Longest usable probe. The input box wraps near the pane width, so a probe
# beyond this straddles a wrap point and cannot match any single rendered line
# even while the text IS sitting unsubmitted.
_PANE_PROBE_MAX_CHARS=60
# Prompt-glyph anchor. Alternation rather than a [>❯] bracket expression so the
# multibyte glyph stays one literal byte sequence under any locale — byte-safe by
# construction rather than by luck. (_IDLE_PATTERN_BASE below spells the same
# glyph set as a bracket; that form did NOT misbehave under LC_ALL=C when tested
# against these fixtures, so this is a consistency gap to collapse later, not a
# live defect — folding the two onto one glyph constant means touching a gated
# SSOT that every idle consumer reads, which does not belong in this change.)
_PANE_INPUT_GLYPH_RE='^[[:space:]]*(>|❯)'

# pane_input_region <pane_text>
# The input-box slice of a captured pane on stdout: the last prompt-glyph line
# through the end of the capture.
#
# Anchored to the glyph rather than taken at a fixed tail depth because the
# number of lines BELOW the input line is variable — box border, hint line and
# mode footer at rest, more while an agent tree is drawn — so no fixed depth
# reaches the input line in every state. A depth padded for the worst case
# instead reaches UP into the transcript, where a cleanly-submitted command is
# still visible, and would re-fire Enter at an already-idle prompt.
#
# Empty output when the pane has no prompt glyph at all: no prompt means nothing
# is sitting unsubmitted, so the caller must not retry.
#
# One awk pass, not grep|tail|cut to find the anchor plus a second serialization
# through sed to re-slice the same text: this runs per poll tick, and the
# cold-start bottleneck is CPU, not IO (documentation/runbooks/audit-cold-start-timing.md)
# — forks here land while every bot on the box is starting at once.
pane_input_region() {
    printf '%s\n' "$1" | awk -v re="$_PANE_INPUT_GLYPH_RE" '
        { line[NR] = $0; if ($0 ~ re) last = NR }
        END { if (last) for (i = last; i <= NR; i++) print line[i] }'
}

# pane_holds_unsubmitted <pane_text> <probe>
# Returns 0 when the input box still holds an unsubmitted payload: <probe> is
# visible inside it, or the box shows the collapsed-paste placeholder.
#
# Positive evidence only. A cleanly-submitted send leaves the box empty and
# matches neither, and a send that was QUEUED against a busy pane leaves only
# the TUI's own hint text there, which matches neither either — so the retry
# cannot fire on a send that actually landed.
pane_holds_unsubmitted() {
    local region
    region=$(pane_input_region "$1")
    [ -n "$region" ] || return 1
    # One grep, two literals — the alternative runs both patterns on every CLEAR
    # pane, which is the common case.
    printf '%s\n' "$region" | grep -qF -e "$2" -e "$_PANE_PASTE_COLLAPSE_MARKER"
}

# pane_send_verified <socket> <session> <text>
# THE verified pane send, and the one home for the send/settle/Enter/verify-retry
# dance: send <text>, let the buffer settle, send Enter, then poll the input box
# and re-send Enter once if the payload is still sitting there unsubmitted.
#
# Sends <text> VERBATIM — no sanitize pass, no `set +H;` prefix. THIS is why the
# slash-command sites cannot route through a sanitizing helper: a slash command
# must be the FIRST characters in the input or Claude Code will not recognise it.
# Callers that DO want sanitizing (the cross-socket dispatch path) sanitize first
# and hand the result down; see bot_tmux_send. Callers that want the `set +H;`
# history-expansion guard prepend it themselves; see start-bot.sh's STARTUP_PROMPT
# and dispatch.sh's classifier.
pane_send_verified() {
    local socket="${1?Usage: pane_send_verified <socket> <session> <text>}"
    local session="${2:?Usage: pane_send_verified <socket> <session> <text>}"
    local text="${3:?Usage: pane_send_verified <socket> <session> <text>}"
    local probe="${text:0:$_PANE_PROBE_MAX_CHARS}"

    bot_tmux "$socket" send-keys -t "$session" "$text" || return 1
    sleep "${PANE_SEND_SETTLE_S:-$_PANE_SEND_SETTLE_DEFAULT}"
    bot_tmux "$socket" send-keys -t "$session" Enter || return 1

    local tick=0 pane
    local ticks="${PANE_SEND_VERIFY_TICKS:-$_PANE_SEND_VERIFY_TICKS_DEFAULT}"
    # A zero budget means "do not verify", not "skip straight to the blind
    # resend" — without this the knob would invert, buying an operator who set it
    # to 0 a ghost Enter into an idle pane on every single send.
    [ "$ticks" -gt 0 ] || return 0
    while [ "$tick" -lt "$ticks" ]; do
        sleep "$_PANE_VERIFY_POLL_S"
        tick=$((tick + 1))
        pane=$(bot_tmux "$socket" capture-pane -t "$session" -p 2>/dev/null) || return 0
        pane_holds_unsubmitted "$pane" "$probe" || return 0
    done
    # Still at the input line after the whole budget — the TUI swallowed the
    # Enter during a render. Best-effort resend; a failure here is never fatal to
    # the caller (startup and watchdog paths must not abort on a stuck pane).
    #
    # Emit the retry, because a silent retry is how this verify shipped dead for
    # so long: with nothing in the ledger, "the retry never fires" and "the retry
    # cannot fire" look identical from outside. Distinct from send_miss — the
    # send DID reach the pane, so this must not read as a dropped dispatch to
    # fleet-pulse's escalation.
    emit_fleet_event send_retry dispatch \
        "$(printf '{"session":"%s","reason":"enter-swallowed"}' "$(json_escape "$session")")"
    bot_tmux "$socket" send-keys -t "$session" Enter 2>/dev/null || true
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

# Base busy-detection regex — single source of truth for keepalive.sh
# classify_pane and every "should I inject keystrokes?" consumer
# (sprint-trigger, bot-sweep-cron). "esc to interrupt" is drawn during ANY
# active turn and is stable across Claude Code releases and
# prefersReducedMotion; the churning verb lists (Thinking/Running/…) that
# consumers previously grepped silently degrade on UI changes and must not
# reappear (gate: tests/test_busy_ssot.py). Operators extend at runtime via
# KEEPALIVE_BUSY_PATTERNS.
_BUSY_PATTERN_BASE='[Ee]sc to interrupt'

# Default recency window (seconds) for the data/.last-tool-call liveness
# marker — one home, consumed by bot_is_busy and keepalive.sh so the two
# can never silently disagree about "recently active". Override per fleet
# via KEEPALIVE_ACTIVE_WINDOW_S.
_ACTIVE_WINDOW_DEFAULT=180

# pane_is_busy <pane_text>
# Returns 0 if the pane shows an active turn, 1 otherwise. Mirror of
# pane_is_idle. Operators extend via KEEPALIVE_BUSY_PATTERNS.
pane_is_busy() {
    local text="$1"
    local _busy_pattern="$_BUSY_PATTERN_BASE"
    if [ -n "${KEEPALIVE_BUSY_PATTERNS:-}" ]; then
        _busy_pattern="$_busy_pattern|$KEEPALIVE_BUSY_PATTERNS"
    fi
    printf '%s' "$text" | grep -qE "$_busy_pattern"
}

# bot_dir_for_session <session> [bots_dir]
# Session name -> bot runtime dir, on stdout — _session_candidate_dir (the
# resolution shared with tmux_socket_for_session) plus an existence gate,
# so marker-based checks can find a bot's data/ from just its session name.
# Returns 1 (empty stdout) when no fleet has such a bot; rc 2 on empty arg.
bot_dir_for_session() {
    local target
    target=$(_session_candidate_dir bot_dir_for_session "$@") || return $?
    [ -d "$target" ] || return 1
    printf '%s' "$target"
}

# bot_is_busy <socket> <session> [bot_dir]
# THE busy check for keystroke injectors. Marker-first: a data/.last-tool-call
# fresher than KEEPALIVE_ACTIVE_WINDOW_S (default _ACTIVE_WINDOW_DEFAULT)
# reads BUSY without touching tmux (rendering-immune, and the common case for
# a working bot). Fallback: capture the pane and apply pane_is_busy. bot_dir
# is resolved from the session name when not supplied; when unresolvable the
# pane alone decides.
# Returns 0 = busy (do NOT inject), 1 = not busy; rc 2 on empty session.
bot_is_busy() {
    local socket="${1:-}" session="${2:-}" bot_dir="${3:-}"
    if [ -z "$session" ]; then
        echo "bot_is_busy: empty session name" >&2
        return 2
    fi
    if [ -z "$bot_dir" ]; then
        bot_dir=$(bot_dir_for_session "$session" 2>/dev/null) || true
    fi
    if [ -n "$bot_dir" ] && marker_age_within "$bot_dir/data/.last-tool-call" "${KEEPALIVE_ACTIVE_WINDOW_S:-$_ACTIVE_WINDOW_DEFAULT}"; then
        return 0
    fi
    local pane
    pane=$(bot_tmux "$socket" capture-pane -t "$session" -p 2>/dev/null | tail -10) || true
    pane_is_busy "$pane"
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
#                 [recipient] [renotify_after_s]
# Fires the notification on first occurrence, then debounces. Caller clears the
# marker via debounce_clear when the condition resolves.
#
# <recipient> is an identity for WHO is being notified. The marker records it as
# its CONTENT, so a changed recipient re-fires: keying on <bot_id>.<suffix>
# alone recorded only THAT a notification fired, never to whom, so an alert
# fired once into whatever manager session existed at episode start and every
# later session was structurally unable to receive it (#831 — a manager restart
# mid-episode cost a 1.25-day dark-bot outage). Identity is the content and not
# part of the filename on purpose: one marker per (bot, suffix) cannot
# accumulate one-per-restart, and debounce_clear needs no glob.
#
# <renotify_after_s> re-fires once the marker ages past that many seconds (0 =
# never). It composes with, rather than replaces, the recipient check: identity
# cannot cover a send that failed silently or a reused pid, and a long episode
# should re-surface even to a recipient that never changed.
#
# Both are optional. With neither, this is the original fire-once-per-episode
# behavior, which is what an *action* debounce with no recipient wants
# (reload-fleet.sh's npx warm attempt).
debounce_notify() {
    local state_dir="$1" bot_id="$2" suffix="$3" notify_fn="$4" message="$5"
    local recipient="${6:-}" renotify_after="${7:-0}"
    local marker="$state_dir/${bot_id}.${suffix}"
    local fire=0 seen=""
    if [ ! -f "$marker" ]; then
        fire=1
    else
        seen=$(cat "$marker" 2>/dev/null || true)
        if [ -n "$recipient" ] && [ "$seen" != "$recipient" ]; then
            fire=1
        elif [ "$renotify_after" -gt 0 ] && ! marker_age_within "$marker" "$renotify_after"; then
            fire=1
        fi
    fi
    if [ "$fire" -eq 1 ]; then
        "$notify_fn" "$message"
        printf '%s' "$recipient" > "$marker"
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
    # `last_updated:` ISO-8601 UTC frontmatter field (written by /claudna:session handoff
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

# proc_rss_kb <pid>
# Sum resident-set size (KB) of a process and its direct children. Portable:
# `ps -A -o pid=,ppid=,rss=` is read by both GNU and BSD ps, replacing the
# GNU-only `ps --ppid <pid>` selector, which errors on BSD/macOS and silently
# yields 0. One level deep — self plus direct children — the shape a tmux pane
# presents (the pane shell plus its `claude` child).
proc_rss_kb() {
    local root="${1:?Usage: proc_rss_kb <pid>}"
    ps -A -o pid=,ppid=,rss= 2>/dev/null \
        | awk -v r="$root" '$1 == r || $2 == r { s += $3 } END { print s + 0 }'
}

# --- Fleet path resolution ---------------------------------------------------

# resolve_fleet_dir <fleet> — echo the fleet overlay dir, flat OR nested.
# Flat local/<fleet>/ wins (byte-identical to pre-nesting). Else the unique
# local/<system>/<fleet>/ carrying a fleet.yaml (one level under a container).
# Marker-agnostic: a fleet is a dir with fleet.yaml. Empty output + nonzero if
# none. The bash twin of Python paths._find_fleet_dir — the ONE home for the
# flat-vs-nested rule every supervision path routes through.
resolve_fleet_dir() {
    local fleet="$1" root="${CLAUDLOBBY_ROOT:?}" flat d
    flat="$root/local/$fleet"
    # Flat wins first — byte-identical: a bare dir resolves (scaffolding relies on it).
    if [ -d "$flat" ]; then printf '%s\n' "$flat"; return 0; fi
    # Nested: the unique local/<system>/<fleet>/ that carries a fleet.yaml.
    local match="" n=0
    for d in "$root"/local/*/"$fleet"; do
        [ -f "$d/fleet.yaml" ] || continue
        match="$d"; n=$((n+1))
    done
    if [ "$n" -eq 1 ]; then printf '%s\n' "$match"; return 0; fi
    return 1   # none, or ambiguous (F5 — caller decides; keep flat-first semantics)
}

# shellcheck disable=SC2120  # fleet arg is optional by design (env fallback);
# tmux_socket_for_session calls it argless, other-file callers pass a fleet.
resolve_bots_dir() {
    # Resolve the runtime/bots directory for a fleet.
    # Usage: BOTS_DIR=$(resolve_bots_dir [fleet-name])
    # Falls back to CLAUDLOBBY_FLEET / FLEET_NAME env vars, then root-mode runtime/bots.
    local fleet="${1:-${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}}"
    local fleet_dir
    if [ -n "$fleet" ]; then
        # resolve_fleet_dir returns local/<fleet> for flat (byte-identical) or the
        # nested local/<system>/<fleet>; flat fallback keeps the pre-create path.
        fleet_dir=$(resolve_fleet_dir "$fleet") || fleet_dir="$CLAUDLOBBY_ROOT/local/$fleet"
        printf '%s' "$fleet_dir/runtime/bots"
    else
        printf '%s' "$CLAUDLOBBY_ROOT/runtime/bots"
    fi
}

# fleet_runtime_dir
# Directory for fleet-scoped runtime state (report-back.jsonl, workstreams.json):
# overlay local/<fleet>/runtime, else root runtime/fleet. The bash twin of
# Paths.fleet_state — the one home for this overlay-vs-root rule.
# Usage: DIR=$(fleet_runtime_dir [fleet-name])
fleet_runtime_dir() {
    local fleet="${1:-${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}}"
    local fleet_dir
    if [ -n "$fleet" ]; then
        fleet_dir=$(resolve_fleet_dir "$fleet") || fleet_dir="$CLAUDLOBBY_ROOT/local/$fleet"
        printf '%s' "$fleet_dir/runtime"
    else
        printf '%s' "$CLAUDLOBBY_ROOT/runtime/fleet"
    fi
}

# dispatch_ledger_path
# The manager-written dispatch ledger, on stdout. Host-global (one file per
# CLAUDLOBBY_ROOT), unlike the per-fleet report ledger fleet_runtime_dir locates.
# One home because the writer (dispatch-task.sh) and both readers (fleet-pulse's
# watchdog, report-back's open-dispatch resolver) must agree byte-for-byte: a
# resolver reading a different file than the watchdog would re-resolve rows the
# watchdog still considers open. Self-locating fallback so a caller with no
# CLAUDLOBBY_ROOT exported still resolves the install it was invoked from.
dispatch_ledger_path() {
    local root="${CLAUDLOBBY_ROOT:-}"
    [ -n "$root" ] || root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    printf '%s' "$root/state/dispatch-log.jsonl"
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
    # Nested vault: a fleet under a system container is one level deeper. The
    # [ -d ] guard drops the literal glob when nothing matches (flat = additive).
    for d in "$CLAUDLOBBY_ROOT"/local/*/*/runtime/bots; do
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

# service_is_active <bot_service>
# rc 0 iff the bot's supervised service is active; non-zero == confirmed down
# (the actionable service_down signal the caller alerts on). Cross-platform sibling of
# bot_unit_present (which tests unit-FILE presence — this tests LIVE state), so
# fleet-pulse detection and its summary column share one OS-dispatch and cannot
# drift. Linux: `systemctl --user is-active` (non-zero for inactive/failed).
# macOS: `launchctl print gui/<uid>/<label>` — BOT_SERVICE is the launchd Label
# verbatim (composer sets the plist Label to the same string); a non-zero print
# means the job is not bootstrapped. Presence-based, mirroring status.py
# _check_launchd_service (a loaded KeepAlive agent reads active). _OS is
# Linux|Darwin by construction (detect_os); an unrecognized OS cannot prove down,
# so it reports active — the alert path never pages a host it cannot supervise.
service_is_active() {
    local svc="${1:?Usage: service_is_active <bot_service>}"
    case "$_OS" in
    Linux) systemctl --user is-active "$svc" >/dev/null 2>&1 ;;
    Darwin) launchctl print "gui/$(id -u)/$svc" >/dev/null 2>&1 ;;
    *) return 0 ;;
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
        fleet_dir=$(resolve_fleet_dir "$fleet") || fleet_dir="$CLAUDLOBBY_ROOT/local/$fleet"
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
# An absent var is a normal state (empty output, exit 0): without the explicit
# return, grep's no-match status becomes the pipeline's under pipefail and the
# $(...) assignment call sites abort strict callers — same class as #610.
extract_bot_conf_var() {
    local conf_file="$1" var_name="$2"
    grep -m1 "^export ${var_name}=" "$conf_file" | cut -d= -f2- | tr -d "'"
    return 0
}

# --- Script error events ------------------------------------------------------

# emit_script_error <bot_dir> <script_name> <exit_code> <message>
# Write a script_error event to the bot's JSONL event log.
# For scripts that run outside a bot context, pass "" for bot_dir and
# the event is written to $CLAUDLOBBY_ROOT/state/events/.
emit_script_error() {
    local bot_dir="$1" script_name="$2" exit_code="$3" message="$4"
    local data
    data=$(printf '{"script":"%s","exit_code":%d,"message":"%s"}' \
        "$script_name" "$exit_code" "$(json_escape "$message")")
    # Pass bot_id explicitly (bot_dir's own basename) so a bot-context error
    # attributes to that dir — never the ambient $BOT_ID of whatever installed
    # the trap (a manager script or shell trapping a different bot's dir). An
    # empty bot_dir yields an empty bot_id, so the primitive's fleet-level branch
    # attributes it to "fleet".
    emit_fleet_event script_error lib "$data" "$bot_dir" "${bot_dir:+$(basename "$bot_dir")}"
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

    local data
    data=$(printf '{"reason":"%s"}' "$(json_escape "$reason")")
    emit_fleet_event "$event_type" "$ev_source" "$data" "" fleet

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

    # Telegram (loudest channel) — the one shared fleet-alert target resolver, so
    # this path, fleet-pulse escalation, and creds-check all pick the same chat-id.
    local chat_id state_dir
    resolve_alert_target "$bots_dir"
    # shellcheck disable=SC2154  # _alert_* are set by resolve_alert_target above
    chat_id="$_alert_chat_id"; state_dir="$_alert_state_dir"
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
# Set an ERR trap — arming `set -E` so it is inherited by shell functions — that
# emits a script_error event on non-zero exit.
# Call after sourcing lib-common.sh and resolving the bot directory.
# Pass "" for fleet-level scripts that run outside a bot context.
# NOTE: does NOT replace existing EXIT traps — only fires on ERR.
#
# errtrace is armed here rather than left to callers: a bare ERR trap is not
# inherited by shell functions, and lib/ does nearly all its work in functions,
# so without it the trap instruments top-level failures only — the minority of
# the surface it exists to cover (#844). Arming it at the single call site means
# no caller of this helper can forget it. That binds callers only: a script that
# hand-rolls its own bare `trap … ERR` instead of calling this is not covered and
# still has the #844 defect. Safe to arm because
# errtrace is control-flow neutral (it changes only whether the trap runs, never
# whether a script aborts) and deliberate tolerance stays silent: bash suppresses
# the ERR trap in the same contexts it suppresses errexit — `f || true`, `if f`,
# `f && g` — and that suppression is inherited by callees.
#
# The handler's stdout is discarded because under errtrace the trap fires INSIDE
# the failing command substitution, so anything it printed would be captured as
# the caller's value — `local v=$(fn)` silently becoming the handler's output,
# with no error and no row. The event write is its own redirect to the ledger and
# is unaffected.
install_error_trap() {
    set -E
    local _err_bot_dir="$1"
    local _err_script
    _err_script=$(basename "$0")
    trap 'emit_script_error "'"$_err_bot_dir"'" "'"$_err_script"'" "$?" "non-zero exit at line $LINENO" >/dev/null' ERR
}

# bot_conf_get <bot_dir> <key> <default>
# Read a single variable from a bot's bot.conf without sourcing the file
# (no side effects on the caller's environment). Handles both `export VAR=val`
# and plain `VAR=val` forms. Strips one layer of surrounding single OR double
# quotes (the composer emits values via shlex.quote, which single-quotes any
# value containing a space, e.g. a multi-plugin FLEET_PLUGINS_REQUIRED).
# Returns <default> if the file is missing or the key isn't found.
bot_conf_get() {
    local bot_dir="$1" key="$2" default="$3" val=""
    if [ -f "$bot_dir/bot.conf" ]; then
        val=$(grep "^\(export \)\?$key=" "$bot_dir/bot.conf" | head -1 \
            | sed -E "s/^(export )?$key=//" || true)
        # Strip a surrounding quote pair (single or double) via parameter
        # expansion, kept outside the command substitution above so no literal
        # quote sits inside $( ) where bash 3.2 mis-scans it.
        case "$val" in
            \"*\") val=${val#\"}; val=${val%\"} ;;
            \'*\') val=${val#\'}; val=${val%\'} ;;
        esac
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
    # Nested vault: a fleet under a system container is one level deeper.
    # first_bot_with_conf guards a nonexistent dir, so the literal glob is inert.
    for d in "$CLAUDLOBBY_ROOT"/local/*/*/runtime/bots; do
        [ "$d" = "$bots_dir" ] && continue
        if first_bot_with_conf "$d" "$key"; then
            return 0
        fi
    done
    return 1
}

# resolve_alert_target [bots_dir]
# THE single fleet-alert delivery-target resolver. Sets _alert_chat_id and
# _alert_state_dir (either may be empty) so every env-less fleet-alert path —
# fleet-pulse escalation, _emit_fleet_signal, and creds-check — resolves the
# SAME Telegram chat-id from one precedence:
#   1. FLEET_PULSE_ESCALATION_CHAT_ID  — operator override (route alerts anywhere)
#   2. TELEGRAM_GROUP_CHAT_ID (env)    — the FLEET-level value the composer bakes
#      into every fleet timer unit; preferred over the bot.conf scan so a per-bot
#      chat_id override never hijacks a fleet-wide alert
#   3. bot.conf scan for TELEGRAM_GROUP_CHAT_ID — for host jobs with no composed
#      env. scan_scope "any" (default) is cross-fleet (host-scope callers run
#      fleet-less); "fleet" restricts to <bots_dir> so a fleet-scoped caller
#      neither pages another fleet's channel nor loses its no-receiver warning.
# The state dir is resolved INDEPENDENTLY of the chat-id branch: the composed
# timer env carries the chat-id but NOT TELEGRAM_STATE_DIR, and tg-post reads the
# delivery token from that dir — so an env-supplied chat-id still scans a
# declaring bot for its live channel dir, else delivery falls to tg-post's dead
# default dir. Outputs via globals (bash 3.2 has no namerefs; mirrors detect_os).
resolve_alert_target() {
    local bots_dir="${1:-}" scan_scope="${2:-any}" _bot
    _alert_chat_id=""
    _alert_state_dir="${TELEGRAM_STATE_DIR:-}"
    if [ -n "${FLEET_PULSE_ESCALATION_CHAT_ID:-}" ]; then
        _alert_chat_id="$FLEET_PULSE_ESCALATION_CHAT_ID"
    elif [ -n "${TELEGRAM_GROUP_CHAT_ID:-}" ]; then
        _alert_chat_id="$TELEGRAM_GROUP_CHAT_ID"
    fi
    # Scan a declaring bot when the chat-id is still unresolved, OR to source a
    # live channel state dir the env did not carry (tg-post's token lives there).
    if [ -z "$_alert_chat_id" ] || [ -z "$_alert_state_dir" ]; then
        if [ "$scan_scope" = "fleet" ]; then
            _bot=$(first_bot_with_conf "$bots_dir" TELEGRAM_GROUP_CHAT_ID 2>/dev/null || true)
        else
            _bot=$(first_bot_with_conf_any_fleet "$bots_dir" TELEGRAM_GROUP_CHAT_ID 2>/dev/null || true)
        fi
        if [ -n "$_bot" ]; then
            [ -n "$_alert_chat_id" ] || _alert_chat_id=$(bot_conf_get "$_bot" TELEGRAM_GROUP_CHAT_ID "")
            [ -n "$_alert_state_dir" ] || _alert_state_dir=$(bot_conf_get_path "$_bot" TELEGRAM_STATE_DIR "")
        fi
    fi
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

# --- Test-harness assertion --------------------------------------------------

# harness_check "<desc>" "<yes|no>" — PASS/FAIL assertion for the lib/ rehearsal
# and gate harnesses. Increments the caller's ambient `pass`/`fail` counters
# (each harness sets `pass=0; fail=0` before its check block); kept ambient by
# design so the run summary stays in caller scope.
harness_check() {
    if [ "$2" = yes ]; then
        pass=$((pass + 1)); printf '  PASS  %s\n' "$1"
    else
        fail=$((fail + 1)); printf '  FAIL  %s\n' "$1"
    fi
}

# --- Fleet event-ledger retention -------------------------------------------

# reap_event_files <events_dir> <name_glob> <reap_days> — delete JSONL event
# files older than <reap_days>. The caller resolves <reap_days> in its own
# context (process env, bot.conf, or a script-specific override) and passes it
# in; only the find shape lives here. No-op when <events_dir> is absent.
reap_event_files() {
    local events_dir="$1" name_glob="$2" reap_days="$3"
    [ -d "$events_dir" ] || return 0
    find "$events_dir" -name "$name_glob" -type f -mtime +"$reap_days" -delete 2>/dev/null || true
}
