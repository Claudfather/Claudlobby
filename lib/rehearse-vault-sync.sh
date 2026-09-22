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
# Scenario 5 additionally needs tmux, because the routing question can only be
# answered by observing DELIVERY -- see the long note at that scenario. Without
# tmux both its arms SKIP loudly and the summary says the run does not cover them.
#
#   bash lib/rehearse-vault-sync.sh
#
# exit 0 every scenario that RAN held · 1 an assertion failed · 2 a dependency
# is missing. A skipped arm does not fail the run, so read the summary line: it
# names the count.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$LIB_DIR/.." && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "rehearse: python3 required" >&2; exit 2; }

WS="$(mktemp -d "${TMPDIR:-/tmp}/rehearse-vault-sync.XXXXXX")"
cleanup() {
    # Before the tree goes: a server whose socket file we delete underneath it
    # stays running and unreachable. `if`, not `A && { }` -- under `set -e` a
    # failed AND-list that is not the last statement of the function aborts the
    # trap, and this one would abort it on every host WITHOUT tmux, i.e. exactly
    # where there is nothing to reap and the temp tree would be left behind.
    if command -v tmux >/dev/null 2>&1; then
        tmux -L "${DECOY_SOCK:-}" kill-server 2>/dev/null || true
        tmux -L "${TARGET_SOCK:-}" kill-server 2>/dev/null || true
    fi
    if [ -n "${REHEARSE_KEEP:-}" ]; then echo "kept: $WS"; return 0; fi
    rm -rf "$WS"
}
trap cleanup EXIT

ROOT="$WS/root"; BIN="$WS/bin"; VAULT="$WS/vault"
mkdir -p "$ROOT/state" "$ROOT/lib" "$BIN" "$VAULT"

# Scenario 5 observes DELIVERY, so it needs real manager sessions -- on private
# sockets under a throwaway TMUX_TMPDIR, so this can neither see nor touch a live
# fleet's server (the rehearse-debounce-recipient.sh isolation, #846).
export TMUX_TMPDIR="$WS/tmx"; mkdir -p "$TMUX_TMPDIR"
DECOY_SOCK="rvsd$$"; TARGET_SOCK="rvst$$"

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
# Each declares its OWN tmux socket, which is what makes the recipient
# observable: _emit_fleet_signal resolves MANAGER_TMUX_SOCKET from the bot it
# picked, so WHICH socket receives the nudge is the routing answer itself.
printf 'export CLAUDRON_VAULT_PATH="%s"\nexport MANAGER_TMUX="decoy-mgr"\nexport MANAGER_TMUX_SOCKET="%s"\n' \
    "$VAULT" "$DECOY_SOCK" > "$ROOT/local/$DECOY_FLEET/runtime/bots/decoy-mgr/bot.conf"
printf 'export CLAUDRON_VAULT_PATH="%s"\nexport MANAGER_TMUX="target-mgr"\nexport MANAGER_TMUX_SOCKET="%s"\n' \
    "$VAULT" "$TARGET_SOCK" > "$ROOT/local/$TARGET_FLEET/runtime/bots/target-mgr/bot.conf"

