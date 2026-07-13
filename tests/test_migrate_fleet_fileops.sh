#!/bin/bash
# test_migrate_fleet_fileops.sh — file-op core of migrate-fleet-to-system.sh on
# plain throwaway dirs (no bots, no services): move + no-husk + rollback + the
# resolve_fleet_dir flat<->nested flip. Standalone; runs under macOS /bin/bash
# (bash 3.2). Not collected by pytest (only test_*.py is). The full real-bot
# migration is validated LIVE in P4 (staged, per-bot).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok: $*"; }

# Throwaway vault root.
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
export CLAUDLOBBY_ROOT="$ROOT"

# --- fixture: a FLAT fleet with tracked config + a gitignored runtime husk ----
mkdir -p "$ROOT/local/web/runtime/bots/b1/data"
mkdir -p "$ROOT/local/web/shared/knowledge"
cat > "$ROOT/local/web/fleet.yaml" <<'YAML'
fleet:
  name: web
bots:
  b1:
    expertise: [software-engineering]
YAML
echo "durable-note" > "$ROOT/local/web/shared/knowledge/note.md"
# gitignored-style runtime state + a marker file to prove it moves too.
echo "RUNTIME-STATE-MARKER" > "$ROOT/local/web/runtime/bots/b1/data/state.txt"

# Vault git repo: commit the tracked bits; gitignore runtime/ so it is UNTRACKED
# (the exact husk a `git mv` would strand).
(
  cd "$ROOT/local"
  git init -q
  git config user.email t@e.st
  git config user.name test
  printf '%s\n' 'runtime/' > .gitignore
  git add -A
  git commit -qm 'seed flat web fleet (runtime/ untracked)'
)

# Prove the runtime state really is untracked before we start.
if ( cd "$ROOT/local" && git ls-files --error-unmatch web/runtime/bots/b1/data/state.txt >/dev/null 2>&1 ); then
    fail "runtime state.txt is TRACKED — fixture wrong (should be gitignored/untracked)"
fi
pass "fixture: runtime/ is untracked (a real husk that git mv would strand)"

# --- source the tool (defines mfs_* WITHOUT running main) ---------------------
# lib-common installs its own EXIT trap on source; re-assert ours afterwards so
# the throwaway root is always cleaned (and still clean up lib-common's tmp).
. "$REPO_ROOT/lib/migrate-fleet-to-system.sh"
trap 'rm -rf "$ROOT"; _lc_cleanup 2>/dev/null || true' EXIT

# --- preflight PASSES on the flat fleet ---------------------------------------
mfs_preflight web sys1 || fail "preflight rejected a valid flat fleet"
pass "preflight: flat fleet accepted"

# --- THE MOVE -----------------------------------------------------------------
mfs_move_dir web sys1 >/dev/null || fail "mfs_move_dir returned nonzero"

# (a) nested dir exists with fleet.yaml AND the gitignored runtime husk.
[ -f "$ROOT/local/sys1/web/fleet.yaml" ] || fail "(a) sys1/web/fleet.yaml missing"
[ -d "$ROOT/local/sys1/web/runtime/bots/b1/data" ] || fail "(a) runtime/bots/b1/data did not move"
[ "$(cat "$ROOT/local/sys1/web/runtime/bots/b1/data/state.txt")" = "RUNTIME-STATE-MARKER" ] \
    || fail "(a) runtime state content lost in the move"
[ -f "$ROOT/local/sys1/web/shared/knowledge/note.md" ] || fail "(a) shared/knowledge/note.md did not move"
pass "(a) nested sys1/web has fleet.yaml + gitignored runtime moved too (no husk)"

# (b) the flat dir is entirely GONE — nothing stranded.
[ ! -e "$ROOT/local/web" ] || fail "(b) local/web still exists — content stranded"
pass "(b) local/web is gone — nothing stranded"

