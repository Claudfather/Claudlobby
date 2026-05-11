#!/bin/bash
# Single-entry-point Mac mini setup for a claudlobby fleet host.
#
# Idempotent: detects what's already installed and only does the missing
# work. Re-running is safe (and cheap — `brew list` checks are fast).
#
# Usage:
#   lib/setup-mac-mini.sh --fleet <name>           # prereqs + agents
#   lib/setup-mac-mini.sh --fleet <name> --with-data    # also install neonctl/railway/vercel/dbt
#   lib/setup-mac-mini.sh --fleet <name> --enroll-all   # also install LaunchAgents for every bot
#   lib/setup-mac-mini.sh --fleet <name> --dry-run      # report what would change, do nothing
#
# Phases (each no-ops if its target is already in place):
#   1. preflight      — macOS check, $CLAUDLOBBY_ROOT resolution
#   2. homebrew       — brew + core CLI tools
#   3. claude         — claude binary + Telegram plugin
#   4. data-clis      — optional, gated on --with-data
#   5. env-shared     — verify $CLAUDLOBBY_ROOT/.env present
#   6. keepalive      — install-keepalive.sh
#   7. creds-check    — install-creds-check.sh (only if .env present)
#   8. bot-enroll     — list / optionally enroll bots from runtime/bots/
#   9. report         — summary matrix + next steps

set -euo pipefail

FLEET=""
WITH_DATA=0
ENROLL_ALL=0
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --fleet)      FLEET="${2:-}"; shift 2 ;;
        --with-data)  WITH_DATA=1; shift ;;
        --enroll-all) ENROLL_ALL=1; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "setup: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

if [ -z "$FLEET" ]; then
    FLEET="${CLAUDLOBBY_FLEET:-}"
fi
if [ -z "$FLEET" ]; then
    echo "setup: pass --fleet <name> or set CLAUDLOBBY_FLEET" >&2
    exit 2
fi

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib-common.sh
. "$SCRIPT_DIR/lib-common.sh"
FLEET_DIR="$CLAUDLOBBY_ROOT/local/$FLEET"
RUNTIME_BOTS="$FLEET_DIR/runtime/bots"

log()  { printf 'setup: %s\n' "$*"; }
ok()   { printf 'setup: \xe2\x9c\x93 %s\n' "$*"; }   # ✓
miss() { printf 'setup: \xe2\x97\x8b %s\n' "$*"; }   # ○
run()  {
    if [ "$DRY_RUN" = 1 ]; then
        printf 'setup: [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

PREREQ_OK=()
PREREQ_MISSING=()
NEXT_STEPS=()

# ---------------------------------------------------------------------------
# 1. preflight
# ---------------------------------------------------------------------------
phase_preflight() {
    log "phase 1/9: preflight"
    if [ "$_OS" != "Darwin" ]; then
        echo "setup: this script is macOS-only (saw uname=$_OS)" >&2
        exit 1
    fi
    if [ ! -d "$CLAUDLOBBY_ROOT" ]; then
        echo "setup: claudlobby checkout not found at $CLAUDLOBBY_ROOT" >&2
        echo "setup: clone the repo first, or set CLAUDLOBBY_ROOT to its location" >&2
        exit 1
    fi
    if [ ! -d "$FLEET_DIR" ]; then
        echo "setup: fleet '$FLEET' not found at $FLEET_DIR" >&2
        echo "setup: scaffold one with: claudlobby --fleet $FLEET new-bot ..." >&2
        exit 1
    fi
    ok "macOS host, $CLAUDLOBBY_ROOT, fleet=$FLEET"
}

# ---------------------------------------------------------------------------
# 2. homebrew + core tools
# ---------------------------------------------------------------------------
ensure_brew_pkg() {
    local pkg="$1"
    if brew list --formula "$pkg" >/dev/null 2>&1; then
        ok "brew $pkg already installed"
        PREREQ_OK+=("$pkg")
    else
        miss "brew $pkg missing — installing"
        run "brew install --quiet $pkg"
        PREREQ_OK+=("$pkg")
    fi
}

phase_homebrew() {
    log "phase 2/9: homebrew + core CLI tools"
    if ! command -v brew >/dev/null 2>&1; then
        miss "homebrew missing — installing"
        run '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    else
        ok "homebrew already installed"
    fi

    for pkg in node tmux jq gh bun uv; do
        ensure_brew_pkg "$pkg"
    done
}

# ---------------------------------------------------------------------------
# 3. claude CLI + Telegram plugin
# ---------------------------------------------------------------------------
phase_claude() {
    log "phase 3/9: claude CLI + Telegram plugin"
    local claude=""
    if [ -x "$HOME/.local/bin/claude" ]; then
        claude="$HOME/.local/bin/claude"
    elif [ -x "$_HOMEBREW/bin/claude" ]; then
        claude="$_HOMEBREW/bin/claude"
    elif claude="$(command -v claude 2>/dev/null)" && [ -n "$claude" ]; then
        :
    else
        claude=""
    fi

    if [ -z "$claude" ]; then
        miss "claude CLI missing — installing via npm"
        run "npm install -g @anthropic-ai/claude-code"
        NEXT_STEPS+=("Run \`claude /login\` once to authenticate (OAuth, browser)")
        PREREQ_MISSING+=("claude (login required)")
        return
    fi

    ok "claude found at $claude"
    PREREQ_OK+=("claude")

    if "$claude" plugin list 2>/dev/null | grep -qi telegram; then
        ok "telegram plugin already installed"
    else
        miss "telegram plugin missing — installing"
        run "$claude plugin install telegram@claude-plugins-official"
    fi
}

# ---------------------------------------------------------------------------
# 4. optional data-fleet CLIs
# ---------------------------------------------------------------------------
phase_data_clis() {
    if [ "$WITH_DATA" != 1 ]; then
        log "phase 4/9: data CLIs (skipped — pass --with-data to enable)"
        return
    fi
    log "phase 4/9: data CLIs"
    for cli in railway neonctl vercel; do
        if command -v "$cli" >/dev/null 2>&1; then
            ok "$cli already installed"
        else
            miss "$cli missing — installing"
            case "$cli" in
                railway) run "npm install -g @railway/cli" ;;
                *)       run "npm install -g $cli" ;;
            esac
        fi
    done

    if command -v dbt >/dev/null 2>&1; then
        ok "dbt already installed"
    else
        miss "dbt missing — installing via uv (Python 3.12 pin)"
        run "uv tool install --python 3.12 dbt-core --with dbt-snowflake"
    fi
}

