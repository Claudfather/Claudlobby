#!/bin/sh
# host-readiness-v1: standalone preinstall inventory, suitable for closed-stdin
# non-PTY exec before Python/core exists. It runs uname, id, dd and tr, by
# absolute path, plus command -v lookups, and executes nothing it reads or finds.
# Presence is not readiness: versions, Python >=3.10/venv/SSL, the native user
# manager and its lingering, provider sign-in and Tailscale access stay unknown.
# No paths, hostnames, environment values or credentials are returned. Account
# names must be 1..64 ASCII letters/digits/_.-; other names refuse safely.
# As root on Linux it also reads /etc/passwd, /etc/group and setup's own root
# receipts, each bounded, and reports "logins"; otherwise, or on doubt, null.
# There, when /etc/passwd has a claudlobby line, it also runs Python as root:
# /usr/bin/python3 -I -S -B on the fixed block in $team_check below, a byte for
# byte copy of linux-system-v1.sh's team account check. Read-only, it reads the
# account files (through the system's account lookup), /etc/shadow,
# /etc/gshadow, /etc/subuid, /etc/subgid, /etc/nsswitch.conf, /etc/sudoers and
# the files in /etc/sudoers.d, and the team home and mail spool's metadata. It
# writes nothing and runs nothing; an alarm ends it after 4 seconds.
# stdout: one schema-1 JSON line, always <8 KiB; stderr: empty. Exit 0 inventory,
# 1 unavailable/malformed fact, 2 unsupported arguments. Callers must enforce
# their own execution deadline/output limits; the shell installs no timer.
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
# policy is the team account check, run only by check_team below.
bounded_fact() {
    raw=$(
        {
            case "$1" in
                (os) /usr/bin/uname -s ;;
                (arch) /usr/bin/uname -m ;;
                (uid) /usr/bin/id -u ;;
                (user) /usr/bin/id -un ;;
                (policy) /usr/bin/python3 -I -S -B -c "$team_check" </dev/null ;;
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

# The next-uid rule. linux-system-v1.sh must compute the same answer, and
# tests/fixtures/next-uid-rule.json holds the cases both are tested against:
# - passwd and group are each at most 65,536 bytes and 512 lines, or logins
#   is null. Lines are split on newline only; a last line needs no newline.
# - Fields are split on ":". A passwd line counts its field 3 when it has at
#   least 3 fields and its field 4 when it has at least 4; a group line counts
#   its field 3. The name and the other fields do not matter.
# - A field of 1 to 10 ASCII digits, matched in full, is a number; leading
#   zeros are dropped. All digits but more than 10, even zero-padded, makes
#   logins null. Anything else (a space, tab, "+", "-", CR, NUL) is no number;
#   the leaf still rechecks the chosen id through NSS.
# - next_uid is 1 + the highest number from 1000 to 59999, at least 1000, and
#   null when that passes 59999.
# The first claudlobby line is the team account: $team is its uid, or null.
# $users keeps each well-named account's first line as RS name:uid, with - for
# a uid that is not a number.
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
            team=$value
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

# The leaf writes sorted, ensure_ascii JSON, so a receipt's top-level
# selection is the last object in the file and only the state and submission
# strings follow it. Sets $selection and $state from there; any other shape
# fails. The selection is at most about 1 KiB, so no prefix scan ever runs
# over a long string.
receipt_tail() {
    IFS='{'
    set -- $content
    unset IFS
    [ "$#" -ge 2 ] || return 1
    shift "$(($# - 2))"
    case "$1" in *',"schema_version":1,"selection":') ;; *) return 1 ;; esac
    [ "${#2}" -le 1024 ] || return 1
    selection=${2%%'}'*}
    state=${2#"$selection"}
    case "$state" in '},"state":"'*) state=${state#'},"state":"'} ;; *) return 1 ;; esac
    rest=${state#*'"'}
    state=${state%%'"'*}
    case "$state" in ''|*[!a-z_]*) return 1 ;; esac
    case "$rest" in '}'|','*'}') ;; *) return 1 ;; esac
}

