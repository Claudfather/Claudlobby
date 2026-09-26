#!/usr/bin/env bash
# pull-root.sh — fast-forward $CLAUDLOBBY_ROOT once a day, and watch what it did (#1251).
#
# Delivery had no owner: notify-behind.sh reports the root, update-siblings.sh
# excludes it, reload-fleet.sh composes whatever the root already holds, and a
# merged fix stayed inert until someone pulled by hand (the root sat 24, 46, 83
# and 107 commits behind between hand pulls). This is the pull, with the checks
# the 2026-09-26 hand pull needed:
#
#   * fast-forward ONLY, to repo_currency_target (the newest release tag, else
#     origin/<default branch>) and never past a hold; merge --ff-only, never
#     pull, so pull.rebase=true cannot rebase the install.
#   * refuse ANY dirty tree and name the paths. Host-local config lives in the
#     host override (~/.config/claudlobby/system.yaml), not in the tree, so a
#     dirty tree is someone's work -- or residue like the staged file that held
#     83 commits for 14 days from 2026-08-27 while the daily notice printed a
#     remedy that could not run. The page names the blocker, not a command.
#   * restart the plane daemon and view when claudlobby/ moved. They are
#     long-lived Python and keep the modules they loaded; the daemon's exit 4
#     (#1485) fires only once a migration outruns it, and the 2026-09-22 pull
#     changed its ingest code with no migration at all.
#   * watch every fleet on the host for PULL_ROOT_WATCH_S (900) and page on a
#     regression: a critical event type new since the pull, a script_error from
#     a script the pull changed, a bot that heartbeated in the window before the
#     pull and not since, a service restart that failed, or a read that could
#     not run. lib/ reaches every bot at once, so the schedule and this watch
#     are the canary. When the pull added a plane migration, the page carries
#     the revert runbook; nothing here automates a revert.
#   * one source_pull record per run, host-anchored, whatever the outcome.
#
# RUNS FROM INSIDE THE TREE IT PULLS. git's checkout replaces a changed file
# with a new inode instead of writing it in place, and bash keeps reading the
# inode it opened, so this run finishes on the bytes it started with (measured
# with an in-place write as the positive control: #1251 issuecomment-5845912184
# and issuecomment-5846080563). Everything started AFTER the merge -- the
# restarts and the watch's reads -- runs the new tree, which is the point.
#
# Usage: pull-root.sh [--dry-run]
#   --dry-run reports what it would do and touches nothing, the plane included.
set -euo pipefail
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
    -h | --help) show_help "${BASH_SOURCE[0]}"; exit 0 ;;
    --dry-run) DRY_RUN=1 ;;
    esac
done

ROOT="$CLAUDLOBBY_ROOT"
BOTS_DIR="$(resolve_bots_dir "")"
LOG="$ROOT/state/pull-root.log"
STATE_DIR="$ROOT/state/currency"
RUN_DIR="$ROOT/state/pull-root"
# The watch window, and the equal window before the pull it is compared with.
# An env override is a harness and test seam: the timer's environment is closed.
WATCH_S="${PULL_ROOT_WATCH_S:-900}"
# shellcheck disable=SC2088  # shown to the operator in messages, never expanded
OVERRIDE_HINT="~/.config/claudlobby/system.yaml"
mkdir -p "$STATE_DIR" "$RUN_DIR" 2>/dev/null || true
setup_log_dir "$LOG"
log() { printf '%s %s\n' "$(ts_iso)" "$*" >> "$LOG"; }

OUTCOME="" FROM="" TO="" TARGET="" BEHIND=0 LIB_N=0 CL_CHANGED=false
MIGRATIONS="" UV_BEFORE="" UV_AFTER="" RESTARTED="" RESTART_CHECK="" HOLD="" BLOCKER=""
VERDICT="" FINDINGS=""

