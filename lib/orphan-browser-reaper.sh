#!/bin/bash
# orphan-browser-reaper.sh — reap leaked browser processes that outlived the
# task that spawned them. Runs as the orphan-browser-reaper host job
# (system.yaml host.jobs, enrolled by setup-system).
#
# A browser-automation task (Playwright, visual-crawl, a research browse) that
# dies without tearing its browser down leaves the browser re-parented to init.
# Nothing then owns it and nothing reclaims it: #807 traced an 11-day ~490 MB/day
# headroom drift on the Pi to one such instance -- 18 processes, ~1.4 GB, 51h old.
# This is the catch-all; a teardown guard in the spawning path is the complement,
# not a substitute, because it only covers the paths someone remembered to guard.
#
# A process is reaped only when ALL of these hold:
#   1. orphaned         — its parent is init/launchd, or a subreaper that adopted
#                         it. "ppid == 1" alone is NOT the test: systemd --user
#                         sets itself a subreaper, so a browser orphaned by a bot
#                         session is adopted by the user manager and keeps a
#                         non-1 ppid. On this fleet that is the common case --
#                         bot sessions ARE systemd user services -- so a ppid==1
#                         test would miss exactly the leak #807 is about. The
#                         parent's comm is matched instead (--orphan-parents).
#                         A browser with a live parent is someone's working
#                         session and is never touched.
#   2. comm matches     — the executable's BASENAME is a known browser binary.
#                         Matching is on comm, never the full cmdline, so this
#                         script can never match its own arguments (the pkill -f
#                         footgun) and a bot merely discussing chromium is safe.
#   3. age >= threshold — old enough that no live task plausibly owns it.
#   4. same user        — only this user's processes are considered.
#
# Descendants of a reaped root go with it: the 18 processes were one tree whose
# children point at the browser, not at init, so root-only reaping would reclaim
# a fraction now and the rest a run later.
#
# Usage:
#   orphan-browser-reaper.sh [--max-age-hours N] [--pattern REGEX] [--dry-run]
#   orphan-browser-reaper.sh --dry-run              # report, kill nothing
#   orphan-browser-reaper.sh --max-age-hours 2
#
# Defaults: --max-age-hours 6, the browser basename pattern below.
#
# Output is written to $CLAUDLOBBY_ROOT/lib/orphan-browser-reaper.log. A reap
# raises a FLEET NOTICE (not an ALERT — reclaiming a leak is routine hygiene,
# and routine events that page train operators to ignore the channel). Exits 0
# whether or not anything was reaped: monitoring must never abort a job chain.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

# Anchored alternatives over the executable BASENAME, so `chrome-notes.sh` or a
# checkout named `playwright-tests` is not a browser. `headless_shell` is what
# recent Playwright actually execs; the crashpad handlers are the stragglers that
# outlive a browser crash.
DEFAULT_PATTERN='^(chromium|chromium-browser|chrome|google-chrome|google-chrome-stable|Google Chrome|Google Chrome Helper|chrome_crashpad_handler|crashpad_handler|headless_shell|playwright|playwright.sh|msedge|Microsoft Edge)$'

# Parents that mean "nothing owns this any more": init/launchd (pid 1) and the
# subreapers that adopt in its place. Narrow on purpose -- a browser whose parent
# is a real task (node, python, a bot session) is in use, not leaked.
DEFAULT_ORPHAN_PARENTS='^(systemd|launchd|init)$'

MAX_AGE_HOURS=6
PATTERN="$DEFAULT_PATTERN"
ORPHAN_PARENTS="$DEFAULT_ORPHAN_PARENTS"
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --max-age-hours)  MAX_AGE_HOURS="$2"; shift 2 ;;
        --pattern)        PATTERN="$2"; shift 2 ;;
        --orphan-parents) ORPHAN_PARENTS="$2"; shift 2 ;;
        --dry-run)        DRY_RUN=1; shift ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            printf 'orphan-browser-reaper: unknown arg: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

case "$MAX_AGE_HOURS" in
    ''|*[!0-9]*)
        printf 'orphan-browser-reaper: --max-age-hours must be a non-negative integer, got: %s\n' \
            "$MAX_AGE_HOURS" >&2
        exit 2
        ;;
esac
MAX_AGE_SECS=$(( MAX_AGE_HOURS * 3600 ))

LOG="$CLAUDLOBBY_ROOT/lib/orphan-browser-reaper.log"
setup_log_dir "$LOG"
TS=$(ts_iso)

# --- Never-kill set ----------------------------------------------------------
# This script's own pid and every ancestor up to init. A reaper that can kill its
# own process group is worse than the leak. Belt to the comm-matching braces:
# neither alone is trusted.
self_and_ancestors() {
    local pid=$$ guard=0
    while [ -n "$pid" ] && [ "$pid" -gt 1 ] && [ "$guard" -lt 64 ]; do
        printf '%s\n' "$pid"
        pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ') || break
        guard=$(( guard + 1 ))
    done
}
PROTECTED=$(self_and_ancestors | tr '\n' ',')