# (c) the interim system marker exists.
[ -f "$ROOT/local/sys1/.claudron-system" ] || fail "(c) .claudron-system marker missing"
pass "(c) local/sys1/.claudron-system marker present"

# (d) resolve_fleet_dir now returns the nested path.
got="$(resolve_fleet_dir web)"
[ "$got" = "$ROOT/local/sys1/web" ] || fail "(d) resolve_fleet_dir web = '$got', want '$ROOT/local/sys1/web'"
pass "(d) resolve_fleet_dir web -> local/sys1/web"

# preflight now REFUSES (fleet no longer flat).
if mfs_preflight web sys1 2>/dev/null; then fail "preflight accepted an already-nested fleet"; fi
pass "preflight: already-nested fleet refused"

# --- ROLLBACK -----------------------------------------------------------------
mfs_rollback_dir web sys1 >/dev/null || fail "mfs_rollback_dir returned nonzero"

# (e) flat restored fully (incl runtime); nested gone; empty container cleaned.
[ -f "$ROOT/local/web/fleet.yaml" ] || fail "(e) rollback did not restore fleet.yaml"
[ "$(cat "$ROOT/local/web/runtime/bots/b1/data/state.txt")" = "RUNTIME-STATE-MARKER" ] \
    || fail "(e) rollback lost the runtime state"
[ -f "$ROOT/local/web/shared/knowledge/note.md" ] || fail "(e) rollback lost shared/knowledge"
[ ! -e "$ROOT/local/sys1/web" ] || fail "(e) nested sys1/web still present after rollback"
[ ! -e "$ROOT/local/sys1" ] || fail "(e) empty system container not cleaned up"
got="$(resolve_fleet_dir web)"
[ "$got" = "$ROOT/local/web" ] || fail "(e) resolve_fleet_dir web = '$got', want '$ROOT/local/web'"
pass "(e) rollback restored flat local/web fully (incl runtime); resolve_fleet_dir web -> local/web"

# --- mfs_verify health gate: _mfs_not_healthy (M1 regression) -----------------
# The pure helper the migration verify polls on — fed canned reconcile reports,
# no real bots. Proves the false-GREEN fix: reconcile-fleet.sh has NO bucket for
# a bot that is both down AND unsupervised, so orphan/missing read (none) while
# it is dead; only a POSITIVE healthy-membership check catches it.
_report() {  # <healthy> <orphan> <missing>
    printf 'Fleet: web\n  healthy:  %s\n  orphan:   %s\n  missing:  %s\n  unbound: (none)\n' "$1" "$2" "$3"
}

[ -z "$(_mfs_not_healthy "b1 b2" "$(_report "b1 b2" "(none)" "(none)")")" ] \
    || fail "(v1) all declared bots healthy should report none outstanding"
pass "(v1) all declared bots in healthy -> none outstanding"

got="$(_mfs_not_healthy "b1" "$(_report "(none)" "(none)" "(none)")")"
[ "$got" = "b1" ] \
    || fail "(v2) false-GREEN: a down+unsupervised bot (NO bucket, orphan/missing both none) must be flagged, got '$got'"
pass "(v2) down+unsupervised bot (orphan/missing both none) -> caught not-healthy"

got="$(_mfs_not_healthy "b1 b2 b3" "$(_report "b1 b2" "(none)" "(none)")")"
[ "$got" = "b3" ] \
    || fail "(v3) a not-yet-healthy tail bot (serial boot) must be reported, got '$got'"
pass "(v3) tail bot not yet healthy -> reported (drives the settle-poll)"

got="$(_mfs_not_healthy "b1" "$(_report "(none)" "b1" "(none)")")"
[ "$got" = "b1" ] \
    || fail "(v4) an orphan bot is NOT healthy, got '$got'"
pass "(v4) orphan bot -> not-healthy (orphan != healthy)"

echo "PASS: all migrate-fleet-to-system file-op + verify-gate tests passed"
