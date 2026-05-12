#!/bin/bash
# Cross-platform host setup for a claudlobby fleet host.
#
# Idempotent: detects what's already installed and only does the missing
# work. Re-running is safe.
#
# Usage:
#   lib/setup-host.sh                 # install prereqs for any fleet
#   lib/setup-host.sh --dry-run       # report what would change, do nothing
#   lib/setup-host.sh --help          # show this usage text
#
# Supported hosts:
#   - Raspberry Pi 5 (Debian bookworm arm64)
#   - macOS Sonoma+ (arm64 / x86_64)
#
# Phases (each no-ops if its target is already in place):
#   1. preflight      — OS/arch detection, repo root check
#   2. packages       — package manager + core CLI tools (tmux, jq, gh)
#   3. node           — node + npm; note bun availability
#   4. python         — python3 3.10+ check, pip install claudlobby
#   5. claude         — claude CLI + authentication check
#   6. plugins        — Telegram plugin + Claudfather plugin
#   7. systemd        — Linux only: linger + XDG_RUNTIME_DIR
#   8. report         — summary matrix + next steps

set -euo pipefail

DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "setup-host: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

# Resolve repo root and source shared helpers
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT
# shellcheck source=lib-common.sh
. "$SCRIPT_DIR/lib-common.sh"

log()  { printf 'setup-host: %s\n' "$*"; }
ok()   { printf 'setup-host: \xe2\x9c\x93 %s\n' "$*"; }   # checkmark
miss() { printf 'setup-host: \xe2\x97\x8b %s\n' "$*"; }   # circle
run()  {
    if [ "$DRY_RUN" = 1 ]; then
        printf 'setup-host: [dry-run] would run: %s\n' "$*"
    else
        "$@"
    fi
}

PREREQ_OK=()
PREREQ_MISSING=()
NEXT_STEPS=()

# ---------------------------------------------------------------------------
# 1. preflight — OS/arch detection, repo root check
# ---------------------------------------------------------------------------
phase_preflight() {
    log "phase 1/8: preflight"
    local arch
    arch="$(uname -m)"
    if [ "$_OS" != "Darwin" ] && [ "$_OS" != "Linux" ]; then
        echo "setup-host: unsupported OS (uname=$_OS). Supported: Linux, Darwin." >&2
        exit 1
    fi
    if [ ! -d "$CLAUDLOBBY_ROOT" ]; then
        echo "setup-host: claudlobby checkout not found at $CLAUDLOBBY_ROOT" >&2
        echo "setup-host: clone the repo first, or set CLAUDLOBBY_ROOT to its location" >&2
        exit 1
    fi
    if [ ! -f "$CLAUDLOBBY_ROOT/lib/lib-common.sh" ]; then
        echo "setup-host: $CLAUDLOBBY_ROOT does not look like a claudlobby repo (missing lib/lib-common.sh)" >&2
        exit 1
    fi
    ok "$_OS $arch, CLAUDLOBBY_ROOT=$CLAUDLOBBY_ROOT"
}

# ---------------------------------------------------------------------------
# 2. packages — package manager + core CLI tools
# ---------------------------------------------------------------------------
ensure_apt_pkg() {
    local pkg="$1"
    if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        ok "apt $pkg already installed"
        PREREQ_OK+=("$pkg")
    else
        miss "apt $pkg missing — installing"
        run sudo apt-get install -y -qq "$pkg"
        PREREQ_OK+=("$pkg")
    fi
}

ensure_brew_pkg() {
    local pkg="$1"
    if brew list --formula "$pkg" >/dev/null 2>&1; then
        ok "brew $pkg already installed"
        PREREQ_OK+=("$pkg")
    else
        miss "brew $pkg missing — installing"
        run brew install --quiet "$pkg"
        PREREQ_OK+=("$pkg")
    fi
}

