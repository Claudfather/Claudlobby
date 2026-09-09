#!/bin/bash
# rehearse-briefing-timer.sh — #627 P6 gate 2: rehearse the equippable briefing
# timer chain on a THROWAWAY fleet and assert the composed
# <prefix>.briefing-<bot>-<slot> unit FIRES (journal + a real trigger event) and
# that removing the slot PRUNES its units on BOTH platforms (systemd
# .service/.timer AND the launchd .plist), then DISABLES the live orphan via the
# enroll-side reconcile. Sibling of rehearse-keepalive-swap.sh; the empirical
# pre-deploy harness in the validate-bot-change.sh mold for the composed-timer
# plumbing (validate-bot-change.sh covers the delivery behavior).
#
# Run BEFORE trusting the briefing feature on a real fleet:
#   CLAUDLOBBY_ROOT=<checkout> bash lib/rehearse-briefing-timer.sh
#
# What it does (~2 min wall clock where user systemd is present):
#   1. COMPOSES a briefing-equipped throwaway bot's timers via the real
#      compose_fleet_timers — production shape by construction — and asserts the
#      composed unit set (.service + .timer + .plist) exists. [no systemd]
#   2. enrolls the composed .service under a real 60s .timer, waits for one
#      journal-verified activation, and confirms the fire ran the trigger
#      end-to-end (briefing-trigger.sh emits a briefing event). [systemd]
#   3. removes the slot, RECOMPOSES, and asserts the generate-side reconcile
#      pruned the unit files on BOTH platforms — the launchd .plist half runs
#      here regardless of host OS. [no systemd]
#   4. runs the enroll-side reconcile (setup-fleet on the now-botless fleet, the
#      rehearse-keepalive-swap.sh recipe — no bot spin-up) so the live orphan
#      timer is disabled, and asserts it is gone. [systemd]
#
# Steps 1 + 3 (compose + both-platform prune) need no systemd and always run;
# steps 2 + 4 (live fire + enroll-side disable) gate on a `systemctl --user`
# probe and SKIP with a notice where it is unavailable (CI container, no user
# session). Exit 0 iff every attempted assertion passed. Cleans up all throwaway
# units + the throwaway fleet dir on any exit. Linux for the live-fire half; the
# .plist prune is the cross-platform launchd assertion.
set -euo pipefail

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:?set CLAUDLOBBY_ROOT to the checkout under test}"
FLEET=briefing-rehearsal
PREFIX=com.briefing.rehearsal
BOT=rbot
SLOT=morning
UNIT="$PREFIX.briefing-$BOT-$SLOT"
UD="$HOME/.config/systemd/user"
FLEET_DIR="$CLAUDLOBBY_ROOT/local/$FLEET"
BOT_DIR="$FLEET_DIR/runtime/bots/$BOT"
TIMERS_DIR="$FLEET_DIR/runtime/fleet/timers"
WORK="$(mktemp -d)"

# briefing_event_landed <root> <fleet> <bot> — the trigger rides emit_fleet_event, so
# its briefing_dispatched / briefing_deferred lands on the PLANE under the
# checkout's root (F18 closure R2b-2: no per-bot event file). True iff the plane
# holds a briefing-sourced event for the bot; an unreachable plane is a FAILED
# check, never a skipped one.
briefing_event_landed() {
    local _err
    _err="$(mktemp "${TMPDIR:-/tmp}/rbt-events.XXXXXX")" || return 1
    if python3 -S -E "$1/lib/plane-lookup.py" --root "$1" --events --fleet "$2" --bot "$3" \
            --type briefing_dispatched 2>"$_err" | grep -qE '"source": ?"briefing"'; then
        rm -f "$_err"; return 0
    fi
    [ -s "$_err" ] && log "briefing_event_landed: $(tail -1 "$_err" | cut -c1-160)"
    rm -f "$_err"; return 1
}

pass=0
fail=0
check() {
    if [ "$2" = yes ]; then
        pass=$((pass + 1))
        printf "  PASS  %s\n" "$1"
    else
        fail=$((fail + 1))
        printf "  FAIL  %s\n" "$1"
    fi
}
log() { echo "[$(date +%H:%M:%S)] $*"; }

cleanup() {
    systemctl --user disable --now "$UNIT.timer" >/dev/null 2>&1 || true
    rm -f "$UD/$UNIT.timer" "$UD/$UNIT.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user reset-failed >/dev/null 2>&1 || true
    rm -rf "$FLEET_DIR" "$WORK"
}
trap cleanup EXIT

# fleet.yaml WITH ($1 = yes) the equipped slot, or a BOTLESS fleet ($1 = no) so
# the recompose prunes and setup-fleet spins up nothing (rehearse-keepalive
# recipe). A non-shell-ident free var name is avoided — BOT/SLOT are idents.
write_fleet_yaml() {
    cat >"$FLEET_DIR/fleet.yaml" <<YML
fleet:
  name: $FLEET
  service_prefix: $PREFIX
  bots:
YML
    [ "$1" = yes ] && cat >>"$FLEET_DIR/fleet.yaml" <<YML
    $BOT:
      expertise: [software-engineering]
      briefing:
        slots:
          $SLOT: "*-*-* 08:30:00"
YML
    return 0
}

# Compose runtime/fleet/timers via the REAL composer. merged_defaults={} so ONLY
# the briefing units emit — no host default jobs land on this machine.
compose_timers() {
    python3 - "$CLAUDLOBBY_ROOT" "$FLEET_DIR" <<'PY'
import sys
from pathlib import Path

checkout, fleet_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, checkout)
from claudlobby.config import load_fleet
from claudlobby.composer import compose_fleet_timers
from claudlobby.paths import Paths

