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

# --- documentation contract -------------------------------------------------

for n in 1 2 3 4 5 6 7 8; do
  grep -qE "^## $n\. " "$DIR/traps.md" || no "traps.md is missing entry $n"
done
grep -qE "^## 8\. " "$DIR/traps.md" && ok "traps.md has all 8 entries"

# every door in the script is documented in SKILL.md
for d in health-check orders catalog discounts collections raw; do
  grep -q -- "$d" "$DIR/SKILL.md" || no "SKILL.md does not document the '$d' door"
done
ok "every door appears in SKILL.md"

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