phase_packages() {
    log "phase 2/8: package manager + core CLI tools"
    if [ "$_OS" = "Linux" ]; then
        # Ensure apt cache is reasonably fresh
        if [ "$DRY_RUN" != 1 ]; then
            sudo apt-get update -qq
        fi
        for pkg in tmux jq curl; do
            ensure_apt_pkg "$pkg"
        done
        # gh (GitHub CLI) — may need dedicated repo on Debian
        if command -v gh >/dev/null 2>&1; then
            ok "gh already installed"
            PREREQ_OK+=("gh")
        else
            miss "gh missing — installing"
            if [ "$DRY_RUN" != 1 ]; then
                # Install gh from official apt repo
                sudo mkdir -p -m 755 /etc/apt/keyrings
                curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
                sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
                echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli-stable.list >/dev/null
                sudo apt-get update -qq
                sudo apt-get install -y -qq gh
            else
                printf 'setup-host: [dry-run] would run: install gh from GitHub apt repo\n'
            fi
            PREREQ_OK+=("gh")
        fi
    elif [ "$_OS" = "Darwin" ]; then
        if ! command -v brew >/dev/null 2>&1; then
            miss "homebrew missing — installing"
            run /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        else
            ok "homebrew already installed"
        fi
        for pkg in tmux jq gh; do
            ensure_brew_pkg "$pkg"
        done
    fi
}

# ---------------------------------------------------------------------------
# 3. node — check for node; install if missing; note bun
# ---------------------------------------------------------------------------
phase_node() {
    log "phase 3/8: node"
    if command -v node >/dev/null 2>&1; then
        local node_version
        node_version="$(node --version 2>/dev/null || echo 'unknown')"
        ok "node $node_version already installed"
        PREREQ_OK+=("node")
    else
        miss "node missing — installing"
        if [ "$_OS" = "Linux" ]; then
            run sudo apt-get install -y -qq nodejs npm
        elif [ "$_OS" = "Darwin" ]; then
            run brew install --quiet node
        fi
        PREREQ_OK+=("node")
    fi

    # Note bun availability (informational only)
    if command -v bun >/dev/null 2>&1; then
        local bun_version
        bun_version="$(bun --version 2>/dev/null || echo 'unknown')"
        ok "bun $bun_version available"
    else
        log "bun not found (optional — not installing)"
    fi
}

# ---------------------------------------------------------------------------
# 4. python — ensure python3 3.10+, install claudlobby package
# ---------------------------------------------------------------------------
phase_python() {
    log "phase 4/8: python"
    if ! command -v python3 >/dev/null 2>&1; then
        miss "python3 not found"
        PREREQ_MISSING+=("python3")
        NEXT_STEPS+=("Install Python 3.10+ manually, then re-run this script")
        return
    fi

    local py_version py_major py_minor
    py_version="$(python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
    py_major="$(echo "$py_version" | cut -d. -f1)"
    py_minor="$(echo "$py_version" | cut -d. -f2)"

    if [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 10 ]; }; then
        miss "python3 $py_version found — 3.10+ required"
        PREREQ_MISSING+=("python3 (need 3.10+, have $py_version)")
        NEXT_STEPS+=("Upgrade Python to 3.10+ manually, then re-run this script")
        return
    fi

    ok "python3 $py_version"
    PREREQ_OK+=("python3")

    # Install claudlobby package
    log "installing claudlobby package via pip"
    if [ "$DRY_RUN" != 1 ]; then
        python3 -m pip install -e "$CLAUDLOBBY_ROOT" --quiet 2>&1 || {
            miss "pip install claudlobby failed — see output above"
            PREREQ_MISSING+=("claudlobby-pip")
            NEXT_STEPS+=("Run 'pip install -e $CLAUDLOBBY_ROOT' manually to debug")
            return
        }
    else
        printf 'setup-host: [dry-run] would run: python3 -m pip install -e %s\n' "$CLAUDLOBBY_ROOT"
    fi
    ok "claudlobby package installed"
    PREREQ_OK+=("claudlobby-pip")
}

