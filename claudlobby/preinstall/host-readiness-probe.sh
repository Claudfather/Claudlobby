#!/bin/sh
# host-readiness-v1: standalone preinstall inventory, suitable for closed-stdin
# non-PTY exec before Python/core exists. It runs only uname, id, dd and tr, by
# absolute path, plus command -v lookups, and executes nothing it reads or finds.
# Presence is not readiness: versions, Python >=3.10/venv/SSL, the native user
# manager and its lingering, provider sign-in and Tailscale access stay unknown.
# No paths, hostnames, environment values or credentials are returned. Account
# names must be 1..64 ASCII letters/digits/_.-; other names refuse safely.
# As root on Linux it also reads /etc/passwd, /etc/group and setup's own root
# receipts, each bounded, and reports "logins"; otherwise, or on doubt, null.
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
                (os) /usr/bin/uname -s ;;
                (arch) /usr/bin/uname -m ;;
                (uid) /usr/bin/id -u ;;
                (user) /usr/bin/id -un ;;
                (*) return 1 ;;
            esac
            printf '\nS%s' "$?"
        } 2>/dev/null | {
            /bin/dd bs=1 count=256 2>/dev/null
            printf '\nD%s' "$?"
        } | {
            /usr/bin/tr '\000' '?' 2>/dev/null
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

# Root on Linux only: the account list and setup's own receipts, which only
# root writes. These paths are fixed here; nothing in the environment or on
# stdin can move them. Contents are matched with case patterns and split with
# IFS under noglob, never evaluated, so nothing read is ever executed.
passwd_file=/etc/passwd
group_file=/etc/group
store=/var/lib/claudlobby-browser-system/v1
nl='
'
rs=$(printf '\036')

# Read one regular file whole into $content, at most $2 bytes, framed like
# bounded_fact. NUL and RS become a question mark and each newline becomes
# RS, so lines split under IFS without collapsing.
read_bounded() {
    [ -f "$1" ] || return 1
    content=$(
        {
            /bin/dd if="$1" bs="$(($2 + 1))" count=1 2>/dev/null
            printf '\nD%s' "$?"
        } | {
            /usr/bin/tr '\000\036\n' '??\036' 2>/dev/null
            printf '\nT%s!' "$?"
        }
    ) || return 1
    case "$content" in
        *"${rs}D0${nl}T0!") content=${content%"${rs}D0${nl}T0!"} ;;
        *) return 1 ;;
    esac
    [ "${#content}" -le "$2" ]
}

# A numeric id sets $value without leading zeros. More than 10 digits is
# refused before any numeric test, so a huge value never reaches [ or $(( )).
id_value() {
    case "$1" in ''|*[!0-9]*) return 1 ;; esac
    if [ "${#1}" -gt 10 ]; then
        overflow=true
        return 1
    fi
    value=$1
    while :; do
        case "$value" in
            0?*) value=${value#0} ;;
            *) break ;;
        esac
    done
}

# Every numeric uid or gid counts toward next_uid, whatever its line holds.
note_id() {
    if id_value "$1" && [ "$value" -ge "$next_uid" ] && [ "$value" -le 59999 ]; then
        next_uid=$((value + 1))
    fi
}

valid_name() {
    case "$1" in [a-z_]*) ;; *) return 1 ;; esac
    case "$1" in *[!a-z0-9_-]*) return 1 ;; esac
    [ "${#1}" -le 32 ]
}

# passwd and group: at most 64 KiB and 512 lines each. next_uid is 1 + the
# highest id from 1000 to 59999 (passwd uid and gid, group gid), at least 1000.
# The first claudlobby line is the team account. $users keeps each well-named
# account's first line as RS name:uid, with - for a uid that is not a number.
read_accounts() {
    next_uid=1000; team=null; users=; overflow=false
    read_bounded "$passwd_file" 65536 || return 1
    passwd=$content
    read_bounded "$group_file" 65536 || return 1
    group=$content
    lines=0
    IFS=$rs
    for line in $passwd; do
        lines=$((lines + 1))
        [ "$lines" -le 512 ] || return 1
        IFS=:
        set -- $line
        if [ "$#" -ge 1 ] && [ "$1" = claudlobby ] && [ "$team" = null ]; then
            [ "$#" -ge 3 ] && id_value "$3" || return 1
            team='{"uid":'$value'}'
        fi
        [ "$#" -ge 3 ] || continue
        note_id "$3"
        if [ "$#" -ge 4 ]; then note_id "$4"; fi
        valid_name "$1" || continue
        case "$users$rs" in *"$rs$1:"*) continue ;; esac
        if id_value "$3"; then users=$users$rs$1:$value; else users=$users$rs$1:-; fi
    done
    lines=0
    IFS=$rs
    for line in $group; do
        lines=$((lines + 1))
        [ "$lines" -le 512 ] || return 1
        IFS=:
        set -- $line
        if [ "$#" -ge 3 ]; then note_id "$3"; fi
    done
    [ "$overflow" = false ]
}

