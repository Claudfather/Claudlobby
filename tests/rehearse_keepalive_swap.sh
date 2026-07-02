#!/bin/bash
# rehearse_keepalive_swap.sh — Phase 6 gate 1: rehearse the atomic keepalive
# prefix swap on a THROWAWAY fleet with real systemd user timers at production
# cadence (60s), and assert the no-gap contract from systemd's own journal.
#
# Run this BEFORE migrating a real fleet (the deploy runbook's first step):
#   CLAUDLOBBY_ROOT=<checkout> bash tests/rehearse_keepalive_swap.sh
#
# What it does (~5 min wall clock):
#   1. installs a legacy-shape claudlobby-p6-rehearsal-keepalive unit
#      (argless ExecStart + CLAUDLOBBY_FLEET env — the prior release's shape)
#      and lets it tick twice;
#   2. runs `setup-fleet p6-rehearsal`, whose step 1b performs the staged swap
#      (enable-new → verify-active → verification run → disable-old);
#   3. lets the new com.p6.rehearsal.keepalive unit tick twice;
#   4. extracts every service activation from the journal and asserts the max
#      consecutive gap <= 75s (60s interval + 10s AccuracySec + slack).
#
# Exit 0 iff the swap succeeded AND the no-gap assertion passed. Cleans up all
# throwaway units and the throwaway fleet dir on any exit. Linux/systemd only.
# Manual/local harness — not collected by pytest (CI covers the swap logic via
# tests/test_setup_backbone.py; this proves the live timing end-to-end).
set -euo pipefail

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:?set CLAUDLOBBY_ROOT to the checkout under test}"
FLEET=p6-rehearsal
PREFIX=com.p6.rehearsal
NEW="$PREFIX.keepalive"
LEGACY="claudlobby-$FLEET-keepalive"
UD="$HOME/.config/systemd/user"
WORK="$(mktemp -d)"

[ "$(uname -s)" = "Linux" ] || { echo "Linux/systemd only" >&2; exit 2; }

cleanup() {
    systemctl --user disable --now "$NEW.timer" >/dev/null 2>&1 || true
    systemctl --user disable --now "$LEGACY.timer" >/dev/null 2>&1 || true
    rm -f "$UD/$NEW.timer" "$UD/$NEW.service" "$UD/$LEGACY.timer" "$UD/$LEGACY.service"
    systemctl --user daemon-reload || true
    systemctl --user reset-failed >/dev/null 2>&1 || true
    rm -rf "$CLAUDLOBBY_ROOT/local/$FLEET" "$WORK"
}
trap cleanup EXIT

log() { echo "[$(date +%H:%M:%S)] $*"; }

# --- throwaway fleet: fleet.yaml + composed-shape new units -------------------
mkdir -p "$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots" \
         "$CLAUDLOBBY_ROOT/local/$FLEET/runtime/fleet/timers"
printf 'fleet:\n  name: %s\n  service_prefix: %s\n  bots:\n' "$FLEET" "$PREFIX" \
    >"$CLAUDLOBBY_ROOT/local/$FLEET/fleet.yaml"

TDIR="$CLAUDLOBBY_ROOT/local/$FLEET/runtime/fleet/timers"
cat >"$TDIR/$NEW.service" <<UNIT
[Unit]
Description=claudlobby keepalive ($FLEET)

[Service]
Type=oneshot
Environment=CLAUDLOBBY_ROOT=$CLAUDLOBBY_ROOT
Environment=CLAUDLOBBY_FLEET=$FLEET
ExecStart=$CLAUDLOBBY_ROOT/lib/keepalive-all.sh $FLEET
UNIT
cat >"$TDIR/$NEW.timer" <<UNIT
[Unit]
Description=claudlobby keepalive timer ($FLEET) -- tick every 60s

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=10

[Install]
WantedBy=timers.target
UNIT

# --- legacy unit exactly as the prior release shipped it ----------------------
cat >"$UD/$LEGACY.service" <<UNIT
[Unit]
Description=claudlobby fleet keepalive ($FLEET)

[Service]
Type=oneshot
Environment=CLAUDLOBBY_ROOT=$CLAUDLOBBY_ROOT
Environment=CLAUDLOBBY_FLEET=$FLEET
ExecStart=$CLAUDLOBBY_ROOT/lib/keepalive-all.sh
UNIT
cat >"$UD/$LEGACY.timer" <<UNIT
[Unit]
Description=claudlobby fleet keepalive timer ($FLEET) — tick every 60s

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=10

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
T0=$(date +%s)
systemctl --user enable --now "$LEGACY.timer"
log "legacy $LEGACY enabled (production shape) — letting it tick"
sleep 135

log "running the swap: setup-fleet $FLEET"
SWAP_RC=0
env -i HOME="$HOME" PATH="$PATH" USER="${USER:-$(id -un)}" \
    XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
    DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
    CLAUDLOBBY_ROOT="$CLAUDLOBBY_ROOT" \
    "$CLAUDLOBBY_ROOT/lib/setup-fleet" "$FLEET" >"$WORK/setup-fleet.out" 2>&1 || SWAP_RC=$?
log "swap finished rc=$SWAP_RC at T0+$(( $(date +%s) - T0 ))s"
grep -E 'legacy keepalive|swap' "$WORK/setup-fleet.out" || true
if [ "$SWAP_RC" -ne 0 ]; then
    echo "REHEARSAL: FAIL (setup-fleet rc=$SWAP_RC)" >&2
    cat "$WORK/setup-fleet.out" >&2
    exit 1
fi
sleep 135

# --- evidence: every service activation across the window ---------------------
journalctl --user -u "$LEGACY.service" -u "$NEW.service" --since "@$T0" --no-pager -o short-unix \
    | grep -E "Starting ($LEGACY|$PREFIX\.keepalive)\.service" >"$WORK/starts.raw" || true
awk -v legacy="$LEGACY" \
    '{u=($0 ~ legacy) ? "legacy" : "new   "; printf "%s %.1f\n", u, $1}' "$WORK/starts.raw"
awk '{print $1}' "$WORK/starts.raw" | sort -n | awk '
    NR>1 { d=$1-prev; if (d>max) { max=d; at=prev } }
    { prev=$1 }
    END {
        printf "activations=%d max_consecutive_gap=%.1fs (after epoch %.1f)\n", NR, max, at
        exit !(NR >= 6 && max <= 75)
    }' || { echo "NO-GAP ASSERTION: FAIL" >&2; exit 1; }
echo "NO-GAP ASSERTION: PASS (max gap <= 75s = 60s interval + 10s AccuracySec + slack)"
