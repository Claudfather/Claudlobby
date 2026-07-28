#!/usr/bin/env bash
# Tests for the printify helper script.
# Hermetic checks always run (no creds/network) — including write-door --dry-run
# payload/coverage assertions driven by fixtures/. A live smoke runs only when
# PRINTIFY_API_KEY + PRINTIFY_SHOP_ID are present in the env.
#
# Run:  bash test.sh
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SH="$DIR/printify_api.sh"
FIX="$DIR/fixtures"
pass=0; fail=0
ok() { printf 'ok   - %s\n' "$1"; pass=$((pass + 1)); }
no() { printf 'FAIL - %s\n' "$1"; fail=$((fail + 1)); }

# 1. help works with NO creds and exits 0
if out=$(env -u PRINTIFY_API_KEY -u PRINTIFY_API_TOKEN "$SH" help 2>&1) \
   && printf '%s' "$out" | grep -q "printify"; then
  ok "help works without creds"
else no "help without creds"; fi

# 2. unknown command exits non-zero
if env -u PRINTIFY_API_KEY "$SH" bogus >/dev/null 2>&1; then
  no "unknown command should exit non-zero"
else ok "unknown command exits non-zero"; fi

# 3. missing token surfaces a clear error, exit 2 (never fabricates)
env -u PRINTIFY_API_KEY -u PRINTIFY_API_TOKEN "$SH" status >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 2 ]; then ok "missing token exits 2"; else no "missing token exit code ($rc != 2)"; fi

# 4. shop-scoped command with no shop id exits non-zero
if env -u PRINTIFY_SHOP_ID PRINTIFY_API_KEY="dummy" "$SH" products >/dev/null 2>&1; then
  no "missing shop should fail"
else ok "missing shop exits non-zero"; fi

# --- write-door dry-run (hermetic via fixtures; dummy creds, no network) ---
# 5. create --dry-run builds the exact POST body from the catalog fixture
body=$(PRINTIFY_FIXTURE_VARIANTS="$FIX/choice_variants.json" \
       PRINTIFY_API_KEY=dummy PRINTIFY_SHOP_ID=1234567 \
       "$SH" create --png https://example.com/art.png --title "T" \
       --blueprint 400 --provider 99 --price 500 --dry-run 2>/dev/null \
       | sed -n '/^{/,/^}/p')
if printf '%s' "$body" | jq -e '
      .blueprint_id==400 and .print_provider_id==99
      and (.variants|length)==4
      and ([.variants[].price]|unique)==[500]
      and (.print_areas[0].placeholders[0].images[0].id)=="UPLOAD_PENDING"' >/dev/null 2>&1; then
  ok "create --dry-run builds POST body (bp 400, pp 99, 4 variants @500, no upload)"
else no "create --dry-run payload"; fi

# 6. create --enable-variants selects a subset
body=$(PRINTIFY_FIXTURE_VARIANTS="$FIX/choice_variants.json" \
       PRINTIFY_API_KEY=dummy PRINTIFY_SHOP_ID=1234567 \
       "$SH" create --png https://example.com/art.png --title "T" \
       --blueprint 400 --provider 99 --price 500 --enable-variants 45748,45752 --dry-run 2>/dev/null \
       | sed -n '/^{/,/^}/p')
if printf '%s' "$body" | jq -e '([.variants[]|select(.is_enabled)|.id]|sort)==[45748,45752]' >/dev/null 2>&1; then
  ok "create --enable-variants enables only the named ids"
else no "create --enable-variants subset"; fi

# 7. migrate --dry-run coverage report: 4 retained (White) + 4 dropped (Transparent)
cov=$(PRINTIFY_FIXTURE_PRODUCT="$FIX/spoke_sticker_product.json" \
      PRINTIFY_FIXTURE_VARIANTS="$FIX/choice_variants.json" \
      PRINTIFY_API_KEY=dummy PRINTIFY_SHOP_ID=1234567 \
      "$SH" migrate --product SPOKE_STICKER_FIXTURE --to-provider 99 --dry-run 2>/dev/null)
if [ "$(printf '%s' "$cov" | grep -c '\[+\]')" -eq 4 ] && [ "$(printf '%s' "$cov" | grep -c '\[-\]')" -eq 4 ]; then
  ok "migrate --dry-run coverage: 4 retained + 4 dropped"