# A kept receipt is a completed system receipt. Sets $receipt_name and
# $receipt_uid from its selection.
receipt_account() {
    [ "$state" = completed ] || return 1
    # Anywhere in the file is safe today: serve config, apt hooks and package
    # rows have fixed shapes, so only the proposal's own kind is such a pair.
    case "$content" in *',"kind":"system"'[,}]*) ;; *) return 1 ;; esac
    case "$selection" in '"account":"'*'","gid":'*',"uid":'*) ;; *) return 1 ;; esac
    receipt_name=${selection#'"account":"'}
    receipt_name=${receipt_name%%'"'*}
    receipt_uid=${selection##*',"uid":'}
    valid_name "$receipt_name" || return 1
    case "$receipt_uid" in ''|0*|*[!0-9]*) return 1 ;; esac
    [ "${#receipt_uid}" -le 10 ]
}

# Adds a kept receipt's account to $prepared, as read_receipts describes.
keep_receipt() {
    receipt_account || return 0
    [ "$receipt_name" != claudlobby ] || return 0
    if [ "$receipt_uid" -lt 1000 ] || [ "$receipt_uid" -gt 59999 ]; then return 0; fi
    case "$users$rs" in *"$rs$receipt_name:$receipt_uid$rs"*) ;; *) return 0 ;; esac
    case "$seen$rs" in *"$rs$receipt_name$rs"*|*"$rs$receipt_uid$rs"*) return 0 ;; esac
    seen=$seen$rs$receipt_name$rs$receipt_uid
    if [ "$found" -gt 0 ]; then prepared=$prepared,; fi
    prepared=$prepared'{"name":"'$receipt_name'","uid":'$receipt_uid'}'
    found=$((found + 1))
}

# Whether one receipt records an unfinished operation, from its bytes alone,
# by the rules the leaf's no_pending uses for that part of its check. A
# planned receipt never ran, and a finished one (completed, failed_partial or
# conflict) is settled, though a system or index-refresh receipt past planned
# must also hold its dispatch record. Any other state is an operation with no
# recorded end, which the leaf admits only after a live root check this probe
# cannot make (the route it set, or its native unit): uncertain. A receipt
# naming no kind, or more than one, is doubt.
receipt_uncertain() {
    kinds=
    case "$content" in *',"kind":"system"'[,}]*) kinds=$kinds.system ;; esac
    case "$content" in *',"kind":"index-refresh"'[,}]*) kinds=$kinds.refresh ;; esac
    case "$content" in *',"kind":"ingress"'[,}]*) kinds=$kinds.ingress ;; esac
    case "$kinds" in .system|.refresh|.ingress) ;; *) doubt=true; return 0 ;; esac
    [ "$state" != planned ] || return 0
    if [ "$kinds" != .ingress ]; then
        case "$content" in *',"dispatch":{"boot_id":"'*) ;; *) uncertain=true; return 0 ;; esac
    fi
    case "$state" in completed|failed_partial|conflict) ;; *) uncertain=true ;; esac
}

# The leaf names its receipts by request UUID, lowercase.
hx='[0-9a-f]'
receipt_file=$hx$hx$hx$hx$hx$hx$hx$hx-$hx$hx$hx$hx-$hx$hx$hx$hx-$hx$hx$hx$hx-$hx$hx$hx$hx$hx$hx$hx$hx$hx$hx$hx$hx.json

