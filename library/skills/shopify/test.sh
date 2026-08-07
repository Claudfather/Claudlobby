#!/usr/bin/env bash
# Tests for the shopify helper script.
#
# Hermetic checks always run (no creds, no network) — including trap-logic
# assertions driven by fixtures/, which is where the real value is: they pin the
# BEHAVIOUR traps.md documents, so a future edit that quietly reverts to the
# naive field turns them red.
#
# A live smoke runs only when SHOPIFY_STORE_DOMAIN + SHOPIFY_ACCESS_TOKEN are set.
#
# Run:  bash test.sh
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SH="$DIR/shopify_api.sh"
FIX="$DIR/fixtures"
pass=0; fail=0
ok() { printf 'ok   - %s\n' "$1"; pass=$((pass + 1)); }
no() { printf 'FAIL - %s\n' "$1"; fail=$((fail + 1)); }
scrub() { env -u SHOPIFY_STORE_DOMAIN -u SHOPIFY_SHOP_DOMAIN -u SHOPIFY_ACCESS_TOKEN -u SHOPIFY_ADMIN_ACCESS_TOKEN "$@"; }

# --- contract: never fabricate, never run without creds ---------------------

if out=$(scrub "$SH" help 2>&1) && printf '%s' "$out" | grep -q "shopify"; then
  ok "help works without creds"
else no "help without creds"; fi

if scrub "$SH" bogus >/dev/null 2>&1; then
  no "unknown command should exit non-zero"
else ok "unknown command exits non-zero"; fi

scrub "$SH" health-check >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 2 ]; then ok "missing domain exits 2"; else no "missing domain exit code ($rc != 2)"; fi

scrub env SHOPIFY_STORE_DOMAIN="example.myshopify.com" "$SH" catalog >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 2 ]; then ok "missing token exits 2"; else no "missing token exit code ($rc != 2)"; fi

if scrub "$SH" raw >/dev/null 2>&1; then
  no "raw with no path should fail"
else ok "raw with no path exits non-zero"; fi

# --- the actuator is read-only, PROVEN BY EXECUTION -------------------------
#
# These checks used to grep the source for "-X DELETE". That could never match,
# because the script passes -X "$method" from a variable and never spells a verb
# literally — so a wired write door walked straight past this guard and the
# pytest one. Both pinned the appearance of the property, not the property.
#
# So: source the script and call the guards. Exit 7 means a guard refused, and
# nothing else in the script uses 7, so a refusal is distinguishable from a
# network or credential failure.

refused() {
  local desc="$1" code="$2" rc=0
  scrub bash -c "source '$SH'; $code" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 7 ]; then ok "$desc"; else no "$desc (rc=$rc, expected 7)"; fi
}

refused "api refuses DELETE" "api DELETE https://x.example/y"
refused "api refuses PUT"    "api PUT https://x.example/y"
refused "api refuses PATCH"  "api PATCH https://x.example/y"
refused "api refuses POST to a REST path" \
        "api POST https://x.example/admin/api/2026-04/products.json '{}'"

# The load-bearing half: a mutation is a POST, so no method check can see it.
refused "gql refuses a mutation" \
        "gql 'mutation { productUpdate(input:{}) { product { id } } }'"
refused "gql refuses a subscription" "gql 'subscription { x }'"
refused "gql refuses a second operation smuggled after a query" \
        "gql 'query { a } mutation { b }'"

# ...and it must NOT reject the reads this skill exists to perform. A valid
# query passes the guard and dies on the absent credentials instead (2, not 7).
# Without this, a guard that rejected everything would score full marks above.
for q in "query { shop { name } }" "{ shop { name } }"; do
  rc=0
  scrub bash -c "source '$SH'; gql '$q'" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 2 ]; then ok "a valid query reaches the credential check: $q"
  else no "a valid query was blocked ($q, rc=$rc, expected 2)"; fi
done