fails=0 skips=0
say()  { printf '  %s\n' "$*"; }
ok()   { printf '  PASS  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; fails=$((fails + 1)); }
skip() { printf '  SKIP  %s\n' "$*"; skips=$((skips + 1)); }

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

# --- the DELIVERY door, for scenario 5 --------------------------------------
# Fresh sessions per arm rather than clearing between them: `clear-history` only
# drops scrollback and a `sleep` pane ignores C-l, so a "cleared" pane still
# shows the previous arm's push and the next count reads as a false positive
# (rehearse-debounce-recipient.sh hit exactly this).
mgr_panes_up() {
    tmux -L "$DECOY_SOCK"  kill-server 2>/dev/null || true
    tmux -L "$TARGET_SOCK" kill-server 2>/dev/null || true
    tmux -L "$DECOY_SOCK"  new-session -d -s decoy-mgr  "sleep 300" 2>/dev/null || return 1
    tmux -L "$TARGET_SOCK" new-session -d -s target-mgr "sleep 300" 2>/dev/null || return 1
}
# How many FLEET-ALERT pushes landed in one manager's pane. Anchored on the short
# prefix, never the whole line: the pane wraps at its width, so the reason text is
# split across rows and a long-string grep would miss a push that did arrive.
alerts_in() {   # <socket> <session> -> count
    tmux -L "$1" capture-pane -t "$2" -p 2>/dev/null | grep -c 'FLEET-ALERT' || true
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
#
# READ THE DELIVERY DOOR, NOT THE DISCLOSURE EVENT. The first cut of this
# scenario asked the plane for `alert_recipient_resolved` and went RED against
# CORRECT code. _disclose_alert_recipient returns 0 on origin=declared -- the
# #1517 self-clearing property, pinned by tests/test_alert_recipient.sh ("a
# DECLARED recipient discloses nothing"). A declared recipient is therefore the
# one path that channel is silent on, and this scenario declares one: it was
# interrogating the single instrument that cannot answer its question.
#
# It could not FAIL honestly either, which is the worse half. The same silencer
# covers origin=local, so an outcome that ignored the declaration for a local
# pick prints the identical "no row" -- a scenario whose failure message named
# the lexical-pick bug while being unable to distinguish it from correct
# behaviour. A check that fails for the wrong reason is the same defect class as
# one that cannot fail at all.
#
# What CAN answer is where the alert ARRIVED. A real manager session per fleet,
# each on that fleet's own socket (bot.conf MANAGER_TMUX_SOCKET), makes the
# resolved recipient observable. That is the instrument
# tests/test_alert_recipient.sh reaches for at unit level ("the only way to see
# which manager a correct, silent resolution picked") and that
# rehearse-debounce-recipient.sh reaches for on real tmux; this is the same
# method one layer out, through the real job in its own process.
#
# TWO ARMS, and arm B is not an extra scenario -- it is arm A's positive
# control, twice over. A decoy pane reading 0 is equally consistent with
# "correctly not chosen" and "this pane never receives anything"; arm A's silent
# plane is equally consistent with "declared, so silent by contract" and
# "disclosure is broken". Arm B feeds BOTH, in the opposite direction, in the
# same run -- so arm A's two zeros are choices rather than dead instruments.
say "scenario 5 — the alert must reach the DECLARED fleet, not the first one"
first_fleet="$(cd "$ROOT/local" && ls -1 | head -1)"
if [ "$first_fleet" = "$DECOY_FLEET" ] && [ "$DECOY_FLEET" != "$TARGET_FLEET" ]; then
    ok "the fixture poses a real choice (lexically first is '$first_fleet', declared is '$TARGET_FLEET')"
else
    bad "THE FIXTURE POSES NO CHOICE — lexically first is '$first_fleet'. Every assertion below would pass under the old lexical-pick bug; fix the fixture before trusting this run."
fi

if ! command -v tmux >/dev/null 2>&1; then
    # Said out loud and counted. A silent skip of the one scenario this harness
    # exists for reads exactly like a scenario that passed.
    skip "tmux absent — BOTH routing arms were NOT run, so recipient routing is UNVERIFIED by this run"
else
    # --- arm A: a DECLARED recipient wins -----------------------------------
    rm -f "$ROOT"/state/vault-sync/*.last
    stub_claudron '{"ok": false, "error": "refusing to sync: a rebase is stopped part-way"}' 1
    before_seq="$(plane_seq)"
    if mgr_panes_up; then
        run_job "target-mgr"          # declare the TARGET fleet's manager
        a_target="$(alerts_in "$TARGET_SOCK" target-mgr)"
        a_decoy="$(alerts_in "$DECOY_SOCK" decoy-mgr)"
        a_origin="$(recipient_field origin "$before_seq")"
        [ "$a_target" -ge 1 ] \
            && ok "arm A: the alert reached '$TARGET_FLEET'/target-mgr — the DECLARED recipient" \
            || bad "arm A: nothing reached target-mgr ($a_target push(es)) — the declaration did not route the alert"
        [ "$a_decoy" -eq 0 ] \
            && ok "arm A: and NOT '$DECOY_FLEET'/decoy-mgr, which sorts first" \
            || bad "arm A: the alert reached decoy-mgr ($a_decoy push(es)) — the lexical-pick shape #1517 is about"
        [ -z "$a_origin" ] \
            && ok "arm A: and disclosed nothing — a declared recipient is self-clearing (#1517)" \
            || bad "arm A: a declared recipient disclosed origin='$a_origin' — the self-clearing property is broken"
    else
        bad "arm A: the manager sessions would not start — the routing check DID NOT RUN"
    fi

    # --- arm B: nothing declared — the control ------------------------------
    # Same fixture, one variable flipped: no CLAUDLOBBY_ALERT_MANAGER. Routing
    # must now fall through to the cross-fleet glob, which is lexical, so the
    # decoy is the CORRECT answer here. Everything arm A asserted as absent must
    # be present, and vice versa.
    rm -f "$ROOT"/state/vault-sync/*.last
    before_seq="$(plane_seq)"
    if mgr_panes_up; then
        run_job                       # nothing declared
        b_decoy="$(alerts_in "$DECOY_SOCK" decoy-mgr)"
        b_target="$(alerts_in "$TARGET_SOCK" target-mgr)"
        b_origin="$(recipient_field origin "$before_seq")"
        b_fleet="$(recipient_field manager_fleet "$before_seq")"
        b_cands="$(recipient_field candidate_fleets "$before_seq")"
        [ "$b_decoy" -ge 1 ] \
            && ok "arm B: undeclared, the fallback lands on '$DECOY_FLEET' — so the decoy pane DOES receive, and arm A's zero was a choice" \
            || bad "arm B: the undeclared fallback reached nobody on decoy-mgr ($b_decoy) — arm A's decoy count proves nothing"
        [ "$b_target" -eq 0 ] \
            && ok "arm B: and not target-mgr — the two panes discriminate in both directions" \
            || bad "arm B: the alert reached target-mgr ($b_target) with nothing declared — the panes do not discriminate"
        [ "$b_origin" = "discovered" ] \
            && ok "arm B: and SAYS so on the plane — origin=discovered, nobody chose this reader" \
            || bad "arm B: origin is '${b_origin:-<no row>}', want 'discovered' — the audit trail arm A's silence is measured against is not firing"
        [ "$b_fleet" = "$DECOY_FLEET" ] \
            && ok "arm B: naming '$b_fleet' as the accidental recipient" \
            || bad "arm B: named '${b_fleet:-<none>}', want '$DECOY_FLEET'"
        [ "${b_cands:-0}" -ge 2 ] \
            && ok "arm B: out of $b_cands candidate fleets — a genuine choice, not a single option" \
            || bad "arm B: ${b_cands:-0} candidate fleet(s) — with fewer than 2 nothing here separates lexical-first from correct resolution"
    else
        bad "arm B: the manager sessions would not start — arm A's control DID NOT RUN"
    fi
fi
echo
_cov=""
[ "$skips" -gt 0 ] && _cov=" ($skips check(s) SKIPPED — see the SKIP line(s) above; this run does NOT cover them)"
if [ "$fails" -eq 0 ]; then
    echo "rehearse-vault-sync: PASS — all scenarios that RAN held$_cov"
    exit 0
fi
echo "rehearse-vault-sync: FAIL — $fails assertion(s)$_cov"
exit 1