# ---------------------------------------------------------------------------
# 5. .env check
# ---------------------------------------------------------------------------
ENV_PRESENT=0
phase_env_shared() {
    log "phase 5/9: .env check"
    if [ -f "$CLAUDLOBBY_ROOT/.env" ]; then
        ok "$CLAUDLOBBY_ROOT/.env present"
        ENV_PRESENT=1
    else
        miss "$CLAUDLOBBY_ROOT/.env missing — creds-check agent will be skipped"
        NEXT_STEPS+=("Create $CLAUDLOBBY_ROOT/.env with the env vars referenced by your bot.conf and lib/creds-check.sh (e.g. GITHUB_PERSONAL_ACCESS_TOKEN, RAILWAY_API_TOKEN, MCP_PROBE_URL/TOKEN)")
    fi
}

# ---------------------------------------------------------------------------
# 6. keepalive LaunchAgent
# ---------------------------------------------------------------------------
phase_keepalive() {
    log "phase 6/9: keepalive LaunchAgent"
    run "$SCRIPT_DIR/install-keepalive.sh $FLEET"
}

# ---------------------------------------------------------------------------
# 7. creds-check LaunchAgent
# ---------------------------------------------------------------------------
phase_creds_check() {
    log "phase 7/9: creds-check LaunchAgent"
    if [ "$ENV_PRESENT" != 1 ]; then
        miss "skipped — .env not present"
        return
    fi
    run "$SCRIPT_DIR/install-creds-check.sh $FLEET"
}

# ---------------------------------------------------------------------------
# 8. bot enrollment
# ---------------------------------------------------------------------------
ENROLLED=()
UNENROLLED=()
phase_bot_enroll() {
    log "phase 8/9: bot enrollment"
    if [ ! -d "$RUNTIME_BOTS" ]; then
        miss "no runtime/bots dir at $RUNTIME_BOTS — run 'claudlobby --fleet $FLEET generate' first"
        NEXT_STEPS+=("Run \`claudlobby --fleet $FLEET generate\` to compose bots, then re-run this script")
        return
    fi

    local agents="$HOME/Library/LaunchAgents"
    for conf in "$RUNTIME_BOTS"/*/bot.conf; do
        [ -f "$conf" ] || continue
        local bot_dir bot_name plist
        bot_dir="$(dirname "$conf")"
        bot_name="$(basename "$bot_dir")"
        plist=$(find "$agents" -maxdepth 1 -name "*.$bot_name.plist" 2>/dev/null | head -1)
        if [ -n "$plist" ]; then
            ok "$bot_name enrolled"
            ENROLLED+=("$bot_name")
        elif [ "$ENROLL_ALL" = 1 ]; then
            miss "$bot_name not enrolled — installing"
            run "$SCRIPT_DIR/install-bot.sh $bot_dir"
            ENROLLED+=("$bot_name")
        else
            miss "$bot_name not enrolled (run: lib/install-bot.sh $bot_dir)"
            UNENROLLED+=("$bot_name")
        fi
    done

    if [ "${#UNENROLLED[@]}" -gt 0 ] && [ "$ENROLL_ALL" != 1 ]; then
        NEXT_STEPS+=("Populate .env / .mcp.json for each unenrolled bot, then run lib/install-bot.sh \$BOT_DIR (or re-run with --enroll-all)")
    fi
}

# ---------------------------------------------------------------------------
# 9. report
# ---------------------------------------------------------------------------
phase_report() {
    log "phase 9/9: summary"
    echo
    echo "  fleet:            $FLEET"
    echo "  prereqs ok:       ${PREREQ_OK[*]:-(none)}"
    echo "  prereqs missing:  ${PREREQ_MISSING[*]:-(none)}"
    echo "  bots enrolled:    ${ENROLLED[*]:-(none)}"
    echo "  bots unenrolled:  ${UNENROLLED[*]:-(none)}"
    echo "  .env:             $([ "$ENV_PRESENT" = 1 ] && echo present || echo missing)"
    if [ "${#NEXT_STEPS[@]}" -gt 0 ]; then
        echo
        echo "  next steps:"
        for step in "${NEXT_STEPS[@]}"; do
            echo "    - $step"
        done
    fi
    echo
    if [ "$DRY_RUN" = 1 ]; then
        log "dry-run complete — no changes made"
    else
        log "setup complete"
    fi
}

phase_preflight
phase_homebrew
phase_claude
phase_data_clis
phase_env_shared
phase_keepalive
phase_creds_check
phase_bot_enroll
phase_report
