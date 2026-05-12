#!/bin/bash
# Cross-platform host provisioning for a claudlobby fleet.
#
# Idempotent: detects what's already installed and only installs missing pieces.
# Supports macOS (brew) and Linux (apt-get, dnf, pacman).
#
# Usage:
#   lib/setup-host.sh                          # detect + install missing
#   lib/setup-host.sh --dry-run                # report only, install nothing
#   lib/setup-host.sh --with-data              # also install data CLIs (railway, neon, vercel, dbt)
#   lib/setup-host.sh --fleet <name>           # scope to overlay fleet
#
# Phases:
#   1. preflight       — OS detection, repo root check
#   2. package manager  — brew (macOS), apt-get/dnf/pacman (Linux)
#   3. core tools       — node (18+), tmux, jq, gh, curl
#   4. claude CLI       — claude binary + auth check
#   5. telegram plugin  — claude plugin install
#   6. data CLIs        — optional, gated on --with-data
#   7. .env check       — verify .env exists
#   8. supervision      — loginctl enable-linger (Linux), keepalive (macOS)
#   9. report           — summary matrix

set -euo pipefail

FLEET=""
WITH_DATA=0
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --fleet)      FLEET="${2:-}"; shift 2 ;;
        --with-data)  WITH_DATA=1; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'setup-host: unknown arg: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

# --- Source common helpers ---------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$SCRIPT_DIR/lib-common.sh"

# --- Logging helpers ---------------------------------------------------------

log()  { printf 'setup-host: %s\n' "$*"; }
ok()   { printf 'setup-host: \xe2\x9c\x93 %s\n' "$*"; }   # ✓
miss() { printf 'setup-host: \xe2\x97\x8b %s\n' "$*"; }   # ○
warn() { printf 'setup-host: \xe2\x9a\xa0 %s\n' "$*"; }   # ⚠

run()  {
    if [ "$DRY_RUN" = 1 ]; then
        printf 'setup-host: [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

PREREQ_OK=()
PREREQ_INSTALLED=()
PREREQ_MISSING=()
NEXT_STEPS=()

# --- Linux distro detection --------------------------------------------------

_DISTRO=""
_PKG_MGR=""

detect_distro() {
    if [ "$_OS" != "Linux" ]; then
        return
    fi
    if [ -f /etc/os-release ]; then
        # shellcheck source=/dev/null
        . /etc/os-release
        case "${ID:-}" in
            debian|ubuntu|raspbian|linuxmint|pop)
                _DISTRO="debian"
                _PKG_MGR="apt-get"
                ;;
            fedora|rhel|centos|rocky|alma)
                _DISTRO="fedora"
                _PKG_MGR="dnf"
                ;;
            arch|manjaro|endeavouros)
                _DISTRO="arch"
                _PKG_MGR="pacman"
                ;;
            *)
                _DISTRO="unknown"
                # Try to detect from available package managers
                if command -v apt-get >/dev/null 2>&1; then
                    _PKG_MGR="apt-get"
                elif command -v dnf >/dev/null 2>&1; then
                    _PKG_MGR="dnf"
                elif command -v pacman >/dev/null 2>&1; then
                    _PKG_MGR="pacman"
                fi
                ;;
        esac
    else
        _DISTRO="unknown"
        if command -v apt-get >/dev/null 2>&1; then
            _PKG_MGR="apt-get"
        elif command -v dnf >/dev/null 2>&1; then
            _PKG_MGR="dnf"
        elif command -v pacman >/dev/null 2>&1; then
            _PKG_MGR="pacman"
        fi
    fi
}

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------

phase_preflight() {
    log "phase 1/9: preflight"

    if [ ! -d "$CLAUDLOBBY_ROOT" ]; then
        printf 'setup-host: claudlobby root not found at %s\n' "$CLAUDLOBBY_ROOT" >&2
        printf 'setup-host: clone the repo first, or set CLAUDLOBBY_ROOT\n' >&2
        exit 1
    fi

    if [ "$_OS" = "Darwin" ]; then
        ok "macOS host ($(uname -m))"
    elif [ "$_OS" = "Linux" ]; then
        detect_distro
        if [ -z "$_PKG_MGR" ]; then
            printf 'setup-host: could not detect package manager on this Linux distro\n' >&2
            printf 'setup-host: supported: apt-get (Debian/Ubuntu), dnf (Fedora/RHEL), pacman (Arch)\n' >&2
            exit 1
        fi
        ok "Linux host ($(uname -m), distro=$_DISTRO, pkg=$_PKG_MGR)"
    else
        printf 'setup-host: unsupported OS: %s\n' "$_OS" >&2
        exit 1
    fi

    ok "CLAUDLOBBY_ROOT=$CLAUDLOBBY_ROOT"
}

