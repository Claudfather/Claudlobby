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
cleanup() { [ -n "${REHEARSE_KEEP:-}" ] && { echo "kept: $WS"; return 0; }; rm -rf "$WS"; }
trap cleanup EXIT

ROOT="$WS/root"; BIN="$WS/bin"; VAULT="$WS/vault"
mkdir -p "$ROOT/state" "$ROOT/lib" "$BIN" "$VAULT"

# TWO fleets, and they must DISAGREE about who the alert goes to -- this is the
# whole reason the fixture exists in this shape (review). With one fleet, "pick
# whichever sorts first" and "resolve correctly" produce byte-identical output,
# so a regression straight back to `host_fleet_bots_dirs | head -1` would pass
# every assertion below without changing one of them. A harness that cannot
# distinguish the bug from the fix certifies the bug.
#
#   aaa-decoy  sorts FIRST and declares a manager, so a lexical pick lands here
#   zzz-target sorts LAST and is the DECLARED recipient, so correct resolution
#              lands here
DECOY_FLEET="aaa-decoy"; TARGET_FLEET="zzz-target"
mkdir -p "$ROOT/local/$DECOY_FLEET/runtime/bots/decoy-mgr" \
         "$ROOT/local/$TARGET_FLEET/runtime/bots/target-mgr"
# Only the DECOY declares MANAGER_TMUX, so plain discovery would choose it.
printf 'export CLAUDRON_VAULT_PATH="%s"\nexport MANAGER_TMUX="decoy-mgr"\n' "$VAULT" \
    > "$ROOT/local/$DECOY_FLEET/runtime/bots/decoy-mgr/bot.conf"
printf 'export CLAUDRON_VAULT_PATH="%s"\nexport MANAGER_TMUX="target-mgr"\n' "$VAULT" \
    > "$ROOT/local/$TARGET_FLEET/runtime/bots/target-mgr/bot.conf"

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

# The four identity vars are UNSET, not merely overridden (review). The runbook
# tells a reviewer to run this, and a reviewer runs it from inside a bot
# session -- where resolve_bots_dir's own fallback chain
# (${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}) silently substitutes the CALLING bot's
# fleet for the fleet-less host-job shape this is supposed to exercise. A run
# that inherits them passes by contamination and proves nothing.
run_job() {
    env -u FLEET_NAME -u CLAUDLOBBY_FLEET -u BOT_DIR -u BOT_ID \
        -u CLAUDLOBBY_ALERT_MANAGER \
        CLAUDLOBBY_ROOT="$ROOT" PATH="$BIN:$PATH" \
        CLAUDLOBBY_ALERT_MANAGER="${1:-}" \
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

plane_seq() {   # the plane's high-water mark, so a read cannot be stale
    python3 -S -E - "$ROOT" <<'PY3'
import sqlite3, sys, pathlib
db = pathlib.Path(sys.argv[1]) / "state" / "plane" / "plane.db"
if not db.exists():
    print(0); raise SystemExit(0)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
print(c.execute("SELECT COALESCE(MAX(ingest_seq), 0) FROM events").fetchone()[0])
PY3
}

# WHO the alert resolved to, from the plane's own record of the decision.
#
# SCOPED TO ROWS THIS SCENARIO PRODUCED (<after-seq>). Reading "the newest
# alert_recipient_resolved" is stale-prone and was: an earlier scenario's row
# answered for a scenario that emitted none, which reported a routing failure
# that had not happened. An instrument that silently answers from the wrong run
# is the same defect class this harness exists to close.
recipient_field() {   # <field> <after-seq> -> value, or empty if no NEW row
    python3 -S -E - "$ROOT" "$1" "${2:-0}" <<'PY2'
import json, sqlite3, sys, pathlib
db = pathlib.Path(sys.argv[1]) / "state" / "plane" / "plane.db"
if not db.exists():
    raise SystemExit(0)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
row = c.execute("SELECT detail FROM events WHERE event = 'alert_recipient_resolved'"
                " AND ingest_seq > ? ORDER BY ingest_seq DESC LIMIT 1",
                (int(sys.argv[3]),)).fetchone()
if not row or not row[0]:
    raise SystemExit(0)
try:
    d = json.loads(row[0])
except Exception:
    raise SystemExit(0)
d = d.get("data", d)
v = d.get(sys.argv[2])
print("" if v is None else v)
PY2
}

echo "rehearse-vault-sync: throwaway root at $ROOT"

# --- 1. clean ---------------------------------------------------------------
say "scenario 1 — a clean vault"
stub_claudron '{"ok": true, "data": {"detail": "up to date", "ahead": 0, "behind": 0}}' 0
run_job
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

# --- 5. ROUTING: the assertion this harness exists to be able to fail --------
# The #1517 defect is "pick whichever fleet sorts first". With one fleet that is
# indistinguishable from resolving correctly, so this poses a real choice and
# then checks which way it went.
say "scenario 5 — the alert must reach the DECLARED fleet, not the first one"
first_fleet="$(cd "$ROOT/local" && ls -1 | head -1)"
if [ "$first_fleet" = "$DECOY_FLEET" ] && [ "$DECOY_FLEET" != "$TARGET_FLEET" ]; then
    ok "the fixture poses a real choice (lexically first is '$first_fleet', declared is '$TARGET_FLEET')"
else
    bad "THE FIXTURE POSES NO CHOICE — lexically first is '$first_fleet'. Every assertion below would pass under the old lexical-pick bug; fix the fixture before trusting this run."
fi

rm -f "$ROOT"/state/vault-sync/*.last
stub_claudron '{"ok": false, "error": "refusing to sync: a rebase is stopped part-way"}' 1
before_seq="$(plane_seq)"
run_job "target-mgr"          # declare the TARGET fleet's manager

cands="$(recipient_field candidate_fleets "$before_seq")"
if [ "${cands:-0}" -ge 2 ]; then
    ok "the resolver saw $cands candidate fleets — a genuine choice, not a single option"
else
    bad "the resolver saw ${cands:-0} candidate fleet(s): with fewer than 2 this harness cannot tell lexical-first from correct resolution"
fi

origin="$(recipient_field origin "$before_seq")"
mgr_fleet="$(recipient_field manager_fleet "$before_seq")"
declared="$(recipient_field declared "$before_seq")"
if [ -z "$origin" ]; then
    bad "this scenario produced NO alert_recipient_resolved row — the harness cannot see the decision it is meant to check (an absent answer, not a passing one)"
elif [ "$mgr_fleet" = "$TARGET_FLEET" ]; then
    ok "resolved to '$mgr_fleet' (origin=$origin) — the DECLARED fleet, not the lexically first"
else
    bad "resolved to '${mgr_fleet:-<none>}' (origin=${origin:-<none>}), want '$TARGET_FLEET'. '$DECOY_FLEET' sorts first, so this is the lexical-pick shape #1517 is about."
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "rehearse-vault-sync: PASS — all scenarios held"
    exit 0
fi
echo "rehearse-vault-sync: FAIL — $fails assertion(s)"
exit 1
