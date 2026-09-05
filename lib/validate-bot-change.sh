#!/bin/bash
# validate-bot-change.sh — the Deliver → Add config → Recompose → Observe loop,
# made runnable for the observability/trust-loop behaviors.
#
# Unit tests prove COMPOSITION (the env var lands in bot.conf). Only running the
# code proves BEHAVIOR (the event actually fires). This harness stands up a
# throwaway bot + tmux sessions in a temp root, drives the observability sweep,
# and ASSERTS that activity_stuck + overdue_dispatch events are emitted and that
# the manager is notified — no Claude auth or real fleet required.
#
# Use it as the "Observe" step when changing observability behavior, and as the
# worked example of the loop for other bot-behavior changes (see
# documentation/validating-bot-changes.md).
#
# Usage: bash lib/validate-bot-change.sh
#   Exit 0 = all observations matched intent, 1 = a behavior did not fire.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

if ! command -v tmux >/dev/null 2>&1; then
    echo "validate-bot-change: tmux required (it backs the observe step)" >&2
    exit 2
fi

# --- Per-bot socket isolation (mirror production) ----------------------------
# Each bot now runs its OWN tmux server. The scripts under test resolve a bot's
# socket via tmux_socket_for_bot(); for these hand-written bot.confs (BOT_SERVICE
# left empty) that resolves to the F2 test-harness fallback "tmux-<dir-slug>",
# which only applies when FLEET_NAME is unset — so unset it here. Then shadow
# `tmux` so every session op in THIS harness lands on that session's private
# server (-L tmux-<name>), matching what the scripts resolve. The scripts call
# "$_TMUX_BIN" in their own subprocesses, so this function never affects them.
unset FLEET_NAME
# Also drop an ambient CLAUDE_FLAGS: the fixture bot.confs deliberately omit it
# (minimal composed-looking confs), so a dev's exported CLAUDE_FLAGS would leak
# into the start-bot.sh boots and MASK a regression that CI (clean env) exposes.
unset CLAUDE_FLAGS