# ---------------------------------------------------------------------------
# 2. Package manager
# ---------------------------------------------------------------------------

phase_package_manager() {
    log "phase 2/9: package manager"

    if [ "$_OS" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            ok "homebrew already installed"
        else
            miss "homebrew missing — installing"
            run /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            PREREQ_INSTALLED+=("homebrew")
        fi
    else
        case "$_PKG_MGR" in
            apt-get)
                log "updating apt package index"
                run sudo apt-get update -qq
                ;;
            dnf)
                ok "dnf available"
                ;;
            pacman)
                log "syncing pacman database"
                run sudo pacman -Sy --noconfirm
                ;;
        esac
    fi
}

# ---------------------------------------------------------------------------
# 3. Core tools
# ---------------------------------------------------------------------------

ensure_pkg() {
    local cmd="$1"
    shift
    # Remaining args are package names per platform: [brew_name] [apt_name] [dnf_name] [pacman_name]
    local brew_pkg="${1:-$cmd}"
    local apt_pkg="${2:-$cmd}"
    local dnf_pkg="${3:-$cmd}"
    local pacman_pkg="${4:-$cmd}"

    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$cmd already installed"
        PREREQ_OK+=("$cmd")
        return
    fi

    miss "$cmd missing — installing"
    if [ "$_OS" = "Darwin" ]; then
        run brew install --quiet "$brew_pkg"
    else
        case "$_PKG_MGR" in
            apt-get) run sudo apt-get install -y -qq "$apt_pkg" ;;
            dnf)     run sudo dnf install -y -q "$dnf_pkg" ;;
            pacman)  run sudo pacman -S --noconfirm "$pacman_pkg" ;;
        esac
    fi
    PREREQ_INSTALLED+=("$cmd")
}

check_node_version() {
    if ! command -v node >/dev/null 2>&1; then
        return 1
    fi
    local version
    version="$(node --version 2>/dev/null | sed 's/^v//')"
    local major
    major="$(printf '%s' "$version" | cut -d. -f1)"
    if [ -z "$major" ] || [ "$major" -lt 18 ]; then
        return 1
    fi
    return 0
}

phase_core_tools() {
    log "phase 3/9: core tools"

    # Node.js — needs special handling for version check
    if check_node_version; then
        ok "node $(node --version) already installed (>= 18)"
        PREREQ_OK+=("node")
    elif command -v node >/dev/null 2>&1; then
        local old_ver
        old_ver="$(node --version 2>/dev/null)"
        warn "node $old_ver is too old (need >= 18)"
        if [ "$_OS" = "Darwin" ]; then
            miss "upgrading node via brew"
            run brew install --quiet node
        elif [ "$_PKG_MGR" = "apt-get" ]; then
            warn "Debian/Ubuntu ships old node — installing from NodeSource"
            if [ "$DRY_RUN" = 1 ]; then
                printf 'setup-host: [dry-run] curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -\n'
                printf 'setup-host: [dry-run] sudo apt-get install -y nodejs\n'
            else
                curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - || true
                sudo apt-get install -y nodejs || true
            fi
        else
            ensure_pkg node nodejs nodejs nodejs nodejs
        fi
        PREREQ_INSTALLED+=("node")
    else
        if [ "$_OS" != "Darwin" ] && [ "$_PKG_MGR" = "apt-get" ]; then
            # Debian/Ubuntu: install from NodeSource for a recent version
            miss "node missing — installing from NodeSource (v20)"
            if [ "$DRY_RUN" = 1 ]; then
                printf 'setup-host: [dry-run] curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -\n'
                printf 'setup-host: [dry-run] sudo apt-get install -y nodejs\n'
            else
                curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - || true
                sudo apt-get install -y nodejs || true
            fi
            PREREQ_INSTALLED+=("node")
        else
            ensure_pkg node node nodejs nodejs nodejs
        fi
    fi

    # tmux, jq, curl — straightforward
    ensure_pkg tmux tmux tmux tmux tmux
    ensure_pkg jq jq jq jq jq
    ensure_pkg curl curl curl curl curl

    # GitHub CLI — different package name on Arch
    if command -v gh >/dev/null 2>&1; then
        ok "gh already installed"
        PREREQ_OK+=("gh")
    else
        miss "gh (GitHub CLI) missing — installing"
        if [ "$_OS" = "Darwin" ]; then
            run brew install --quiet gh
        elif [ "$_PKG_MGR" = "apt-get" ]; then
            # GitHub CLI needs its own repo on Debian/Ubuntu
            if [ "$DRY_RUN" = 1 ]; then
                printf 'setup-host: [dry-run] install gh from GitHub apt repo\n'
            else
                if ! apt-cache show gh >/dev/null 2>&1; then
                    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
                        | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
                    printf 'deb [arch=%s signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' \
                        "$(dpkg --print-architecture)" \
                        | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
                    sudo apt-get update -qq
                fi
                sudo apt-get install -y -qq gh
            fi
        elif [ "$_PKG_MGR" = "dnf" ]; then
            run sudo dnf install -y -q gh
        elif [ "$_PKG_MGR" = "pacman" ]; then
            run sudo pacman -S --noconfirm github-cli
        fi
        PREREQ_INSTALLED+=("gh")
    fi
}