# The ONE plane record of this run. emit_fleet_event with no fleet in the
# environment (a host unit carries none) anchors on the host.
record() {
    [ "$DRY_RUN" -eq 1 ] && return 0
    local data
    data=$(printf '{"outcome":"%s","from":"%s","to":"%s","target":"%s","behind":%s,"lib_files":%s,"claudlobby_changed":%s,"migrations":"%s","user_version_before":"%s","user_version_after":"%s","restarted":"%s","restart_check":"%s","hold":"%s","blocker":"%s","watch":"%s","findings":"%s"}' \
        "$(json_escape "$OUTCOME")" "$FROM" "$TO" "$(json_escape "$TARGET")" "${BEHIND:-0}" "${LIB_N:-0}" \
        "$CL_CHANGED" "$(json_escape "$MIGRATIONS")" "$UV_BEFORE" "$UV_AFTER" "$(json_escape "$RESTARTED")" "$(json_escape "$RESTART_CHECK")" \
        "$(json_escape "$HOLD")" "$(json_escape "$BLOCKER")" "$VERDICT" "$(json_escape "$FINDINGS")")
    emit_fleet_event source_pull pull-root "$data" "" || true
    log "RECORD outcome=$OUTCOME from=$FROM to=$TO watch=$VERDICT findings=${FINDINGS:-none}"
}

# notify_currency debounces on (name, type, distinct): a blocker or a held host
# pages once, and again only when it changes or CURRENCY_RENOTIFY_S passes.
notice() {  # <type> <distinct> <message>
    [ "$DRY_RUN" -eq 1 ] && { log "DRY-RUN would notice $1: $3"; return 0; }
    notify_currency claudlobby-root "$1" "$2" "$3"
}

# An ISO-8601 UTC instant <n> seconds ago: the form plane-lookup stores.
iso_ago() {
    python3 -c 'import datetime as d, sys; print((d.datetime.now(d.timezone.utc) - d.timedelta(seconds=int(sys.argv[1]))).isoformat(timespec="seconds"))' "$1"
}

# plane.db PRAGMA user_version, read-only; empty when it cannot be read.
user_version() {
    local db="$ROOT/state/plane/plane.db"
    [ -f "$db" ] || return 0
    python3 -c 'import sqlite3, sys; print(sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True).execute("PRAGMA user_version").fetchone()[0])' "$db" 2>/dev/null || true
}

if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    OUTCOME=not_a_checkout; log "SKIP — $ROOT is not a git checkout"; record; exit 0
fi
if ! with_timeout 120 git -C "$ROOT" fetch --quiet --tags origin 2>>"$LOG"; then
    OUTCOME=fetch_failed; log "FETCH FAILED — nothing pulled"; record; exit 0
fi
TARGET=$(repo_currency_target "$ROOT")
FROM=$(git -C "$ROOT" rev-parse --short HEAD)
TO="$FROM"

# --- the hold (host.jobs.pull-root.hold), read through the host override ----
# `claudlobby host-job` applies the same merge generate does. A hold that
# cannot be read holds: an unreadable override may carry one.
if ! claudlobby_cli host-job pull-root > "$RUN_DIR/job.json" 2>>"$LOG"; then
    OUTCOME=hold_unreadable; HOLD="unreadable"
    log "HELD — the host override could not be read (see above); not pulling"
    notice source_pull_held unreadable "claudlobby root on $(hostname) was NOT pulled: the pull-root job config could not be read (claudlobby host-job pull-root failed), so a hold may be in force -- fix $OVERRIDE_HINT"
    record; exit 0
fi
if ! python3 - "$RUN_DIR/job.json" > "$RUN_DIR/hold.txt" 2>>"$LOG" <<'PY'
import json, sys
hold = (json.load(open(sys.argv[1])) or {}).get("hold")
if hold is not None:
    h = hold if isinstance(hold, dict) else {}
    print("\t".join(str(h.get(k) or "") for k in ("sha", "by", "reason", "until")))
PY
then
    printf '\t\t\t\n' > "$RUN_DIR/hold.txt"          # unparseable: an incomplete hold, held