# Secondary and structural: curl must appear only inside api(), or a future door
# could issue its own request and bypass both guards. Deliberately NOT the
# load-bearing check — that is what the executed refusals above are for.
# Counts INVOCATIONS, not the word: the header prose says "anyone can curl
# Shopify", and a check that counts prose is the same mistake as the greps this
# section replaced.
if [ "$(grep -cE '^[^#]*\bcurl[[:space:]]+-' "$SH")" -eq 2 ]; then
  ok "curl is invoked only in api() (2 sites: with body / without)"
else no "unexpected curl call sites — a door may be bypassing the guards"; fi

# --- traps.md logic, driven by fixtures (no creds, no network) --------------

command -v jq >/dev/null 2>&1 || { printf '\njq missing — skipping fixture logic tests\n'; printf '\n%d passed, %d failed\n' "$pass" "$fail"; [ "$fail" -eq 0 ]; exit $?; }

# trap 1: a store whose variants are all "manual" must be reported as UNUSABLE
# for supplier identification, not silently accepted.
svc=$(jq -c '[.products[].variants[].fulfillment_service]|unique' "$FIX/products_trap_shapes.json")
if [ "$svc" = '["manual"]' ]; then
  ok "fixture encodes trap 1 (fulfillment_service uniformly manual)"
else no "trap 1 fixture drifted: $svc"; fi

# trap 2: the discriminator is inventory_management, NOT the 9999 value. The
# fixture deliberately contains a 9999, a negative, and a genuinely tracked
# variant, so a rule keyed on 9999 gets a different answer than the sound rule.
naive=$(jq '[.products[].variants[]|select(.inventory_quantity==9999)]|length' "$FIX/products_trap_shapes.json")
sound=$(jq '[.products[].variants[]|select(.inventory_management==null)]|length' "$FIX/products_trap_shapes.json")
if [ "$naive" -eq 1 ] && [ "$sound" -eq 5 ] && [ "$naive" -ne "$sound" ]; then
  ok "trap 2: the 9999 rule and the inventory_management rule disagree ($naive vs $sound)"
else no "trap 2 fixture drifted (naive=$naive sound=$sound)"; fi

# trap 5: node count and code count must differ, or the fixture proves nothing.
nodes=$(jq '[.data.codeDiscountNodes.edges[]]|length' "$FIX/discount_nodes.json")
codes=$(jq '[.data.codeDiscountNodes.edges[].node.codeDiscount.codesCount.count]|add' "$FIX/discount_nodes.json")
if [ "$nodes" -lt "$codes" ] && [ "$codes" -gt 1000 ]; then
  ok "trap 5: fixture has $nodes nodes carrying $codes codes"
else no "trap 5 fixture drifted (nodes=$nodes codes=$codes)"; fi

# trap 6: exactly one product sets half the unlisted convention — the case the
# health-check is meant to surface.
half=$(jq '[.products[]|select((.product_type=="Hidden") != ((.tags//"")|test("(^|, *)hidden( *,|$)")))]|length' "$FIX/products_trap_shapes.json")
if [ "$half" -eq 1 ]; then
  ok "trap 6: fixture contains a half-hidden product for the check to catch"
else no "trap 6 fixture drifted (half=$half)"; fi

