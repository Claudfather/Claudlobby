#!/bin/bash
# rehearse-vault-sync.sh — prove the scheduled vault door on a REAL plane
# (#1721; `rehearse-*` family sibling).
#
# WHAT THIS PROVES that a unit test cannot: the job's four outcomes end to end
# against a real plane db, read back through the plane's own reader rather than
# by grepping the log the job wrote. The log is the job's own account of
# itself; the plane is what an operator would actually read.
#
# SAFETY IS STRUCTURAL. Everything happens under one throwaway CLAUDLOBBY_ROOT
# in a temp dir: its own plane db, its own fake fleet, a stubbed `claudron`
# first on PATH, and a fake escalation chat id so the page can never reach a
# real operator. No production fleet, no real vault, no network.
#
#   bash lib/rehearse-vault-sync.sh
#
# exit 0 rehearsed clean · 1 an assertion failed · 2 a dependency is missing
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$LIB_DIR/.." && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "rehearse: python3 required" >&2; exit 2; }

WS="$(mktemp -d "${TMPDIR:-/tmp}/rehearse-vault-sync.XXXXXX")"
cleanup() { rm -rf "$WS"; }
trap cleanup EXIT

ROOT="$WS/root"; BIN="$WS/bin"; VAULT="$WS/vault"
mkdir -p "$ROOT/state" "$ROOT/lib" "$BIN" "$VAULT" \
         "$ROOT/local/rehearsal/runtime/bots/worker"
printf 'export CLAUDRON_VAULT_PATH="%s"\n' "$VAULT" \
    > "$ROOT/local/rehearsal/runtime/bots/worker/bot.conf"

fails=0
say()  { printf '  %s\n' "$*"; }
ok()   { printf '  PASS  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; fails=$((fails + 1)); }

# The engine the job calls. Its envelope is the seam: the job's contract is
# that it parses this and never the text.
stub_claudron() {   # <sync-json> <sync-rc>
    cat > "$BIN/claudron" <<EOS
#!/bin/bash
for a in "\$@"; do
  # No --check on this engine: rc 2 is argparse's usage error, the CLI
  # contract's signal for a flag the installed engine does not have.
  [ "\$a" = "--check" ] && exit 2
done
printf '%s' '$1'
exit $2
EOS
    chmod +x "$BIN/claudron"
}

run_job() {
    env CLAUDLOBBY_ROOT="$ROOT" PATH="$BIN:$PATH" \
        FLEET_PULSE_ESCALATION_CHAT_ID="-100999" \
        TELEGRAM_BOT_TOKEN="" \
        bash "$LIB_DIR/vault-sync.sh" >/dev/null 2>&1 || true
}

# THE READ DOOR, not a log grep: ask the plane what it holds.
plane_samples() {   # <metric> -> one value per line
    python3 -S -E - "$ROOT" "$1" <<'PY'
import json, sqlite3, sys, pathlib
db = pathlib.Path(sys.argv[1]) / "state" / "plane" / "plane.db"
if not db.exists():
    raise SystemExit(0)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
for (v,) in c.execute(
        "SELECT value FROM metric_samples WHERE metric = ? ORDER BY ingest_seq",
        (sys.argv[2],)).fetchall():
    try:
        d = json.loads(v)
    except Exception:
        print(v); continue
    print(d if not isinstance(d, (dict, list)) else json.dumps(d))
PY
}
plane_events() {    # <event name> -> count
    python3 -S -E - "$ROOT" "$1" <<'PY'
import sqlite3, sys, pathlib
db = pathlib.Path(sys.argv[1]) / "state" / "plane" / "plane.db"
if not db.exists():
    print(0); raise SystemExit(0)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
print(c.execute("SELECT COUNT(*) FROM events WHERE event = ?",
                (sys.argv[2],)).fetchone()[0])
PY
}
marker() { cat "$ROOT"/state/vault-sync/*.last 2>/dev/null || printf '(none)'; }

echo "rehearse-vault-sync: throwaway root at $ROOT"

# --- 1. clean ---------------------------------------------------------------
say "scenario 1 — a clean vault"
stub_claudron '{"ok": true, "data": {"detail": "up to date", "ahead": 0, "behind": 0}}' 0
run_job
n_ok="$(plane_samples vault.sync_ok | grep -c '"state"\|1' || true)"
if [ "$(plane_samples vault.sync_ok | wc -l)" -ge 1 ]; then
    ok "a sample was recorded on the plane"
else
    bad "no vault.sync_ok sample reached the plane"
fi
[ "$(marker)" = "ok" ] && ok "state recorded ok" || bad "state is $(marker), want ok"
[ "$(plane_events vault_sync)" = "0" ] && ok "a clean run raises no event" \
    || bad "a clean run raised $(plane_events vault_sync) event(s)"

# --- 2. wedged --------------------------------------------------------------
say "scenario 2 — the vault wedges (a stopped rebase, the real refusal text)"
stub_claudron '{"ok": false, "error": "refusing to sync: a rebase is stopped part-way"}' 1
run_job
[ "$(marker)" = "bad:unknown" ] && ok "state flipped to bad (engine has no --check: unknown)" \
    || bad "state is $(marker), want bad:unknown"
ev_after_first="$(plane_events vault_sync)"
[ "$ev_after_first" -ge 1 ] && ok "one vault_sync event on the plane" \
    || bad "the wedge raised no event"

# --- 3. STILL wedged — the debounce -----------------------------------------
say "scenario 3 — still wedged on the next tick (must NOT page again)"
run_job
ev_after_second="$(plane_events vault_sync)"
if [ "$ev_after_second" = "$ev_after_first" ]; then
    ok "no second event: an unchanged state says nothing"
else
    bad "a still-true condition paged again ($ev_after_first -> $ev_after_second)"
fi

# --- 4. recovery ------------------------------------------------------------
say "scenario 4 — the vault recovers"
stub_claudron '{"ok": true, "data": {"detail": "up to date"}}' 0
run_job
[ "$(marker)" = "ok" ] && ok "state flipped back to ok" || bad "state is $(marker), want ok"

# --- the positive control ---------------------------------------------------
# Without this the run above proves only that nothing happened. A harness that
# has never seen the failure it is built to detect is not evidence.
say "control — the assertions can FAIL"
rm -f "$ROOT"/state/vault-sync/*.last
stub_claudron '{"ok": false, "error": "a second, different wedge"}' 1
run_job
ev_ctl="$(plane_events vault_sync)"
[ "$ev_ctl" -gt "$ev_after_second" ] \
    && ok "a CHANGED state does page again — the debounce is state-based, not a mute" \
    || bad "a changed state failed to page: the debounce is muting, not debouncing"

echo
if [ "$fails" -eq 0 ]; then
    echo "rehearse-vault-sync: PASS — all scenarios held"
    exit 0
fi
echo "rehearse-vault-sync: FAIL — $fails assertion(s)"
exit 1