# Setup's store: at most 128 *.json names, each read at most 96 KiB. Symlinks
# and non-files are skipped. A receipt counts only when its account is a live
# passwd account with the same uid, from 1000 to 59999, and not claudlobby.
# No two entries share a name or a uid: two passwd names may share a uid.
# $prepared stops growing at the second distinct account; a receipt it cannot
# read before then makes it null (return 1).
# $uncertain reads every receipt within the same bounds: true when one is
# uncertain (receipt_uncertain), else null on any doubt (a link, a non-file,
# a name that is no receipt name, a receipt over 96 KiB or of another shape),
# else false. Reading stops early only once it is true and $prepared is
# settled. It reports unfinished operations only. It does not check whether
# a finished system receipt's native unit is still active, package health
# after failed_partial, stray files in the store, or a receipt's mode, links
# or digest: the leaf's root step checks those, and on a visit that skips
# that step nothing checks them.
read_receipts() {
    prepared=; seen=; found=0; uncertain=false; doubt=false; unread=false
    if [ -h "${store%/*}" ] || [ -h "$store" ]; then return 1; fi
    if [ -e "${store%/*}" ] && [ ! -d "${store%/*}" ]; then return 1; fi
    [ -e "$store" ] || return 0
    [ -d "$store" ] || return 1
    set +f
    set -- "$store"/*.json
    set -f
    if [ "$#" -eq 1 ] && [ ! -e "$1" ] && [ ! -h "$1" ]; then return 0; fi
    [ "$#" -le 128 ] || return 1
    for receipt in "$@"; do
        if [ -h "$receipt" ] || [ ! -f "$receipt" ]; then
            doubt=true
            continue
        fi
        case "${receipt##*/}" in $receipt_file) ;; *) doubt=true ;; esac
        if ! read_bounded "$receipt" 98304; then
            [ "$found" -ge 2 ] || unread=true
            doubt=true
            continue
        fi
        if receipt_tail; then
            receipt_uncertain
            if [ "$found" -lt 2 ] && [ "$unread" = false ]; then keep_receipt; fi
        else
            doubt=true
        fi
        if [ "$uncertain" = true ] && { [ "$found" -ge 2 ] || [ "$unread" = true ]; }; then break; fi
    done
    if [ "$uncertain" = false ] && [ "$doubt" = true ]; then uncertain=null; fi
    [ "$unread" = false ]
}

# The team account check: Python from BEGIN to END, held in single quotes,
# so it holds no single quote. Every definition in it but main is the leaf
# linux-system-v1.sh's own, byte for byte, and
# tests/test_host_readiness_probe.py holds each to the leaf.
team_check='
# BEGIN TEAM POLICY COPY
import os
import re
import signal
import stat
import pwd
import grp
from pathlib import Path

OWNER = 0
LIMIT = 262144
TEAM = "claudlobby"
TEAM_HOME = "/home/claudlobby"
PATHS = {"passwd": "/etc/passwd", "group": "/etc/group", "shadow": "/etc/shadow", "subuid": "/etc/subuid",
         "subgid": "/etc/subgid", "nsswitch": "/etc/nsswitch.conf", "home": TEAM_HOME, "gshadow": "/etc/gshadow",
         "sudoers": "/etc/sudoers", "sudoers.d": "/etc/sudoers.d",
         "mail": "/var/mail/" + TEAM, "proc": "/proc", "run": "/run/user"}


class Refusal(Exception):
    def __init__(self, code):
        self.code = code


def require(ok, code):
    if not ok:
        raise Refusal(code)


def trusted_parents(path):
    for parent in reversed(path.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER
                and not info.st_mode & 0o022, "unsafe_parent")


def lookup(function, key):
    try:
        return function(key)
    except KeyError:
        return None


def lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def read_root(name, limit=65536, child=""):
    # One fixed root file, or a child listed in a fixed root folder, at most
    # limit + 1 bytes so callers see oversize; None when it is absent. Never
    # follows a link.
    path = Path(PATHS[name], child)
    trusted_parents(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022, "unsafe_file")
        raw = b""
        while len(raw) <= limit:
            part = os.read(fd, limit + 1 - len(raw))
            if not part:
                break
            raw += part
        return raw
    finally:
        os.close(fd)