else no "migrate --dry-run coverage counts (got +$(printf '%s' "$cov" | grep -c '\[+\]') -$(printf '%s' "$cov" | grep -c '\[-\]'))"; fi

# 8. migrate --dry-run maps the right ids: White retained, Transparent dropped
if printf '%s' "$cov" | grep -q '\[+\] 45748' \
   && printf '%s' "$cov" | grep -q '\[+\] 45754' \
   && printf '%s' "$cov" | grep -q '\[-\] 45747' \
   && printf '%s' "$cov" | grep -q '\[-\] 45753'; then
  ok "migrate --dry-run: White 45748/45754 retained, Transparent 45747/45753 dropped"
else no "migrate --dry-run id mapping"; fi

# 9. migrate --dry-run create body carries only the 4 matched White variants
mbody=$(printf '%s' "$cov" | sed -n '/^{/,/^}/p')
if printf '%s' "$mbody" | jq -e '
      .print_provider_id==99 and (.variants|length)==4
      and ([.variants[].id]|sort)==[45748,45750,45752,45754]' >/dev/null 2>&1; then
  ok "migrate --dry-run body: 4 matched White variants, provider 99"
else no "migrate --dry-run body variants"; fi

# 10. create missing required args exits non-zero
if env PRINTIFY_API_KEY=dummy PRINTIFY_SHOP_ID=1 "$SH" create --title "x" >/dev/null 2>&1; then
  no "create without required args should fail"
else ok "create without required args exits non-zero"; fi

# 11. publish is human-gated: refuses without --yes
if env PRINTIFY_API_KEY=dummy PRINTIFY_SHOP_ID=1 "$SH" publish --product ABC >/dev/null 2>&1; then
  no "publish without --yes should refuse"
else ok "publish without --yes refuses (human-gated)"; fi

# 12. publish --dry-run prints the publish body without sending
pbody=$(env PRINTIFY_API_KEY=dummy PRINTIFY_SHOP_ID=1 "$SH" publish --product ABC --dry-run 2>/dev/null | sed -n '/^{/,/^}/p')
if printf '%s' "$pbody" | jq -e '.title==true and .images==true' >/dev/null 2>&1; then
  ok "publish --dry-run prints publish body"
else no "publish --dry-run"; fi

# --- live smoke (only with real creds) ---
TOKEN="${PRINTIFY_API_TOKEN:-${PRINTIFY_API_KEY:-}}"
if [ -n "$TOKEN" ] && [ -n "${PRINTIFY_SHOP_ID:-}" ]; then
  if "$SH" status | jq -e '.shops[0].id' >/dev/null 2>&1; then
    ok "live: status returns a real shop"; else no "live: status"; fi
  pid=$("$SH" products 1 | jq -r '.products[0].id // empty' 2>/dev/null)
  if [ -n "$pid" ]; then ok "live: products returns items"; else no "live: products"; fi
  if [ -n "$pid" ] && "$SH" product "$pid" | jq -e '.description | type == "string" and length > 0' >/dev/null 2>&1; then
    ok "live: product returns a lossless (non-empty) description"; else no "live: product description"; fi
  if "$SH" orders 1 | jq -e 'has("orders")' >/dev/null 2>&1; then
    ok "live: orders returns a list"; else no "live: orders"; fi
  # regression: a write door makes >1 api_get call in its own scope; the fetch
  # helpers must not leak a RETURN trap (would re-fire as "cfg: unbound" under
  # set -u). Migrate the sampled product to its OWN provider (all variants
  # retained) — exercises fetch_source_product + fetch_target_variants back-to-back.
  prov=$("$SH" raw "shops/$PRINTIFY_SHOP_ID/products/$pid.json" 2>/dev/null | jq -r '.print_provider_id // empty')
  if [ -n "$pid" ] && [ -n "$prov" ] && "$SH" migrate --product "$pid" --to-provider "$prov" --dry-run >/dev/null 2>&1; then
    ok "live: migrate --dry-run multi-fetch runs (no RETURN-trap/unbound crash)"; else no "live: migrate multi-fetch regression"; fi
else
  printf 'skip - live smoke (no PRINTIFY creds in env)\n'
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