# A kept receipt is a completed system receipt. The leaf writes sorted,
# ensure_ascii JSON, so its top-level selection is the last object in the
# file and only the state and submission strings follow it. Sets
# $receipt_name and $receipt_uid from that selection.
receipt_account() {
    case "$content" in *',"kind":"system"'[,}]*) ;; *) return 1 ;; esac
    case "$content" in *'},"state":"completed"'*) ;; *) return 1 ;; esac
    IFS='{'
    set -- $content
    unset IFS
    [ "$#" -ge 2 ] || return 1
    shift "$(($# - 2))"
    case "$1" in *',"schema_version":1,"selection":') ;; *) return 1 ;; esac
    [ "${#2}" -le 1024 ] || return 1
    selection=${2%%'}'*}
    after=${2#"$selection"}
    case "$after" in
        '},"state":"completed"}'|'},"state":"completed",'*'}') ;;
        *) return 1 ;;
    esac
    case "$selection" in '"account":"'*'","gid":'*',"uid":'*) ;; *) return 1 ;; esac
    receipt_name=${selection#'"account":"'}
    receipt_name=${receipt_name%%'"'*}
    receipt_uid=${selection##*',"uid":'}
    valid_name "$receipt_name" || return 1
    case "$receipt_uid" in ''|0*|*[!0-9]*) return 1 ;; esac
    [ "${#receipt_uid}" -le 10 ]
}

# Setup's store: at most 128 *.json names, each read at most 96 KiB. Symlinks
# and non-files are skipped. A receipt counts only when its account is a live
# passwd account with the same uid, from 1000 to 59999, and not claudlobby.
# Scanning stops at the second distinct account.
read_receipts() {
    prepared=; seen=; found=0
    if [ -h "${store%/*}" ] || [ -h "$store" ]; then return 1; fi
    [ -e "$store" ] || return 0
    [ -d "$store" ] || return 1
    set +f
    set -- "$store"/*.json
    set -f
    if [ "$#" -eq 1 ] && [ ! -e "$1" ] && [ ! -h "$1" ]; then return 0; fi
    [ "$#" -le 128 ] || return 1
    for receipt in "$@"; do
        if [ -h "$receipt" ] || [ ! -f "$receipt" ]; then continue; fi
        read_bounded "$receipt" 98304 || return 1
        receipt_account || continue
        [ "$receipt_name" != claudlobby ] || continue
        if [ "$receipt_uid" -lt 1000 ] || [ "$receipt_uid" -gt 59999 ]; then continue; fi
        case "$users$rs" in *"$rs$receipt_name:$receipt_uid$rs"*) ;; *) continue ;; esac
        case "$seen$rs" in *"$rs$receipt_name$rs"*) continue ;; esac
        seen=$seen$rs$receipt_name
        if [ "$found" -gt 0 ]; then prepared=$prepared,; fi
        prepared=$prepared'{"name":"'$receipt_name'","uid":'$receipt_uid'}'
        found=$((found + 1))
        [ "$found" -lt 2 ] || break
    done
}

logins=null
if [ "$os" = linux ] && [ "$uid" = 0 ]; then
    if read_accounts; then
        unset IFS
        if [ "$next_uid" -le 59999 ]; then next=$next_uid; else next=null; fi
        if read_receipts; then prepared="[$prepared]"; else prepared=null; fi
        logins='{"team":'$team',"next_uid":'$next',"prepared":'$prepared'}'
    fi
    unset IFS
fi

printf '{"schema_version":1,"operation":"host-readiness-v1","os":"%s","architecture":"%s","account":{"uid":%s,"name":"%s"},' "$os" "$architecture" "$uid" "$account"
printf '"tools":{'
printf '"python3":{"present":%s,"version":null,"readiness":"unknown"},' "$python"
printf '"git":{"present":%s,"version":null,"readiness":"unknown"},' "$git"
printf '"tailscale":{"present":%s,"version":null,"readiness":"unknown"},' "$tailscale"
printf '"claude":{"present":%s,"version":null,"readiness":"unknown"},' "$claude"
printf '"tmux":{"present":%s,"version":null,"readiness":"unknown"},' "$tmux"
printf '"jq":{"present":%s,"version":null,"readiness":"unknown"}},' "$jq"
printf '"native_manager":{"expected":"%s","systemctl_present":%s,"launchctl_present":%s,"readiness":"unknown","lingering":"unknown"},' "$manager" "$systemctl" "$launchctl"
printf '"checks":{"python_minimum_version":"unknown","python_venv":"unknown","python_ssl":"unknown","provider_authentication":"unknown","tailscale_access":"unknown"},'
printf '"logins":%s}\n' "$logins"