def directory_service():
    # Accounts, passwords, groups and sudo rules come from local files only.
    # Read as glibc 2.39 does: # is no comment, a name ends at a colon or a
    # space, and names are exact (sudo alone reads its line in any case).
    # Every line for these must parse and list only files or systemd, with no
    # [action]; a later line replaces an earlier one, so this holds whichever
    # wins.
    raw = read_root("nsswitch")
    require(raw is not None and len(raw) <= 65536, "team_directory_service")
    seen, names = set(), (b"passwd", b"group", b"shadow", b"gshadow", b"initgroups", b"sudoers")
    for line in raw.split(b"\n"):
        text = line.strip()
        text = text.lower() if text.lower().startswith(b"sudoers") else text
        if text.startswith(names):
            entry = re.fullmatch(rb"([a-z]+)[\s:]+(.*)", text)
            require(entry and entry[1] in names and entry[2] and set(entry[2].split()) <= {b"files", b"systemd"},
                    "team_directory_service")
            seen.add(entry[1])
    require(seen >= {b"group", b"passwd"}, "team_directory_service")


def subordinate(uid):
    # A subuid or subgid line for the team or its id, or a range holding it.
    for name in ("subuid", "subgid"):
        raw = read_root(name)
        if raw is None:
            continue
        if len(raw) > 65536:
            return True
        for line in raw.split(b"\n"):
            fields = line.split(b":")
            if fields[0] in (TEAM.encode(), str(uid).encode()):
                return True
            if (len(fields) == 3 and all(re.fullmatch(rb"[0-9]{1,10}", field) for field in fields[1:])
                    and int(fields[1]) <= uid < int(fields[1]) + int(fields[2])):
                return True
    return False


def team_lines(name):
    # The lines of a shadow file whose first field is the team, split on
    # colons; [] when the file is absent, None past LIMIT.
    raw = read_root(name, LIMIT)
    if raw is None or len(raw) <= LIMIT:
        return [f for f in (line.split(b":") for line in (raw or b"").split(b"\n")) if f[0] == TEAM.encode()]


def sudo_rules(code):
    # A heuristic: no line sudo reads names the team as a word, in any case,
    # comments included (to sudo, # before digits is a uid), with continued
    # lines joined, once its \xHH and \ escapes are undone. Aliases, groups,
    # #uid forms or sudo.conf can still hide it, so the live run also checks
    # sudo -l -U claudlobby.
    # Every include must be /etc/sudoers.d, whose files are read as sudo picks
    # them (no name ending in ~ or holding a dot), at most 64 of 64 KiB.
    try:
        names = sorted(n for n in os.listdir(PATHS["sudoers.d"]) if not n.endswith("~") and "." not in n)
    except FileNotFoundError:
        names = []
    require(len(names) <= 64, code)
    for name, child in [("sudoers", "")] + [("sudoers.d", n) for n in names]:
        raw = read_root(name, child=child) or b""
        # No NUL (sudo ends a line there), no last \ (sudo carries it into the
        # next file), and any include line exactly the one allowed.
        require(len(raw) <= 65536 and b"\0" not in raw and not re.search(rb"\\\s*\Z", raw), code)
        for line in raw.split(b"\n"):
            include = re.match(rb"[\s!]*@include|\s*#include(?:dir)?\s", line)
            require(not include or re.fullmatch(rb"\s*[#@]include(?:dir)?\s+/etc/sudoers\.d\s*", line), code)
        text = raw + b"\n" + re.sub(rb"\\[ \t]*\r?\n[ \t]*", b"", raw)
        text = re.sub(rb"\\x([0-9a-fA-F]{2})", lambda m: bytes([int(m[1], 16)]), text).replace(b"\\", b"")
        require(not re.search(rb"\bclaudlobby\b", text, re.I), code)


