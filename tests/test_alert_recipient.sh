#!/usr/bin/env bash
# tests/test_alert_recipient.sh — who receives a fleet signal, and whether that
# choice is auditable (#1517).
#
# A host job runs fleet-less, so its bots_dir does not exist and the manager
# resolver falls through to a cross-fleet glob that expands LEXICALLY. The
# recipient was therefore whichever fleet directory sorted first -- nobody chose
# it, a newly-added directory moves it silently, and the previous recipient
# cannot detect the loss (alerts stopping looks exactly like alerts not firing).
#
# These assertions pin the two halves of the fix and, deliberately, the DEFECT
# itself: the lexical fallback still exists (removing it would strand every host
# that has declared nothing), so the test asserts it is still reachable AND that
# it now announces itself. A fix that made the fallback silent-but-correct would
# pass a weaker test and leave the auditability requirement unmet.
#
# Hermetic without env -i: CLAUDLOBBY_ROOT is a temp estate, PLANE_SOCKET is a
# dead path, and the plane's cold rung is the suite's capture shim -- so no real
# fleet, daemon or unit is reachable. Standalone bash; macOS /bin/bash (3.2).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export CLAUDLOBBY_ROOT="$T"
export PLANE_EMIT_CLI="$SCRIPT_DIR/plane_capture_cli.sh"
export PLANE_SOCKET="$T/no.sock"
export PLANE_CAPTURE="$T/plane-capture.jsonl"; : > "$PLANE_CAPTURE"
cap_count() { grep -c "$1" "$PLANE_CAPTURE" 2>/dev/null | tr -d ' '; }
cap_reset() { : > "$PLANE_CAPTURE"; }

# A NESTED estate (local/<system>/<fleet>/), which is the layout the flat glob
# misses and the nested one matches. "ai-platform" sorts before "zeta" -- the
# real shape of the defect, not a contrived one.
for f in home/ai-platform home/zeta; do
    for b in otis ravi; do
        mkdir -p "$T/local/$f/runtime/bots/$b"
        printf 'export BOT_ID=%s\nexport MANAGER_TMUX=mgr-%s\nexport TELEGRAM_GROUP_CHAT_ID=chat-%s\n' \
            "$b" "$(basename "$f")" "$(basename "$f")" \
            > "$T/local/$f/runtime/bots/$b/bot.conf"
    done
done
# A uniquely-named bot in the LATER-sorting fleet: the declared target must be
# reachable somewhere discovery would never have landed.
mkdir -p "$T/local/home/zeta/runtime/bots/solo"
printf 'export BOT_ID=solo\nexport MANAGER_TMUX=mgr-zeta\n' > "$T/local/home/zeta/runtime/bots/solo/bot.conf"

# shellcheck disable=SC1091
. "$LIB_DIR/lib-common.sh" 2>/dev/null || true
set +e +u   # lib-common re-arms -e/-u at source time; this suite owns its flow

echo "== fleet enumeration is shared by the search and the count =="
assert_eq "enumerates every nested fleet" "2" "$(host_fleet_bots_dirs | wc -l | tr -d ' ')"

echo "== bot_dir_for_id =="
assert_eq "a unique id resolves" "$T/local/home/zeta/runtime/bots/solo" "$(bot_dir_for_id solo)"
bot_dir_for_id ravi >/dev/null 2>&1
assert_eq "a COLLIDING id refuses rather than picking one (#526)" "1" "$?"
assert_eq "and prints nothing to resolve against" "" "$(bot_dir_for_id ravi 2>/dev/null)"
bot_dir_for_id nosuch >/dev/null 2>&1
assert_eq "an absent id refuses" "1" "$?"

echo "== the defect is still reachable, and still lexical =="
_hit=$(first_bot_with_conf_any_fleet "$T/runtime/bots" MANAGER_TMUX)
assert_eq "host-scope resolution lands on the lexically-first fleet" "ai-platform" \
    "$(basename "$(dirname "$(dirname "$(dirname "$_hit")")")")"

echo "== disclosure fires exactly where the choice was not made on purpose =="
cap_reset
_disclose_alert_recipient "$T/runtime/bots" "$T/local/home/ai-platform/runtime/bots/otis" "mgr-ai-platform" "discovered" ""
assert_eq "a cross-fleet fallback announces itself" "1" "$(cap_count alert_recipient_resolved)"
assert_eq "  and quantifies how movable it is" '"candidate_fleets":2' \
    "$(grep -o '"candidate_fleets":[0-9]*' "$PLANE_CAPTURE" | head -1)"