fi
if [ -s "$RUN_DIR/hold.txt" ]; then
    IFS=$'\t' read -r h_sha h_by h_reason h_until < "$RUN_DIR/hold.txt" || true
    HOLD="${h_sha:-?} by ${h_by:-?} until ${h_until:-?}: ${h_reason:-?}"
    today=$(date +%Y-%m-%d)
    if [ -z "$h_sha" ] || [ -z "$h_by" ] || [ -z "$h_reason" ] || [ -z "$h_until" ]; then
        # Who, why and until are what make a hold distinguishable from drift
        # (#865); without them it still holds, where HEAD is.
        notice source_pull_held "incomplete:$HOLD" "claudlobby root on $(hostname) is HELD by an incomplete hold ($HOLD): sha, by, reason and until are all required -- complete it in $OVERRIDE_HINT"
        h_sha=""
    elif [[ "$h_until" < "$today" ]]; then
        # An expired hold keeps holding: lapsing would roll the very change it
        # protected, and the reason it was set may still stand.
        notice source_pull_held "expired:$HOLD" "claudlobby root on $(hostname) is HELD past its expiry ($HOLD) -- renew or remove it in $OVERRIDE_HINT"
    fi
    if [ -z "$h_sha" ]; then
        TARGET="HEAD"
    elif ! git -C "$ROOT" rev-parse --verify --quiet "${h_sha}^{commit}" >/dev/null; then
        notice source_pull_held "unknown:$h_sha" "claudlobby root on $(hostname) is HELD at a sha git does not know ($h_sha) -- fix the hold in $OVERRIDE_HINT"
        TARGET="HEAD"
    elif git -C "$ROOT" merge-base --is-ancestor HEAD "$h_sha"; then
        TARGET="$h_sha"                      # up to the hold, never past it
    else
        # HEAD is past (or beside) the hold: the job never moves backwards.
        notice source_pull_held "behind:$h_sha" "claudlobby root on $(hostname) is at $FROM, past its hold at $h_sha -- the job never moves backwards; a rollback is an operator act (see the pull-root failure page runbook)"
        TARGET="HEAD"
    fi
fi

BEHIND=$(git -C "$ROOT" rev-list --count "HEAD..$TARGET")
if [ "$BEHIND" -eq 0 ]; then
    if [ -n "$HOLD" ]; then OUTCOME=held; else OUTCOME=current; fi
    log "$OUTCOME at $FROM (target $TARGET)"; record; exit 0
fi

BLOCKER=$(repo_pull_blocker "$ROOT")
if [ -n "$BLOCKER" ]; then
    paths=$(git -C "$ROOT" status --porcelain \
        | awk 'NR <= 5 { print $NF } END { if (NR > 5) print "+" NR - 5 " more" }' | paste -sd, -)
    BLOCKER="$BLOCKER: ${paths:-?}"
    OUTCOME=blocked
    log "BLOCKED ($BLOCKER) — $BEHIND behind $TARGET, not pulling"
    notice source_pull_blocked "$BLOCKER" "claudlobby root on $(hostname) is $BEHIND commit(s) behind $TARGET and was NOT pulled: $BLOCKER. Host-local config belongs in $OVERRIDE_HINT; anything else there is someone's work or residue -- look before clearing it."
    record; exit 0
fi

git -C "$ROOT" diff --name-only HEAD "$TARGET" > "$RUN_DIR/changed.txt"
LIB_N=$(grep -c '^lib/' "$RUN_DIR/changed.txt" || true)
grep -q '^claudlobby/' "$RUN_DIR/changed.txt" && CL_CHANGED=true
MIGRATIONS=$(git -C "$ROOT" diff --name-only --diff-filter=A HEAD "$TARGET" -- claudlobby/plane/migrations/ \
    | awk -F/ '{print $NF}' | paste -sd' ' -)
if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN would fast-forward $BEHIND commit(s) $FROM -> $TARGET (lib files: $LIB_N, claudlobby/ changed: $CL_CHANGED, migrations: ${MIGRATIONS:-none})"
    exit 0
fi