# trap 3: set difference finds the product absent from the fulfiller list.
missing=$(jq --rawfile ids "$FIX/fulfiller_ids.txt" '
  ($ids|split("\n")|map(select(length>0))) as $known |
  [.products[]|select(.status=="active")| . as $p |select(($known|index($p.id|tostring))==null)]|length' \
  "$FIX/products_trap_shapes.json")
if [ "$missing" -eq 1 ]; then
  ok "trap 3: set difference isolates the unlinked active product"
else no "trap 3 fixture drifted (missing=$missing)"; fi

# --- the five doors added for #998, driven by fixtures (no creds, no network) -
#
# Each door is called for real with `rest` stubbed to return a fixture, so this
# exercises the door logic rather than asserting it exists. Same reasoning as the
# read-only block above: a guard you cannot execute is a guard you cannot trust.

door_out() {  # door_out <door-fn> <stub-body>
  scrub bash -c "source '$SH'; $2; $1" 2>/dev/null
}

# Assert on a door's output, having first proven there IS output.
#
# `jq -e` EXITS 0 ON EMPTY INPUT. So the obvious form —
#   printf '%s' "$out" | jq -e '<claim>' && ok ...
# reports a pass when the door printed nothing at all, which is exactly what a
# door that died looks like. A broken door sailed through this block until CI
# disagreed with a green local run. Same family as the greps this suite already
# replaced: an assertion whose failure mode is a tick.
jq_is() {  # jq_is <desc> <json> <filter>
  local desc="$1" json="$2" filter="$3"
  if [ -z "$json" ]; then no "$desc — the door produced NO output"; return; fi
  if ! printf '%s' "$json" | jq -e . >/dev/null 2>&1; then
    no "$desc — the door output is not valid JSON"; return
  fi
  if printf '%s' "$json" | jq -e "$filter" >/dev/null 2>&1; then ok "$desc"
  else no "$desc"; fi
}

# webhooks: the answer is the MISSING list, not the registered one (trap 9).
wh="$(door_out door_webhooks "rest() { cat '$DIR/fixtures/webhooks_missing_orders.json'; }")"
jq_is "webhooks: flags a missing orders/create topic" "$wh" ".critical_missing | index(\"orders/create\")"
jq_is "webhooks: still reports what IS registered" "$wh" ".registered_topics | index(\"products/update\")"
jq_is "webhooks: states that registration is not delivery" "$wh" ".bound | test(\"REGISTRATION ONLY\")"

# copy: the .: marker is the one a human reading JSON cannot see (trap 10).
cp_="$(door_out door_copy "rest() { cat '$DIR/fixtures/products_copy_defects.json'; }")"
jq_is "copy: matches the invisible .: marker exactly" "$cp_" ".defects.dot_colon_marker == [\"dot-colon-one\"]"
jq_is "copy: markup-only body counts as empty" "$cp_" ".defects.empty_description == [\"empty-one\"]"
jq_is "copy: does not flag a clean description" "$cp_" ".defects.dot_colon_marker | index(\"clean-one\") | not"

# redirects: BOTH directions, and the second is the one people miss (trap 11).
rd="$(door_out door_redirects "
  rest_paged() { cat '$DIR/fixtures/redirects_both_directions.json'; }
  rest() { case \"\$1\" in products.json*) cat '$DIR/fixtures/redirect_catalogue.json' ;; *) printf '{}' ;; esac; }")"
jq_is "redirects: a drafted target counts as dead (trap 7)" "$rd" "[.dead_destinations[].target] | index(\"/products/drafted-item\")"
jq_is "redirects: flags a revived SOURCE, the expensive direction" "$rd" "[.revived_sources[].path] | index(\"/products/came-back\")"
jq_is "redirects: does not judge an off-catalogue target it cannot check" "$rd" "[.dead_destinations[].target] | index(\"https://elsewhere.test/page\") | not"

# orphans: a triage list, never a delete list (trap 12).
or_="$(door_out door_orphans "
  rest() { case \"\$1\" in
    products.json*) printf '%s' '{\"products\":[{\"id\":1,\"handle\":\"linked\",\"title\":\"L\",\"status\":\"active\",\"product_type\":\"Tees\",\"tags\":\"\"},{\"id\":2,\"handle\":\"lonely\",\"title\":\"O\",\"status\":\"active\",\"product_type\":\"Tees\",\"tags\":\"\"},{\"id\":3,\"handle\":\"by-design\",\"title\":\"D\",\"status\":\"active\",\"product_type\":\"Hidden\",\"tags\":\"hidden\"}]}' ;;
    custom_collections.json*) printf '%s' '{\"custom_collections\":[{\"id\":9,\"handle\":\"c\"}]}' ;;
    smart_collections.json*) printf '%s' '{\"smart_collections\":[]}' ;;
    collects.json*) printf '%s' '{\"collects\":[{\"product_id\":1}]}' ;;
  esac; }")"