# Compress BOTH #860 budgets. This harness stubs `claude` with `exec cat`, so its
# panes never draw a box BY CONSTRUCTION — which means every send here classifies
# as never-drawn and pays both waits: the 45s readiness budget, and then the 12s
# recovery poll that a never-drawn send earns, across 18 start-bot invocations.
# Uncompressed that is minutes of waiting for a TUI the stub cannot render, and
# the settle sleep on top of it. PANE_READY_TICKS is deliberately NOT set: start-bot
# arms its own per-call value, and every other caller runs at the production default.
# it timed out this harness at 120s when only the first was compressed.
# Nothing about the behaviour under test changes; only the wait for a box that
# will never appear. The real budgets are exercised against real boots in
# lib/boot-strand-sampler.sh, which is where they belong, and the unit contract
# is pinned in tests/test_pane_send_verified.sh.
export PANE_READY_POLL_S=0.05 PANE_RECOVER_TICKS=2 PANE_SEND_SETTLE_S=0
# Pin the escalation chat id for the WHOLE run (#846). fleet-pulse's critical
# alerts do NOT travel through MANAGER_TMUX — the isolation this harness provides
# by shadowing tmux — they go straight out via tg-post.sh, keyed on this var. So
# every fleet-pulse invocation below inherits the real fleet chat unless it is
# overridden, and the send is `2>/dev/null || true`, so a harness run that reaches
# production succeeds silently. It has: a two-throwaway-bot scenario crosses the
# session_missing threshold of 2 and paged a human.
#
# Run-wide, not per call site: the harness drives fleet-pulse from ten places and
# only the one scenario that deliberately tests escalation used to set this.
# Isolation is the default here; that scenario re-exports the same fake id
# explicitly, which is the shape it should have had from the start.
export FLEET_PULSE_ESCALATION_CHAT_ID="-100999"
vsock() { printf 'tmux-%s' "$1"; }
tmux() {
    local i sock=""
    local -a a=("$@")
    for ((i = 0; i < ${#a[@]}; i++)); do
        case "${a[i]}" in
            -t | -s) [ $((i + 1)) -lt ${#a[@]} ] && sock="$(vsock "${a[i + 1]}")"; break ;;
        esac
    done
    if [ -n "$sock" ]; then
        command tmux -L "$sock" "$@"
    else
        command tmux "$@"
    fi
}

FLEET="valfleet"
BOT="valbot"
MGR="valmgr"
IBOT="validle"
BUSY="valbusy"
SBOT="valsubmit"
MBOT="valmarker"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate.XXXXXX")"
# #586: a per-run tmux namespace. Concurrent runs (parallel reviews, pytest)
# otherwise share ${TMUX_TMPDIR:-/tmp}/tmux-<uid>/ and collide on the fixed
# per-session socket names (tmux-<name>) — duplicate-session aborts, and one
# run's cleanup kill-server tears down the other's live sessions. Exporting
# TMUX_TMPDIR moves every socket — the harness's own clients AND the scripts
# under test, which inherit the env when resolving the same names — into a
# run-private dir. Literal /tmp, not $TMPDIR: socket paths must stay within
# sun_path (108 bytes) on hosts with long TMPDIRs (macOS /var/folders,
# per-session scratch dirs). Contract for the fixture bot.confs below: they
# must NOT carry the composed "export TMUX_TMPDIR=/tmp" pin (composer.py) —
# keepalive/start-bot SOURCE bot.conf, and a sourced pin would yank the
# scripts under test back into the shared namespace mid-run.
TMUX_TMPDIR="$(mktemp -d /tmp/claudlobby-validate-sock.XXXXXX)"
export TMUX_TMPDIR
# fleet-pulse resolves bots via resolve_bots_dir <fleet> = local/<fleet>/runtime/bots.
BOT_DIR="$ROOT/local/$FLEET/runtime/bots/$BOT"
install_error_trap "$BOT_DIR"
EVENTS="$BOT_DIR/data/events"   # the marker/idle files still live under data/; no event file does (F18 R1)

# ---------------------------------------------------------------------------
# The plane is the ONLY record every lib/ door writes (F18 closure, R1). No
# door appends a JSONL row any more, so every observation this harness makes
# of a fleet event, a dispatch or a report is read back FROM THE PLANE, and
# every ledger row it used to seed is seeded AS PLANE ROWS instead.
#
# Every door emits through lib/plane-emit.sh; with no daemon here the shim
# takes its cold-CLI rung, so the CLI is a prerequisite of the whole harness
# (as tmux is) rather than a leg that may skip. Each throwaway root gets its
# own plane db at <root>/state/plane/plane.db. PLANE_SOCKET names a socket
# that does not exist, at a SHORT path (sun_path is 104 bytes on macOS).
# ---------------------------------------------------------------------------
VAL_REPO="$(cd "$LIB_DIR/.." && pwd)"
VAL_CLI=""
if [ -x "$VAL_REPO/.venv/bin/claudlobby" ]; then
    VAL_CLI="$VAL_REPO/.venv/bin/claudlobby"
elif command -v claudlobby >/dev/null 2>&1; then
    VAL_CLI="$(command -v claudlobby)"
fi
if [ -z "$VAL_CLI" ]; then
    echo "validate-bot-change: no claudlobby CLI resolvable (python3 -m venv .venv && ./.venv/bin/python -m pip install -e '.[dev]') — the plane is the only record the doors write, so nothing can be observed without it" >&2
    exit 2
fi
export PLANE_EMIT_CLI="$VAL_CLI"
export PLANE_SOCKET="/tmp/vbc-nosock-$$"
# Every door reads its root from the environment (the shim defaults to its own
# parent dir otherwise — a stub lib dir sourced from this shell would land rows
# in the checkout). Scenarios with their own root pass theirs inline.
export CLAUDLOBBY_ROOT="$ROOT"
# The fleet anchor for the doors. Production's timer units stamp
# CLAUDLOBBY_FLEET and a session's bot.conf carries FLEET_NAME; a hand-run
# script carries neither, and a fleet event with no fleet is NOT recorded.
# The harness plays the unit: CLAUDLOBBY_FLEET, never FLEET_NAME (the
# socket-fallback contract above needs FLEET_NAME unset).
export CLAUDLOBBY_FLEET="$FLEET"

# val_events <root> <fleet> [bot|fleet|""] [type] [since-iso]: the fleet's
# events rendered as the legacy JSONL rows, oldest first, from the plane — so
# every grep this harness ever made on a fleet-<day>.jsonl works unchanged on
# the output. A bot's own events by name; the fleet-level receipts (the old
# fleet-anchored plane row) as "fleet"; empty = nothing recorded, unreachable = empty
# too (the assertion that expected a row then fails, which is the honest
# reading of an instrument that cannot answer).
val_events() {
    local root="$1" fleet="$2" bot="${3:-}" type="${4:-}" since="${5:-}"
    set -- --root "$root" --events --fleet "$fleet"
    [ -n "$bot" ] && set -- "$@" --bot "$bot"
    [ -n "$type" ] && set -- "$@" --type "$type"
    [ -n "$since" ] && set -- "$@" --since "$since"
    python3 -S -E "$LIB_DIR/plane-lookup.py" "$@" 2>/dev/null || true
}
# val_sql <root> <sql>: one read of a throwaway root's plane db.
val_sql() { sqlite3 "$1/state/plane/plane.db" "$2" 2>/dev/null || true; }
# val_iso <epoch>: the instant as the doors stamp it.
val_iso() { epoch_to_iso_utc "$1"; }
# val_plane_ready <root> <fleet>: the plane db exists (a first fleet-level
# receipt through the real door creates it). A fleet with no manifest gets an
# EMPTY one (parse_fleet_bots reads an empty bots map exactly like a missing
# file — every dir is scanned, as before). Nothing is declared: since the F18
# closure (R3) every reader reads the plane alone, no flag, no declaration.
val_plane_ready() {
    local root="$1" fleet="$2" fdir
    fdir="$(CLAUDLOBBY_ROOT="$root" resolve_fleet_dir "$fleet" 2>/dev/null || true)"
    [ -n "$fdir" ] || fdir="$root/local/$fleet"
    mkdir -p "$fdir"
    [ -f "$fdir/fleet.yaml" ] || printf 'fleet:\n  name: %s\n  bots: {}\n' "$fleet" > "$fdir/fleet.yaml"
    CLAUDLOBBY_ROOT="$root" CLAUDLOBBY_FLEET="$fleet" emit_fleet_event validate_started harness '{}' "" >/dev/null 2>&1 || true
    mkdir -p "$root/state" "$fdir/runtime"
}
# val_link_plane_shim <libdir>: a stub lib dir that carries a symlinked or
# copied lib-common.sh reaches the shim by ITS OWN path, so the shim and the
# stdlib readers the doors consult must sit beside it.
val_link_plane_shim() {
    local d="$1" f
    for f in plane-emit.sh plane-socket-client.py plane-lookup.py plane-readers.py dispatch-overdue.py dispatch-supersede-hint.py; do
        [ -e "$d/$f" ] || ln -s "$LIB_DIR/$f" "$d/$f"
    done
}
# val_seed_dispatch <root> <fleet> <manager> <bot> <task_id> <dispatched_epoch> [expected_epoch] [body]:
# the construct triple + transmission the REAL dispatch door emits, at the
# instants given — the plane's twin of the ledger row this harness used to
# printf (the ids derive from the row, so a re-seed is a duplicate, never a
# second dispatch).
val_seed_dispatch() {
    local root="$1" fleet="$2" mgr="$3" bot="$4" tid="$5" at="$6" exp="${7:-}" body="${8:-task $5}"
    local wi asg msg iso f deadline="" safe_body
    wi="wi_$(sha256_hex32 "wi:$fleet:$bot:$tid:$at")"
    asg="asg_$(sha256_hex32 "asg:$fleet:$bot:$tid:$at")"
    msg="msg_$(sha256_hex32 "msg:$fleet:$bot:$tid:$at")"
    iso="$(val_iso "$at")"
    safe_body="$(json_escape "$body")"
    [ -n "$exp" ] && deadline=",\"expected_by\":\"$(val_iso "$exp")\""
    f="$(safe_mktemp)"
    cat > "$f" <<JSON
{"events":[
{"event_type":"work_item","emitter":"dispatch-task","source_ref":"dispatch-log:$tid","fleet":"$fleet","occurred_at":"$iso","payload":{"work_item_id":"$wi","title":"$safe_body","created_by":"bot:$fleet/$mgr"}},
{"event_type":"assignment","emitter":"dispatch-task","source_ref":"dispatch-log:$tid","fleet":"$fleet","occurred_at":"$iso","payload":{"assignment_id":"$asg","work_item_id":"$wi","assignee":"bot:$fleet/$bot","assigned_by":"bot:$fleet/$mgr"$deadline,"dispatch_msg_id":"$msg"}},
{"event_type":"communication","emitter":"dispatch-task","source_ref":"dispatch-log:$tid","fleet":"$fleet","occurred_at":"$iso","payload":{"msg_id":"$msg","sender":"bot:$fleet/$mgr","recipient_raw":"$bot","message_class":"task_request","command_type":"task","work_item_id":"$wi","assignment_id":"$asg","body":"$safe_body"}},
{"event_type":"transmission","emitter":"dispatch-task","fleet":"$fleet","occurred_at":"$iso","payload":{"msg_id":"$msg","attempt_no":1,"carrier":"tmux","destination":"$bot","state":"pane_submitted"}}
]}
JSON
    "$VAL_CLI" --root "$root" emit-batch --json "$f" >/dev/null 2>&1 \
        || echo "validate-bot-change: seeding dispatch $tid for $bot failed" >&2
    rm -f "$f"
}
# val_seed_report <root> <fleet> <bot> <task_id|""> <status> <epoch> [summary] [manager]:
# the report communication + what the REAL report door lands with it — the
# task event on the assignment carrying <task_id> for <bot> when the plane
# holds one, else the report_status marker a report that resolved nothing
# carries (the idle-worker check reads either) — the twin of a report ledger row.
val_seed_report() {
    local root="$1" fleet="$2" bot="$3" tid="$4" status="$5" at="$6" summary="${7:-x}" mgr="${8:-$MGR}"
    local msg iso ids wi asg link="" ev="" f ref safe_summary
    msg="msg_$(sha256_hex32 "rmsg:$fleet:$bot:$tid:$at:$status")"
    iso="$(val_iso "$at")"; ref="report-back:$msg"; safe_summary="$(json_escape "$summary")"
    if [ -n "$tid" ]; then
        ids=$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$root" --task-id "$tid" \
            --assignee "bot:$fleet/$bot" 2>/dev/null || true)
        if [ -n "$ids" ]; then
            wi=${ids%% *}; ids=${ids#* }; asg=${ids%% *}
            link="\"work_item_id\":\"$wi\",\"assignment_id\":\"$asg\","
        fi
    fi
    case "$status" in
        completed) ev=completed ;; failed) ev=failed ;; blocked) ev=returned_blocked ;; progress) ev=progress ;;
    esac
    f="$(safe_mktemp)"
    {
        printf '{"events":[{"event_type":"communication","emitter":"report-back","source_ref":"%s","fleet":"%s","occurred_at":"%s","payload":{"msg_id":"%s","sender":"bot:%s/%s","recipient":"bot:%s/%s","recipient_raw":"%s","message_class":"report",%s"body":"%s"}}' \
            "$ref" "$fleet" "$iso" "$msg" "$fleet" "$bot" "$fleet" "$mgr" "$mgr" "$link" "$safe_summary"
        if [ -n "$link" ] && [ -n "$ev" ]; then
            printf ',{"event_type":"task","emitter":"report-back","source_ref":"%s","fleet":"%s","occurred_at":"%s","payload":{%s"event":"%s","actor":"bot:%s/%s","summary":"%s"}}' \
                "$ref" "$fleet" "$iso" "$link" "$ev" "$fleet" "$bot" "$safe_summary"
        elif [ -n "$ev" ] && [ "$ev" != progress ]; then
            printf ',{"event_type":"system","emitter":"report-back","source_ref":"%s","fleet":"%s","occurred_at":"%s","payload":{"event":"report_status","subject_kind":"actor","subject":"bot:%s/%s","data":{"status":"%s","msg_id":"%s"}}}' \
                "$ref" "$fleet" "$iso" "$fleet" "$bot" "$status" "$msg"
        fi
        printf ']}\n'
    } > "$f"
    "$VAL_CLI" --root "$root" emit-batch --json "$f" >/dev/null 2>&1 \
        || echo "validate-bot-change: seeding a $status report by $bot failed" >&2
    rm -f "$f"
}

cleanup() {
    # Per-bot servers must be torn down with kill-server, or empty servers leak.
    for _s in "$BOT" "$MGR" "$IBOT" "$BUSY" "$SBOT" "$MBOT" "${HBOT:-}" "${RB_SESSION:-}" "${MP_SESSION:-}" "${IDLEK:-}" "${SOCKB:-}" "${BUSYM:-}" "${BUSYP:-}" "${BRIEF:-}" "${BRIEFBUSY:-}" "${SINK:-}"; do
        [ -n "$_s" ] && command tmux -L "$(vsock "$_s")" kill-server 2>/dev/null || true
    done
    # Bridge-hijack pollers are plain bun processes, not tmux panes — TERM any
    # still-alive ones so a mid-scenario abort never leaks a poller.
    # shellcheck disable=SC2086
    for _p in ${BH_PIDS:-}; do kill -TERM "$_p" 2>/dev/null || true; done
    # The #1002 boot probe installs a REAL systemd user unit. Torn down from the
    # trap and ONLY the trap, so an abort mid-scenario cannot leave an enabled
    # throwaway unit behind on a production host.
    if [ -n "${BP_SVC:-}" ]; then
        systemctl --user stop "$BP_SVC" >/dev/null 2>&1 || true
        rm -f "$HOME/.config/systemd/user/$BP_SVC.service"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user reset-failed "$BP_SVC" >/dev/null 2>&1 || true
        command tmux -L "$BP_SVC" kill-server 2>/dev/null || true
    fi
    # The plane leg's daemon is a plain background process (never a unit, so
    # it can never self-boot) — but an abort between its start and its inline
    # kill would leak it until reboot. Trap-owned teardown, same rule as the
    # boot probe above: the trap and ONLY the trap guarantees it.
    if [ -n "${PL_DPID:-}" ]; then
        kill "$PL_DPID" 2>/dev/null || true
    fi
    [ -n "${PL_ROOT:-}" ] && rm -rf "$PL_ROOT" 2>/dev/null
    [ -n "${PL_SOCKDIR:-}" ] && rm -rf "$PL_SOCKDIR" 2>/dev/null
    rm -rf "$ROOT" "${RB_ROOT:-}" "${WR_ROOT:-}" "${BP_ROOT:-}" "${SC_ROOT:-}" "$TMUX_TMPDIR"
}
trap cleanup EXIT

mkdir -p "$BOT_DIR/data" "$ROOT/state"

# --- Add config: a composed-looking bot.conf with low thresholds for a fast loop ---
cat > "$BOT_DIR/bot.conf" <<CONF
BOT_NAME="$BOT"
BOT_ID="$BOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD=1
TELEGRAM_BOT_HANDLE="$BOT"
OBSERVABILITY_BRIDGE_DOWN_GRACE=0
CONF

# --- Run: stand up a non-idle worker pane + a manager session to receive alerts ---
tmux new-session -d -s "$MGR" "sleep 600"
tmux new-session -d -s "$BOT" 'printf "\n⠹ Cogitating (esc to interrupt)\n"; sleep 600'
sleep 1  # let panes render

# Worker made a tool call a moment ago, then went silent (gap will exceed 1s).
touch "$BOT_DIR/data/.last-tool-call"

# A task was dispatched and is already past its deadline, with no report —
# on the plane, where the watchdog now reads it (the fleet declared first).
now=$(date +%s)
val_plane_ready "$ROOT" "$FLEET"
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$BOT" t-1-aaaa "$((now - 600))" "$((now - 10))" "do x"

sleep 2  # ensure activity gap > threshold (1s)

# --- Observe: run the real pulse against the scratch fleet ---
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$FLEET" >/dev/null 2>&1 || true

# --- Assert ---
pass=0; fail=0
events_rows="$(val_events "$ROOT" "$FLEET" "$BOT")"
mgr_pane=$(tmux capture-pane -t "$MGR" -p 2>/dev/null || true)

echo "=== validate-bot-change: observe the trust-loop behaviors ==="
printf '%s' "$events_rows" | grep -q '"type":"activity_stuck"' && r=yes || r=no
harness_check "activity_stuck event emitted (animated-but-hung worker)" "$r"
printf '%s' "$events_rows" | grep -q '"type":"overdue_dispatch"' && r=yes || r=no
harness_check "overdue_dispatch event emitted (deadline passed, no report)" "$r"
printf '%s' "$mgr_pane" | grep -q '\[FLEET-PULSE\]' && r=yes || r=no
harness_check "manager notified via [FLEET-PULSE] push" "$r"
printf '%s' "$events_rows" | grep -q '"type":"bridge_down"' && r=yes || r=no
harness_check "bridge_down event emitted (live session, Telegram poller not delivering)" "$r"
ls "$EVENTS"/*.jsonl >/dev/null 2>&1 && r=no || r=yes
harness_check "no legacy event file was written by the sweep (the plane is the only record — F18 R1)" "$r"

# #460: a never-closing dispatch must age out of the overdue set so fleet-pulse
# stops re-emitting overdue_dispatch every cycle. Drive the real matcher (the CLI
# fleet-pulse consumes) with a 25h-old, never-reported dispatch and assert nothing.
# The retired ledgers' paths survive only as the #1187 shape probe's junk
# inputs below (a path in the bot slot); the matcher answers from the plane.
VAL_REPORT_LEDGER="$ROOT/local/$FLEET/runtime/report-back.jsonl"
VAL_DISPATCH_LOG="$ROOT/state/dispatch-log.jsonl"
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" valaged t-aged-0000 "$((now - 90000))" "$((now - 89400))" "x"
aged_out=$(python3 "$LIB_DIR/dispatch-overdue.py" --all "$(date +%s)" \
    --fleet "$FLEET" --root "$ROOT" 2>/dev/null | grep -c "^valaged " || true)
[ "${aged_out:-1}" -eq 0 ] && r=yes || r=no
harness_check "overdue_dispatch expires past max age (#460 — no re-emit for a 25h-old dispatch)" "$r"

# ===========================================================================
# Task-id end-to-end (goal-aware plan P4) — the dispatch row seeded above
# carries task_id t-1-aaaa; the REAL fleet-pulse run consumed it. Assert the
# id made it through the pipeline: into the emitted overdue event, and into
# the manager nudge with the self-heal echo instruction. (Join-matrix unit
# semantics live in tests/test_dispatch_overdue.py — not re-run here.)
echo ""
echo "=== validate task-id end-to-end (P4: event + nudge carry the id) ==="
printf '%s' "$events_rows" | grep '"type":"overdue_dispatch"' | grep -q '"task_id":"t-1-aaaa"' && r=yes || r=no
harness_check "overdue_dispatch event carries the dispatch task_id" "$r"
printf '%s' "$mgr_pane" | grep -q 'no report has closed' && printf '%s' "$mgr_pane" | grep -q 't-1-aaaa' && r=yes || r=no
harness_check "manager nudge names the open id (for the manager to act on)" "$r"

# ===========================================================================
# #835 — the two halves that stop the watchdog crying wolf over finished work.
# Both drive the REAL scripts: report-back.sh for the resolve, fleet-pulse.sh
# for the orphan split. Unit semantics are in tests/test_dispatch_overdue.py;
# what only running the code can prove is that the id actually lands in the
# ledger and that the pulse actually stops emitting.
# ===========================================================================
echo ""
echo "=== validate #835: an id-less report closes its dispatch; a respawn orphan goes quiet ==="

# --- Half 1: report-back.sh with NO --task must resolve the open dispatch. ---
T835_BOT="valrb835"
T835_DIR="$ROOT/local/$FLEET/runtime/bots/$T835_BOT"
mkdir -p "$T835_DIR/data"
cat > "$T835_DIR/bot.conf" <<CONF
BOT_NAME="$T835_BOT"
BOT_ID="$T835_BOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
t835_dispatch="$VAL_DISPATCH_LOG"
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$T835_BOT" t-835-0001 "$((now - 600))" "$((now - 10))" "do y"

# Deliberately NO --task, the way every worker actually calls it.
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" MANAGER_TMUX="$MGR" \
    "$LIB_DIR/report-back.sh" "$T835_BOT" completed "finished the thing" >/dev/null 2>&1 || true

t835_ledger="$VAL_REPORT_LEDGER"
# The resolved id lands as the report's task event on THAT assignment (the
# plane's twin of the ledger row's stamped task_id).
t835_closed=$(val_sql "$ROOT" "SELECT COUNT(*) FROM events e JOIN assignments a ON a.assignment_id = e.assignment_id WHERE e.kind = 'task' AND e.event = 'completed' AND a.source_ref = 'dispatch-log:t-835-0001'")
[ "${t835_closed:-0}" -eq 1 ] && r=yes || r=no
harness_check "#835 report-back without --task lands its task event on the resolved dispatch (the plane's stamped id)" "$r"

# The join is unchanged — so the row closing is proof the id is the RIGHT one.
t835_left=$(python3 "$LIB_DIR/dispatch-overdue.py" --all "$(date +%s)" \
    --fleet "$FLEET" --root "$ROOT" 2>/dev/null | grep -c "^$T835_BOT " || true)
[ "${t835_left:-1}" -eq 0 ] && r=yes || r=no
harness_check "#835 the resolved id actually closes the dispatch (watchdog join untouched)" "$r"

# A second id-less report with nothing open must stay id-less, not grab a peer's:
# no new task event, and the report_status marker a resolved-nothing report carries.
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" MANAGER_TMUX="$MGR" \
    "$LIB_DIR/report-back.sh" "$T835_BOT" completed "and again" >/dev/null 2>&1 || true
t835_tasks=$(val_sql "$ROOT" "SELECT COUNT(*) FROM events e JOIN identity_registry i ON i.uid = e.actor_uid WHERE e.kind = 'task' AND i.alias = 'bot:$FLEET/$T835_BOT'")
t835_marker=$(val_sql "$ROOT" "SELECT COUNT(*) FROM events e WHERE e.kind = 'system' AND e.event = 'report_status' AND e.subject_alias = 'bot:$FLEET/$T835_BOT'")
{ [ "${t835_tasks:-0}" -eq 1 ] && [ "${t835_marker:-0}" -ge 1 ]; } && r=yes || r=no
harness_check "#835 nothing open -> report stays id-less (no scavenging a peer's row; the report_status marker lands instead)" "$r"

# --- Half 2: a respawn orphan must stop reaching the pulse's overdue path. ---
# Same bot dir, but .spawn is now NEWER than the dispatch: the session that
# received the id is gone, so it can never be echoed.
OR_BOT="valor835"
OR_DIR="$ROOT/local/$FLEET/runtime/bots/$OR_BOT"
mkdir -p "$OR_DIR/data/events"
cat > "$OR_DIR/bot.conf" <<CONF
BOT_NAME="$OR_BOT"
BOT_ID="$OR_BOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$OR_BOT" t-835-0002 "$((now - 600))" "$((now - 10))" "do z"
touch "$OR_DIR/data/.spawn"   # respawned just now, i.e. after the dispatch

CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$FLEET" >/dev/null 2>&1 || true

# Scope the match to overdue_dispatch specifically: the orphan DOES get a
# dispatch_orphaned event in this same file, and a bare task-id grep would
# match that and read a correctly-silenced alarm as a firing one.
or_overdue=$(val_events "$ROOT" "$FLEET" "$OR_BOT" overdue_dispatch | grep -c 't-835-0002' || true)
[ "${or_overdue:-1}" -eq 0 ] && r=yes || r=no
harness_check "#835 respawn orphan emits NO overdue_dispatch from the real pulse" "$r"

or_listed=$(python3 "$LIB_DIR/dispatch-overdue.py" --orphans "$(date +%s)" \
    --bots-dir "$ROOT/local/$FLEET/runtime/bots" --fleet "$FLEET" --root "$ROOT" 2>/dev/null | grep -c 't-835-0002' || true)
[ "${or_listed:-0}" -ge 1 ] && r=yes || r=no
harness_check "#835 the orphan is still listable (evidence kept, not reaped away)" "$r"

# Inert for the ALARM, but recorded once — a task lost to a restart is
# actionable, and silence would trade this issue's noise for #826/#831/#833's.
or_ev=$(val_events "$ROOT" "$FLEET" "$OR_BOT" dispatch_orphaned | grep -c 't-835-0002' || true)
[ "${or_ev:-0}" -eq 1 ] && r=yes || r=no
harness_check "#835 the orphan is recorded once as dispatch_orphaned" "$r"

# Latched on id-set membership, so a second sweep must not re-record it.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$FLEET" >/dev/null 2>&1 || true
or_ev2=$(val_events "$ROOT" "$FLEET" "$OR_BOT" dispatch_orphaned | grep -c 't-835-0002' || true)
[ "${or_ev2:-0}" -eq 1 ] && r=yes || r=no
harness_check "#835 a second sweep does NOT re-record the same orphan (latch holds)" "$r"

# ===========================================================================
# #1481 chunk M-A — the manager's two acts on ONE open task.
#
# Unit tests pin the resolution and the vocabulary. What only running the real
# doors proves is the pair of facts the acts exist for and that no unit test
# reaches: a WITHDRAWN row actually leaves the matcher's open set (the
# watchdog stops crying wolf over a send that never landed), and an ESCALATED
# one actually does NOT -- `escalated` is non-terminal by ruling, so the work
# survives the human deciding, and the only thing that can see it is the
# dedicated read fleet-pulse will page from.
#
# The reason and the question are CONTENT, so this rig -- which carries no
# capture.json, i.e. metadata mode -- deliberately asserts the ARM and the
# person rather than the prose. The text round-trip is pinned under full
# capture in tests/test_task_loop_doors.py; a harness that flipped the
# capture policy mid-run would be testing the policy, not the door.
# ===========================================================================
echo ""
echo "=== validate #1481: withdraw closes a row, escalate keeps it open ==="

# NO bot directory for the worker, deliberately. Every door under test here
# reads the PLANE (the acts, the matcher, the escalated read) and none needs
# one -- while a directory under the fleet's bots dir is what makes
# fleet-pulse health-check a bot, so creating one would add a session_missing
# push to the shared manager pane on every later sweep and scroll the line a
# LATER scenario captures out of view. Measured: with the directory, the
# #1024 pane assertion failed on both runs and passed on the baseline.
TA_BOT="valact1481"
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$TA_BOT" t-1481-0001 "$((now - 600))" "$((now + 3600))" "the undelivered one"
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$TA_BOT" t-1481-0002 "$((now - 600))" "$((now + 3600))" "the twin id"

# --- withdraw: run as the MANAGER, the way a manager session would ---
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" BOT_ID="$MGR" BOT_NAME="$MGR" \
    "$LIB_DIR/task-act.sh" withdraw t-1481-0001 --reason "the broadcast never landed" \
    > "$ROOT/ta-withdraw.out" 2> "$ROOT/ta-withdraw.err" || true

ta_cancel=$(val_sql "$ROOT" "SELECT COUNT(*) FROM events e JOIN assignments a ON a.assignment_id = e.assignment_id WHERE e.kind = 'task' AND e.event = 'cancelled' AND a.source_ref = 'dispatch-log:t-1481-0001'")
[ "${ta_cancel:-0}" -eq 1 ] && r=yes || r=no
harness_check "#1481 task-act.sh withdraw lands ONE cancelled task event on the resolved assignment" "$r"

ta_by=$(val_sql "$ROOT" "SELECT json_extract(e.detail, '\$.by') FROM events e JOIN assignments a ON a.assignment_id = e.assignment_id WHERE e.kind = 'task' AND e.event = 'cancelled' AND a.source_ref = 'dispatch-log:t-1481-0001'")
[ "$ta_by" = "$MGR" ] && r=yes || r=no
harness_check "#1481   ...stamped with WHO withdrew it (the manager, by name)" "$r"

ta_open=$(python3 "$LIB_DIR/dispatch-overdue.py" --open "$TA_BOT" \
    --fleet "$FLEET" --root "$ROOT" 2>/dev/null | grep -c 't-1481-0001' || true)
[ "${ta_open:-1}" -eq 0 ] && r=yes || r=no
harness_check "#1481 the withdrawn row leaves the matcher OPEN set (the watchdog stops chasing it)" "$r"

# The refusal that keeps a manager from cancelling the wrong worker: a second
# OPEN assignment under the same id, on another bot.
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$T835_BOT" t-1481-0002 "$((now - 500))" "$((now + 3600))" "a twin id on another bot"
ta_amb_rc=0
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" BOT_ID="$MGR" BOT_NAME="$MGR" \
    "$LIB_DIR/task-act.sh" withdraw t-1481-0002 --reason "which one?" \
    >/dev/null 2> "$ROOT/ta-amb.err" || ta_amb_rc=$?
ta_amb_events=$(val_sql "$ROOT" "SELECT COUNT(*) FROM events e JOIN assignments a ON a.assignment_id = e.assignment_id WHERE e.kind = 'task' AND a.source_ref = 'dispatch-log:t-1481-0002'")
{ [ "$ta_amb_rc" -eq 2 ] && grep -q "matches 2 open assignments" "$ROOT/ta-amb.err" \
    && [ "${ta_amb_events:-1}" -eq 0 ]; } && r=yes || r=no
harness_check "#1481 an id matching TWO open assignments is REFUSED, naming them, with nothing acted" "$r"

# --- escalate: a fresh id, so the ambiguity above cannot reach it ---
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$TA_BOT" t-1481-0003 "$((now - 400))" "$((now + 3600))" "the one with a question"
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" BOT_ID="$MGR" BOT_NAME="$MGR" \
    "$LIB_DIR/task-act.sh" escalate t-1481-0003 "do we ship without the migration" \
    > "$ROOT/ta-esc.out" 2> "$ROOT/ta-esc.err" || true

ta_esc_open=$(python3 "$LIB_DIR/dispatch-overdue.py" --open "$TA_BOT" \
    --fleet "$FLEET" --root "$ROOT" 2>/dev/null | grep -c 't-1481-0003' || true)
[ "${ta_esc_open:-0}" -ge 1 ] && r=yes || r=no
harness_check "#1481 an escalated task stays OPEN (non-terminal by ruling: the work survives the human)" "$r"

ta_esc=$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$ROOT" --escalated \
    --fleet "$FLEET" 2>/dev/null | grep -c 't-1481-0003' || true)
[ "${ta_esc:-0}" -eq 1 ] && r=yes || r=no
harness_check "#1481 plane-lookup --escalated lists it (the only read that can see a non-terminal raise)" "$r"

ta_esc_by=$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$ROOT" --escalated \
    --fleet "$FLEET" 2>/dev/null | grep 't-1481-0003' | cut -f3 || true)
[ "$ta_esc_by" = "$MGR" ] && r=yes || r=no
harness_check "#1481   ...naming who asked, which no capture mode strips" "$r"

# A later report is an ACT: the arm holds only while `escalated` is the
# assignment newest task event, so nothing has to remember to un-escalate.
#
# Deliberately addressed to a manager session that does not exist. The plane
# record is emitted BEFORE the send (report-back.sh, F9 intent-before-transport),
# so the fact under test lands either way -- and pointing this at the shared
# $MGR pane pushed two [BOTREPORT] lines into it, which scrolled the line a
# LATER scenario captures out of the visible pane and failed a #1024 push
# assertion that had nothing to do with this change. A harness scenario must
# not perturb its neighbours to make its own point.
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" MANAGER_TMUX="valnomgr1481" \
    "$LIB_DIR/report-back.sh" "$TA_BOT" progress "on it" --progress 30 \
    --task t-1481-0003 >/dev/null 2>&1 || true
ta_esc_after=$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$ROOT" --escalated \
    --fleet "$FLEET" 2>/dev/null | grep -c 't-1481-0003' || true)
[ "${ta_esc_after:-1}" -eq 0 ] && r=yes || r=no
harness_check "#1481 a later report CLEARS the escalation (no second door, nothing to reconcile)" "$r"

# ===========================================================================
# #1187 — a read door whose misuse was indistinguishable from "nothing open".
#
# --open, --open-task and single-bot mode each name ONE bot and take it first;
# --all/--orphans/--unassigned name none. Calling a bot-slot mode with the
# every-bot grammar keeps the ARITY valid, so a path lands in the bot slot,
# nothing matches, and it exits 0 printing nothing -- byte-identical to a real
# empty result. Wrong COUNT was always loud; only wrong ORDER was silent.
# Single-bot mode has the same grammar and is NOT gated; see the module docstring.
#
# Unit tests pin the matcher. What only running the real scripts can prove is
# the half that has no unit: report-back.sh:117 pipes --open STDOUT through
# awk to decide whether a supplied --task id is open, so the scope disclosure
# has to reach a human WITHOUT reaching that pipe. This path had no runtime
# coverage at all before #1187.
# ===========================================================================
echo ""
echo "=== validate #1187: --open refuses a mis-ordered call and states its scope ==="

T1187_BOT="val1187"
T1187_DIR="$ROOT/local/$FLEET/runtime/bots/$T1187_BOT"
mkdir -p "$T1187_DIR/data"
cat > "$T1187_DIR/bot.conf" <<CONF
BOT_NAME="$T1187_BOT"
BOT_ID="$T1187_BOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$T1187_BOT" t-1187-0001 "$((now - 600))" "$((now - 10))" "do w"

# THE defect: --all's grammar passed to --open. Three positionals, so the arity
# check passes and a ledger path is read as the bot name.
t1187_wrong_out=$(python3 "$LIB_DIR/dispatch-overdue.py" --open \
    "$t835_dispatch" "$VAL_REPORT_LEDGER" "$now" 2>/dev/null || true)
t1187_wrong_rc=0
python3 "$LIB_DIR/dispatch-overdue.py" --open \
    "$t835_dispatch" "$VAL_REPORT_LEDGER" "$now" >/dev/null 2>&1 || t1187_wrong_rc=$?
[ "$t1187_wrong_rc" -eq 2 ] && [ -z "$t1187_wrong_out" ] && r=yes || r=no
harness_check "#1187 mis-ordered --open is REFUSED (rc 2), not a silent empty result" "$r"

# The refusal has to name the remedy: the operator error is not knowing the two
# grammars differ, so "invalid argument" alone would leave them stuck.
python3 "$LIB_DIR/dispatch-overdue.py" --open \
    "$t835_dispatch" "$VAL_REPORT_LEDGER" "$now" 2>"$ROOT/t1187.err" >/dev/null || true
grep -q "expects <bot_id> first" "$ROOT/t1187.err" && r=yes || r=no
harness_check "#1187   ...and names the grammar split, not merely that it refused" "$r"

# Wrong COUNT was already loud before this change. Pinned so the shape gate is
# never mistaken for the thing that made misuse loud -- measuring THIS shape is
# what makes the real defect read as unreproducible.
t1187_arity_rc=0
python3 "$LIB_DIR/dispatch-overdue.py" --open \
    "$t835_dispatch" "$VAL_REPORT_LEDGER" >/dev/null 2>&1 || t1187_arity_rc=$?
[ "$t1187_arity_rc" -eq 2 ] && r=yes || r=no
harness_check "#1187 wrong ARITY was already loud and stays loud (the gate is about SHAPE)" "$r"

# STDOUT must stay rows-only. This is the assertion that protects report-back.
t1187_stdout=$(python3 "$LIB_DIR/dispatch-overdue.py" --open \
    "$T1187_BOT" --fleet "$FLEET" --root "$ROOT" 2>/dev/null || true)
printf '%s' "$t1187_stdout" | grep -q 't-1187-0001' \
    && ! printf '%s' "$t1187_stdout" | grep -q -- '--open:' && r=yes || r=no
harness_check "#1187 --open STDOUT is rows only (no scope header for awk to eat)" "$r"

# ...and the scope reaches a human, on stderr, even with ZERO rows -- the case
# the shape gate cannot reach (a typo, or another fleet's bot under #526).
python3 "$LIB_DIR/dispatch-overdue.py" --open \
    "nosuchbot-1187" --fleet "$FLEET" --root "$ROOT" 2>"$ROOT/t1187b.err" >/dev/null || true
grep -q "nosuchbot-1187" "$ROOT/t1187b.err" && grep -q "0 open" "$ROOT/t1187b.err" && r=yes || r=no
harness_check "#1187 an EMPTY result names the bot it filtered on (cannot read as nothing-exists)" "$r"

# The regression probe, through the REAL report-back.sh. With nothing open the
# supplied-id guard must stay fail-open (#1146): only a NON-EMPTY open set may
# contradict the caller. A scope line on stdout makes that set ["->"] and flags
# a correct report. Note the shape -- a bot HOLDING a row still matches its own
# id, so that case reads clean and would pass a placement that is actually broken.
CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$FLEET" MANAGER_TMUX="$MGR" \
    "$LIB_DIR/report-back.sh" "nobodyhome1187" completed "nothing open here" \
    --task "t-1187-0002" >/dev/null 2>"$ROOT/t1187c.err" || true
grep -q "is not open for" "$ROOT/t1187c.err" && r=no || r=yes
harness_check "#1187 report-back with NOTHING open raises no false supplied-id anomaly" "$r"

# ===========================================================================
# #1024 — the MIRROR watchdog: reported, then never re-dispatched.
#
# Unit tests prove the join. What only running the real pulse can prove is that
# the knob actually gates, that the manager exclusion actually excludes, and
# that a re-dispatched worker actually goes quiet — three places where a check
# that "works" in isolation still ships useless or noisy.
#
# The six-dispatch case below is the one that decides whether this design is
# right at all. It is a real pattern, not a contrived edge: a manager amending a
# task re-dispatches repeatedly, the worker answers only the last id, and every
# earlier row stays open forever. Any check keyed on open-dispatch-exists breaks
# on it in one of two directions.
# ===========================================================================
echo ""
echo "=== validate #1024: reported-but-never-re-dispatched (mirror watchdog) ==="

ua_iso() { python3 -c "import datetime,sys;print(datetime.datetime.fromtimestamp(int(sys.argv[1]),datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"; }

# ua_bot <name> <check:1|0> <manager_tmux> [trailing_comment]
# The composer writes the trailing comment on MANAGER bots only, so only the
# manager fixture gets one — and it must, because bot_conf_get strips quotes but
# NOT comments. bot_is_manager does its own stripping; putting the comment on a
# worker here would instead corrupt _manager_target and silently kill the push
# this section asserts on.
ua_bot() {
    local n="$1" chk="$2" mgr="$3" cmt="${4:-}" d="$ROOT/local/$FLEET/runtime/bots/$1"
    mkdir -p "$d/data/events"
    cat > "$d/bot.conf" <<CONF
BOT_NAME="$n"
BOT_ID="$n"
BOT_SERVICE=""
export MANAGER_TMUX=$mgr$cmt
OBSERVABILITY_UNASSIGNED_CHECK=$chk
OBSERVABILITY_UNASSIGNED_THRESHOLD=7200
OBSERVABILITY_UNASSIGNED_MAX_AGE=86400
CONF
}
# ua_report <bot> <reported_at_epoch> <status> [task_id] — the report on the plane
ua_report()   { val_seed_report "$ROOT" "$FLEET" "$1" "${4:-}" "$3" "$2" "x"; }
# ua_dispatch <bot> <dispatched_at_epoch> [task_id] — the dispatch on the plane.
# Ids are t-<digits>-<4 hex>: the plane link accepts only the minted grammar.
ua_dispatch() { val_seed_dispatch "$ROOT" "$FLEET" "$MGR" "$1" "${3:-t-1024-0000}" "$2" "$(($2 + 600))" "x"; }

ua_bot valunfire 1 "$MGR"      # armed, stranded  -> MUST fire
ua_bot valunret  1 "$MGR"      # armed, re-tasked -> must stay quiet
ua_bot valunoff  0 "$MGR"      # DEFAULT OFF      -> must stay quiet
ua_bot valunmgr  1 valunmgr "  # this bot is a manager"   # -> must stay quiet
ua_bot valunold  1 "$MGR"      # stale past cap   -> must stay quiet
ua_bot valunsix  1 "$MGR"      # the six-dispatch case -> MUST fire

# Stranded: dispatched 4h ago, reported terminal 3h ago, nothing since.
ua_dispatch valunfire "$((now - 14400))"; ua_report valunfire "$((now - 10800))" completed t-1024-0f1e
# Re-tasked AFTER reporting — the loop is intact, so this must NOT alarm. This is
# the positive control: without it, a check that fires on every terminal report
# would pass every other assertion here.
ua_dispatch valunret "$((now - 14400))"; ua_report valunret "$((now - 10800))" completed t-1024-0ae7
ua_dispatch valunret "$((now - 3600))"
# Default-off: identical stranded shape, knob absent.
ua_dispatch valunoff "$((now - 14400))"; ua_report valunoff "$((now - 10800))" completed t-1024-0aff
# Manager: no assigner exists, so this is its resting state, not a strand.
ua_dispatch valunmgr "$((now - 14400))"; ua_report valunmgr "$((now - 10800))" completed t-1024-0a9c
# Stale past the 24h cap: a known state, not an event.
ua_dispatch valunold "$((now - 111600))"; ua_report valunold "$((now - 108000))" completed t-1024-0a1d
# THE SIX-DISPATCH CASE. One logical task, amended six times in 35 minutes. The
# worker answers only the last id, so five rows stay open forever. It IS stranded
# and must be reported — while a check keyed on those open rows would either page
# about a healthy re-dispatching manager or read them as "still busy" and never
# fire at all, which is #1024 recurring inside its own watchdog.
ua_i=0
while [ "$ua_i" -lt 6 ]; do
    ua_dispatch valunsix "$((now - 14400 + ua_i * 300))" "t-1024-060$ua_i"
    ua_i=$((ua_i + 1))
done
ua_report valunsix "$((now - 10800))" completed t-1024-0605

CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/fleet-pulse.sh" "$FLEET" >/dev/null 2>&1 || true
ua_fired() { val_events "$ROOT" "$FLEET" "$1" worker_unassigned | grep -q '"type":"worker_unassigned"'; }

ua_fired valunfire && r=yes || r=no
harness_check "#1024 a worker reported-and-never-re-dispatched emits worker_unassigned" "$r"
ua_fired valunsix && r=yes || r=no
harness_check "#1024 fires through five stale OPEN dispatches (re-dispatch case, the common one)" "$r"
ua_fired valunret && r=no || r=yes
harness_check "#1024 a worker re-tasked after reporting stays quiet (positive control)" "$r"
ua_fired valunoff && r=no || r=yes
harness_check "#1024 DEFAULT OFF — no event without the composed knob" "$r"
ua_fired valunmgr && r=no || r=yes
harness_check "#1024 a manager is excluded (bot_is_manager, trailing comment and all)" "$r"
ua_fired valunold && r=no || r=yes
harness_check "#1024 a strand past max_age stops being reported — and so goes silent, the deliberate half of the trade" "$r"
mgr_pane2=$(tmux capture-pane -t "$MGR" -p 2>/dev/null || true)
printf '%s' "$mgr_pane2" | grep -q 'worker_unassigned' && r=yes || r=no
harness_check "#1024 the manager is actually pushed the strand via [FLEET-PULSE]" "$r"

# ===========================================================================
# Mechanism 1 (fleet update lifecycle) — daily plugin/skill live reload.
# Stubs claude/claudlobby on PATH so this needs no Claude auth or real fleet.
# ===========================================================================
echo ""
echo "=== validate reload-fleet (Mechanism 1: daily live reload) ==="

STUB_BIN="$ROOT/stubbin"
mkdir -p "$STUB_BIN"
printf '#!/bin/bash\nexit 0\n' > "$STUB_BIN/claude"
printf '#!/bin/bash\nexit 0\n' > "$STUB_BIN/claudlobby"
chmod +x "$STUB_BIN/claude" "$STUB_BIN/claudlobby"
# reload-fleet reads the plugin list from a bot.conf (fleet-global value).
echo 'FLEET_PLUGINS_REQUIRED="claudna@Claudfather"' >> "$BOT_DIR/bot.conf"

# Happy path: download + generate succeed -> .reload-pending dropped on the running bot.
rm -f "$BOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" PATH="$STUB_BIN:$PATH" "$LIB_DIR/reload-fleet.sh" "$FLEET" >/dev/null 2>&1 || true
[ -f "$BOT_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "reload-fleet marks a running bot with .reload-pending (happy path)" "$r"

# Loud-fail: a failing 'claude plugin update' must be LOUD, never silent.
printf '#!/bin/bash\necho boom >&2; exit 1\n' > "$STUB_BIN/claude"
chmod +x "$STUB_BIN/claude"
rm -f "$BOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" PATH="$STUB_BIN:$PATH" "$LIB_DIR/reload-fleet.sh" "$FLEET" >/dev/null 2>&1 || true
val_events "$ROOT" "$FLEET" fleet reload_failed | grep -q '"type":"reload_failed"' && r=yes || r=no
harness_check "reload-fleet emits reload_failed event on failure (loud, not silent)" "$r"
mgr_pane=$(tmux capture-pane -t "$MGR" -p 2>/dev/null || true)
printf '%s' "$mgr_pane" | grep -q 'reload_failed' && r=yes || r=no
harness_check "reload-fleet alerts the manager on failure (shared emit_failure_alert)" "$r"
[ ! -f "$BOT_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "reload-fleet does not half-reload (no marker when download fails)" "$r"

# ===========================================================================
# F2(b) consolidated activation — keepalive performs the live reload at idle.
# ===========================================================================
echo ""
echo "=== validate keepalive reload consumer (idle-gated activation) ==="

IBOT_DIR="$ROOT/local/$FLEET/runtime/bots/$IBOT"
mkdir -p "$IBOT_DIR/data"
cat > "$IBOT_DIR/bot.conf" <<CONF
BOT_NAME="$IBOT"
BOT_ID="$IBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
# Idle pane: last line ends in a prompt glyph '>' (matches the idle base pattern).
# Draw the prompt with chrome BELOW it, as Claude Code does — this is the pane
# that exercises keepalive's send_reload_command, so a prompt-as-last-line shape
# here would validate the reload path against a geometry production never has.
# classify_pane captures the whole pane (not a tail), so idle detection is
# unaffected by the extra lines.
tmux new-session -d -s "$IBOT" 'printf -- "\n--------\n> \n--------\n\n  auto mode on\n"; sleep 600'
sleep 1
touch "$IBOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$IBOT_DIR" >/dev/null 2>&1 || true
sleep 1
ibot_pane=$(tmux capture-pane -t "$IBOT" -p 2>/dev/null || true)
printf '%s' "$ibot_pane" | grep -q '/reload-plugins' && r=yes || r=no
harness_check "keepalive sends /reload-plugins to an idle bot with .reload-pending" "$r"
printf '%s' "$ibot_pane" | grep -q '/reload-skills' && r=yes || r=no
harness_check "keepalive sends /reload-skills to an idle bot with .reload-pending" "$r"
[ ! -f "$IBOT_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "keepalive clears .reload-pending after firing the reload" "$r"

# WS-1 (#7) long-think path: a bot mid-thought (or a long single tool call) with
# NO recent tool-call marker — the pane's "esc to interrupt" active-turn affordance
# is the only active signal. classify_pane must read it BUSY so the reload is NOT
# injected (ghost-text into a working bot). No .last-tool-call → exercises the
# pane fallback, not the marker.
BUSY_DIR="$ROOT/local/$FLEET/runtime/bots/$BUSY"
mkdir -p "$BUSY_DIR/data"
cat > "$BUSY_DIR/bot.conf" <<CONF
BOT_NAME="$BUSY"
BOT_ID="$BUSY"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
tmux new-session -d -s "$BUSY" 'printf "⠋ Thinking… (esc to interrupt)\n"; sleep 600'
sleep 1
touch "$BUSY_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$BUSY_DIR" >/dev/null 2>&1 || true
[ -f "$BUSY_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "keepalive long-think: esc-to-interrupt pane (no recent marker) → BUSY, NOT reloaded (#7)" "$r"

# WS-1 (#7) marker path: a bot whose pane looks IDLE (a bare prompt glyph, no
# active-turn affordance) but made a tool call moments ago — a long op between
# calls. Only the fresh data/.last-tool-call says active; pane parsing alone would
# call it IDLE and inject reload keystrokes. The marker must classify it BUSY.
MBOT_DIR="$ROOT/local/$FLEET/runtime/bots/$MBOT"
mkdir -p "$MBOT_DIR/data"
cat > "$MBOT_DIR/bot.conf" <<CONF
BOT_NAME="$MBOT"
BOT_ID="$MBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
# Identical idle-looking pane to the idle bot above — the ONLY difference is the
# fresh marker, isolating the marker's effect.
tmux new-session -d -s "$MBOT" 'printf "\n❯ \n"; sleep 600'
sleep 1
touch "$MBOT_DIR/data/.reload-pending"
touch "$MBOT_DIR/data/.last-tool-call"   # fresh marker = active recently
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$MBOT_DIR" >/dev/null 2>&1 || true
[ -f "$MBOT_DIR/data/.reload-pending" ] && r=yes || r=no
harness_check "keepalive marker path: idle-looking pane + fresh .last-tool-call → BUSY, NOT reloaded (#7)" "$r"
[ ! -f "$MBOT_DIR/data/.idle" ] && r=yes || r=no
harness_check "keepalive marker path: .idle marker not set (fleet-pulse stays consistent)" "$r"

# ===========================================================================
# #453 Phase 5 — Telegram bridge auto-heal (Tier-2, flag-gated F6b). Proves the
# heal ladder in keepalive.sh end-to-end: a DARK poller (no bot.pid → no_bridge)
# on an IDLE bot, with the heal flag on, triggers the restart ladder — the ONLY
# respawn (a claude bounce; the bun poller is an MCP stdio child of claude). The
# ladder's start-bot.sh fallback is a RECORDER stub, so nothing is really
# restarted. Also asserts the gates: flag-off is a no-op; no_token never bounces;
# the attempt budget caps then escalates (F3 escalate-only).
# CAVEAT: this exercises the heal MACHINERY on a deterministically-dark bridge.
# Recovery from genuine upstream nondeterministic non-spawn is measured by the
# production bounce→recovery telemetry that gates Tier-2 rollout (F6b), not here.
echo ""
echo "=== validate bridge heal (#453 Phase 5: keepalive respawns a dark poller) ==="
HBOT="valheal"
HDIR="$ROOT/local/$FLEET/runtime/bots/$HBOT"
HSTATE="$ROOT/ch/telegram-$HBOT"   # TELEGRAM_STATE_DIR — no bot.pid ⇒ bridge_state=no_bridge
HREC="$HDIR/data/.heal-restart-count"
mkdir -p "$HDIR/data" "$HSTATE"

# BOT_SERVICE="" so (a) the socket resolves to the harness fallback tmux-valheal
# (matching the session below) and (b) the restart ladder falls through to the
# start-bot.sh recorder rather than any real systemd unit.
_heal_conf() {   # $1 = OBSERVABILITY_BRIDGE_HEAL (0/1)   $2 = token present (y/n)
    cat > "$HDIR/bot.conf" <<CONF
BOT_NAME="$HBOT"
BOT_ID="$HBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
TELEGRAM_BOT_HANDLE="$HBOT"
TELEGRAM_STATE_DIR="$HSTATE"
TELEGRAM_TOKEN_ENV_NAME=VALHEAL_TG_TOKEN
OBSERVABILITY_BRIDGE_DOWN_GRACE=0
OBSERVABILITY_BRIDGE_HEAL=$1
BRIDGE_HEAL_MAX_ATTEMPTS=2
CONF
    if [ "$2" = "y" ]; then
        printf 'VALHEAL_TG_TOKEN=8888888:AAAAAAAAAAAAAAAAAAAA\n' > "$HDIR/.env"
    else
        rm -f "$HDIR/.env"
    fi
}

# Stub lib dir: REAL keepalive + lib-common (symlinked), but a recorder start-bot.sh
# so the heal's restart ladder is observed, never executed. keepalive resolves
# LIB_DIR from its own path, so both the sourced lib-common and the invoked
# start-bot come from here.
HLIB="$ROOT/stublib"
mkdir -p "$HLIB"
ln -sf "$LIB_DIR/keepalive.sh" "$HLIB/keepalive.sh"
ln -sf "$LIB_DIR/lib-common.sh" "$HLIB/lib-common.sh"
val_link_plane_shim "$HLIB"
cat > "$HLIB/start-bot.sh" <<'REC'
#!/bin/bash
# Recorder stub for the heal restart ladder (start-bot.sh fallback). Counts the
# bounce against the bot dir passed as $1 instead of restarting a service.
d="$1"
c=0; [ -f "$d/data/.heal-restart-count" ] && c=$(cat "$d/data/.heal-restart-count" 2>/dev/null)
printf '%s' "$((c + 1))" > "$d/data/.heal-restart-count"
REC
chmod +x "$HLIB/start-bot.sh"

# Idle pane (bare prompt glyph) so keepalive reaches the IDLE branch where the heal
# runs — the BUSY-gate is implicit in that placement.
tmux new-session -d -s "$HBOT" 'printf "\n> \n"; sleep 600'
sleep 1
_heal_reset() {   # clear the recorder + heal-ladder state between phases
    printf '%s' "0" > "$HREC"
    rm -f "$HDIR/data/.bridge-heal" "$HDIR/data/.bridge-heal-escalated"
}
run_heal() { CLAUDLOBBY_ROOT="$ROOT" "$HLIB/keepalive.sh" "$HDIR" >/dev/null 2>&1 || true; }

# --- Phase A: flag ON, token present, dark poller → bounce, retry, cap, escalate ---
_heal_conf 1 y
_heal_reset
run_heal   # tick 1 → bounce #1
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "1" ] && r=yes || r=no
harness_check "heal bounces a dark poller on an idle bot (restart ladder invoked)" "$r"
val_events "$ROOT" "$FLEET" "$HBOT" bridge_heal | grep -q '"type":"bridge_heal"' && r=yes || r=no
harness_check "heal emits a bridge_heal fleet event (the BRIDGE_HEAL transition, on the plane)" "$r"
run_heal   # tick 2 → bounce #2 (reaches the cap of 2)
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "2" ] && r=yes || r=no
harness_check "heal retries up to the attempt cap (2nd bounce)" "$r"
run_heal   # tick 3 → budget exhausted → escalate-only, NO 3rd bounce
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "2" ] && r=yes || r=no
harness_check "heal stops bouncing at the cap (no 3rd bounce — F3 escalate-only)" "$r"
[ -f "$HDIR/data/.bridge-heal-escalated" ] && r=yes || r=no
harness_check "heal escalates once when the budget is exhausted" "$r"
val_events "$ROOT" "$FLEET" "$HBOT" bridge_heal | grep -q 'budget exhausted' && r=yes || r=no
harness_check "heal logs budget-exhausted escalation" "$r"

# --- Phase B: flag OFF, same dark poller → never bounces (the F6b gate) ---
_heal_conf 0 y
_heal_reset
run_heal
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "0" ] && r=yes || r=no
harness_check "heal is a no-op when OBSERVABILITY_BRIDGE_HEAL!=1 (F6b gate holds)" "$r"

# --- Phase C: flag ON but token unresolvable → no_token, never bounce (F2/5e) ---
_heal_conf 1 n
_heal_reset
run_heal
[ "$(cat "$HREC" 2>/dev/null || echo 0)" = "0" ] && r=yes || r=no
harness_check "heal never bounces on no_token (a bounce cannot conjure a missing token)" "$r"

# ===========================================================================
# #608 — a tokenless canary/throwaway (EXPECT_NO_TOKEN=1) must NOT fire the
# no_token alert at BRING-UP or via FLEET-PULSE; a REAL bot with no token still
# MUST. Both hit the SAME classification (handle set, token unresolvable →
# no_token); the ONLY difference is the marker, so any divergence below IS the
# exemption. Drive the REAL bringup + down-state deciders (the exact functions
# that choose to alert), so the fix is OBSERVED end-to-end — not just composed.
echo ""
echo "=== validate no_token canary exemption (#608: EXPECT_NO_TOKEN gates the alert) ==="
NTBOTS="$ROOT/local/$FLEET/runtime/bots"
NTC="$NTBOTS/valcanary"   # EXPECT_NO_TOKEN=1 → exempt throwaway
NTR="$NTBOTS/valreal"     # no marker → real bot, a missing token is a fault
# The bring-up alerts are fleet-level receipts (the old state/events/ file).
nt_events() { val_events "$ROOT" "$FLEET" fleet; }
_nt_conf() {   # $1 = bot dir   $2 = y|n (carry the canary marker?). Handle set, token absent.
    local d="$1" b; b="$(basename "$1")"
    mkdir -p "$d/data"
    cat > "$d/bot.conf" <<CONF
BOT_NAME="$b"
BOT_ID="$b"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
TELEGRAM_BOT_HANDLE="$b"
TELEGRAM_TOKEN_ENV_NAME=NT_ABSENT_TOKEN
CONF
    [ "$2" = y ] && printf 'EXPECT_NO_TOKEN=1\n' >> "$d/bot.conf" || true
}
_nt_conf "$NTC" y
_nt_conf "$NTR" n

# Sanity: identical token-absent input — both classify as no_token.
{ [ "$(bridge_state "$NTC" 2>/dev/null || true)" = "no_token" ] \
  && [ "$(bridge_state "$NTR" 2>/dev/null || true)" = "no_token" ]; } && r=yes || r=no
harness_check "#608 canary + real both classify no_token (same token-absent input)" "$r"

# --- Bring-up path (bridge_bringup_verify): canary silent, real escalates ---
ntv="$(CLAUDLOBBY_ROOT="$ROOT" bridge_bringup_verify "$NTC" "$(dirname "$NTC")" 0 2>/dev/null || true)"
[ "$ntv" = "expected:no_token" ] && r=yes || r=no
harness_check "#608 canary bring-up verdict is expected:no_token (marker exempts)" "$r"
nt_events | grep -q 'valcanary Telegram bridge no_token' && r=no || r=yes
harness_check "#608 canary bring-up emits NO bridge_down alert (no fleet event)" "$r"

ntv="$(CLAUDLOBBY_ROOT="$ROOT" bridge_bringup_verify "$NTR" "$(dirname "$NTR")" 0 2>/dev/null || true)"
[ "$ntv" = "missing:no_token" ] && r=yes || r=no
harness_check "#608 real-bot bring-up verdict is missing:no_token (still a fault)" "$r"
nt_events | grep -q 'valreal Telegram bridge no_token at bring-up' && r=yes || r=no
harness_check "#608 real-bot bring-up DOES emit the no_token bridge_down alert" "$r"

# --- Fleet-pulse path (bridge_down_state, grace 0): canary not-down, real down ---
CLAUDLOBBY_ROOT="$ROOT" bridge_down_state "$NTC" 0 >/dev/null 2>&1 && r=no || r=yes  # rc 0 = down
harness_check "#608 canary is NOT actionably down for fleet-pulse (no pulse alert)" "$r"
[ "$(CLAUDLOBBY_ROOT="$ROOT" bridge_down_state "$NTR" 0 2>/dev/null || true)" = "no_token" ] && r=yes || r=no
harness_check "#608 real bot IS actionably down (no_token) for fleet-pulse" "$r"

# ===========================================================================
# The no_bridge bring-up alert must tell the truth about auto-heal. keepalive's
# _bridge_heal is gated OFF fleet-wide (OBSERVABILITY_BRIDGE_HEAL != 1), so an
# alert that promises "keepalive will heal" lies whenever the gate is off — and
# even when on, the poller is an MCP stdio child of claude, so the only lever is
# a full bot bounce, never a gentle in-place respawn. Reuse the #453 dark-bridge
# fixture (valheal: no bot.pid -> no_bridge) and drive the REAL
# bridge_bringup_verify in both gate states — the verify reads the gate from the
# env, so one fixture serves both. Assert the emitted alert text tracks reality.
echo ""
echo "=== validate bridge_down heal-honesty (OBSERVABILITY_BRIDGE_HEAL gates the wording) ==="
_heal_conf 0 y   # token present, dark poller -> no_bridge (gate in bot.conf is inert here; verify reads the env)

# Sanity: token resolves + poller absent classifies no_bridge (not no_token/no_handle).
[ "$(bridge_state "$HDIR" 2>/dev/null || true)" = "no_bridge" ] && r=yes || r=no
harness_check "heal-honesty fixture classifies no_bridge (token set, poller absent)" "$r"

# --- Gate OFF (fleet default): the alert must NOT promise a heal that never runs ---
bhv="$(CLAUDLOBBY_ROOT="$ROOT" OBSERVABILITY_BRIDGE_HEAL=0 \
    bridge_bringup_verify "$HDIR" "$(dirname "$HDIR")" 0 2>/dev/null || true)"
[ "$bhv" = "missing:no_bridge" ] && r=yes || r=no
harness_check "gate-off bring-up verdict is missing:no_bridge (unchanged)" "$r"
bhoff="$(nt_events | grep 'valheal Telegram bridge down at bring-up' | tail -n1 || true)"
printf '%s' "$bhoff" | grep -q 'dark until restart' && r=yes || r=no
harness_check "gate-off alert states inbound dark until restart (honest, mirrors no_token)" "$r"
printf '%s' "$bhoff" | grep -q 'keepalive will heal' && r=no || r=yes
harness_check "gate-off alert drops the false 'keepalive will heal' promise" "$r"

# --- Gate ON: the alert states a bounce (full claude restart), not a respawn ---
CLAUDLOBBY_ROOT="$ROOT" OBSERVABILITY_BRIDGE_HEAL=1 \
    bridge_bringup_verify "$HDIR" "$(dirname "$HDIR")" 0 >/dev/null 2>&1 || true
nt_events | grep -q 'valheal Telegram bridge down at bring-up.*bounce' && r=yes || r=no
harness_check "gate-on alert states keepalive will bounce to recover" "$r"

# ===========================================================================
# #579 — the dead-session path must emit a RESTART line the uptime parser reads.
# The #577 review: test_uptime.py only feeds the PARSER a hand-written sample;
# nothing drove keepalive's real dead-session branch to prove it EMITS a line the
# parser recognizes — so the #577 restart_bot_service extraction left that wording
# one refactor from silently drifting out of uptime.py's _LOG_LINE_RE. Drive the
# real path (a session-less bot, via the HLIB recorder stub so nothing truly
# restarts) and assert the REAL parser extracts a RESTART from the emitted log.
echo ""
echo "=== validate dead-session RESTART line (#579: keepalive emitter ⇄ uptime parser) ==="
DBOT="valdead"
DDIR="$ROOT/local/$FLEET/runtime/bots/$DBOT"
mkdir -p "$DDIR/data"
# A COMPOSED bot's shape: FLEET_NAME + BOT_SERVICE (the private tmux socket). Both
# are load-bearing for what this scenario asserts — with no FLEET_NAME keepalive
# anchors the restart on the HOST sentinel and records no per-bot fact, and with
# FLEET_NAME but no BOT_SERVICE the socket resolver refuses before the dead-session
# branch is reached (both probed on a throwaway rig, F18 R2b). No unit or plist
# exists for the label, so the restart ladder falls through to the recorder stub.
cat > "$DDIR/bot.conf" <<CONF
BOT_NAME="$DBOT"
BOT_ID="$DBOT"
BOT_SERVICE="com.val.$DBOT"
FLEET_NAME="$FLEET"
MANAGER_TMUX="$MGR"
CONF
# No tmux session for valdead on its socket → keepalive takes the dead-session
# branch. The RESTART log line is echoed before the restart action fires, so it
# lands regardless of the (stubbed) restart.
CLAUDLOBBY_ROOT="$ROOT" "$HLIB/keepalive.sh" "$DDIR" >/dev/null 2>&1 || true
grep -qE 'RESTART.*session dead' "$DDIR/keepalive.log" 2>/dev/null && r=yes || r=no
harness_check "keepalive dead-session path emits a RESTART … session dead log line" "$r"
# Load-bearing assertion: the READER the fleet consumes must see that restart.
# F18 closure R2b: the keepalive.log parser is gone with the file; `claudlobby
# uptime` reads the plane's keepalive entries — the RESTART is the
# keepalive_restart fleet event the real tick landed. The tick's emission is
# detached (the cold CLI lands it in the background), so poll, bounded.
dead_restarts=0
for _i in $(seq 1 40); do
    dead_restarts=$(python3 - "$LIB_DIR" "$ROOT" "$FLEET" "$DBOT" <<'PY' 2>/dev/null || echo 0
import importlib.util, sys
spec = importlib.util.spec_from_file_location("pr", sys.argv[1] + "/plane-readers.py")
pr = importlib.util.module_from_spec(spec); spec.loader.exec_module(pr)
try:
    conn = pr.connect(sys.argv[2])
except Exception:
    print(0); sys.exit(0)
print(sum(1 for _, s in pr.keepalive_entries(conn, sys.argv[3], sys.argv[4], None) if s == "RESTART"))
PY
)
    [ "${dead_restarts:-0}" -ge 1 ] && break
    sleep 0.5
done
[ "${dead_restarts:-0}" -ge 1 ] && r=yes || r=no
harness_check "the plane's keepalive entries yield the RESTART the real tick landed (#579, F18 R2b)" "$r"

# Regression guard: send_reload_command must resend Enter ONLY when the TUI
# swallowed it (command still on the input line), never after a clean submit.
# This fixture consumes each submitted line and redraws a fresh prompt below it,
# mimicking Claude Code — where a submitted command scrolls up into the transcript
# but stays visible in the pane. A verify scoped to the whole pane matches that
# scrolled-up text and fires a spurious empty Enter at the idle prompt; one scoped
# to the bottom input line does not. Assert exactly one submission per reload
# command (2 total) — i.e. zero spurious Enters. The idle checks above only assert
# the commands appear, so without this a widened/deleted verify regresses silently.
SBOT_DIR="$ROOT/local/$FLEET/runtime/bots/$SBOT"
mkdir -p "$SBOT_DIR/data"
cat > "$SBOT_DIR/bot.conf" <<CONF
BOT_NAME="$SBOT"
BOT_ID="$SBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
SUBMIT_LOG="$SBOT_DIR/submits.log"
: > "$SUBMIT_LOG"
# Idle prompt so keepalive takes the reload branch; each read logs the submission,
# then prints it and pushes it well above the bottom input line, so only a
# genuinely-unsubmitted command is still at the prompt for the verify to match.
#
# The prompt is drawn inside a bordered box with footer lines BELOW it, because
# that is where Claude Code puts it — the input line is never the last line of
# the pane. A fixture that put the prompt last would let a verify that only ever
# reads the last few lines pass here while never once reaching the input line in
# production, which is exactly how the pre-#763 `tail -3` verify shipped dead.
cat > "$SBOT_DIR/fixture.sh" <<FIX
#!/bin/bash
draw_prompt() {
    printf -- '--------\n> \n--------\n\n  auto mode on\n'
}
draw_prompt
while IFS= read -r l; do
    printf '%s\n' "\$l" >> "$SUBMIT_LOG"
    printf 'sent[%s]\n\n\n\n\n\n\n' "\$l"
    draw_prompt
done
FIX
tmux new-session -d -s "$SBOT" "bash '$SBOT_DIR/fixture.sh'"
sleep 1
touch "$SBOT_DIR/data/.reload-pending"
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/keepalive.sh" "$SBOT_DIR" >/dev/null 2>&1 || true
sleep 1
submits=$(wc -l < "$SUBMIT_LOG")
[ "$submits" -eq 2 ] && r=yes || r=no
harness_check "send_reload_command fires no spurious Enter on clean submit (verify scoped to prompt)" "$r"

# === Scenario 2: lossless restart — age-gated resume injection on start ===
# Drive the REAL start-bot.sh against a throwaway bot whose `claude` is a stub
# injected via CLAUDE_BIN (prints the readiness string, then `cat` so send-keys
# echo into the pane), with plugin management off (empty FLEET_PLUGINS_REQUIRED)
# so the start is hermetic and fast — no real auth, MCP, or plugin network call.
# A fresh session.md -> /claudna:session resume is sent BEFORE STARTUP_PROMPT; a
# stale one -> resume skipped (clean start).
RB_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate-rb.XXXXXX")"
RB_DIR="$RB_ROOT/local/$FLEET/runtime/bots/valrb"
mkdir -p "$RB_DIR/.claude" "$RB_DIR/logs" "$RB_ROOT/bin" "$RB_ROOT/tmp"
# Controlled HOME with consent pre-accepted. start-bot.sh's consent block runs
# with_lock on $HOME/.claude/settings.json.lock, which fails on a fresh HOME (no
# .claude dir yet) under set -e — a CI runner's empty HOME would abort start-bot.sh
# before the resume injection ever fires. Pinning HOME keeps the scenario hermetic.
RB_HOME="$RB_ROOT/home"
mkdir -p "$RB_HOME/.claude"
# Seed the session-resume CAPABILITY (#1163). start-bot now injects the resume
# command only when the skill it names can actually resolve, so this hermetic
# HOME — which has no plugins at all — would otherwise skip the injection and
# take the age-gate checks below down with it. They are testing the AGE gate;
# the capability has to be present for that to be what they measure.
RB_PLUGIN_DIR="$RB_HOME/.claude/plugins/cache/ValMarketplace/claudna"
mkdir -p "$RB_PLUGIN_DIR"
printf '{"skipAutoPermissionPrompt":true,"skipDangerousModePermissionPrompt":true}\n' > "$RB_HOME/.claude/settings.json"
cat > "$RB_ROOT/bin/claude" <<'STUB'
#!/bin/bash
exec cat
STUB
chmod +x "$RB_ROOT/bin/claude"
cat > "$RB_DIR/bot.conf" <<CONF
BOT_NAME="valrb"
BOT_ID="valrb"
BOT_LABEL="valrb"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
STARTUP_PROMPT="ZZZ_STARTUPMARK"
FLEET_PLUGINS_REQUIRED=""
CONF
RB_SESSION="$(tmux_session_name "$RB_DIR")"

_run_startbot() {  # $1 = fresh|stale -> echo the resulting pane
    local iso
    if [ "$1" = fresh ]; then iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; else iso="2020-01-01T00:00:00Z"; fi
    printf -- '---\ncwd: %s\nlast_updated: %s\nschema_version: 2\n---\n' "$RB_DIR" "$iso" \
        > "$RB_DIR/.claude/session.md"
    tmux kill-session -t "$RB_SESSION" 2>/dev/null || true
    sleep 0.3
    TMPDIR="$RB_ROOT/tmp" BOOT_LOCK_HOLD_S=0 CLAUDE_BIN="$RB_ROOT/bin/claude" \
        HOME="$RB_HOME" PATH="$RB_ROOT/bin:$PATH" CLAUDLOBBY_ROOT="$RB_ROOT" \
        "$LIB_DIR/start-bot.sh" "$RB_DIR" >"$RB_ROOT/startbot.$1.out" 2>&1 || true
    sleep 1
    tmux capture-pane -t "$RB_SESSION" -p 2>/dev/null || true
}

echo ""
echo "=== validate-bot-change: lossless restart (resume on start, age-gated) ==="
_lossless_fail_before=$fail
pane_fresh="$(_run_startbot fresh)"
printf '%s' "$pane_fresh" | grep -q '/claudna:session resume' && r=yes || r=no
harness_check "fresh session.md -> /claudna:session resume injected on start" "$r"
_rln="$(printf '%s\n' "$pane_fresh" | grep -n '/claudna:session resume' | head -1 | cut -d: -f1 || true)"
_sln="$(printf '%s\n' "$pane_fresh" | grep -n 'ZZZ_STARTUPMARK' | head -1 | cut -d: -f1 || true)"
{ [ -n "$_rln" ] && [ -n "$_sln" ] && [ "$_rln" -lt "$_sln" ]; } && r=yes || r=no
harness_check "resume keystroke precedes STARTUP_PROMPT in the pane" "$r"
pane_stale="$(_run_startbot stale)"
printf '%s' "$pane_stale" | grep -q '/claudna:session resume' && r=no || r=yes
harness_check "stale session.md -> resume injection skipped (clean start)" "$r"
grep -q 'RESUME SKIP' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "stale skip recorded in startup.log (RESUME SKIP)" "$r"

# --- #1163: no resume CAPABILITY -> skip, and skip LOUDLY -------------------
# The other half of the gate. A fleet running plugins.include_defaults:false has
# no session provider, and the old code fired an unresolvable slash command into
# every pane on every boot. The failure this must never become is the SILENT one
# — a bot that quietly stops resuming and says nothing — so both the log line
# and the event are asserted, not just the absence of the keystroke.
rm -rf "$RB_PLUGIN_DIR"
rm -f "$RB_DIR/logs/startup.log"
pane_nocap="$(_run_startbot fresh)"
printf '%s' "$pane_nocap" | grep -q '/claudna:session resume' && r=no || r=yes
harness_check "no resume capability -> unresolvable command NOT injected" "$r"
grep -q 'RESUME SKIP.*no resume capability' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "  ...and the skip is recorded in startup.log with its reason" "$r"
grep -q 'provider-absent' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "  ...naming WHICH capability was missing (not just that one was)" "$r"
val_events "$RB_ROOT" "$FLEET" valrb resume_skipped | grep -q '"type":"resume_skipped"' && r=yes || r=no
harness_check "  ...and emits resume_skipped so a silent degradation is visible" "$r"
printf '%s' "$pane_nocap" | grep -q 'ZZZ_STARTUPMARK' && r=yes || r=no
harness_check "  ...while STARTUP_PROMPT still reaches the pane (boot not broken)" "$r"
mkdir -p "$RB_PLUGIN_DIR"

# Surface hermeticity evidence when the lossless checks fail (e.g. a CI runner
# where this scenario fails but a dev host passes): start-bot.sh's stderr is
# otherwise swallowed to a file we discard, hiding WHY the resume block did not
# run. Gated on failure so a passing run stays quiet.
if [ "$fail" -gt "$_lossless_fail_before" ]; then
    echo "  --- DIAGNOSTIC: lossless checks failed; dumping start-bot evidence ---"
    echo "  [tmux] $(tmux -V 2>&1)"
    echo "  [start-bot fresh stdout+stderr]"
    sed 's/^/    /' "$RB_ROOT/startbot.fresh.out" 2>/dev/null || echo "    (none)"
    echo "  [start-bot stale stdout+stderr]"
    sed 's/^/    /' "$RB_ROOT/startbot.stale.out" 2>/dev/null || echo "    (none)"
    echo "  [startup.log]"
    sed 's/^/    /' "$RB_DIR/logs/startup.log" 2>/dev/null || echo "    (none)"
    echo "  [fresh pane]"; printf '%s\n' "$pane_fresh" | sed 's/^/    /'
    echo "  [stale pane]"; printf '%s\n' "$pane_stale" | sed 's/^/    /'
    echo "  ----------------------------------------------------------------"
fi

# === Scenario 2b: readiness probe — READY vs TIMEOUT + rc_timeout event (#533/#751) ===
# Same REAL start-bot.sh. The probe now asserts bridge_state ground truth, not a
# bring-up pane string (#751: the old `remote-control is active` grep drifted out of
# current builds and false-fired rc_timeout on every start). Positive path first
# (the fresh/stale valrb above has no TELEGRAM_BOT_HANDLE -> bridge_state no_handle
# -> ready, no poller to await): READY logged, NO rc_timeout. Then the negative
# path: a CHANNEL bot whose poller never wrote a live bot.pid -> bridge_state
# no_bridge -> the probe must TIMEOUT and emit the (now true-positive) rc_timeout
# event fleet-pulse escalates. Empirical proof of #533 items 3-4 + the #751 fix
# (unit tests prove composition; only running start-bot proves the event fires —
# and, for #751, that a ready bot no longer FALSELY fires). RC_READY_TIMEOUT_S=1
# keeps the negative run fast — the 90s default is untestable in a harness (poll 2
# is a single check now, so there is no second timeout to shorten).
echo ""
echo "=== validate-bot-change: readiness alerting (#533 items 3-4, #751) ==="
_rc_fail_before=$fail
grep -q 'READY —' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "bridge_state ready (no_handle bot) -> READY recorded in startup.log" "$r"
val_events "$RB_ROOT" "$FLEET" valrb rc_timeout | grep -q '"type":"rc_timeout"' && r=no || r=yes
harness_check "ready verdict -> no rc_timeout event (no false alarm, #751)" "$r"

# Negative: reconfigure valrb as a CHANNEL bot (handle + a resolvable token) whose
# poller never came up — no bot.pid under TELEGRAM_STATE_DIR -> bridge_state stays
# no_bridge -> the probe times out and emits the now-true-positive rc_timeout.
cat > "$RB_ROOT/bin/claude" <<'STUB'
#!/bin/bash
exec cat
STUB
chmod +x "$RB_ROOT/bin/claude"
cat >> "$RB_DIR/bot.conf" <<CONF
TELEGRAM_BOT_HANDLE="valrb"
TELEGRAM_TOKEN_ENV_NAME="VALRB_TOKEN"
TELEGRAM_STATE_DIR="$RB_DIR/state"
CONF
printf 'VALRB_TOKEN=8888888:AAAAAAAAAAAAAAAAAAAA\n' > "$RB_DIR/.env"
mkdir -p "$RB_DIR/state"   # exists, but no bot.pid -> bridge_state no_bridge
tmux kill-session -t "$RB_SESSION" 2>/dev/null || true
sleep 0.3
printf -- '---\ncwd: %s\nlast_updated: %s\nschema_version: 2\n---\n' "$RB_DIR" "2020-01-01T00:00:00Z" \
    > "$RB_DIR/.claude/session.md"
TMPDIR="$RB_ROOT/tmp" BOOT_LOCK_HOLD_S=0 RC_READY_TIMEOUT_S=1 \
    CLAUDE_BIN="$RB_ROOT/bin/claude" \
    HOME="$RB_HOME" PATH="$RB_ROOT/bin:$PATH" CLAUDLOBBY_ROOT="$RB_ROOT" \
    "$LIB_DIR/start-bot.sh" "$RB_DIR" >"$RB_ROOT/startbot.timeout.out" 2>&1 || true
sleep 1
grep -q 'TIMEOUT' "$RB_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "no RC string -> TIMEOUT recorded in startup.log" "$r"
_rcev="$(val_events "$RB_ROOT" "$FLEET" valrb rc_timeout | head -1 || true)"
[ -n "$_rcev" ] && r=yes || r=no
harness_check "TIMEOUT emits an rc_timeout fleet event (fleet-pulse escalation input)" "$r"
if [ -n "$_rcev" ]; then
    printf '%s' "$_rcev" | python3 -c "import sys,json; e=json.loads(sys.stdin.readline()); sys.exit(0 if e['type']=='rc_timeout' and e['ts'] else 1)" 2>/dev/null && r=yes || r=no
else r=no; fi
harness_check "rc_timeout event is valid JSON with ts+type (fleet-pulse-readable)" "$r"

if [ "$fail" -gt "$_rc_fail_before" ]; then
    echo "  --- DIAGNOSTIC: RC readiness checks failed ---"
    echo "  [startup.log]"; sed 's/^/    /' "$RB_DIR/logs/startup.log" 2>/dev/null || echo "    (none)"
    echo "  [events]"; val_events "$RB_ROOT" "$FLEET" valrb | sed 's/^/    /'; echo "    (end of plane events)"
    echo "  [start-bot timeout stdout+stderr]"; sed 's/^/    /' "$RB_ROOT/startbot.timeout.out" 2>/dev/null || echo "    (none)"
fi

# === Scenario 2c: RC readiness ESCALATION — fleet-pulse pages on an rc_timeout burst (#533) ===
# 2b proved start-bot EMITS rc_timeout. This proves the downstream half: fleet-pulse reads
# that event from >= threshold bots within the window and FIRES the escalation page. The real
# fleet-pulse runs from a stub lib dir whose tg-post.sh RECORDS the page instead of sending it,
# so the assertion is the alert message the burst-detector actually produced. A single bot
# (below threshold) must stay silent. 2b + 2c together cover #533 items 3-4 end-to-end: emit
# then escalate. The incidental service_down / session_missing pages are the sandbox bots
# having no live session; the assertions target the rc_timeout line.
echo ""
echo "=== validate-bot-change: RC readiness ESCALATION page (#533 items 3-4) ==="
_esc_fail_before=$fail
_esc_fleet="valesc"
_esc_lib="$ROOT/esclib"
mkdir -p "$_esc_lib"
ln -s "$LIB_DIR/fleet-pulse.sh" "$_esc_lib/fleet-pulse.sh"
ln -s "$LIB_DIR/lib-common.sh"  "$_esc_lib/lib-common.sh"
val_link_plane_shim "$_esc_lib"
_esc_pages="$ROOT/esc-pages.log"
: > "$_esc_pages"
cat > "$_esc_lib/tg-post.sh" <<STUB
#!/bin/bash
printf '%s\n' "\$1" >> "$_esc_pages"
STUB
chmod +x "$_esc_lib/tg-post.sh"
_esc_bots="$ROOT/local/$_esc_fleet/runtime/bots"
# The events flip for the sandbox fleet: the sweep's escalation reads the plane
# (a plane row cannot be deleted between the two halves the way a file could,
# so the below-threshold half runs under a SECOND sandbox fleet).
val_plane_ready "$ROOT" "$_esc_fleet"

esc_seed() {  # <fleet> <bot> <emit rc_timeout: yes|no> — seed a sandbox bot, optionally with a timeout event
    local bots="$ROOT/local/$1/runtime/bots"
    mkdir -p "$bots/$2"
    printf 'BOT_SERVICE=%s\n' "$2" > "$bots/$2/bot.conf"
    # Route the seed through the SAME door start-bot.sh emits rc_timeout with, so the
    # seeded row can never drift from the real emitter (it anchors the event on the
    # bot, stamps the instant and the provenance the sweep then reads from the plane).
    if [ "$3" = yes ]; then
        CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_FLEET="$1" emit_fleet_event rc_timeout startup '{}' "$bots/$2" "$2"
    fi
    return 0
}
esc_run() {  # <fleet>
    CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_FLEET="$1" FLEET_PULSE_ESCALATION_CHAT_ID="-100999" \
        "$_esc_lib/fleet-pulse.sh" "$1" >/dev/null 2>&1 || true
}

# Positive: 2 bots TIMEOUT within the window (== default threshold) -> page fires.
esc_seed "$_esc_fleet" escone yes
esc_seed "$_esc_fleet" esctwo yes
esc_run "$_esc_fleet"
grep -q 'FLEET ALERT: rc_timeout on 2 bots' "$_esc_pages" && r=yes || r=no
harness_check "rc_timeout burst on >= threshold bots FIRES the escalation page" "$r"

# Negative: only 1 bot with rc_timeout -> below threshold -> no rc_timeout page.
_esc_fleet2="valesc2"
val_plane_ready "$ROOT" "$_esc_fleet2"
: > "$_esc_pages"
esc_seed "$_esc_fleet2" escone yes
esc_seed "$_esc_fleet2" esctwo no
esc_run "$_esc_fleet2"
grep -q 'rc_timeout' "$_esc_pages" && r=no || r=yes
harness_check "a single rc_timeout (below threshold) does NOT page (no false alarm)" "$r"

if [ "$fail" -gt "$_esc_fail_before" ]; then
    echo "  --- DIAGNOSTIC: escalation pages recorded ---"
    sed 's/^/    /' "$_esc_pages" 2>/dev/null || echo "    (none)"
fi

# === Scenario 2d: plugin marketplace registration — positional add, verified, loud on failure (#596) ===
# Same REAL start-bot.sh, plugin management ON (non-empty FLEET_PLUGINS_REQUIRED)
# with `claude` stubbed via the CLAUDE_BIN seam (the PATH rebuild inside
# start-bot discards a stub dir, so the seam is the only hermetic route; the
# same stub serves the pane). ONE run covers both paths: valmarket registers
# (the stub writes the registry like the real CLI), valmarketbad fails (stub
# exits 1, registers nothing). start-bot must use the positional
# `plugin marketplace add <owner>/<repo>` form (dead flags are how a fleet
# silently lands on the wrong plugin), verify + log valmarket, and for
# valmarketbad log PLUGIN ERROR + emit a plugin_marketplace_failed fleet
# event — loud, but never startup-blocking.
echo ""
echo "=== validate-bot-change: marketplace registration (#596: positional + verified + loud) ==="
_mp_fail_before=$fail
MP_DIR="$RB_ROOT/local/$FLEET/runtime/bots/valmp"
mkdir -p "$MP_DIR/.claude" "$MP_DIR/logs"
cat > "$MP_DIR/bot.conf" <<CONF
BOT_NAME="valmp"
BOT_ID="valmp"
BOT_LABEL="valmp"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
FLEET_PLUGINS_REQUIRED="demo@valmarket"
FLEET_PLUGINS_MARKETPLACES="valmarket=github:ExampleOrg/example-plugins valmarketbad=github:ExampleOrg/does-not-exist"
CONF
MP_SESSION="$(tmux_session_name "$MP_DIR")"
cat > "$RB_ROOT/bin/claude" <<STUB
#!/bin/bash
if [ "\$1" = plugin ]; then
    printf '%s\n' "\$*" >> "$RB_ROOT/plugin-argv.log"
    if [ "\$2" = marketplace ] && [ "\$3" = add ]; then
        [ "\$4" = ExampleOrg/does-not-exist ] && exit 1
        mkdir -p "\$HOME/.claude/plugins"
        printf '{"valmarket":{}}\n' > "\$HOME/.claude/plugins/known_marketplaces.json"
    fi
    exit 0
fi
exec cat
STUB
chmod +x "$RB_ROOT/bin/claude"
TMPDIR="$RB_ROOT/tmp" BOOT_LOCK_HOLD_S=0 RC_READY_TIMEOUT_S=10 CLAUDE_BIN="$RB_ROOT/bin/claude" \
    HOME="$RB_HOME" PATH="$RB_ROOT/bin:$PATH" CLAUDLOBBY_ROOT="$RB_ROOT" \
    "$LIB_DIR/start-bot.sh" "$MP_DIR" >"$RB_ROOT/startbot.mp.out" 2>&1 || true
grep -qx 'plugin marketplace add ExampleOrg/example-plugins' "$RB_ROOT/plugin-argv.log" 2>/dev/null && r=yes || r=no
harness_check "marketplace add uses the positional owner/repo form (no dead flags)" "$r"
grep -q 'PLUGIN marketplace valmarket registered' "$MP_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "registration verified against known_marketplaces.json + logged" "$r"
grep -q 'PLUGIN ERROR marketplace valmarketbad' "$MP_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "failed registration logs PLUGIN ERROR (not swallowed)" "$r"
_mpev="$(val_events "$RB_ROOT" "$FLEET" valmp plugin_marketplace_failed || true)"
[ -n "$_mpev" ] && r=yes || r=no
harness_check "failed registration emits plugin_marketplace_failed fleet event (loud, not silent)" "$r"
if [ -n "$_mpev" ]; then
    printf '%s\n' "$_mpev" | python3 -c "import sys,json; evs=[json.loads(l) for l in sys.stdin if 'plugin_marketplace_failed' in l]; sys.exit(0 if len(evs)==1 and evs[0]['data']['marketplace']=='valmarketbad' else 1)" 2>/dev/null && r=yes || r=no
else r=no; fi
harness_check "exactly one failure event, and it names valmarketbad (success stays quiet)" "$r"
grep -q 'READY —' "$MP_DIR/logs/startup.log" 2>/dev/null && r=yes || r=no
harness_check "registration failure does NOT block startup (session still comes up)" "$r"

if [ "$fail" -gt "$_mp_fail_before" ]; then
    echo "  --- DIAGNOSTIC: marketplace registration checks failed ---"
    echo "  [plugin argv]"; sed 's/^/    /' "$RB_ROOT/plugin-argv.log" 2>/dev/null || echo "    (none)"
    echo "  [valmp startup.log]"; sed 's/^/    /' "$MP_DIR/logs/startup.log" 2>/dev/null || echo "    (none)"
    echo "  [valmp events]"; val_events "$RB_ROOT" "$FLEET" valmp | sed 's/^/    /'; echo "    (end of plane events)"
fi

# === Scenario 3: weekly worker-only restart — manager skip + loud failure ===
# Run weekly-worker-restart.sh from a stub lib dir (stub spin-up-bot FAILS, so
# the loud emit_failure_alert path is exercised too). The manager (MANAGER_TMUX==BOT_ID)
# must be skipped; the worker must be processed.
WR_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate-wr.XXXXXX")"
WR_LIB="$WR_ROOT/lib"
mkdir -p "$WR_LIB"
cp "$LIB_DIR/lib-common.sh" "$LIB_DIR/weekly-worker-restart.sh" "$WR_LIB/"
val_link_plane_shim "$WR_LIB"
printf '#!/bin/bash\nexit 0\n' > "$WR_LIB/pre-stop-handoff.sh"
printf '#!/bin/bash\necho "stub spin-up: $1" >&2\nexit 7\n' > "$WR_LIB/spin-up-bot.sh"
chmod +x "$WR_LIB/pre-stop-handoff.sh" "$WR_LIB/spin-up-bot.sh"
WR_BOTS="$WR_ROOT/local/$FLEET/runtime/bots"
mkdir -p "$WR_BOTS/wmgr/data" "$WR_BOTS/wworker/data"
printf 'BOT_ID=wmgr\nMANAGER_TMUX=wmgr  # this bot is a manager\n' > "$WR_BOTS/wmgr/bot.conf"
printf 'BOT_ID=wworker\nMANAGER_TMUX=wmgr\n' > "$WR_BOTS/wworker/bot.conf"
CLAUDLOBBY_ROOT="$WR_ROOT" "$WR_LIB/weekly-worker-restart.sh" "$FLEET" >/dev/null 2>&1 || true
wr_log="$WR_ROOT/state/weekly-worker-restart.log"
wr_events="$(val_events "$WR_ROOT" "$FLEET" fleet restart_failed || true)"

echo ""
echo "=== validate-bot-change: weekly worker-only restart ==="
grep -q 'skip (manager): wmgr' "$wr_log" 2>/dev/null && r=yes || r=no
harness_check "weekly restart SKIPS the manager (MANAGER_TMUX==BOT_ID)" "$r"
grep -q 'worker: wworker' "$wr_log" 2>/dev/null && r=yes || r=no
harness_check "weekly restart PROCESSES the worker" "$r"
grep -q 'worker: wmgr' "$wr_log" 2>/dev/null && r=no || r=yes
harness_check "manager never entered the worker restart path" "$r"
printf '%s' "$wr_events" | grep -q '"type":"restart_failed"' && r=yes || r=no
harness_check "worker restart failure raises a restart_failed alert (shared emit_failure_alert)" "$r"

# === Scenario 4: daily bounce retired from update-claude-code.sh (static) ===
echo ""
echo "=== validate-bot-change: daily bounce retired (download-only) ==="
grep -Eq 'BOUNCE|spin-up-bot\.sh' "$LIB_DIR/update-claude-code.sh" && r=no || r=yes
harness_check "update-claude-code.sh no longer bounces the fleet" "$r"
grep -q 'npm install -g @anthropic-ai/claude-code@latest' "$LIB_DIR/update-claude-code.sh" && r=yes || r=no
harness_check "update-claude-code.sh still downloads the binary daily" "$r"

# ===========================================================================
# #415 — fleet.yaml-authoritative discovery + pane_stuck idle-guard.
#   (1) An UNDECLARED runtime dir (stale/cross-fleet residue — e.g. a bot moved
#       to another fleet, leaving its old dir behind) must be SKIPPED: zero pulse
#       events. RED before #415 — the filesystem-glob loop health-checked every
#       dir and emitted session_missing/service_down/pane_stuck for orphans
#       (the #415 bug).
#   (2) pane_stuck must honor the .idle marker like activity_stuck does: a bot
#       parked at an idle prompt has a stable pane — that is idle, not stuck.
# ===========================================================================
echo ""
echo "=== validate #415: fleet.yaml discovery filter + pane_stuck idle-guard ==="

F2="valf415"
F2_BOTS="$ROOT/local/$F2/runtime/bots"
KEEP="valkeep"; ORPH="valorphan"; IDLEK="validlek"
mkdir -p "$ROOT/local/$F2" "$F2_BOTS/$KEEP/data" "$F2_BOTS/$ORPH/data" "$F2_BOTS/$IDLEK/data"

# fleet.yaml declares KEEP + IDLEK but NOT ORPH (the residue analogue).
cat > "$ROOT/local/$F2/fleet.yaml" <<YAML
fleet:
  name: $F2
  bots:
    $KEEP:
      expertise: [software-engineering]
    $IDLEK:
      expertise: [software-engineering]
YAML
for b in "$KEEP" "$ORPH" "$IDLEK"; do
    cat > "$F2_BOTS/$b/bot.conf" <<CONF
BOT_NAME="$b"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
done

# IDLEK gets a live idle pane so its pane-hash state can seed; KEEP + ORPH get no
# session, so the old glob loop would emit session_missing for BOTH.
tmux new-session -d -s "$IDLEK" 'printf "\n> \n"; sleep 600'
sleep 1

val_plane_ready "$ROOT" "$F2"   # the readers declared for this fleet too (its manifest is kept)
# Run 1: seeds IDLEK pane hash/ts; health-checks declared bots.
CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_FLEET="$F2" "$LIB_DIR/fleet-pulse.sh" "$F2" >/dev/null 2>&1 || true

# Make IDLEK idle (.idle newer than .last-tool-call) and backdate its pane ts so
# the next sweep sees elapsed >= 300 without a real 5-minute wait.
touch "$F2_BOTS/$IDLEK/data/.last-tool-call"; sleep 1; touch "$F2_BOTS/$IDLEK/data/.idle"
_now415=$(date +%s); printf '%s' "$((_now415 - 400))" > "$ROOT/state/pulse/$IDLEK.pane_ts"

# Run 2: IDLEK pane unchanged + elapsed 400 would trip pane_stuck — idle-guard must suppress.
CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_FLEET="$F2" "$LIB_DIR/fleet-pulse.sh" "$F2" >/dev/null 2>&1 || true

keep_ev=$(val_events "$ROOT" "$F2" "$KEEP")
orph_ev=$(val_events "$ROOT" "$F2" "$ORPH")
idlek_ev=$(val_events "$ROOT" "$F2" "$IDLEK")

printf '%s' "$keep_ev" | grep -q '"type":"session_missing"' && r=yes || r=no
harness_check "#415 declared bot is still health-checked (session_missing fired for $KEEP)" "$r"

if [ -z "$orph_ev" ]; then r=yes
elif printf '%s' "$orph_ev" | grep -qE '"type":"(session_missing|service_down|pane_stuck)"'; then r=no
else r=yes; fi
harness_check "#415 undeclared orphan dir emits ZERO pulse events (filtered via fleet.yaml)" "$r"

printf '%s' "$idlek_ev" | grep -q '"type":"pane_stuck"' && r=no || r=yes
harness_check "#415 pane_stuck suppressed for an idle-at-prompt bot (.idle guard)" "$r"

# ===========================================================================
# #611 summary socket resolution + pane_stuck busy-guard (this PR).
#   (a) #611: the SUMMARY must resolve the tmux socket the SAME way the main loop
#       does (tmux_socket_for_bot), else a bot whose TMUX_SOCKET != BOT_SERVICE
#       shows a false SESSION DOWN in the summary while the main loop sees it up.
#   (b) pane_stuck must NOT fire on a WORKING bot: a fresh data/.last-tool-call
#       (mid-tool-call) or an "esc to interrupt" pane (active turn / waiting on a
#       subagent) is busy, not stuck.
# ===========================================================================
echo ""
echo "=== validate #611 summary socket + pane_stuck busy-guard (this PR) ==="

# (a) #611 repro: TMUX_SOCKET points at the live server, BOT_SERVICE is a
#     DIFFERENT string. The shadow tmux() puts the session on tmux-valsock.
SOCKB="valsock"
mkdir -p "$F2_BOTS/$SOCKB/data"
cat > "$F2_BOTS/$SOCKB/bot.conf" <<CONF
BOT_NAME="$SOCKB"
BOT_SERVICE="com.diverge.$SOCKB"
TMUX_SOCKET="tmux-$SOCKB"
MANAGER_TMUX="$MGR"
CONF
tmux new-session -d -s "$SOCKB" 'printf "\n> \n"; sleep 600'

# (b) pane_stuck busy repros: BUSYM = fresh .last-tool-call (mid-tool-call),
#     BUSYP = "esc to interrupt" pane (active turn). Both static + backdated.
BUSYM="valbusym"; BUSYP="valbusyp"
mkdir -p "$F2_BOTS/$BUSYM/data" "$F2_BOTS/$BUSYP/data"
for b in "$BUSYM" "$BUSYP"; do
    cat > "$F2_BOTS/$b/bot.conf" <<CONF
BOT_NAME="$b"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF
done
tmux new-session -d -s "$BUSYM" 'printf "\n> \n"; sleep 600'
tmux new-session -d -s "$BUSYP" 'printf "\nWorking (esc to interrupt)\n"; sleep 600'
sleep 1

# Declare the three new bots so fleet-pulse does not filter them as orphans (#415).
cat > "$ROOT/local/$F2/fleet.yaml" <<YAML
fleet:
  name: $F2
  bots:
    $KEEP:
      expertise: [software-engineering]
    $IDLEK:
      expertise: [software-engineering]
    $SOCKB:
      expertise: [software-engineering]
    $BUSYM:
      expertise: [software-engineering]
    $BUSYP:
      expertise: [software-engineering]
YAML

# Run 3: seed pane hashes/ts for the new bots.
CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_FLEET="$F2" "$LIB_DIR/fleet-pulse.sh" "$F2" >/dev/null 2>&1 || true

# Mark BUSYM "working" (fresh marker, no .idle) and backdate both busy bots past
# the 300s pane threshold; BUSYP stays marker-less (its esc-to-interrupt pane is
# the only busy signal).
_n611=$(date +%s)
touch "$F2_BOTS/$BUSYM/data/.last-tool-call"
printf '%s' "$((_n611 - 400))" > "$ROOT/state/pulse/$BUSYM.pane_ts"
printf '%s' "$((_n611 - 400))" > "$ROOT/state/pulse/$BUSYP.pane_ts"

# Run 4: the sweep where pane_stuck would trip for the busy bots + the summary runs.
CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_FLEET="$F2" "$LIB_DIR/fleet-pulse.sh" "$F2" >/dev/null 2>&1 || true

val_events "$ROOT" "$F2" "$BUSYM" pane_stuck | grep -q '"type":"pane_stuck"' && r=no || r=yes
harness_check "pane_stuck NOT fired for a working bot (fresh .last-tool-call, mid-tool-call)" "$r"
val_events "$ROOT" "$F2" "$BUSYP" pane_stuck | grep -q '"type":"pane_stuck"' && r=no || r=yes
harness_check "pane_stuck NOT fired for a working bot (esc-to-interrupt pane, active turn)" "$r"

# #611: the summary must show the TMUX_SOCKET-only bot as up, not a false DOWN.
_sumfile="$ROOT/state/pulse/pulse-summary.txt"
printf '%s' "$(grep "^$SOCKB " "$_sumfile" 2>/dev/null || true)" | awk '{print $2}' | grep -qx up && r=yes || r=no
harness_check "#611 summary session=up for a bot whose TMUX_SOCKET != BOT_SERVICE" "$r"
# related: a BOT_SERVICE-less bot must not show a false SERVICE DOWN in the summary.
printf '%s' "$(grep "^$IDLEK " "$_sumfile" 2>/dev/null || true)" | awk '{print $3}' | grep -qx ok && r=yes || r=no
harness_check "#611 summary service=ok (not false DOWN) for a BOT_SERVICE-less bot" "$r"

# ===========================================================================
# Per-bot socket isolation (#414) — blast radius = 1 + observable misses.
# valmgr + valbot are up on DISTINCT private servers (tmux-valmgr/tmux-valbot),
# so a single server's death can no longer drop the whole fleet at once.
# ===========================================================================
echo ""
echo "=== validate #414: per-bot socket isolation (blast radius + send-miss) ==="

command tmux -L "$(vsock "$MGR")" has-session -t "$MGR" 2>/dev/null && r=yes || r=no
harness_check "#414 precondition: manager is up on its own private server" "$r"
command tmux -L "$(vsock "$BOT")" has-session -t "$BOT" 2>/dev/null && r=yes || r=no
harness_check "#414 precondition: worker is up on a DISTINCT private server" "$r"

# Crown jewel: kill ONE bot's whole server; only that bot dies.
command tmux -L "$(vsock "$BOT")" kill-server 2>/dev/null || true
sleep 0.3
command tmux -L "$(vsock "$BOT")" has-session -t "$BOT" 2>/dev/null && r=no || r=yes
harness_check "#414 blast radius: the killed bot's server is gone" "$r"
command tmux -L "$(vsock "$MGR")" has-session -t "$MGR" 2>/dev/null && r=yes || r=no
harness_check "#414 blast radius = 1: a peer SURVIVES the kill (shared-server SPOF removed)" "$r"

# Observable miss: a cross-socket send to a dead target lands a send_miss event
# in the caller's ledger — the silent `|| true` is gone.
BOT_DIR="$BOT_DIR" BOT_ID="$BOT" bash -c \
    '. "'"$LIB_DIR"'/lib-common.sh"; bot_tmux_send "tmux-valgone" "valgone" "ping"' \
    >/dev/null 2>&1 || true
val_events "$ROOT" "$FLEET" "$BOT" send_miss | grep -q '"type":"send_miss"' && r=yes || r=no
harness_check "#414 send-miss: a cross-socket send to a dead target is logged, not silently dropped" "$r"

# ===========================================================================
# #591 Phase 1 — Telegram bridge poller-hijack mechanism (the regression gate
# for the poller-hijack fix plan). Proves, against the REAL installed plugin,
# that the boot-time reap in server.ts is last-writer-wins: a second instance
# started in the same state dir SIGTERMs the LIVE holder, takes the single
# getUpdates slot, and abandons it on exit (ownership-checked unlink) — the
# confirmed root cause of the permanent dark-bridge incidents. Promoted from
# the bot-local repro (17/17 x3 at root-cause time) onto the scaffolding it
# shares with the scenarios above: per-run private dirs, the check counters,
# the cleanup trap.
#
# Fake token only — the env prefix on each spawn also shields any real
# TELEGRAM_BOT_TOKEN in the caller's environment, so the pollers never reach
# a real bot. No tmux: pollers are raw bun processes held open on fifos,
# mirroring how claude runs the MCP (stdin EOF = the graceful
# client-disconnect path). No bot.conf either — keepalive is not involved
# (the heal half of the incident is the #453 scenario above). If this
# scenario ever grows a throwaway bot.conf: tmux_socket_for_bot refuses an
# empty BOT_SERVICE while FLEET_NAME is set — the harness-wide unset at the
# top is what makes the empty-BOT_SERVICE fixtures work.
#
# When the plugin learns to defer to a live holder (#591 Phases 3/4), the
# anchor probe below FAILS loudly — flip this scenario's expectations to
# "defer occurs" in the same change that adopts the fixed plugin.
echo ""
echo "=== validate bridge-hijack mechanism (#591 Phase 1: last-writer-wins reap) ==="

# Newest installed plugin version, never a pinned one (#591 P1 step 2). The
# cache root honors CLAUDE_CONFIG_DIR — multi-account bots keep their plugin
# cache under the account dir, same seam as start-bot.sh. A missing or empty
# cache just yields a path whose server.ts fails the probe below → SKIP.
BH_BASE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache/claude-plugins-official/telegram"
BH_PLUGIN="$BH_BASE/$(ls -1 "$BH_BASE" 2>/dev/null | sort -V | tail -n1 || true)"
BH_SKIP=""
command -v bun >/dev/null 2>&1 || BH_SKIP="bun not installed"
[ -n "$BH_SKIP" ] || [ -f "$BH_PLUGIN/server.ts" ] || BH_SKIP="telegram plugin not installed under $BH_BASE"

if [ -n "$BH_SKIP" ]; then
    # Dep-absent hosts (CI runners) skip clean with a reason — mirroring the
    # pytest skipif contract. Hosts WITH the deps are the enforcement point.
    echo "  SKIP  bridge-hijack scenario: $BH_SKIP (needs bun + the installed telegram plugin)"
else
    BH_DIR="$ROOT/bh"
    BH_STATE="$BH_DIR/state"
    mkdir -p "$BH_STATE"
    BH_TOKEN="8888888:AAAAAAAAAAAAAAAAAAAA"
    _bh_fail_before=$fail

    # Anchor probe — the promoted patch-anchor assertion (#591 P1 step 3).
    # The checks below encode EXACTLY this reap block. On drift, fail loud
    # and skip the spawns: a changed reap makes them meaningless, and
    # cascading FAILs would bury the real message. Deliberately a FAIL, not
    # a SKIP — a present-but-different plugin must never read as "covered".
    python3 - "$BH_PLUGIN/server.ts" 2>/dev/null <<'PY' && r=yes || r=no
import sys
src = open(sys.argv[1]).read()
block = """    process.kill(stale, 0)
    process.stderr.write(`telegram channel: replacing stale poller pid=${stale}\\n`)
    process.kill(stale, 'SIGTERM')"""
sys.exit(0 if src.count(block) == 1 else 1)
PY
    harness_check "plugin reap anchor present exactly once (drift fails loud, never tests nothing)" "$r"

    if [ "$r" != "yes" ]; then
        echo "  NOTE  plugin at $BH_PLUGIN no longer matches the hijack expectations — update this scenario alongside the plugin (#591 Phase 4)"
    else
        # spawn_poller <label> <fd-number> [prev_pid] — start the plugin the
        # way claude does (bun run ... start), stdin held open on a fifo so
        # the poller lives until its fd closes. MUST run in the main shell —
        # a subshell would take the fifo writer fd down with it. Fixed fd
        # numbers via eval keep this parseable by bash 3.2 (macOS /bin/bash;
        # exec {var}> auto-allocation is bash 4.1+). Sets BH_PID from
        # bot.pid, waiting for a value different from prev_pid.
        BH_PIDS=""
        spawn_poller() {
            local label="$1" fd="$2" prev="${3:-}" fifo i cur
            fifo="$BH_DIR/$label.fifo"
            mkfifo "$fifo"
            ( TELEGRAM_STATE_DIR="$BH_STATE" TELEGRAM_BOT_TOKEN="$BH_TOKEN" \
                bun run --cwd "$BH_PLUGIN" --shell=bun --silent start \
                < "$fifo" > "$BH_DIR/$label.out" 2> "$BH_DIR/$label.err" ) &
            eval "exec $fd>\"\$fifo\""
            BH_PID=""
            for i in $(seq 1 60); do
                cur="$(cat "$BH_STATE/bot.pid" 2>/dev/null || true)"
                if [ -n "$cur" ] && [ "$cur" != "$prev" ]; then BH_PID="$cur"; break; fi
                sleep 0.25
            done
            [ -n "$BH_PID" ] && BH_PIDS="$BH_PIDS $BH_PID"
            return 0
        }
        bh_until() { # <max_s> <cmd...> — poll at 0.25s ticks; yes on success
            local i n=$(( $1 * 4 ))
            shift
            for i in $(seq 1 "$n"); do
                "$@" 2>/dev/null && { echo yes; return; }
                sleep 0.25
            done
            echo no
        }
        bh_gone() { ! ps -p "$1" >/dev/null 2>&1; }

        spawn_poller A 8
        BH_A="$BH_PID"
        { [ -n "$BH_A" ] && ps -p "$BH_A" >/dev/null 2>&1; } && r=yes || r=no
        harness_check "victim poller A up and holding bot.pid (real plugin, fake token)" "$r"

        spawn_poller B 9 "$BH_A"
        BH_B="$BH_PID"
        [ "$(bh_until 6 grep -q "replacing stale poller pid=$BH_A" "$BH_DIR/B.err")" = "yes" ] && r=yes || r=no
        harness_check "newcomer B SIGTERMs the LIVE holder (last-writer-wins reap fired)" "$r"

        { [ "$(bh_until 5 bh_gone "$BH_A")" = "yes" ] && [ "$(bh_until 5 grep -q "shutting down" "$BH_DIR/A.err")" = "yes" ]; } && r=yes || r=no
        harness_check "victim A exits via the graceful shutdown path" "$r"

        { [ -n "$BH_B" ] && [ "$(cat "$BH_STATE/bot.pid" 2>/dev/null)" = "$BH_B" ]; } && r=yes || r=no
        harness_check "slot taken over: bot.pid now = newcomer B" "$r"

        exec 9>&-   # transient B loses its client: stdin EOF
        [ "$(bh_until 6 bh_gone "$BH_B")" = "yes" ] && r=yes || r=no
        harness_check "transient B exits when its client goes away (stdin EOF)" "$r"

        [ ! -f "$BH_STATE/bot.pid" ] && r=yes || r=no
        harness_check "B unlinks bot.pid on exit — slot ABANDONED" "$r"

        sleep 1   # settle window for the negative assertion below
        { [ ! -f "$BH_STATE/bot.pid" ] && bh_gone "${BH_A:-0}" && bh_gone "${BH_B:-0}"; } && r=yes || r=no
        harness_check "victim never returns: slot stays dark after settle (no respawn, no reclaim)" "$r"

        exec 8>&-   # release the dead victim's writer fd

        if [ "$fail" -gt "$_bh_fail_before" ]; then
            echo "  --- DIAGNOSTIC: bridge-hijack poller stderr ---"
            for _f in "$BH_DIR"/*.err; do
                [ -s "$_f" ] || continue
                echo "  [$(basename "$_f")]"
                sed 's/^/    /' "$_f" | tail -6
            done
        fi
    fi
fi

# ===========================================================================
# Nested-fleet supervision (#602 P2 slice 2). The recursive-containment vault
# may nest a fleet one level under a system container:
#     flat    local/<fleet>/...            (the live Pi today — must stay identical)
#     nested  local/<system>/<fleet>/...   (opt-in; the container has no fleet.yaml)
# This proves the bash supervision layer resolves + health-checks a NESTED bot
# end-to-end, WITHOUT disturbing the flat path (the byte-identical invariant).
# ===========================================================================
echo ""
echo "=== validate #602 P2: nested-fleet resolution + supervision ==="

NSYS="valsys"; NF="valnest"; NBOT="valnestbot"
NF_DIR="$ROOT/local/$NSYS/$NF"
NF_BOTS="$NF_DIR/runtime/bots"
mkdir -p "$NF_BOTS/$NBOT/data"
cat > "$NF_DIR/fleet.yaml" <<YAML
fleet:
  name: $NF
  bots:
    $NBOT:
      expertise: [software-engineering]
YAML
cat > "$NF_BOTS/$NBOT/bot.conf" <<CONF
BOT_NAME="$NBOT"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
CONF

# Direct resolver assertions run in THIS shell (lib-common sourced at the top),
# so point CLAUDLOBBY_ROOT at the throwaway root for the duration, then restore.
_prev_root="${CLAUDLOBBY_ROOT:-}"
CLAUDLOBBY_ROOT="$ROOT"

[ "$(resolve_fleet_dir "$NF")" = "$NF_DIR" ] && r=yes || r=no
harness_check "#602 resolve_fleet_dir finds a fleet nested under a system container" "$r"

[ "$(resolve_bots_dir "$NF")" = "$NF_BOTS" ] && r=yes || r=no
harness_check "#602 resolve_bots_dir returns the nested runtime/bots" "$r"

host_bots_dirs | grep -qxF "$NF_BOTS" && r=yes || r=no
harness_check "#602 host_bots_dirs enumerates the nested bots dir" "$r"

# Byte-identical invariant: the FLAT valfleet must STILL resolve flat, unchanged.
[ "$(resolve_fleet_dir "$FLEET")" = "$ROOT/local/$FLEET" ] && r=yes || r=no
harness_check "#602 flat fleet STILL resolves flat (byte-identical invariant holds)" "$r"

CLAUDLOBBY_ROOT="$_prev_root"

# End-to-end: the REAL fleet-pulse supervision script must find + health-check
# the nested bot (no live session -> session_missing), proving resolve_bots_dir
# and the nested fleet.yaml discovery both fire through a production script.
val_plane_ready "$ROOT" "$NF"   # resolves the NESTED fleet dir; its manifest is kept
CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_FLEET="$NF" "$LIB_DIR/fleet-pulse.sh" "$NF" >/dev/null 2>&1 || true
val_events "$ROOT" "$NF" "$NBOT" session_missing | grep -q '"type":"session_missing"' && r=yes || r=no
harness_check "#602 fleet-pulse health-checks a bot in a NESTED fleet (session_missing fired)" "$r"

# ===========================================================================
# Scenario: equippable briefing trigger (#627 P3/P4/P6). The timer-fired
# /briefing dispatch must (a) fire a briefing_dispatched event, (b) land as a
# REAL bare slash command — NOT set +H; prose (the F6 regression canary) — and
# (c) defer on a busy bot; the shared dispatch.sh classifier must keep the
# set +H; guard on (d) prose, (e) file-path prose + a leading-whitespace slash;
# and (f) the /briefing skill's documented env-read must resolve the CONFIGURED
# sections, not the canonical default. This is BEHAVIOR a unit test cannot
# prove. The composed-timer plumbing (compose -> enroll -> journal fire ->
# reconcile prune, incl. the launchd .plist prune) is the sibling
# lib/rehearse-briefing-timer.sh (real systemd timer, ~2 min, run separately).
# ===========================================================================
echo ""
echo "=== validate-bot-change: equippable briefing trigger (#627 P6) ==="

BRIEF="valbrief"; BRIEFBUSY="valbriefbusy"; SINK="valsink"
BRIEF_DIR="$ROOT/local/$FLEET/runtime/bots/$BRIEF"
BRIEFBUSY_DIR="$ROOT/local/$FLEET/runtime/bots/$BRIEFBUSY"
# data/ holds the busy bot's .last-tool-call marker and is where the trigger
# emits events; briefing-trigger.sh + emit_fleet_event land on the plane (logs/ still)
# on demand. SINK is a pure classifier sink (pane-only) — no dir needed; its
# socket resolves by the basename fallback.
mkdir -p "$BRIEF_DIR/data" "$BRIEFBUSY_DIR/data"

# Composed-SHAPE bot.conf: BOT_SERVICE empty so tmux_socket_for_bot resolves the
# harness fallback tmux-<name> (the socket the tmux() shim targets). BRIEFING_*
# mirror the composer emission (compose_bot_conf); BRIEFING_SECTIONS_MORNING is
# UPPER-CASED exactly as composer.py emits it — the var the /briefing skill reads
# by upper-casing the dispatched slot — with a NON-default section list so (f)
# can tell config-tracking from the canonical morning default. (Emission case is
# unit-tested in tests/test_briefing.py; this scenario proves the read-side.)
for _d in "$BRIEF_DIR" "$BRIEFBUSY_DIR"; do
    _n="$(basename "$_d")"
    cat > "$_d/bot.conf" <<CONF
BOT_NAME="$_n"
BOT_ID="$_n"
BOT_SERVICE=""
MANAGER_TMUX="$MGR"
BRIEFING_SLOTS="morning"
BRIEFING_SOURCES="github"
BRIEFING_SECTIONS_MORNING="wrap tomorrow overnight"
CONF
done

# Idle briefing bot: plain pane, no esc-to-interrupt, no fresh .last-tool-call
# -> bot_is_busy reads not-busy -> the trigger dispatches.
tmux new-session -d -s "$BRIEF" "sleep 600"
# Busy briefing bot: a fresh data/.last-tool-call -> bot_is_busy reads BUSY via
# the rendering-immune marker branch (no pane-render race) -> the trigger defers.
tmux new-session -d -s "$BRIEFBUSY" "sleep 600"
touch "$BRIEFBUSY_DIR/data/.last-tool-call"
# Classifier sink: an idle pane that receives direct dispatch.sh sends, so the
# computed PAYLOAD (bare vs set +H;) is observable verbatim in the captured pane.
tmux new-session -d -s "$SINK" "sleep 600"
sleep 1  # let panes render

# --- Observe: the real trigger against the idle + busy briefing bots ----------
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/briefing-trigger.sh" "$FLEET" "$BRIEF" morning >/dev/null 2>&1 || true
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/briefing-trigger.sh" "$FLEET" "$BRIEFBUSY" morning >/dev/null 2>&1 || true
# Prose + classifier-edge payloads straight through dispatch.sh: prose with a
# bang, file-path prose, and a leading-whitespace slash must ALL keep set +H;.
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/dispatch.sh" "$SINK" "deploy failed alert !!" >/dev/null 2>&1 || true
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/dispatch.sh" "$SINK" "/home/user/x is broken" >/dev/null 2>&1 || true
CLAUDLOBBY_ROOT="$ROOT" "$LIB_DIR/dispatch.sh" "$SINK" " /leading-space-prose" >/dev/null 2>&1 || true
sleep 1  # let the sends render into the panes

# --- Assert ---
brief_events=$(val_events "$ROOT" "$FLEET" "$BRIEF")
briefbusy_events=$(val_events "$ROOT" "$FLEET" "$BRIEFBUSY")
brief_pane=$(tmux capture-pane -t "$BRIEF" -p 2>/dev/null || true)
busy_pane=$(tmux capture-pane -t "$BRIEFBUSY" -p 2>/dev/null || true)
sink_pane=$(tmux capture-pane -t "$SINK" -p 2>/dev/null || true)

# (a) the trigger fires — briefing_dispatched on the idle bot's ledger
printf '%s' "$brief_events" | grep -q '"type":"briefing_dispatched"' && r=yes || r=no
harness_check "briefing trigger fires: briefing_dispatched event emitted (idle bot)" "$r"

# (b) bare-slash lands — /briefing morning present, NO set +H; prefix (F6 canary)
{ printf '%s' "$brief_pane" | grep -q '/briefing morning' \
    && ! printf '%s' "$brief_pane" | grep -q 'set +H'; } && r=yes || r=no
harness_check "briefing dispatch lands as a BARE slash command (no set +H; — F6 regression canary)" "$r"

# (c) busy-defer — briefing_deferred/bot_busy, and NOTHING sent to the busy pane
printf '%s' "$busy_pane" | grep -q '/briefing' && _sent=yes || _sent=no
{ printf '%s' "$briefbusy_events" | grep -q '"type":"briefing_deferred".*"reason":"bot_busy"' \
    && [ "$_sent" = no ]; } && r=yes || r=no
harness_check "briefing defers on a busy bot: briefing_deferred/bot_busy, no dispatch" "$r"

# (d) prose control — a non-slash payload keeps the set +H; guard
printf '%s' "$sink_pane" | grep -qE 'set \+H; deploy failed alert' && r=yes || r=no
harness_check "dispatch classifier keeps set +H; on prose (deploy failed !!)" "$r"

# (e) classifier edges — file-path prose + leading-whitespace slash keep the guard
printf '%s' "$sink_pane" | grep -qE 'set \+H; /home/user/x is broken' && r=yes || r=no
harness_check "dispatch classifier keeps set +H; on file-path prose (/home/... not a slash cmd)" "$r"
printf '%s' "$sink_pane" | grep -qE 'set \+H; +/leading-space-prose' && r=yes || r=no
harness_check "dispatch classifier keeps set +H; on a leading-whitespace slash (not anchored ^/)" "$r"

# (f) skill env-consumption (F4). The /briefing skill must actually CONSUME the
# configured sections — the F4 "silently-ignored section list" risk. SKILL.md is
# model-facing prose with no runnable artifact in this model-free harness, so
# prove the consumption CONTRACT lives in the REAL library skill file: its
# actionable "## Instructions" must READ BRIEFING_SECTIONS_<SLOT> and RENDER the
# configured sections (a mention in a reference table is not consumption). TEETH:
# gutting SKILL.md's BRIEFING_SECTIONS handling makes this go RED (mutation-
# verified). A live model honoring the instruction is the P7 human canary; this
# is the deterministic file-contract gate. NOTE: the prior version re-read a
# bot.conf var the test itself wrote and never touched SKILL.md, so a gutted
# skill stayed green — hollow (#640 request-changes).
_skill="$LIB_DIR/../library/skills/briefing/SKILL.md"
_instr=$(awk '/^## Instructions/{f=1; next} /^## /{f=0} f' "$_skill" 2>/dev/null)
{ [ -f "$_skill" ] \
    && printf '%s\n' "$_instr" | grep -q 'BRIEFING_SECTIONS' \
    && printf '%s\n' "$_instr" | grep -qi 'configured section'; } && r=yes || r=no
harness_check "briefing SKILL.md Instructions consume BRIEFING_SECTIONS_<SLOT> (read the var + render the configured sections)" "$r"

# ===========================================================================
# #1002 — the boot window. A bot whose unit is mid-start has no tmux session
# yet, which by state alone is indistinguishable from a dead one: fleet-pulse
# alarmed on it (service_down carrying state=activating, self-proving false)
# and keepalive restarted it, killing the very boot that would have produced
# the session.
#
# Only running this proves it. A stubbed systemctl would let the predicate
# assert whatever it likes about a state machine it never met, so this drives a
# REAL systemd user unit built to the composed shape (Type=simple +
# RemainAfterExit=yes + a spawner ExecStart that exits) through all three of its
# states, and drives the REAL keepalive.sh and fleet-pulse.sh against it.
#
# The control at the end is the load-bearing half: a settled unit with a dead
# session MUST still restart. Without it, a predicate that simply returned
# "starting" always — permanently disabling the watchdog while every surface
# reads healthy — would pass every other assertion here.
# ===========================================================================
echo ""
echo "=== validate #1002: the boot window is not down, and not a restart trigger ==="

# Gated: needs a real systemd --user bus. macOS and CI containers without a
# user manager skip rather than fail — a harness that cannot run the mechanism
# must not report a verdict about it (coverage honesty).
if ! systemd_user_bus_available; then
    echo "  SKIP  #1002 boot-window scenario — no systemd --user bus (needs Linux + linger)"
    echo "        NOT a pass: the activating / active-running / active-exited"
    echo "        transitions and both consumers went unexercised on this host."
else
    BP_SVC="claudlobby-vbc-bootprobe-$$"
    BP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate-bp.XXXXXX")"
    BP_FLEET="bpfleet"
    BP_BOT="bootprobe"
    BP_DIR="$BP_ROOT/local/$BP_FLEET/runtime/bots/$BP_BOT"
    BP_UNIT="$HOME/.config/systemd/user/$BP_SVC.service"
    BP_EVENTS="$BP_DIR/data/events"
    mkdir -p "$BP_DIR/data" "$BP_ROOT/state" "$HOME/.config/systemd/user"

    # Mirror start-bot.sh: do slow pre-session work (there, plugin install), THEN
    # create the session, THEN exit. The gap between "unit went active" and
    # "session exists" is the window that stranded a bot for 35-178s.
    cat > "$BP_ROOT/spawner.sh" <<BPSPAWN
#!/bin/bash
sleep 8
tmux -L "$BP_SVC" new-session -d -s "$BP_BOT" 'sleep 600'
BPSPAWN
    chmod +x "$BP_ROOT/spawner.sh"

    cat > "$BP_UNIT" <<BPUNIT
[Unit]
Description=claudlobby validate-bot-change boot-window probe
[Service]
Type=simple
RemainAfterExit=yes
KillMode=process
ExecStartPre=/bin/sleep 4
ExecStart=$BP_ROOT/spawner.sh
BPUNIT

    cat > "$BP_DIR/bot.conf" <<BPCONF
BOT_NAME=$BP_BOT
BOT_SERVICE=$BP_SVC
TMUX_SESSION=$BP_BOT
BPCONF

    # The plane setup (five reader declarations through the CLI: seconds) must
    # run BEFORE the unit starts — placed after it, it consumed the 4s
    # ExecStartPre window the first sample exists to observe (CI: "observed
    # active/running" where activating/start-pre was the point).
    val_plane_ready "$BP_ROOT" "$BP_FLEET"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user start --no-block "$BP_SVC" >/dev/null 2>&1 || true

    bp_state() { systemctl --user show -p ActiveState -p SubState --value "$BP_SVC" 2>/dev/null | paste -sd/ -; }
    bp_starting() { service_is_starting "$BP_SVC"; }

    bp_pulse() {
        CLAUDLOBBY_ROOT="$BP_ROOT" CLAUDLOBBY_FLEET="$BP_FLEET" FLEET_PULSE_ESCALATE_CHAT_ID="" \
            "$LIB_DIR/fleet-pulse.sh" "$BP_FLEET" >/dev/null 2>&1 || true
    }

    # --- State 1: activating (ExecStartPre — the boot-stagger sleep) ---
    sleep 2
    _s1=$(bp_state)
    bp_starting && r=yes || r=no
    harness_check "activating unit reads as mid-start (observed $_s1)" "$r"
    [ "$_s1" = "activating/start-pre" ] && r=yes || r=no
    harness_check "  ...and that state really was activating, not assumed" "$r"

    # Pulse HERE, in the activating window. This is the only state where
    # service_is_active reports not-active, so it is the only state that can
    # reach the service_down branch — the finding-B false positive
    # (service_down state=activating) lives here and nowhere else. Sampling the
    # pulse only in the later active/running window would leave finding B
    # entirely unexercised while reading green.
    bp_pulse

    # --- State 2: active/running (spawner executing, session not up yet) ---
    # The state ActiveState alone cannot see, and where all 3 restarts landed.
    sleep 6
    _s2=$(bp_state)
    _sess2=no; command tmux -L "$BP_SVC" has-session -t "$BP_BOT" 2>/dev/null && _sess2=yes
    [ "$_s2" = "active/running" ] && [ "$_sess2" = no ] && r=yes || r=no
    harness_check "mid-boot window reached: unit active/running with NO session (observed $_s2, session=$_sess2)" "$r"
    bp_starting && r=yes || r=no
    harness_check "  ...and service_is_starting still reads mid-start there" "$r"

    # Consumer C: the real keepalive must NOT restart this boot.
    CLAUDLOBBY_ROOT="$BP_ROOT" "$LIB_DIR/keepalive.sh" "$BP_DIR" >/dev/null 2>&1 || true
    _kl="$BP_DIR/keepalive.log"
    grep -q 'boot in flight' "$_kl" 2>/dev/null && r=yes || r=no
    harness_check "keepalive SKIPs a boot in flight instead of restarting it" "$r"
    grep -q 'RESTART' "$_kl" 2>/dev/null && r=no || r=yes
    harness_check "  ...and emitted no RESTART line for it" "$r"

    # Consumer B: the real fleet-pulse must not alarm anywhere in the boot. The
    # ledger below accumulates BOTH pulse runs — the activating one above and
    # this active/running one — so the assertions cover the whole window rather
    # than whichever state happened to be sampled.
    bp_pulse
    _bpev=$(val_events "$BP_ROOT" "$BP_FLEET" "$BP_BOT")
    printf '%s' "$_bpev" | grep -q '"type":"service_down"' && r=no || r=yes
    harness_check "fleet-pulse emits no service_down across the boot (incl. the activating window, where it is reachable)" "$r"
    printf '%s' "$_bpev" | grep -q '"type":"session_missing"' && r=no || r=yes
    harness_check "  ...and no session_missing either (same tick, same non-problem)" "$r"

    # --- State 3: active/exited — settled. The assumption the predicate rests on. ---
    for _i in $(seq 1 100); do
        [ "$(bp_state)" = "active/exited" ] && break
        sleep 0.2
    done
    _s3=$(bp_state)
    [ "$_s3" = "active/exited" ] && r=yes || r=no
    harness_check "a SETTLED bot unit reads active/exited (observed $_s3) — if this ever reads active/running, SubState stops meaning mid-boot and the watchdog silently dies" "$r"
    bp_starting && r=no || r=yes
    harness_check "  ...and service_is_starting stops suppressing once settled" "$r"

    # --- CONTROL: settled unit + dead session MUST still restart. ---
    # This is what distinguishes the fix from "disable the watchdog".
    command tmux -L "$BP_SVC" kill-server 2>/dev/null || true
    : > "$_kl"
    CLAUDLOBBY_ROOT="$BP_ROOT" "$LIB_DIR/keepalive.sh" "$BP_DIR" >/dev/null 2>&1 || true
    grep -q 'RESTART' "$_kl" 2>/dev/null && r=yes || r=no
    harness_check "CONTROL: a genuinely dead session on a settled unit still restarts" "$r"

fi

# ===========================================================================
# #1019 — the GitHub mention guard. Bots wrote @teammate in PR comments;
# every fleet bot name is also a real GitHub account, and one of those people
# asked us to stop. Composition cannot prove this: what matters is whether a
# real gh invocation is actually rewritten before it runs, and whether Telegram
# is genuinely left alone.
# ===========================================================================
echo ""
echo "=== validate #1019: GitHub mention guard rewrites, and spares Telegram ==="

GM_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate-gm.XXXXXX")"
mkdir -p "$GM_ROOT/runtime/_host"
printf 'worker-1\nworker-2\nworker-3\nworker-4\n' > "$GM_ROOT/runtime/_host/bot-handles"

gm_run() {  # gm_run <payload-json> -> stdout of the hook
    GH_MENTION_HANDLES_FILE="$GM_ROOT/runtime/_host/bot-handles" \
    CLAUDLOBBY_ROOT="$GM_ROOT" bash "$LIB_DIR/gh-mention-guard.sh" <<<"$1" 2>/dev/null
}

# --- the exact shape that caused the incident ---
_gm=$(gm_run '{"tool_name":"Bash","tool_input":{"command":"gh pr comment 1018 --body \"thanks @worker-2\""}}')
printf '%s' "$_gm" | grep -q '"updatedInput"' && r=yes || r=no
harness_check "a real \`gh pr comment\` carrying @worker-2 is rewritten before it runs" "$r"
printf '%s' "$_gm" | grep -q '@worker-2' && r=no || r=yes
harness_check "  ...and the @ sigil is gone from the command GitHub would see" "$r"

# The Bash surface must NOT gain backticks: a comment body sits inside a
# double-quoted shell string, where a backtick is command substitution. The
# naive fix would turn a notification bug into arbitrary execution.
printf '%s' "$_gm" | grep -q '`' && r=no || r=yes
harness_check "  ...with NO backticks injected into the shell command (would be command substitution)" "$r"

# --- MCP writers, the half a Bash-only hook would miss entirely ---
_gm=$(gm_run '{"tool_name":"mcp__github__add_issue_comment","tool_input":{"owner":"o","repo":"r","body":"@worker-2 and @worker-1 found it"}}')
printf '%s' "$_gm" | grep -q '`worker-2`' && r=yes || r=no
harness_check "an mcp__github__* body is rewritten too (backticks are safe in JSON)" "$r"
printf '%s' "$_gm" | grep -q '@worker-2' && r=no || r=yes
harness_check "  ...no @mention survives in the MCP payload" "$r"

# --- what must NOT be touched ---
[ -z "$(gm_run '{"tool_name":"Bash","tool_input":{"command":"tg-post.sh \"done @worker-3\""}}')" ] && r=yes || r=no
harness_check "TELEGRAM is untouched — tagging there is correct and load-bearing" "$r"
[ -z "$(gm_run '{"tool_name":"mcp__plugin_telegram_telegram__reply","tool_input":{"chat_id":"1","text":"@worker-3 done"}}')" ] && r=yes || r=no
harness_check "  ...including the Telegram MCP tool" "$r"
# POLICY CHANGE, deliberate: the merged denylist guard let any non-bot handle
# through, so this asserted @chrisrogers37 was untouched. Under the inversion
# nothing notifies unless DECLARED — which is the whole point, since Botfather,
# latest and 216 were all non-bot handles that emailed real people. The
# declared-handle case is asserted below, once an allowlist exists.
[ -n "$(gm_run '{"tool_name":"Bash","tool_input":{"command":"gh pr comment 1 --body \"cc @chrisrogers37\""}}')" ] && r=yes || r=no
harness_check "an UNDECLARED handle is rewritten (default-deny; was allowed pre-inversion)" "$r"
[ -z "$(gm_run '{"tool_name":"Bash","tool_input":{"command":"gh pr view 1018 --json body"}}')" ] && r=yes || r=no
harness_check "a gh READ is not rewritten (only writes can notify)" "$r"

# --- fails open, loudly, rather than blocking every GitHub write ---
_gm=$(GH_MENTION_HANDLES_FILE=/nonexistent CLAUDLOBBY_ROOT="$GM_ROOT" \
    bash "$LIB_DIR/gh-mention-guard.sh" <<<'{"tool_name":"Bash","tool_input":{"command":"gh pr comment 1 -b \"@worker-2\""}}' 2>/dev/null; echo "rc=$?")
printf '%s' "$_gm" | grep -q 'rc=0' && r=yes || r=no
harness_check "a missing handle manifest FAILS OPEN (never blocks the whole fleet from GitHub)" "$r"

# --- the inversion: the three real accounts the denylist guard misses --------
# Botfather, latest and 216 are actual GitHub users we notified. None is a fleet
# bot, so none would ever appear on the composed bot-handle list.
printf 'chrisrogers37\n' > "$GM_ROOT/runtime/_host/mention-allowlist"
gm_inv() {
    GH_MENTION_HANDLES_FILE="$GM_ROOT/runtime/_host/bot-handles" \
    GH_MENTION_ALLOWLIST_FILE="$GM_ROOT/runtime/_host/mention-allowlist" \
    CLAUDLOBBY_ROOT="$GM_ROOT" bash "$LIB_DIR/gh-mention-guard.sh" <<<"$1" 2>/dev/null
}

_inv=$(gm_inv '{"tool_name":"mcp__github__add_issue_comment","tool_input":{"body":"see @Botfather @latest @216"}}')
for h in Botfather latest 216; do
    printf '%s' "$_inv" | grep -q "\`$h\`" && r=yes || r=no
    harness_check "  INVERSION: @$h (a real account, not a bot) is rewritten" "$r"
done
printf '%s' "$_inv" | grep -q '@Botfather\|@latest\|@216' && r=no || r=yes
harness_check "  ...and no @ survives for any of the three" "$r"

_inv=$(gm_inv '{"tool_name":"mcp__github__add_issue_comment","tool_input":{"body":"cc @chrisrogers37"}}')
[ -z "$_inv" ] && r=yes || r=no
harness_check "  a DECLARED handle is left alone (the allowlist works)" "$r"

# Fail-toward-rewriting, driven through the composed hook rather than the module.
_inv=$(gm_inv '{"tool_name":"mcp__github__add_issue_comment","tool_input":{"body":"a\n```\n@worker-2 after an UNCLOSED fence"}}')
printf '%s' "$_inv" | grep -q 'worker-2' && r=yes || r=no
harness_check "  INVARIANT: an unclosed fence does NOT protect a mention" "$r"

_inv=$(gm_inv '{"tool_name":"mcp__github__add_issue_comment","tool_input":{"body":"x\n```\n@worker-2 in a closed fence\n```\ny"}}')
[ -z "$_inv" ] && r=yes || r=no
harness_check "  ...but a CLOSED fence is respected (GitHub does not linkify it)" "$r"

# --- by-reference content: the bypass #1019 exposed ----------------------------
# gh can take the body from a FILE, so the hook sees only a filename and the
# command carries no mention at all. #1019 — the issue filed ABOUT us spamming
# a stranger — was itself filed this way and re-notified her.
GM_CLEAN="$GM_ROOT/clean.md"; printf 'an ordinary body\nno mentions\n' > "$GM_CLEAN"
GM_DIRTY="$GM_ROOT/dirty.md"; printf 'intro\nthanks worker-2 and @latest\n' > "$GM_DIRTY"
# An ALLOW is empty output, not JSON — jq on empty stdin prints nothing, so the
# empty case must be named here rather than left to a jq default that never runs.
_dec() {
    local o; o="$(gm_inv "$1")"
    [ -z "$o" ] && { printf 'none'; return; }
    printf '%s' "$o" | jq -r '.hookSpecificOutput.permissionDecision // "rewrite"' 2>/dev/null
}

[ "$(_dec '{"tool_name":"Bash","tool_input":{"command":"gh issue comment 1 --body-file '"$GM_CLEAN"'"}}')" = none ] && r=yes || r=no
harness_check "  BY-REF: a body FILE with no mention passes untouched (common path costs nothing)" "$r"

for _shape in "--body-file $GM_DIRTY" "--body-file=$GM_DIRTY" "--notes-file $GM_DIRTY"; do
    [ "$(_dec '{"tool_name":"Bash","tool_input":{"command":"gh issue comment 1 '"$_shape"'"}}')" = deny ] && r=yes || r=no
    harness_check "  BY-REF: a body FILE with a mention is REFUSED ($_shape)" "$r"
done

[ "$(_dec '{"tool_name":"Bash","tool_input":{"command":"cat x | gh issue comment 1 --body-file -"}}')" = deny ] && r=yes || r=no
harness_check "  BY-REF: STDIN is refused — unreadable, so unverifiable" "$r"

# The whole reason this refuses rather than rewrites: the file is the author's.
grep -q 'worker-2' "$GM_DIRTY" && r=yes || r=no
harness_check "  BY-REF: the refused file is NOT modified on disk (it is the author's)" "$r"

rm -rf "$GM_ROOT"

# ===========================================================================
# #1009 — source currency across the framework, not just claudlobby.
#
# Composition cannot prove any of this. A unit test showing the script reads a
# second repo says nothing about whether the notice actually fires, whether the
# pull actually moves HEAD, or — the part that matters most — whether the
# guards actually STOP a pull on a tree somebody is working in. So this drives
# the REAL notify-behind.sh and update-siblings.sh against REAL git repos, and
# asserts on HEAD movement and emitted events.
#
# Fixture: throwaway repos with controlled remotes, an "org" that matches
# the fake claudlobby root, and one that does not.
# ===========================================================================
echo ""
echo "=== validate #1009: sibling currency — reports, pulls, and refuses to pull ==="

SC_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate-sc.XXXXXX")"
SC_FLEET="scfleet"
SC_BOT="scbot"
SC_DIR="$SC_ROOT/local/$SC_FLEET/runtime/bots/$SC_BOT"
mkdir -p "$SC_DIR/data" "$SC_ROOT/state" "$SC_ROOT/origins"

# sc_mkrepo <name> <org> — a bare upstream + a clone whose origin URL carries
# <org>. The URL stays a real forge-shaped string (that is what repo_remote_org
# parses) while url.<path>.insteadOf points the actual transport at the local
# bare repo — so discovery sees the org and git can still fetch. Rewriting the
# URL to the bare path instead would delete the very field under test.
sc_mkrepo() {
    local name="$1" org="$2" up="$SC_ROOT/origins/$1.git" work="$SC_ROOT/$1"
    local url="https://github.com/$org/$name.git"
    git init --quiet --bare "$up"
    git init --quiet "$work"
    git -C "$work" config user.email v@example.com
    git -C "$work" config user.name validate
    git -C "$work" config commit.gpgsign false
    git -C "$work" config "url.$up.insteadOf" "$url"
    echo v1 > "$work/f.txt"
    git -C "$work" add f.txt
    git -C "$work" commit --quiet -m c1
    git -C "$work" branch -M main
    git -C "$work" remote add origin "$url"
    git -C "$work" push --quiet origin main
    # Point the bare repo's HEAD at main, so a later clone of it checks out a
    # branch and origin/HEAD resolves — repo_default_branch reads exactly that.
    git -C "$up" symbolic-ref HEAD refs/heads/main
    git -C "$work" remote set-head origin -a >/dev/null 2>&1 || true
    git -C "$work" branch --quiet --set-upstream-to=origin/main main
    printf '%s' "$work"
}

# sc_advance <name> — add an upstream commit the clone is now behind by.
sc_advance() {
    local work="$SC_ROOT/$1" tmp="$SC_ROOT/.adv-$1"
    rm -rf "$tmp"
    git clone --quiet "$SC_ROOT/origins/$1.git" "$tmp" 2>/dev/null
    git -C "$tmp" config user.email v@example.com
    git -C "$tmp" config user.name validate
    git -C "$tmp" config commit.gpgsign false
    echo v2 >> "$tmp/f.txt"
    git -C "$tmp" add f.txt
    git -C "$tmp" commit --quiet -m c2
    git -C "$tmp" push --quiet origin main
    rm -rf "$tmp"
    git -C "$work" fetch --quiet origin 2>/dev/null || true
}

# The fake claudlobby root defines the org every sibling is matched against.
SC_HOME=$(sc_mkrepo "claudlobby" "testorg")
SC_SIB=$(sc_mkrepo "sibling" "testorg")     # framework — must be watched
SC_PROD=$(sc_mkrepo "productrepo" "otherorg") # product — must NOT be watched
SC_DIRTY=$(sc_mkrepo "dirtysib" "testorg")  # framework, but someone is mid-work

cat > "$SC_DIR/bot.conf" <<SCCONF
BOT_NAME=$SC_BOT
MANAGER_TMUX=
SCCONF

# ONE invocation path for both scripts. Only the pip layer is stubbed — the org
# match, the dedupe, the git top-level resolution, the guards and every git
# operation are the real code. $_SC_LOCS is what pip would have reported.
sc_run() {  # sc_run <script.sh> [args...]     ($_SC_LOCS = discovered locations)
    local script="$1"; shift
    CLAUDLOBBY_ROOT="$SC_HOME" \
    CLAUDLOBBY_FLEET="$SC_FLEET" \
    _SC_LOCS="$_SC_LOCS" \
    _SC_BOTS="$SC_ROOT/local/$SC_FLEET/runtime/bots" \
        bash -c '
            . "'"$LIB_DIR"'/lib-common.sh"
            _editable_project_locations() { printf "%s\n" "$_SC_LOCS"; }
            resolve_bots_dir() { printf "%s" "$_SC_BOTS"; }
            s="$1"; shift; . "$s" "$@"
        ' _ "$LIB_DIR/$script" "$@" >/dev/null 2>&1 || true
}

sc_discover() {
    CLAUDLOBBY_ROOT="$SC_HOME" _SC_LOCS="$_SC_LOCS" bash -c '
        . "'"$LIB_DIR"'/lib-common.sh"
        _editable_project_locations() { printf "%s\n" "$_SC_LOCS"; }
        discover_framework_checkouts' 2>/dev/null
}

# Host jobs run with no bot context, so emit_fleet_event anchors the receipt
# on the FLEET (bot "fleet") — the plane's twin of the old state/events file.
# The fleet comes from CLAUDLOBBY_FLEET (the timer units' carrier; sc_run
# plays the unit) — a host job run with no fleet in its environment records
# NOTHING on the plane, which is the R1 door's own gap, reported not hidden.
# Each phase reads only the events landed since its cursor (sc_reset), where
# the file used to be emptied.
SC_SINCE=""
sc_events() { val_events "$SC_HOME" "$SC_FLEET" fleet "" "$SC_SINCE"; }
sc_reset() { sleep 1; SC_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; sleep 1; }
sc_head() { git -C "$1" rev-parse HEAD; }

# --- Discovery: the org test picks framework and drops product ---
_SC_LOCS="$SC_SIB
$SC_PROD
$SC_DIRTY"
sc_watched=$(sc_discover)
printf '%s\n' "$sc_watched" | grep -qx "$SC_SIB" && r=yes || r=no
harness_check "discovery watches a same-org sibling (no path was configured)" "$r"
printf '%s\n' "$sc_watched" | grep -qx "$SC_PROD" && r=no || r=yes
harness_check "discovery EXCLUDES an other-org product repo (bots install those too)" "$r"
[ "$(printf '%s\n' "$sc_watched" | grep -cx "$SC_HOME")" = 1 ] && r=yes || r=no
harness_check "claudlobby appears exactly once (it is itself an editable install)" "$r"

# --- notify-behind: reports, and still never pulls ---
sc_advance sibling
sib_before=$(sc_head "$SC_SIB")
sc_run notify-behind.sh
sc_events | grep -q '"type":"source_behind"' && r=yes || r=no
harness_check "notify-behind emits source_behind for a stale SIBLING" "$r"
[ "$(sc_head "$SC_SIB")" = "$sib_before" ] && r=yes || r=no
harness_check "notify-behind moved nothing — the reporter is still notice-only" "$r"

# --- update-siblings: the guards, before the pull ---
echo "uncommitted" >> "$SC_DIRTY/f.txt"
sc_advance dirtysib
dirty_before=$(sc_head "$SC_DIRTY")

# A local unpushed commit is the other "somebody is mid-work" shape.
SC_AHEAD=$(sc_mkrepo "aheadsib" "testorg")
sc_advance aheadsib
git -C "$SC_AHEAD" config user.email v@example.com
git -C "$SC_AHEAD" config user.name validate
echo local > "$SC_AHEAD/local.txt"
git -C "$SC_AHEAD" add local.txt
git -C "$SC_AHEAD" commit --quiet -m "local work"
ahead_before=$(sc_head "$SC_AHEAD")

sc_reset
_SC_LOCS="$SC_SIB
$SC_PROD
$SC_DIRTY
$SC_AHEAD"
sc_run update-siblings.sh --dry-run
[ "$(sc_head "$SC_SIB")" = "$sib_before" ] && r=yes || r=no
harness_check "--dry-run moves nothing" "$r"

sc_reset
sc_run update-siblings.sh

# HEAD-did-not-move is NOT sufficient here and asserting only that is a trap:
# `git merge --ff-only` refuses a dirty tree by itself, so those assertions pass
# with repo_pull_blocker deleted. Verified by neutering it — all 17 stayed
# green. The discriminator is WHICH event fired: sibling_update_blocked naming
# the human reason (the guard refused, before touching git) versus
# sibling_update_failed (git was asked and would not).
[ "$(sc_head "$SC_DIRTY")" = "$dirty_before" ] && r=yes || r=no
harness_check "GUARD: a DIRTY working tree is not pulled (somebody is mid-work)" "$r"
sc_events | grep '"type":"sibling_update_blocked"' | grep -q 'dirty working tree' && r=yes || r=no
harness_check "  ...refused BY THE GUARD, naming the reason — not merely refused by git" "$r"
[ "$(sc_head "$SC_AHEAD")" = "$ahead_before" ] && r=yes || r=no
harness_check "GUARD: a tree with unpushed local commits is not pulled" "$r"
sc_events | grep '"type":"sibling_update_blocked"' | grep -q 'local commits not pushed' && r=yes || r=no
harness_check "  ...also refused by the guard, naming the unpushed work" "$r"
sc_events | grep -q '"type":"sibling_update_failed"' && r=no || r=yes
harness_check "  ...and no git operation was even attempted on either (no sibling_update_failed)" "$r"
grep -q "uncommitted" "$SC_DIRTY/f.txt" && r=yes || r=no
harness_check "  ...and the uncommitted edit is still there, unstashed" "$r"

# --- update-siblings: the pull it IS supposed to do ---
[ "$(sc_head "$SC_SIB")" != "$sib_before" ] && r=yes || r=no
harness_check "a CLEAN stale sibling is fast-forwarded (the #1009 fix actually applies)" "$r"
sc_events | grep -q '"type":"sibling_updated"' && r=yes || r=no
harness_check "  ...and every movement emits sibling_updated (an invisible auto-update is #1009 inverted)" "$r"
# Give the product repo something a leak WOULD have pulled — without this its
# upstream never moves, so "HEAD is unchanged" holds whether or not discovery
# leaked it, and the assertion cannot fail.
sc_advance productrepo
[ "$(git -C "$SC_PROD" rev-list --count HEAD)" = 1 ] && r=yes || r=no
harness_check "the other-org product repo was never touched (its upstream HAD moved)" "$r"

# --- The release track: the exact shape #1009 was filed from -----------------
# A TAGGED sibling sitting on its newest release while main has moved on. This
# is Claudron on this host — v0.4.0 is the newest tag, and the two fixes that
# started all of this are on main only. The fixtures above are untagged, so
# without this the release-vs-main rule would ship unexercised.
SC_TAGGED=$(sc_mkrepo "taggedsib" "testorg")
git -C "$SC_TAGGED" tag v1.0.0
git -C "$SC_TAGGED" push --quiet origin v1.0.0
sc_advance taggedsib                 # main moves ahead of the tag
tagged_before=$(sc_head "$SC_TAGGED")

sc_reset
_SC_LOCS="$SC_TAGGED"
sc_run notify-behind.sh
sc_events | grep -q '"type":"source_release_gap"' && r=yes || r=no
harness_check "RELEASE GAP: on the newest tag with main ahead reports source_release_gap, not source_behind" "$r"
sc_events | grep -q '"type":"source_behind"' && r=no || r=yes
harness_check "  ...and does NOT tell the operator to pull unreleased code" "$r"

sc_reset
sc_run update-siblings.sh
[ "$(sc_head "$SC_TAGGED")" = "$tagged_before" ] && r=yes || r=no
harness_check "  ...and update-siblings leaves it alone (a dependency tracks releases, not dev)" "$r"

# Cutting a release is what makes it move — the remedy the notice names.
git -C "$SC_TAGGED" fetch --quiet origin
git -C "$SC_TAGGED" tag v1.1.0 "$(git -C "$SC_TAGGED" rev-parse origin/main)"
sc_reset
sc_run update-siblings.sh
[ "$(sc_head "$SC_TAGGED")" != "$tagged_before" ] && r=yes || r=no
harness_check "  ...and DOES fast-forward once a newer release is cut" "$r"


echo ""
echo "=== validate #892: an audit verb with no --enroll must not write ==="
# state/fleet-state.json is ONE host-shared file, while prune builds its keep-list
# from a SINGLE fleet manifest — so every bot outside the invoking fleet WAS
# undeclared by construction and was deleted on a perfect parse. That fired at
# least seven times in one day across three managers, on ordinary session-start
# hygiene, and none of them caught it: the verb is named, documented and flagged
# as report-only, and the output read as routine housekeeping.
#
# Two separate doors were closed. The write moved behind --enroll, so the AUDIT
# verb no longer writes at all; and prune is now SCOPED, so even the enrolled
# write removes only rows this fleet declared and still owns. The checks below
# cover both, because either alone leaves the incident reachable — an audit that
# cannot write is no help once someone legitimately runs --enroll.
#
# FLEET_STATE_PATH is set explicitly below rather than inherited. This harness
# overrides CLAUDLOBBY_ROOT at every call site but never FLEET_STATE_PATH, which
# fleet-state-update.sh PREFERS — so a bot running the harness writes its fixture
# rows into REAL host state. That leak is filed separately; this section must not
# depend on it being fixed, and must not contribute to it.
FS_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-validate-fs.XXXXXX")"
mkdir -p "$FS_ROOT/local/f-alpha/runtime/bots" "$FS_ROOT/local/f-beta/runtime/bots" "$FS_ROOT/state"
printf 'fleet:\n  name: f-alpha\n  bots:\n    a1:\n      expertise: [x]\n' > "$FS_ROOT/local/f-alpha/fleet.yaml"
printf 'fleet:\n  name: f-beta\n  bots:\n    b1:\n      expertise: [x]\n' > "$FS_ROOT/local/f-beta/fleet.yaml"
FS_STATE="$FS_ROOT/state/fleet-state.json"
# a1: declared by f-alpha. a0: f-alpha's DEPARTED bot, still stamped as hers --
# the only row a f-alpha prune may remove, and the witness that the write still
# happens. b1: f-beta's live bot, the row that must survive f-alpha entirely.
fs_seed() { printf '{"updated":"x","bots":{"a1":{"status":"idle","fleet":"f-alpha"},"a0":{"status":"idle","fleet":"f-alpha"},"b1":{"status":"working","fleet":"f-beta"}},"queue":[]}' > "$FS_STATE"; }
fs_reconcile() { CLAUDLOBBY_ROOT="$FS_ROOT" FLEET_STATE_PATH="$FS_STATE" "$LIB_DIR/reconcile-fleet.sh" "$@" 2>&1 || true; }

# The incident, reproduced: f-alpha audits, and f-beta must survive it.
fs_seed
fs_before="$(cat "$FS_STATE")"
fs_out="$(fs_reconcile f-alpha)"
[ "$(cat "$FS_STATE")" = "$fs_before" ] && r=yes || r=no
harness_check "AUDIT (no --enroll) writes NOTHING to host-shared fleet-state" "$r"
[ "$(printf '%s' "$fs_out" | grep -c 'WOULD prune')" -ge 1 ] && r=yes || r=no
harness_check "  ...but still REPORTS what would go (the audit keeps its signal)" "$r"
printf '%s' "$fs_out" | grep -q 'b1 — declared by another fleet' && r=yes || r=no
harness_check "  ...naming the OTHER fleet's row as one it will NOT touch" "$r"
printf '%s' "$fs_out" | grep -q 'host-declared bots have NO row' && r=yes || r=no
harness_check "  ...and what is MISSING, not merely what this run would remove" "$r"

# --enroll still applies it: the write moved behind the flag, it did not vanish.
fs_seed
fs_reconcile f-alpha --enroll >/dev/null
[ "$(jq -r '.bots | has("a0")' "$FS_STATE")" = "false" ] && r=yes || r=no
harness_check "--enroll DOES apply the prune (the write moved, it did not vanish)" "$r"
# This check previously WITNESSED the write by watching b1 -- a SIBLING fleet's
# row -- disappear. The intent was right and the witness was the harm itself, so
# a green harness certified the incident. It now witnesses f-alpha's own departed
# row, and asserts the sibling survives, which is the property that was missing.
[ "$(jq -r '.bots | has("b1")' "$FS_STATE")" = "true" ] && r=yes || r=no
harness_check "  ...while the OTHER fleet's live row SURVIVES it (#892 scoping)" "$r"
[ "$(jq -r '.bots.b1.status' "$FS_STATE")" = "working" ] && r=yes || r=no
harness_check "  ...unmodified, not merely present" "$r"
[ "$(jq -r '.updated' "$FS_STATE")" != "x" ] && r=yes || r=no
harness_check "  ...and stamps .updated, like the delete and update arms" "$r"

# Zero extraction is the guard the wipe needed: an empty keep-set matches no key.
printf 'fleet:\n  name: f-alpha\n  bots:  # my bots\n    a1:\n      expertise: [x]\n' > "$FS_ROOT/local/f-alpha/drift.yaml"
fs_seed
fs_before="$(cat "$FS_STATE")"
CLAUDLOBBY_ROOT="$FS_ROOT" FLEET_STATE_PATH="$FS_STATE" \
    "$LIB_DIR/fleet-state-update.sh" prune "$FS_ROOT/local/f-alpha/drift.yaml" >/dev/null 2>&1 && r=no || r=yes
harness_check "zero-extraction (PyYAML-valid trailing comment) REFUSES, nonzero" "$r"
[ "$(cat "$FS_STATE")" = "$fs_before" ] && r=yes || r=no
harness_check "  ...and touched no rows (an empty keep-set would delete every one)" "$r"

rm -rf "$FS_ROOT"

# === Scenario 12: GitHub App git routing — real helper through the composed gitconfig (#1273 S9) ===
# The pytest battery stubs the helper; THIS is where the real one runs: a real
# openssl-signed JWT through the real composed routing, only curl faked. Seeds
# a throwaway export root, composes a github_app bot with the REAL compositor,
# and drives real git credential fill against the real lib/ helper.
GA_ROOT="$(mktemp -d /tmp/ga-harness.XXXXXX)"
GA_BIN="$GA_ROOT/bin"; mkdir -p "$GA_BIN" "$GA_ROOT/lib" "$GA_ROOT/home"
cp "$LIB_DIR/git-credential-github-app" "$LIB_DIR/mint-github-token.sh" "$LIB_DIR/lib-common.sh" "$GA_ROOT/lib/"
openssl genrsa -out "$GA_ROOT/app-key.pem" 2048 2>/dev/null
cat > "$GA_BIN/curl" <<'GACURL'
#!/bin/bash
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
printf '{"token":"ghs_HARNESSMINT"}' > "$out"
printf '201'
GACURL
chmod 755 "$GA_BIN/curl"
printf '[user]\n\temail = operator@example.com\n' > "$GA_ROOT/home/.gitconfig"

# Compose the gitconfig with the REAL compositor against this root.
GA_CFG="$GA_ROOT/composed.gitconfig"
# The composer needs the venv deps (jinja2); the CLI-resolver convention:
# prefer the checkout venv, fall back to python3 for editable installs.
GA_PY="$LIB_DIR/../.venv/bin/python"
[ -x "$GA_PY" ] || GA_PY=python3
HOME="$GA_ROOT/home" "$GA_PY" - "$GA_ROOT" "$GA_CFG" <<GAPY 2>"$GA_ROOT/compose.err" || sed "s/^/  compose: /" "$GA_ROOT/compose.err" >&2
import sys
sys.path.insert(0, '$LIB_DIR/..')
from pathlib import Path
from claudlobby.composer import compose_bot_gitconfig
from claudlobby.config import BotConfig, GithubAppConfig
from claudlobby.paths import Paths
root = Path(sys.argv[1])
bot = BotConfig(bot_id="ga", name="ga", expertise=["eng"],
                github_app=GithubAppConfig(slug="harness-app", bot_user_id=77))
Path(sys.argv[2]).write_text(compose_bot_gitconfig(bot, Paths(root=root, fleet_dir=root)))
GAPY

# The git-isolation contract lives ONCE (ga_env_base); the App-credentialed
# variant layers on top. A drifted second copy weakens an assertion silently.
ga_env_base() {
    local xdg="$1"; shift
    env -i PATH="$GA_BIN:/usr/bin:/bin" HOME="$GA_ROOT/home" \
        GIT_CONFIG_GLOBAL="$GA_CFG" GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
        XDG_CACHE_HOME="$GA_ROOT/$xdg" CLAUDLOBBY_ROOT="$GA_ROOT" "$@"
}
ga_env() {
    ga_env_base xdg GITHUB_APP_ID=999001 GITHUB_APP_INSTALLATION_ID=555002 \
        GITHUB_APP_PRIVATE_KEY_PATH="$GA_ROOT/app-key.pem" "$@"
}

grep -q "harness-app\[bot\]" "$GA_CFG" && r=yes || r=no
harness_check "app gitconfig composes with the App identity" "$r"

ga_fill="$(printf 'protocol=https\nhost=github.com\npath=AnyOrg/x.git\n\n' | ga_env git credential fill 2>/dev/null | grep '^password=')"
[ "$ga_fill" = "password=ghs_HARNESSMINT" ] && r=yes || r=no
harness_check "real git fill mints a real openssl-signed token via the REAL helper" "$r"

grep -q 'ghs_' "$GA_ROOT/composed.gitconfig" && r=no || r=yes
harness_check "  ...and the composed file itself carries no token" "$r"

printf 'protocol=https\nhost=github.com\npath=AnyOrg/x.git\nusername=x-access-token\npassword=ghs_HARNESSMINT\n\n' | ga_env git credential approve 2>/dev/null
rm -f "$GA_BIN/curl"
ga_fill2="$(printf 'protocol=https\nhost=github.com\npath=AnyOrg/x.git\n\n' | ga_env git credential fill 2>/dev/null | grep '^password=')"
[ "$ga_fill2" = "password=ghs_HARNESSMINT" ] && r=yes || r=no
harness_check "cache answers after approve with NO curl on the host (mint amortized)" "$r"
ga_env git credential-cache exit 2>/dev/null || true

# D9/quit=1: unset the app env + config -> the helper must stop the chain loudly.
ga_fail="$(printf 'protocol=https\nhost=github.com\npath=Other/y.git\n\n' | ga_env_base xdg2 git credential fill 2>&1)" && r=no || r=yes
harness_check "unconfigured helper stops git LOUDLY (quit=1), no silent fall-through" "$r"
printf '%s' "$ga_fail" | grep -q 'password=' && r=no || r=yes
harness_check "  ...and no credential of any identity was served" "$r"

rm -rf "$GA_ROOT"

# =============================================================================
# PR-B T9 — the observable-plane dual-write leg: a REAL daemon on a temp root,
# the REAL dispatch door through the REAL shim, and the ladder's degradation
# observed rather than claimed. Gated: no venv CLI resolvable -> the leg skips
# loudly instead of failing a host that cannot run it.
# =============================================================================
PL_REPO="$VAL_REPO"
PL_CLI="$VAL_CLI"
if false; then
    :   # the CLI is a prerequisite of the whole harness now (F18 R1) — never a skipped leg
else
    PL_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/vbcplane.XXXXXX")"
    PL_SOCKDIR="$(mktemp -d /tmp/vbcpl.XXXXXX 2>/dev/null || mktemp -d)"
    PL_SOCK="$PL_SOCKDIR/s"
    PL_LIB="$PL_ROOT/lib"
    mkdir -p "$PL_LIB"
    for _f in dispatch-task.sh lib-common.sh plane-emit.sh plane-socket-client.py dispatch-supersede-hint.py; do
        ln -s "$PL_REPO/lib/$_f" "$PL_LIB/$_f"
    done
    printf '#!/bin/bash\nexit 0\n' > "$PL_LIB/dispatch.sh"; chmod +x "$PL_LIB/dispatch.sh"
    printf '#!/bin/bash\nexit 0\n' > "$PL_ROOT/tmux"; chmod +x "$PL_ROOT/tmux"

    "$PL_CLI" --root "$PL_ROOT" plane serve --socket "$PL_SOCK" \
        > "$PL_ROOT/daemon.log" 2>&1 &
    PL_DPID=$!
    _pl_i=0
    while [ "$_pl_i" -lt 100 ] && [ ! -S "$PL_SOCK" ]; do sleep 0.1; _pl_i=$((_pl_i + 1)); done
    [ -S "$PL_SOCK" ] && r=yes || r=no
    harness_check "plane daemon binds its socket on a temp root" "$r"

    _pl_dispatch() {  # $1 = extra env assignments, $2 = task text; stderr -> $PL_ROOT/err
        env CLAUDLOBBY_ROOT="$PL_ROOT" TMUX_BIN="$PL_ROOT/tmux" BOT_ID=vbc \
            BOT_NAME=vbc FLEET_NAME=vbc-fleet PLANE_SOCKET="$PL_SOCK" \
            PLANE_EMIT_CLI="$PL_CLI" OBSERVABILITY_DISPATCH_DEADLINE=600 \
            PATH="/usr/bin:/bin" $1 \
            bash "$PL_LIB/dispatch-task.sh" --botcommand w1 "$2" 2> "$PL_ROOT/err"
    }
    _pl_count() {
        sqlite3 "$PL_ROOT/state/plane/plane.db" \
            "SELECT COUNT(*) FROM communications" 2>/dev/null || echo 0
    }

    _pl_dispatch "" "leg one: rung 1" >/dev/null && r=yes || r=no
    harness_check "a dispatch with NO plane flag in its environment succeeds with the daemon up (always-on, F18 R1)" "$r"
    [ "$(_pl_count)" = "1" ] && r=yes || r=no
    harness_check "the communication row LANDED (real db, real shim)" "$r"
    grep -q "falling back" "$PL_ROOT/err" && r=no || r=yes
    harness_check "  ...via rung 1 (no fallback disclosure on stderr)" "$r"

    kill "$PL_DPID" 2>/dev/null || true; wait "$PL_DPID" 2>/dev/null || true
    _pl_dispatch "PLANE_EMIT_ENABLED=0" "leg two: daemon down" >/dev/null && r=yes || r=no
    harness_check "dispatch still succeeds with the daemon DEAD (and PLANE_EMIT_ENABLED=0 is ignored)" "$r"
    [ "$(_pl_count)" = "2" ] && r=yes || r=no
    harness_check "the row still landed (cold-CLI rung)" "$r"
    grep -q "falling back" "$PL_ROOT/err" && r=yes || r=no
    harness_check "  ...and the fallback was DISCLOSED, not silent" "$r"

    _pl_dispatch "PLANE_EMIT_DISABLED=1" "leg three: disabled" >/dev/null && r=yes || r=no
    harness_check "PLANE_EMIT_DISABLED dispatch succeeds" "$r"
    [ "$(_pl_count)" = "2" ] && r=yes || r=no
    harness_check "  ...and wrote NOTHING (harness exemption is a true no-op)" "$r"
    ls "$PL_ROOT/state"/dispatch-log*.jsonl >/dev/null 2>&1 && r=no || r=yes
    harness_check "  ...and no dispatch ledger exists under the root after three dispatches (no legacy write, F18 R1)" "$r"

    # -- keepalive presence door (chunk: keepalive-as-a-door) ---------------
    # An ARMED keepalive tick against a stubbed-idle pane must record the
    # verdict as metric_samples (bot.heartbeat + bot.session_up) through the
    # real shim into the real db — the Observe step for presence recording.
    ln -s "$PL_REPO/lib/keepalive.sh" "$PL_LIB/keepalive.sh"
    printf '#!/bin/bash\nexit 0\n' > "$PL_LIB/start-bot.sh"
    chmod +x "$PL_LIB/start-bot.sh"
    KAB="$PL_ROOT/bots/kbot"
    mkdir -p "$KAB/data"
    # BOT_SERVICE must be REAL: with FLEET_NAME set, tmux_socket_for_bot
    # refuses an empty service (the shared-server SPOF guard) and the tick
    # dies before any verdict — the stub tmux ignores socket args anyway
    printf 'BOT_NAME="kbot"\nFLEET_NAME="vbc-fleet"\nBOT_SERVICE="com.vbc.kbot"\n' \
        > "$KAB/bot.conf"
    printf '#!/bin/bash\ncase "$*" in *capture-pane*) printf ">\\n" ;; *) exit 0 ;; esac\n' \
        > "$PL_ROOT/ktmux"
    chmod +x "$PL_ROOT/ktmux"
    env CLAUDLOBBY_ROOT="$PL_ROOT" TMUX_BIN="$PL_ROOT/ktmux" \
        PLANE_SOCKET="$PL_SOCK" PLANE_EMIT_CLI="$PL_CLI" \
        PATH="/usr/bin:/bin" \
        bash "$PL_LIB/keepalive.sh" "$KAB" >/dev/null 2>&1 || true
    # the emit is BACKGROUNDED (a wedged rung must never stall the
    # watchdog sweep) — poll briefly for the row instead of racing it
    _ka_hb=0; _ka_i=0
    while [ "$_ka_i" -lt 100 ] && [ "$_ka_hb" -lt 1 ]; do
        _ka_hb=$(sqlite3 "$PL_ROOT/state/plane/plane.db" \
            "SELECT COUNT(*) FROM metric_samples WHERE metric='bot.heartbeat'" \
            2>/dev/null || echo 0)
        sleep 0.2; _ka_i=$((_ka_i + 1))
    done
    [ "$_ka_hb" -ge 1 ] && r=yes || r=no
    harness_check "keepalive tick records the heartbeat sample (no flag needed: always-on)" "$r"
    ls "$KAB/data/events"/*.jsonl >/dev/null 2>&1 && r=no || r=yes
    harness_check "  ...and writes no keepalive-<day>.jsonl (the reader-less file is gone, F18 R1)" "$r"

    "$PL_CLI" --root "$PL_ROOT" plane doctor > "$PL_ROOT/doctor.txt" 2>&1 && r=no || r=yes
    harness_check "doctor flags ATTENTION: daemon started historically, not serving" "$r"
    grep -q "not serving" "$PL_ROOT/doctor.txt" && r=yes || r=no
    harness_check "  ...naming the condition and the corrective command" "$r"

    rm -rf "$PL_ROOT" "$PL_SOCKDIR"
fi

echo ""
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