assert_eq "  and names the fleet it crossed into" '"manager_fleet":"ai-platform"' \
    "$(grep -o '"manager_fleet":"[^"]*"' "$PLANE_CAPTURE" | head -1)"

cap_reset
_disclose_alert_recipient "$T/runtime/bots" "$T/local/home/zeta/runtime/bots/solo" "mgr-zeta" "declared" "solo"
assert_eq "a DECLARED recipient discloses nothing (self-clearing)" "0" "$(cap_count alert_recipient_resolved)"

cap_reset
_disclose_alert_recipient "$T/runtime/bots" "$T/local/home/ai-platform/runtime/bots/otis" "mgr-ai" "declared-unresolved" "ravi"
assert_eq "a declared-but-unresolvable recipient is LOUD, not silently substituted" "1" \
    "$(cap_count '"origin":"declared-unresolved"')"

cap_reset
# "local" is the origin THE CASCADE PRODUCES for a local hit. An earlier version
# of this line passed "discovered" by hand, which the cascade never pairs with a
# local scope -- so it proved a property of this function in isolation that did
# not hold for the way it is actually called, and the suite stayed green while
# every ordinary per-fleet alert disclosed. The integration block at the bottom
# is what can catch that class; this line alone never could.
_disclose_alert_recipient "$T/local/home/ai-platform/runtime/bots" "$T/local/home/ai-platform/runtime/bots/otis" "m" "local" ""
assert_eq "a fleet-scoped LOCAL resolution stays silent (nothing was ambiguous)" "0" \
    "$(cap_count alert_recipient_resolved)"

echo "== the over-reach guard: a per-fleet job must be untouched =="
# The declaration is HOST-tier. A per-fleet job (fleet-pulse, creds-check) passes
# its fleet, resolves at step 1, and must keep resolving there even when a host
# recipient is declared -- otherwise the fix changes behaviour for jobs that were
# never broken. This is the assertion that pins that boundary.
export CLAUDLOBBY_ALERT_MANAGER=solo
_local=$(first_bot_with_conf "$T/local/home/ai-platform/runtime/bots" MANAGER_TMUX)
assert_eq "a fleet with its own manager still resolves locally" \
    "$T/local/home/ai-platform/runtime/bots/otis/" "$_local"
assert_eq "  and the declared HOST recipient does not displace it" "mgr-ai-platform" \
    "$(bot_conf_get "$_local" MANAGER_TMUX "")"
unset CLAUDLOBBY_ALERT_MANAGER

echo "== the Telegram half shares the resolver (#1517, seventh surface) =="
# Same lexical fallback on the LOUDER channel: with no env chat-id, a host-scope
# call scans cross-fleet and takes whichever fleet sorted first.
unset TELEGRAM_GROUP_CHAT_ID FLEET_PULSE_ESCALATION_CHAT_ID TELEGRAM_STATE_DIR
# CHARACTERISATION, not a fix. These assert TODAY's behaviour so the separate
# Telegram PR has a red-first target: scan_scope is already the lever (the
# "fleet" branch takes the narrow resolver), so passing scope correctly is
# likely the whole fix there. Deliberately not bundled with the tmux resolver.
resolve_alert_target "$T/runtime/bots"
assert_eq "host-scope chat-id comes from the lexically-first fleet (the defect)" \
    "chat-ai-platform" "$_alert_chat_id"
resolve_alert_target "$T/runtime/bots" fleet
assert_eq "  and scan_scope=fleet already narrows it (the lever, unused today)" \
    "" "$_alert_chat_id"
export TELEGRAM_GROUP_CHAT_ID=chat-declared
resolve_alert_target "$T/runtime/bots"
assert_eq "an env chat-id already wins, so the fallback is only reached unresolved" \
    "chat-declared" "$_alert_chat_id"
unset TELEGRAM_GROUP_CHAT_ID

echo "== INTEGRATION: the public door, not its ingredients =="
# Every assertion above calls an INGREDIENT -- first_bot_with_conf,
# bot_dir_for_id, _disclose_alert_recipient -- with hand-built arguments, so none
# of them can see what the CASCADE passes. That is a structural blind spot, not a
# thin patch of coverage: a silencer keyed on an origin the cascade never emits
# was invisible to 17 passing assertions.
#
# Stub the two delivery doors so the RESOLVED MANAGER becomes observable. That is
# the only way to see which manager a correct, silent resolution picked --
# disclosure is quiet on exactly that path, so the event stream cannot answer it,
# which is why the over-reach property above had no integration-level test.
check_tmux_session() { return 0; }
bot_tmux_send() { printf '%s\n' "$2" >> "$T/sent"; return 0; }
# Telegram stays hermetic on its own: CLAUDLOBBY_ROOT is the temp estate, so
# "$CLAUDLOBBY_ROOT/lib/tg-post.sh" does not exist and the send cannot leave the
# box. It records a delivery failure, which is correct and is not asserted here.

cap_reset; : > "$T/sent"
unset CLAUDLOBBY_ALERT_MANAGER
emit_fleet_notice "$T/local/home/ai-platform/runtime/bots" "test-notice" "routine check" >/dev/null 2>&1
assert_eq "an ordinary per-fleet alert discloses NOTHING" "0" "$(cap_count alert_recipient_resolved)"
assert_eq "  and still reaches its own fleet manager" "mgr-ai-platform" "$(tail -1 "$T/sent")"

cap_reset; : > "$T/sent"
export CLAUDLOBBY_ALERT_MANAGER=solo
emit_fleet_notice "$T/local/home/ai-platform/runtime/bots" "test-notice" "routine check" >/dev/null 2>&1
assert_eq "a declared HOST recipient does not displace a local one" "mgr-ai-platform" "$(tail -1 "$T/sent")"
assert_eq "  and that resolution is unambiguous, so it stays silent too" "0" \
    "$(cap_count alert_recipient_resolved)"
unset CLAUDLOBBY_ALERT_MANAGER

# POSITIVE CONTROL. The three silence assertions above are only evidence if this
# instrument can still SEE a disclosure -- delete disclosure entirely and they
# all pass. This is the one that fails in that case.
cap_reset; : > "$T/sent"
emit_fleet_notice "$T/runtime/bots" "test-notice" "host job" >/dev/null 2>&1
assert_eq "a HOST-scope alert still announces its accidental recipient" "1" \
    "$(cap_count alert_recipient_resolved)"
assert_eq "  and records that nobody chose it" '"origin":"discovered"' \
    "$(grep -o '"origin":"[^"]*"' "$PLANE_CAPTURE" | head -1)"

echo ""
echo "=== $PASS/$TOTAL passed ==="
[ "$FAIL" -eq 0 ] || exit 1
