#!/bin/sh
# host-readiness-v1: standalone preinstall inventory, suitable for closed-stdin
# non-PTY exec before Python/core exists. No runtime helper or prerequisite CLI is
# executed. Only uname/id facts, bounded dd/tr readers and command -v are used.
# Presence is not readiness: versions, Python >=3.10/venv/SSL, native user-manager
# availability/lingering, provider sign-in and Tailscale access stay unknown.
# No paths, hostnames, environment values or credentials are returned. Account
# names must be 1..64 ASCII letters/digits/_.-; other names refuse safely.
# stdout: one schema-1 JSON line, always <8 KiB; stderr: empty. Exit 0 inventory,
# 1 unavailable/malformed fact, 2 unsupported arguments. Callers must enforce
# their own execution deadline/output limits; this script installs no timer.
set -efu
LC_ALL=C
export LC_ALL

refuse() {
    printf '{"schema_version":1,"operation":"host-readiness-v1","error":"%s"}\n' "$1"
    exit "$2"
}
[ "$#" -eq 0 ] || refuse arguments_not_supported 2

# Bound command data to 256 bytes. Each stage appends its own actual status;
# the final nonnewline sentinel preserves trailing newlines in substitution.
# Exact framing and size reject truncated data and failed readers, including a
# reader that emitted complete output before failing. Map NUL to a rejected
# character before substitution, since shell variables cannot preserve NUL.
bounded_fact() {
    raw=$(
        {
            case "$1" in
                (os) command uname -s ;;
                (arch) command uname -m ;;
                (uid) command id -u ;;
                (user) command id -un ;;
                (*) return 1 ;;
            esac
            printf '\nS%s' "$?"
        } 2>/dev/null | {
            command dd bs=1 count=256 2>/dev/null
            printf '\nD%s' "$?"
        } | {
            command tr '\000' '?' 2>/dev/null
            printf '\nT%s!' "$?"
        }
    ) || return 1
    [ "${#raw}" -le 75 ] || return 1
    trailer='

S0
D0
T0!'
    case "$raw" in
        *"$trailer") value=${raw%"$trailer"} ;;
        *) return 1 ;;
    esac
    case "$value" in ''|*[!a-zA-Z0-9_.-]*) return 1 ;; esac
    [ "${#value}" -le 64 ] || return 1
    printf '%s' "$value"
}

os=$(bounded_fact os) || refuse inventory_unavailable 1
architecture=$(bounded_fact arch) || refuse inventory_unavailable 1
uid=$(bounded_fact uid) || refuse inventory_unavailable 1
account=$(bounded_fact user) || refuse inventory_unavailable 1
case "$os" in
    Linux) os=linux; manager=systemd_user ;;
    Darwin) os=macos; manager=launchd_user ;;
    *) refuse inventory_unavailable 1 ;;
esac
case "$uid" in ''|*[!0-9]*|0[0-9]*) refuse inventory_unavailable 1 ;; esac
[ "${#uid}" -le 10 ] || refuse inventory_unavailable 1
[ "${#architecture}" -le 32 ] || refuse inventory_unavailable 1

# Fixed names only. command -v performs lookup without running Python (including
# macOS installer stubs), provider clients, Tailscale, or service managers.
python=false; git=false; tailscale=false; claude=false; tmux=false; jq=false
systemctl=false; launchctl=false
if command -v python3 >/dev/null 2>&1; then python=true; fi
if command -v git >/dev/null 2>&1; then git=true; fi
if command -v tailscale >/dev/null 2>&1; then tailscale=true; fi
if command -v claude >/dev/null 2>&1; then claude=true; fi
if command -v tmux >/dev/null 2>&1; then tmux=true; fi
if command -v jq >/dev/null 2>&1; then jq=true; fi
if command -v systemctl >/dev/null 2>&1; then systemctl=true; fi
if command -v launchctl >/dev/null 2>&1; then launchctl=true; fi

printf '{"schema_version":1,"operation":"host-readiness-v1","os":"%s","architecture":"%s","account":{"uid":%s,"name":"%s"},' "$os" "$architecture" "$uid" "$account"
printf '"tools":{'
printf '"python3":{"present":%s,"version":null,"readiness":"unknown"},' "$python"
printf '"git":{"present":%s,"version":null,"readiness":"unknown"},' "$git"
printf '"tailscale":{"present":%s,"version":null,"readiness":"unknown"},' "$tailscale"
printf '"claude":{"present":%s,"version":null,"readiness":"unknown"},' "$claude"
printf '"tmux":{"present":%s,"version":null,"readiness":"unknown"},' "$tmux"
printf '"jq":{"present":%s,"version":null,"readiness":"unknown"}},' "$jq"
printf '"native_manager":{"expected":"%s","systemctl_present":%s,"launchctl_present":%s,"readiness":"unknown","lingering":"unknown"},' "$manager" "$systemctl" "$launchctl"
printf '"checks":{"python_minimum_version":"unknown","python_venv":"unknown","python_ssl":"unknown","provider_authentication":"unknown","tailscale_access":"unknown"}}\n'