# ---------------------------------------------------------------------------
# 4. Claude CLI
# ---------------------------------------------------------------------------

phase_claude() {
    log "phase 4/9: claude CLI"

    local claude=""
    if [ -x "$HOME/.local/bin/claude" ]; then
        claude="$HOME/.local/bin/claude"
    elif [ "$_OS" = "Darwin" ] && [ -x "$_HOMEBREW/bin/claude" ]; then
        claude="$_HOMEBREW/bin/claude"
    elif claude="$(command -v claude 2>/dev/null)" && [ -n "$claude" ]; then
        :
    else
        claude=""
    fi

    if [ -z "$claude" ]; then
        miss "claude CLI missing — installing via npm"
        run npm install -g @anthropic-ai/claude-code
        PREREQ_INSTALLED+=("claude")
        NEXT_STEPS+=("Run \`claude /login\` to authenticate (OAuth, opens browser)")
        return
    fi

    ok "claude CLI found at $claude"
    PREREQ_OK+=("claude")
}

# ---------------------------------------------------------------------------
# 5. Telegram plugin
# ---------------------------------------------------------------------------

phase_telegram_plugin() {
    log "phase 5/9: telegram plugin"

    local claude_bin=""
    claude_bin="$(command -v claude 2>/dev/null)" || true
    if [ -z "$claude_bin" ] && [ -x "$HOME/.local/bin/claude" ]; then
        claude_bin="$HOME/.local/bin/claude"
    fi

    if [ -z "$claude_bin" ]; then
        miss "claude CLI not available — skipping plugin check"
        NEXT_STEPS+=("Install claude CLI, then run: claude plugin install telegram@claude-plugins-official")
        return
    fi

    if "$claude_bin" plugin list 2>/dev/null | grep -qi telegram; then
        ok "telegram plugin already installed"
    else
        miss "telegram plugin missing — installing"
        run "$claude_bin" plugin install telegram@claude-plugins-official
        PREREQ_INSTALLED+=("telegram-plugin")
    fi
}

# ---------------------------------------------------------------------------
# 6. Data CLIs (optional)
# ---------------------------------------------------------------------------

phase_data_clis() {
    if [ "$WITH_DATA" != 1 ]; then
        log "phase 6/9: data CLIs (skipped — pass --with-data to enable)"
        return
    fi
    log "phase 6/9: data CLIs"

    for cli in railway neonctl vercel; do
        if command -v "$cli" >/dev/null 2>&1; then
            ok "$cli already installed"
        else
            miss "$cli missing — installing"
            case "$cli" in
                railway) run npm install -g @railway/cli ;;
                *)       run npm install -g "$cli" ;;
            esac
            PREREQ_INSTALLED+=("$cli")
        fi
    done

    if command -v dbt >/dev/null 2>&1; then
        ok "dbt already installed"
    else
        miss "dbt missing"
        if command -v uv >/dev/null 2>&1; then
            miss "installing dbt via uv"
            run uv tool install --python 3.12 dbt-core --with dbt-snowflake
        elif command -v pip3 >/dev/null 2>&1; then
            miss "installing dbt via pip3"
            run pip3 install dbt-core dbt-snowflake
        else
            warn "neither uv nor pip3 found — cannot install dbt"
            NEXT_STEPS+=("Install dbt manually: pip install dbt-core dbt-snowflake")
        fi
        PREREQ_INSTALLED+=("dbt")
    fi
}