# The fleets on this host (setup-fleets' discovery), and the critical event
# types each already had in the window before the pull: a type that was firing
# before is not the pull's.
FLEETS=()
for fy in "$ROOT"/local/*/fleet.yaml "$ROOT"/local/*/*/fleet.yaml; do
    [ -f "$fy" ] && FLEETS+=("$(basename "$(dirname "$fy")")")
done
T_PRE=$(iso_ago "$WATCH_S")
for f in ${FLEETS[@]+"${FLEETS[@]}"}; do
    python3 "$LIB_DIR/plane-lookup.py" --escalation --since "$T_PRE" --fleet "$f" --root "$ROOT" \
        > "$RUN_DIR/pre.$f" 2>>"$LOG" || {
        : > "$RUN_DIR/pre.$f"
        FINDINGS="${FINDINGS}$f: the pre-pull critical events could not be read, so a critical below may predate the pull; "
    }
done
UV_BEFORE=$(user_version)

T0=$(iso_ago 0)
# The two windows, for whoever reads this run later (and the tests).
printf '%s %s\n' "$T_PRE" "$T0" > "$RUN_DIR/window"
if ! git -C "$ROOT" merge --ff-only "$TARGET" > "$RUN_DIR/merge.out" 2>&1; then
    cat "$RUN_DIR/merge.out" >> "$LOG"
    OUTCOME=ff_failed
    why=$(awk 'NF { print; exit }' "$RUN_DIR/merge.out")
    log "FF FAILED — $FROM could not fast-forward to $TARGET: $why"
    notice source_pull_failed "$TARGET" "claudlobby root on $(hostname) could not fast-forward $FROM to $TARGET: ${why:-git gave no reason} -- a human has to look: git -C $ROOT status"
    record; exit 0
fi
cat "$RUN_DIR/merge.out" >> "$LOG"
TO=$(git -C "$ROOT" rev-parse --short HEAD)
OUTCOME=pulled
log "PULLED $FROM -> $TO ($BEHIND commit(s), lib files $LIB_N, claudlobby/ changed $CL_CHANGED, migrations ${MIGRATIONS:-none})"

if [ "$CL_CHANGED" = true ]; then
    for unit in claudlobby-plane-daemon claudlobby-plane-view; do
        rc=0
        svc_restart_host "$unit" >>"$LOG" 2>&1 || rc=$?
        case "$rc" in
        0) RESTARTED="${RESTARTED:+$RESTARTED }$unit" ;;
        2) log "no $unit installed here — nothing to restart" ;;
        *) FINDINGS="${FINDINGS}the restart of $unit failed (rc $rc); " ;;
        esac
    done
fi

# --- the watch ----------------------------------------------------------------
sleep "$WATCH_S"
changed_names=$(awk -F/ '{print $NF}' "$RUN_DIR/changed.txt" | sort -u | paste -sd' ' -)
for f in ${FLEETS[@]+"${FLEETS[@]}"}; do
    if python3 "$LIB_DIR/plane-lookup.py" --escalation --since "$T0" --fleet "$f" --root "$ROOT" \
            > "$RUN_DIR/post.$f" 2>>"$LOG"; then
        new=$(awk 'FILENAME == ARGV[1] { seen[$1 " " $2] = 1; next } NF >= 2 && !seen[$1 " " $2] { print $1 "/" $2 }' \
            "$RUN_DIR/pre.$f" "$RUN_DIR/post.$f" | paste -sd, -)
        [ -n "$new" ] && FINDINGS="${FINDINGS}$f: new critical $new; "
    else
        FINDINGS="${FINDINGS}$f: critical events could not be read; "
    fi
    if python3 "$LIB_DIR/plane-lookup.py" --events --fleet "$f" --since "$T0" --type script_error --root "$ROOT" \
            > "$RUN_DIR/errors.$f" 2>>"$LOG"; then
        hit=$(python3 -c '
import json, sys
changed = set(sys.argv[1].split())
for line in open(sys.argv[2]):
    try:
        row = json.loads(line)
    except ValueError:
        continue
    script = (row.get("data") or {}).get("script") or ""
    if script in changed:
        print("%s:%s" % (row.get("bot"), script))' "$changed_names" "$RUN_DIR/errors.$f" | sort -u | paste -sd, -)
        [ -n "$hit" ] && FINDINGS="${FINDINGS}$f: script_error from a script this pull changed ($hit); "
    else
        FINDINGS="${FINDINGS}$f: script_error could not be read; "
    fi
    if claudlobby_cli --fleet "$f" status --json > "$RUN_DIR/status.$f" 2>>"$LOG"; then
        silent=$(python3 -c '
import datetime as d, json, sys
at = lambda s: d.datetime.fromisoformat(s.replace("Z", "+00:00"))
pre, t0 = at(sys.argv[1]), at(sys.argv[2])
for bot in json.load(open(sys.argv[3])).get("bots", []):
    if bot.get("plane_unreachable"):
        print("!unreadable")
        sys.exit(0)
    hb = bot.get("last_heartbeat")
    if hb and pre <= at(hb) < t0:
        print(bot.get("name"))' "$T_PRE" "$T0" "$RUN_DIR/status.$f" | paste -sd, -)
        case "$silent" in
        '') ;;
        '!unreadable'*) FINDINGS="${FINDINGS}$f: heartbeats could not be read; " ;;
        *) FINDINGS="${FINDINGS}$f: no heartbeat since the pull from $silent; " ;;
        esac
    else
        FINDINGS="${FINDINGS}$f: heartbeats could not be read; "
    fi