# ---------------------------------------------------------------------------
# 5. claude — CLI check + auth status
# ---------------------------------------------------------------------------
phase_claude() {
    log "phase 5/8: claude CLI"
    local claude_bin=""
    if [ -x "$HOME/.local/bin/claude" ]; then
        claude_bin="$HOME/.local/bin/claude"
    elif [ -n "${_HOMEBREW:-}" ] && [ -x "$_HOMEBREW/bin/claude" ]; then
        claude_bin="$_HOMEBREW/bin/claude"
    elif claude_bin="$(command -v claude 2>/dev/null)" && [ -n "$claude_bin" ]; then
        :
    else
        claude_bin=""
    fi

    if [ -z "$claude_bin" ]; then
        miss "claude CLI missing — installing via npm"
        run npm install -g @anthropic-ai/claude-code
        NEXT_STEPS+=("Run 'claude /login' once to authenticate (OAuth, browser)")
        PREREQ_MISSING+=("claude (login required)")
        return
    fi

    ok "claude found at $claude_bin"
    PREREQ_OK+=("claude")

    # Check auth status if the command supports it
    if "$claude_bin" auth status >/dev/null 2>&1; then
        ok "claude auth: authenticated"
    else
        miss "claude auth: not authenticated or check unavailable"
        NEXT_STEPS+=("Run 'claude /login' to authenticate (OAuth, browser)")
    fi
}

# ---------------------------------------------------------------------------
# 6. plugins — Telegram + Claudfather
# ---------------------------------------------------------------------------
phase_plugins() {
    log "phase 6/8: plugins"
    local claude_bin=""
    claude_bin="$(command -v claude 2>/dev/null)" || true
    if [ -z "$claude_bin" ]; then
        miss "claude CLI not available — skipping plugin install"
        return
    fi

    local plugin_list=""
    plugin_list="$("$claude_bin" plugin list 2>/dev/null)" || true

    # Telegram plugin
    if echo "$plugin_list" | grep -qi telegram; then
        ok "telegram plugin already installed"
    else
        miss "telegram plugin missing — installing"
        run "$claude_bin" plugin install "telegram@claude-plugins-official"
    fi

    # Claudfather marketplace + plugin
    if echo "$plugin_list" | grep -qi claudna; then
        ok "claudna plugin already installed"
    else
        miss "claudna plugin missing — adding marketplace + installing"
        if [ "$DRY_RUN" != 1 ]; then
            "$claude_bin" plugin marketplace add Claudfather --source github --repo Claudfather/clauDNA 2>/dev/null || true
            "$claude_bin" plugin install "claudna@Claudfather" 2>/dev/null || {
                miss "claudna plugin install failed — may need manual install"
                NEXT_STEPS+=("Run 'claude plugin marketplace add Claudfather --source github --repo Claudfather/clauDNA && claude plugin install claudna@Claudfather' manually")
                return
            }
        else
            printf 'setup-host: [dry-run] would run: claude plugin marketplace add Claudfather + install claudna\n'
        fi
        ok "claudna plugin installed"
    fi
}

# ---------------------------------------------------------------------------
# 7. systemd — Linux only: linger + XDG_RUNTIME_DIR
# ---------------------------------------------------------------------------
phase_systemd() {
    if [ "$_OS" != "Linux" ]; then
        log "phase 7/8: systemd (skipped — not Linux)"
        return
    fi
    log "phase 7/8: systemd setup"

    # Check loginctl linger
    local linger_status=""
    linger_status="$(loginctl show-user "$USER" --property=Linger 2>/dev/null)" || true
    if [ "$linger_status" = "Linger=yes" ]; then
        ok "loginctl linger enabled for $USER"
    else
        miss "loginctl linger not enabled — enabling"
        run sudo loginctl enable-linger "$USER"
        ok "loginctl linger enabled"
    fi

    # Check XDG_RUNTIME_DIR
    if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "${XDG_RUNTIME_DIR}" ]; then
        ok "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
    else
        miss "XDG_RUNTIME_DIR not set or missing"
        NEXT_STEPS+=("Ensure XDG_RUNTIME_DIR is set (typically /run/user/\$(id -u)) — systemd user services require it")
    fi
}

# ---------------------------------------------------------------------------
# 8. report — summary matrix + next steps
# ---------------------------------------------------------------------------
phase_report() {
    log "phase 8/8: summary"
    local arch
    arch="$(uname -m)"
    echo
    echo "  host:             $_OS $arch"
    echo "  claudlobby:       $CLAUDLOBBY_ROOT"
    echo "  prereqs ok:       ${PREREQ_OK[*]:-(none)}"
    echo "  prereqs missing:  ${PREREQ_MISSING[*]:-(none)}"
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
phase_packages
phase_node
phase_python
phase_claude
phase_plugins
phase_systemd
phase_report