# ---------------------------------------------------------------------------
# 7. .env check
# ---------------------------------------------------------------------------

ENV_PRESENT=0

phase_env_check() {
    log "phase 7/9: .env check"

    if [ -n "$FLEET" ]; then
        local fleet_env="$CLAUDLOBBY_ROOT/local/$FLEET/.env"
        if [ -f "$fleet_env" ]; then
            ok "$fleet_env present"
            ENV_PRESENT=1
            return
        fi
    fi

    if [ -f "$CLAUDLOBBY_ROOT/.env" ]; then
        ok "$CLAUDLOBBY_ROOT/.env present"
        ENV_PRESENT=1
    else
        miss ".env not found — credentials not configured yet"
        NEXT_STEPS+=("Create .env with your Telegram bot token and GitHub PAT (see .env.seed.example)")
    fi
}

# ---------------------------------------------------------------------------
# 8. Supervision setup
# ---------------------------------------------------------------------------

phase_supervision() {
    log "phase 8/9: supervision setup"

    if [ "$_OS" = "Darwin" ]; then
        # macOS: launchd — handled by spin-up-bot.sh / install-keepalive.sh
        if [ -n "$FLEET" ]; then
            local keepalive_script="$SCRIPT_DIR/install-keepalive.sh"
            if [ -x "$keepalive_script" ]; then
                run "$keepalive_script" "$FLEET"
            else
                miss "install-keepalive.sh not found — skipping"
            fi
        else
            ok "launchd supervision available (enroll bots via lib/spin-up-bot.sh)"
        fi
    elif [ "$_OS" = "Linux" ]; then
        # Linux: systemd user services need loginctl enable-linger
        if loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
            ok "loginctl linger enabled for $USER"
        else
            miss "loginctl linger not enabled — enabling (required for headless operation)"
            run sudo loginctl enable-linger "$USER"
            PREREQ_INSTALLED+=("linger")
        fi

        # Ensure systemd user dir exists
        local user_unit_dir="$HOME/.config/systemd/user"
        if [ -d "$user_unit_dir" ]; then
            ok "systemd user unit dir exists"
        else
            miss "creating $user_unit_dir"
            run mkdir -p "$user_unit_dir"
        fi

        if [ -n "$FLEET" ]; then
            local keepalive_script="$SCRIPT_DIR/install-keepalive-systemd.sh"
            if [ -x "$keepalive_script" ]; then
                run "$keepalive_script" "$FLEET"
            else
                miss "install-keepalive-systemd.sh not found — skipping"
            fi
        else
            ok "systemd user services available (enroll bots via lib/spin-up-bot.sh)"
        fi
    fi
}

# ---------------------------------------------------------------------------
# 9. Report
# ---------------------------------------------------------------------------

phase_report() {
    log "phase 9/9: summary"
    printf '\n'
    printf '  OS:               %s (%s)\n' "$_OS" "$(uname -m)"
    if [ "$_OS" = "Linux" ]; then
        printf '  Distro:           %s (pkg: %s)\n' "$_DISTRO" "$_PKG_MGR"
    fi
    if [ -n "$FLEET" ]; then
        printf '  Fleet:            %s\n' "$FLEET"
    fi
    printf '  Already ok:       %s\n' "${PREREQ_OK[*]:-(none)}"
    printf '  Installed now:    %s\n' "${PREREQ_INSTALLED[*]:-(none)}"
    printf '  .env:             %s\n' "$([ "$ENV_PRESENT" = 1 ] && printf 'present' || printf 'missing')"

    if [ "${#NEXT_STEPS[@]}" -gt 0 ]; then
        printf '\n'
        printf '  Next steps:\n'
        for step in "${NEXT_STEPS[@]}"; do
            printf '    - %s\n' "$step"
        done
    fi
    printf '\n'

    if [ "$DRY_RUN" = 1 ]; then
        log "dry-run complete — no changes made"
    else
        log "setup complete"
    fi
}

# --- Main execution ----------------------------------------------------------

phase_preflight
phase_package_manager
phase_core_tools
phase_claude
phase_telegram_plugin
phase_data_clis
phase_env_check
phase_supervision
phase_report