fleet, _ = load_fleet(Path(fleet_dir) / "fleet.yaml")
paths = Paths(root=Path(checkout), fleet_dir=Path(fleet_dir))
tdir = compose_fleet_timers(fleet, paths, {})
print("composed:", sorted(p.name for p in tdir.iterdir()))
PY
}

HAVE_SYSTEMD=no
if [ "$(uname -s)" = "Linux" ] && systemctl --user show-environment >/dev/null 2>&1; then
    HAVE_SYSTEMD=yes
fi

echo "=== rehearse-briefing-timer: composed timer chain (#627 P6 gate 2) ==="

# --- 1. compose the equipped (bot,slot) unit + assert the full unit set -------
log "composing briefing timers for $FLEET/$BOT/$SLOT"
write_fleet_yaml yes
compose_timers
{ [ -f "$TIMERS_DIR/$UNIT.service" ] && [ -f "$TIMERS_DIR/$UNIT.timer" ] &&
    [ -f "$TIMERS_DIR/$UNIT.plist" ]; } && r=yes || r=no
check "compose emits the (bot,slot) unit set: .service + .timer + .plist" "$r"

# --- 2. enroll under a real 60s timer + observe one journal-verified fire ------
if [ "$HAVE_SYSTEMD" = yes ]; then
    # The composed .service is self-contained (Environment=CLAUDLOBBY_ROOT,
    # ExecStart = briefing-trigger.sh <fleet> <bot> <slot>). Keep it verbatim and
    # swap only the OnCalendar .timer for a 60s cadence so it fires inside the
    # window. With no live bot session the trigger defers session_absent (a clean
    # exit 0) — but the journal records the timer -> service activation AND the
    # trigger emits a briefing event, the two independent fire proofs.
    mkdir -p "$UD"
    install -m 644 "$TIMERS_DIR/$UNIT.service" "$UD/$UNIT.service"
    cat >"$UD/$UNIT.timer" <<UNIT_TIMER
[Unit]
Description=briefing rehearsal timer ($UNIT) tick every 60s

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=10

[Install]
WantedBy=timers.target
UNIT_TIMER
    systemctl --user daemon-reload
    T0=$(date +%s)
    systemctl --user enable --now "$UNIT.timer"
    log "enrolled $UNIT.timer (60s cadence) — polling up to 90s for one fire"
    # Poll-and-break: journalctl is already scoped to this unit + T0, so any
    # activation line proves the fire. Break on the first, capping at ~90s.
    r=no
    for _i in $(seq 1 9); do
        if journalctl --user -u "$UNIT.service" --since "@$T0" --no-pager -o short-unix 2>/dev/null |
            grep -qE 'Starting|Deactivated|Succeeded'; then
            r=yes
            break
        fi
        sleep 10
    done
    check "composed briefing timer FIRES: journal records $UNIT.service activation" "$r"
    briefing_event_landed "$CLAUDLOBBY_ROOT" "$FLEET" "$BOT" && r=yes || r=no
    check "timer fire runs the trigger end-to-end: briefing-trigger.sh emitted a briefing event" "$r"
else
    log "SKIP live-fire assertions — no user systemd (systemctl --user unavailable on this host)"
fi

# --- 3. remove the slot + recompose -> generate-side prune on BOTH platforms ---
log "removing the slot (botless fleet) + recomposing — generate-side reconcile"
write_fleet_yaml no
compose_timers
{ [ ! -f "$TIMERS_DIR/$UNIT.service" ] && [ ! -f "$TIMERS_DIR/$UNIT.timer" ] &&
    [ ! -f "$TIMERS_DIR/$UNIT.plist" ]; } && r=yes || r=no
check "generate reconcile prunes the removed slot on BOTH platforms (.service + .timer + launchd .plist)" "$r"

# --- 4. enroll-side reconcile disables the now-orphaned LIVE systemd timer -----
if [ "$HAVE_SYSTEMD" = yes ]; then
    # The composed dir no longer has the unit, but the timer is still enrolled +
    # live from step 2 — the exact stale orphan reconcile_briefing_timers exists
    # to disable. Drive it via setup-fleet on the botless fleet (no bot spin-up),
    # the same recipe rehearse-keepalive-swap.sh trusts. Bot dir removed first so
    # the audit has nothing on-disk to reconcile.
    rm -rf "$BOT_DIR"
    log "running setup-fleet $FLEET (enroll-side reconcile — disable the live orphan)"
    env -i HOME="$HOME" PATH="$PATH" USER="${USER:-$(id -un)}" \
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
        DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
        CLAUDLOBBY_ROOT="$CLAUDLOBBY_ROOT" \
        "$CLAUDLOBBY_ROOT/lib/setup-fleet" "$FLEET" >"$WORK/setup-fleet.out" 2>&1 || true
    grep -iE 'briefing (orphan|reconcile)' "$WORK/setup-fleet.out" || true
    systemctl --user list-unit-files --no-legend "$UNIT.timer" 2>/dev/null |
        grep -q "$UNIT" && r=no || r=yes
    check "enroll-side reconcile disables the orphaned live briefing timer ($UNIT)" "$r"
else
    log "SKIP enroll-side reconcile assertion — no user systemd"
fi

echo ""
echo "=== rehearse-briefing-timer: $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