# --- Candidate selection -----------------------------------------------------
# One ps snapshot, one awk pass: a second snapshot could disagree with the first
# and hand a kill list built from a pid that has since been recycled.
#
# `comm` is the last field and may contain spaces (macOS reports a full path,
# e.g. ".../Google Chrome"), so it is rejoined from field 5 on and reduced to its
# basename before matching -- which also normalizes the Linux/macOS difference.
# `etime` is used rather than `etimes` because etimes is procps-only (absent on
# macOS); its [[DD-]HH:]MM:SS form is parsed below.
select_doomed() {
    ps -u "$(id -un)" -o pid=,ppid=,etime=,rss=,comm= 2>/dev/null | awk \
        -v pattern="$PATTERN" \
        -v orphan_parents="$ORPHAN_PARENTS" \
        -v max_age="$MAX_AGE_SECS" \
        -v protected_csv="$PROTECTED" \
        '
        function age_secs(et,   n, p, s, d, hms) {
            # [[DD-]HH:]MM:SS
            d = 0
            n = index(et, "-")
            if (n > 0) { d = substr(et, 1, n - 1) + 0; et = substr(et, n + 1) }
            n = split(et, p, ":")
            if (n == 3)      s = p[1] * 3600 + p[2] * 60 + p[3]
            else if (n == 2) s = p[1] * 60 + p[2]
            else             s = et + 0
            return d * 86400 + s
        }
        function base(path,   n, p) {
            n = split(path, p, "/")
            return p[n]
        }
        BEGIN {
            split(protected_csv, prot, ",")
            for (i in prot) if (prot[i] != "") protected[prot[i]] = 1
        }
        {
            pid = $1; ppid = $2; et = $3; rss = $4
            comm = $5
            for (i = 6; i <= NF; i++) comm = comm " " $i
            # Record the whole table first: descendants are resolved after the
            # roots are known, and a child may be listed before its parent.
            n++
            P[n] = pid; PP[n] = ppid; RSS[n] = rss; COMM[n] = comm
            AGE[n] = age_secs(et)
            comm_by_pid[pid] = comm
        }
        END {
            # Roots are decided here, not per-line: deciding "is my parent a
            # subreaper" needs the parents comm, and a child can be listed
            # before its parent. pid 1 is owned by root so it is absent from a
            # per-user listing -- hence the explicit ppid == 1 arm.
            for (i = 1; i <= n; i++) {
                if (!(PP[i] == 1 || base(comm_by_pid[PP[i]]) ~ orphan_parents)) continue
                if (AGE[i] < max_age) continue
                if (base(COMM[i]) !~ pattern) continue
                doomed[P[i]] = 1; roots++
            }
            # Counter, not length(doomed): length() over an array is a gawk
            # extension that the awk macOS ships does not reliably provide.
            if (roots == 0) exit 0
            # Transitive closure downward: a pid dies if an ancestor is doomed.
            # Bounded by the process count, so a parent/child cycle (impossible,
            # but cheap to rule out) cannot spin.
            changed = 1
            while (changed) {
                changed = 0
                for (i = 1; i <= n; i++) {
                    if (!doomed[P[i]] && doomed[PP[i]]) { doomed[P[i]] = 1; changed = 1 }
                }
            }
            for (i = 1; i <= n; i++) {
                if (!doomed[P[i]]) continue
                if (P[i] <= 1) continue                  # never init
                if (P[i] in protected) continue          # never self/ancestors
                printf "%s\t%s\t%s\t%s\n", P[i], RSS[i], AGE[i], COMM[i]
            }
        }
        '
}

DOOMED=$(select_doomed || true)

if [ -z "$DOOMED" ]; then
    printf '%s OK — no orphaned browser processes older than %sh\n' "$TS" "$MAX_AGE_HOURS" >>"$LOG"
    exit 0
fi

COUNT=$(printf '%s\n' "$DOOMED" | wc -l | tr -d ' ')
RSS_MB=$(printf '%s\n' "$DOOMED" | awk -F'\t' '{ s += $2 } END { printf "%d", s/1024 }')
OLDEST_H=$(printf '%s\n' "$DOOMED" | awk -F'\t' '{ if ($3 > m) m = $3 } END { printf "%d", m/3600 }')

printf '%s FOUND — %s orphaned browser process(es), ~%s MB, oldest %sh:\n' \
    "$TS" "$COUNT" "$RSS_MB" "$OLDEST_H" >>"$LOG"
printf '%s\n' "$DOOMED" | awk -F'\t' \
    '{ printf "  pid=%-8s rss=%-9s age=%ss  %s\n", $1, $2"kB", $3, $4 }' >>"$LOG"

if [ "$DRY_RUN" = 1 ]; then
    printf '%s DRY-RUN — nothing killed\n' "$TS" >>"$LOG"
    exit 0
fi

# --- Reap --------------------------------------------------------------------
# TERM first so a browser can flush and unlink its profile locks; KILL only what
# is still alive after the grace period. Every kill is by explicit pid from the
# snapshot above -- never a pattern, which is what makes self-matching structurally
# impossible rather than merely unlikely.
PIDS=$(printf '%s\n' "$DOOMED" | cut -f1)

for pid in $PIDS; do
    kill -TERM "$pid" 2>/dev/null || true
done

sleep 5

killed=0
survived=0
for pid in $PIDS; do
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
        sleep 0.2
        if kill -0 "$pid" 2>/dev/null; then
            survived=$(( survived + 1 ))
            printf '%s WARN — pid %s survived SIGKILL\n' "$TS" "$pid" >>"$LOG"
            continue
        fi
    fi
    killed=$(( killed + 1 ))
done

MSG="reaped ${killed} orphaned browser process(es) (~${RSS_MB} MB, oldest ${OLDEST_H}h) on $(hostname)"
[ "$survived" -gt 0 ] && MSG="$MSG; ${survived} survived SIGKILL"

printf '%s REAPED — %s\n' "$TS" "$MSG" >>"$LOG"

# Notice, not alert: this is hygiene working as designed. It stays visible so a
# recurring leak shows up as a pattern rather than as silently reclaimed memory.
emit_fleet_notice "$(resolve_bots_dir "${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}")" \
    "orphan_browser_reaped" "$MSG"

exit 0