def team_policy():
    # A present team account is used only as useradd below makes it.
    directory_service()
    account = pwd.getpwnam(TEAM)
    uid, gid = account.pw_uid, account.pw_gid
    users = pwd.getpwall()
    require(1000 <= uid <= 59999 and [p.pw_name for p in users if p.pw_uid == uid or p.pw_name == TEAM] == [TEAM], "team_account_uid")
    group = lookup(grp.getgrgid, gid)
    require(gid == uid and group is not None and group.gr_name == TEAM and not group.gr_mem
            and [p.pw_name for p in users if p.pw_gid == gid] == [TEAM]
            and [g.gr_name for g in grp.getgrall() if g.gr_gid == gid or g.gr_name == TEAM] == [TEAM]
            and set(os.getgrouplist(TEAM, gid)) == {gid}, "team_account_group")
    # Its gshadow line names no administrator or member and holds no hash. A
    # missing line conforms: newgrp then uses the /etc/group field, held here
    # to x, empty or a lock, none of which any password matches.
    lines = team_lines("gshadow")
    require(lines is not None and len(lines) <= 1 and re.fullmatch(r"x|[!*]*", group.gr_passwd)
            and all(len(f) == 4 and re.fullmatch(rb"[!*]*", f[1]) and f[2] == f[3] == b"" for f in lines), "team_account_group")
    require(account.pw_shell == "/bin/bash", "team_account_shell")
    trusted_parents(Path(PATHS["home"]))
    home, mail = lstat(PATHS["home"]), lstat(PATHS["mail"])
    require(account.pw_dir == TEAM_HOME and home is not None and stat.S_ISDIR(home.st_mode) and home.st_uid == uid
            and not home.st_mode & 0o077 and (mail is None or stat.S_ISREG(mail.st_mode) and mail.st_uid == uid
            and mail.st_size == 0 and mail.st_nlink == 1), "team_account_home")
    lines = team_lines("shadow")
    require(lines and len(lines) == 1 and len(lines[0]) == 9
            and account.pw_passwd == "x" and re.fullmatch(rb"[!*]+", lines[0][1]), "team_account_password")
    # No forced change (a last change of 0 by value: only empty or digits
    # above 0 pass), no maximum age, no inactivity limit, no expiry.
    require(re.fullmatch(rb"(0*[1-9][0-9]*)?", lines[0][2]) and all(lines[0][i] in (b"", b"-1") for i in (4, 6, 7)), "team_account_expiry")
    require(not subordinate(uid), "team_account_ids")
    sudo_rules("team_account_sudo")


def main():
    # How the probe runs this copy: as root, with an alarm whose default
    # action ends the process after 4 seconds, whatever it waits on, and one
    # word on stdout. Any other exception exits nonzero, which reads unknown.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.alarm(4)
    if os.geteuid() != 0:
        raise SystemExit(3)
    try:
        team_policy()
        code = "conforms"
    except Refusal as exc:
        code = exc.code
    print(code)


if __name__ == "__main__":
    main()
# END TEAM POLICY COPY
'

# $policy is conforms, or one of the nine team codes the leaf refuses a
# present team account with. It is unknown when Python is missing, fails, is
# stopped by its alarm, or answers anything else, including the leaf file
# checks (unsafe_parent, unsafe_file) that team_policy can also raise.
check_team() {
    policy=unknown
    [ -x /usr/bin/python3 ] || return 0
    word=$(bounded_fact policy) || return 0
    case "$word" in
        conforms|team_directory_service|team_account_uid|team_account_group|team_account_shell) ;;
        team_account_home|team_account_password|team_account_expiry|team_account_ids|team_account_sudo) ;;
        *) return 0 ;;
    esac
    policy=$word
}

logins=null
if [ "$os" = linux ] && [ "$uid" = 0 ]; then
    if read_accounts; then
        unset IFS
        if [ "$next_uid" -le 59999 ]; then next=$next_uid; else next=null; fi
        if read_receipts; then
            prepared="[$prepared]"
        else
            prepared=null
            [ "$uncertain" = true ] || uncertain=null
        fi
        unset IFS
        if [ "$team" != null ]; then
            check_team
            team='{"uid":'$team',"policy":"'$policy'"}'
        fi
        logins='{"team":'$team',"next_uid":'$next',"prepared":'$prepared',"uncertain":'$uncertain'}'
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
