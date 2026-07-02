#!/bin/bash
# rehearse-keepalive-swap.sh — Phase 6 gate 1: rehearse the atomic keepalive
# prefix swap on a THROWAWAY fleet with real systemd user timers at production
# cadence (60s), and assert the no-gap contract from systemd's own journal.
# Empirical pre-deploy harness in the validate-bot-change.sh mold.
#
# Run this BEFORE migrating a real fleet (the migration's first step):
#   CLAUDLOBBY_ROOT=<checkout> bash lib/rehearse-keepalive-swap.sh
#
# What it does (~4 min wall clock):
#   1. COMPOSES the new com.p6.rehearsal.keepalive units via the real
#      compose_fleet_timers (shipped system.yaml keepalive job, filtered to
#      that one job) — production shape by construction, so composed-unit vs
#      script contract drift cannot slip past the rehearsal;
#   2. installs a legacy-shape claudlobby-p6-rehearsal-keepalive unit
#      (argless ExecStart + CLAUDLOBBY_FLEET env — the prior release's shape,
#      hand-written because nothing in-tree generates it anymore) and lets it
#      tick twice;
#   3. runs `setup-fleet p6-rehearsal`, whose step 1b performs the staged swap
#      (enable-new → verify-active → verification run → disable-old);
#   4. lets the new unit tick twice more, then extracts every service
#      activation from the journal and asserts the max consecutive gap
#      <= 75s (60s interval + 10s AccuracySec + slack).
#
# Exit 0 iff the swap succeeded AND the no-gap assertion passed. Cleans up all
# throwaway units and the throwaway fleet dir on any exit. Linux/systemd only.
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

# --- throwaway fleet: fleet.yaml + COMPOSED new units -------------------------
mkdir -p "$CLAUDLOBBY_ROOT/local/$FLEET/runtime/bots"
printf 'fleet:\n  name: %s\n  service_prefix: %s\n  bots:\n' "$FLEET" "$PREFIX" \
    >"$CLAUDLOBBY_ROOT/local/$FLEET/fleet.yaml"

python3 - "$CLAUDLOBBY_ROOT" "$FLEET" "$PREFIX" <<'PY'
import sys
from pathlib import Path

root, fleet, prefix = sys.argv[1:4]
sys.path.insert(0, root)
from claudlobby.composer import compose_fleet_timers
from claudlobby.config import FleetConfig, _load_system_defaults
from claudlobby.paths import Paths

section = _load_system_defaults().get("defaults", {})
merged = {
    # Just the keepalive job: the rehearsal must not enroll the other default
    # timers on the host.
    "jobs": {"keepalive": section["jobs"]["keepalive"]},
    "observability": section.get("observability", {}),
}
paths = Paths(root=Path(root), fleet_dir=Path(root) / "local" / fleet)
tdir = compose_fleet_timers(FleetConfig(name=fleet, service_prefix=prefix), paths, merged)
print(f"composed: {sorted(p.name for p in tdir.iterdir())}")
PY

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
log "legacy $LEGACY enabled (production shape) — letting it tick twice"
sleep 90

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
        exit !(NR >= 5 && max <= 75)
    }' || { echo "NO-GAP ASSERTION: FAIL" >&2; exit 1; }
echo "NO-GAP ASSERTION: PASS (max gap <= 75s = 60s interval + 10s AccuracySec + slack)"