done
# The restarts, judged by what the supervisor reports after the watch -- never
# by svc_restart_host's rc: systemctl returns 0 for a unit that dies a second
# later (measured in the #1883 review: rc 0, then NRestarts 0 -> 1 -> 2). A
# restarted unit is healthy only as active/running with NO restart by systemd
# since ours, which zeroed the counter (service_is_crash_looping, #1769). A
# supervisor that cannot say (launchd has no cheap counter) is recorded as
# not verified, never as healthy.
for unit in $RESTARTED; do
    service_is_crash_looping "$unit" || true
    case "$CRASH_LOOP_VERDICT" in
    unknown) RESTART_CHECK="${RESTART_CHECK:+$RESTART_CHECK }$unit:not-verified" ;;
    none) FINDINGS="${FINDINGS}$unit could not be judged after the restart (${CRASH_LOOP_STATE:-no state read}); " ;;
    over) FINDINGS="${FINDINGS}$unit is $CRASH_LOOP_STATE after the restart; " ;;
    *)
        if [ "$CRASH_LOOP_STATE" = active/running ] && [ "$CRASH_LOOP_RESTARTS" -eq 0 ]; then
            RESTART_CHECK="${RESTART_CHECK:+$RESTART_CHECK }$unit:active"
        else
            FINDINGS="${FINDINGS}$unit is $CRASH_LOOP_STATE, restarted $CRASH_LOOP_RESTARTS time(s) by systemd since the pull; "
        fi
        ;;
    esac
done
UV_AFTER=$(user_version)

if [ -z "$FINDINGS" ]; then
    VERDICT=clean
else
    VERDICT=paged
    msg="root pull $FROM..$TO on $(hostname) ($BEHIND commit(s)): ${FINDINGS%; }. To pin this host: host.jobs.pull-root.hold with sha $FROM, by, reason and until, in $OVERRIDE_HINT."
    if [ -n "$MIGRATIONS" ]; then
        msg="$msg This pull added plane migration(s) $MIGRATIONS (user_version ${UV_BEFORE:-?} -> ${UV_AFTER:-?}); old code refuses a newer db and does not spool, so to roll back below them: set the hold at $FROM; stop claudlobby-plane-daemon; PRAGMA user_version = ${UV_BEFORE:-?} ONLY if every one is additive and idempotent (CREATE ... IF NOT EXISTS) -- a rebuild is fix-forward only; git -C $ROOT reset --keep $FROM; start the daemon."
    fi
    emit_failure_alert "$BOTS_DIR" source_pull_regression "$msg" || true
fi
record
exit 0