jq_is "orphans: an unlinked product needs a decision" "$or_" ".by_likely_action.needs_a_decision == [\"lonely\"]"
jq_is "orphans: separates deliberately-unlinked from accidental (trap 6 + 12)" "$or_" ".by_likely_action.leave_alone_orphaned_by_design == [\"by-design\"]"
jq_is "orphans: states that collection membership is a proxy for inbound links" "$or_" ".bound | test(\"PROXY\")"

# consent: a sample must never be reported as a total (trap 13).
cs="$(door_out door_consent "rest_paged() { cat '$DIR/fixtures/customers_consent.json'; }")"
jq_is "consent: a complete run is marked complete" "$cs" ".complete == true and .customers_counted == 4"
jq_is "consent: tallies states including a null consent object" "$cs" ".by_state.subscribed == 2 and .by_state.null == 1"
cs_trunc="$(door_out door_consent "rest_paged() { cat '$DIR/fixtures/customers_consent.json'; printf '%s\\n' '{\"__truncated__\":true}'; }")"
jq_is "consent: a bounded run declares itself a SAMPLE, not a total" "$cs_trunc" ".complete == false and (.warnings|length) > 0"

# The Link-header rel discrimination, which is what makes full counting possible.
nexturl="$(tr -d '\r' < "$DIR/fixtures/link_header_next_and_prev.txt" | grep -i '^link:' | tr ',' '\n' \
           | grep 'rel="next"' | sed -e 's/.*<//' -e 's/>.*//' | head -1)"
if printf '%s' "$nexturl" | grep -q 'page_info=FWD'; then
  ok "Link header: follows rel=next, not rel=previous"
else no "Link header: picked the wrong rel — this walks backwards forever"; fi

# --- documentation contract -------------------------------------------------

for n in 1 2 3 4 5 6 7 8 9 10 11 12 13; do
  grep -qE "^## $n\. " "$DIR/traps.md" || no "traps.md is missing entry $n"
done
grep -qE "^## 13\. " "$DIR/traps.md" && ok "traps.md has all 13 entries"

# every door in the script is documented in SKILL.md
for d in health-check orders catalog discounts collections webhooks copy redirects orphans consent raw; do
  grep -q -- "$d" "$DIR/SKILL.md" || no "SKILL.md does not document the '$d' door"
done
ok "every door appears in SKILL.md"

# every door is reachable from the dispatch table — a door nobody can call is not a door
for d in webhooks copy redirects orphans consent; do
  grep -qE "^  $d\)" "$SH" || no "'$d' is not wired into the dispatch case"
done
ok "every new door is wired into dispatch"

# --- public-repo hygiene: no real store identifiers ------------------------

if grep -rEiq 'myshopify\.com' "$DIR" --include='*.sh' --include='*.md' --include='*.json' \
   | grep -v 'example\.myshopify\.com' | grep -v 'myshop\.myshopify\.com'; then
  no "a non-placeholder myshopify domain is present"
else ok "no real store domain committed"; fi

if grep -rEoq '[0-9a-f]{16,}' "$DIR" 2>/dev/null; then
  no "a long hex identifier is present — check it is not a real id/token"
else ok "no long hex identifiers committed"; fi

# --- live smoke (opt-in) ----------------------------------------------------

if [ -n "${SHOPIFY_STORE_DOMAIN:-}${SHOPIFY_SHOP_DOMAIN:-}" ] && [ -n "${SHOPIFY_ACCESS_TOKEN:-}${SHOPIFY_ADMIN_ACCESS_TOKEN:-}" ]; then
  printf '\n-- live smoke --\n'
  if "$SH" raw shop.json >/dev/null 2>&1; then ok "live: raw shop.json"; else no "live: raw shop.json"; fi
  if "$SH" discounts | jq -e '.total_codes >= .discount_nodes' >/dev/null 2>&1; then
    ok "live: total_codes >= discount_nodes (trap 5 holds)"
  else no "live: discounts door"; fi
  if "$SH" health-check | jq -e 'has("warnings")' >/dev/null 2>&1; then
    ok "live: health-check emits warnings array"
  else no "live: health-check"; fi
else
  printf '\n(no creds — live smoke skipped)\n'
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
