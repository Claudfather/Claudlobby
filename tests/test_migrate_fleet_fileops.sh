#!/bin/bash
# File-op validation for lib/migrate-fleet-to-system.sh (Claudlobby #602 P3).
# Proves the RISKY operations on plain throwaway dirs -- no bots, no generate,
# no launchd: atomic whole-dir move carries the gitignored runtime/ (no husk),
# the marker lands, resolve_fleet_dir tracks the move, and rollback is exact.
# The full real-bot migration is validated LIVE in P4 (staged, per-bot).
set -uo pipefail
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"

pass=0; fail=0
ok()   { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  FAIL  %s\n' "$1"; fail=$((fail+1)); }
chk()  { if eval "$2"; then ok "$1"; else no "$1  [$2]"; fi; }

CLAUDLOBBY_ROOT="$(mktemp -d)"; export CLAUDLOBBY_ROOT
trap 'rm -rf "$CLAUDLOBBY_ROOT"' EXIT
L="$CLAUDLOBBY_ROOT/local"

# --- a flat fleet with tracked config + a GITIGNORED runtime/ (as in prod) ---
mkdir -p "$L/web/runtime/bots/b1/data" "$L/web/shared/knowledge"
printf 'bots: {}\n'        > "$L/web/fleet.yaml"
printf 'BOT_SERVICE=web-b1\n' > "$L/web/runtime/bots/b1/bot.conf"
printf 'state\n'          > "$L/web/runtime/bots/b1/data/live.state"
printf '# note\n'        > "$L/web/shared/knowledge/note.md"
( cd "$L" && git init -q && printf '*/runtime/\n' > .gitignore \
   && git add -A && git -c user.email=t@t -c user.name=t commit -qm init )

# runtime/ must be untracked (gitignored) -- the husk git mv would strand
git -C "$L" status --porcelain web/runtime >/dev/null 2>&1
chk "precondition: runtime/ is gitignored (untracked)" \
    '[ -z "$(git -C "$L" ls-files web/runtime)" ]'

# shellcheck source=/dev/null
. "$LIB/migrate-fleet-to-system.sh"

echo "=== #602 P3: atomic move flat -> nested (carries gitignored runtime) ==="
mfs_move_dir web sys1 >/dev/null
chk "nested fleet.yaml present"                  '[ -f "$L/sys1/web/fleet.yaml" ]'
chk "gitignored runtime/ state MOVED (no husk)"  '[ -f "$L/sys1/web/runtime/bots/b1/data/live.state" ]'
chk "shared/ knowledge moved"                    '[ -f "$L/sys1/web/shared/knowledge/note.md" ]'
chk "old flat path is GONE (nothing stranded)"   '[ ! -e "$L/web" ]'
chk "system marker created"                      '[ -f "$L/sys1/.claudron-system" ]'
chk "resolve_fleet_dir now finds nested"         '[ "$(resolve_fleet_dir web)" = "$L/sys1/web" ]'

echo "=== #602 P3: rollback is the exact inverse ==="
mfs_rollback_dir web sys1 >/dev/null
chk "flat fleet.yaml restored"                   '[ -f "$L/web/fleet.yaml" ]'
chk "flat runtime/ state restored"              '[ -f "$L/web/runtime/bots/b1/data/live.state" ]'
chk "nested path gone after rollback"            '[ ! -e "$L/sys1/web" ]'
chk "empty system container removed"             '[ ! -e "$L/sys1" ]'
chk "resolve_fleet_dir finds flat again"         '[ "$(resolve_fleet_dir web)" = "$L/web" ]'

echo "=== #602 P3: preflight refuses an already-nested fleet ==="
mfs_move_dir web sys1 >/dev/null
chk "preflight rejects a non-flat fleet"         '! mfs_preflight web sys2 2>/dev/null'
mfs_rollback_dir web sys1 >/dev/null

printf '\n=== %d passed, %d failed ===\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
