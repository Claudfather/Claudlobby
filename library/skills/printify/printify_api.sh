#!/usr/bin/env bash
# printify — direct Printify v1 REST actuator (reads + writes). A sanctioned
# complement to the Printify MCP: the reads the MCP is weak at (real shop/status,
# LOSSLESS descriptions, orders) AND the writes the MCP is buggy/limited at
# (create, migrate) done as clean direct REST. Shares the fleet Printify env
# contract with library/mcp/printify.json (one contract, two tools).
#
# Env contract:
#   PRINTIFY_API_KEY   Printify Personal Access Token / API key (JWT). Also
#                      honors PRINTIFY_API_TOKEN (the name the repo code uses).
#   PRINTIFY_SHOP_ID   Printify shop id (e.g. 1234567).
#
# READ commands (GET, never write):
#   status                 real shop(s) + which is current — no mock fallback
#   products [limit]       list products (id, title, description length, tags)
#   product <product_id>   ONE product with its FULL description (MCP drops this)
#   orders [limit]         recent orders  [contains customer PII — redact]
#   order <order_id>       one order      [contains customer PII — redact]
#   raw <api/path.json>    raw authenticated GET passthrough (escape hatch)
#
# WRITE commands (direct REST; DRAFT-first, never auto-publish):
#   create   PNG -> new DRAFT product   --png <path|url> --title T --blueprint ID --provider ID --price CENTS [--desc D] [--tags a,b] [--position front] [--enable-variants all|id,id] [--dry-run]
#   migrate  product -> new provider    --product <id> --to-provider ID [--to-blueprint ID] [--price-map id:cents,...] [--dry-run]
#   publish  EXPLICIT push to Shopify   --product <id> --yes   (create/migrate never auto-publish; --yes required)
#   help                   this help
#
# Writes leave the product as a DRAFT and print its Printify edit URL; publishing
# is a separate, explicit, human-approved step (the `publish` door, --yes required).
# Surfaces the REAL HTTP error on failure — it never fabricates data.
set -euo pipefail

BASE="https://api.printify.com/v1"
TOKEN="${PRINTIFY_API_TOKEN:-${PRINTIFY_API_KEY:-}}"
SHOP="${PRINTIFY_SHOP_ID:-}"

die() { printf 'printify: %s\n' "$1" >&2; exit "${2:-1}"; }
have_jq() { command -v jq >/dev/null 2>&1; }
need_jq() { have_jq || die "jq is required for this command (JSON build/parse)" 2; }
need_shop() { [ -n "$SHOP" ] || die "PRINTIFY_SHOP_ID is not set" 2; }
is_uint() { [[ $1 =~ ^[0-9]+$ ]]; }

# Authenticated GET. The token is passed via a curl --config file so it never
# appears in the process list (argv) or shell history.
api_get() {
  # Explicit temp-file cleanup, NOT a RETURN trap: a RETURN trap re-fires on the
  # CALLER's return too, where $cfg is out of scope -> "unbound variable" under
  # set -u once a door calls this helper more than once in its own scope.
  local path="$1" cfg out http body rc=0
  [ -n "$TOKEN" ] || die "PRINTIFY_API_KEY (or PRINTIFY_API_TOKEN) is not set" 2
  cfg="$(mktemp)"
  printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$cfg"
  out="$(curl -sS --max-time 30 --config "$cfg" -w $'\n%{http_code}' "$BASE/$path")" || rc=$?
  rm -f "$cfg"
  [ "$rc" -eq 0 ] || die "network error calling $path"
  http="${out##*$'\n'}"; body="${out%$'\n'*}"
  case "$http" in
    2*)  printf '%s' "$body" ;;
    401) die "HTTP 401 — Printify token invalid or expired ($path)" 3 ;;
    404) die "HTTP 404 — not found ($path)" 4 ;;
    *)   die "HTTP $http calling $path: $(printf '%s' "$body" | head -c 200)" 5 ;;
  esac
}

# Authenticated write (POST/PUT/DELETE). Body is written to a temp file and sent
# with --data-binary @file so even a multi-MB base64 image body never lands in
# argv (ARG_MAX-safe). Same token-hiding + status-split conventions as api_get,
# plus 400/422 validation arms for writes.
api_send() {                         # usage: api_send METHOD path [json-body]
  local method="$1" path="$2" body="${3:-}" cfg bodyf out http resp rc=0
  [ -n "$TOKEN" ] || die "PRINTIFY_API_KEY (or PRINTIFY_API_TOKEN) is not set" 2
  cfg="$(mktemp)"; bodyf="$(mktemp)"      # explicit cleanup below (see api_get: no RETURN trap)
  printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$cfg"
  printf 'header = "Content-Type: application/json"\n'   >> "$cfg"
  printf '%s' "$body" > "$bodyf"
  local args=(-sS --max-time 60 --config "$cfg" -X "$method" -w $'\n%{http_code}')
  if [ -n "$body" ]; then args+=(--data-binary @"$bodyf"); fi
  out="$(curl "${args[@]}" "$BASE/$path")" || rc=$?
  rm -f "$cfg" "$bodyf"
  [ "$rc" -eq 0 ] || die "network error calling $path"
  http="${out##*$'\n'}"; resp="${out%$'\n'*}"
  case "$http" in
    2*)      printf '%s' "$resp" ;;
    400|422) die "HTTP $http — validation: $(printf '%s' "$resp" | head -c 300)" 5 ;;
    401)     die "HTTP 401 — Printify token invalid or expired ($path)" 3 ;;
    404)     die "HTTP 404 — not found ($path)" 4 ;;
    *)       die "HTTP $http calling $path: $(printf '%s' "$resp" | head -c 200)" 5 ;;
  esac
}

# Printify dashboard editor deep-link for a product id (deterministic template).
edit_url() { printf 'https://printify.com/app/editor/%s' "$1"; }

# --- test seams --------------------------------------------------------------
# Fixtures short-circuit the ONLY network reads the write doors make, so the
# `--dry-run` payload/coverage assertions in test.sh run hermetically (no creds,
# no network). Unset in production: the doors fetch live from the catalog/shop.
fetch_target_variants() {            # blueprint provider -> catalog variants JSON
  if [ -n "${PRINTIFY_FIXTURE_VARIANTS:-}" ]; then cat "$PRINTIFY_FIXTURE_VARIANTS"
  else api_get "catalog/blueprints/$1/print_providers/$2/variants.json"; fi
}
fetch_source_product() {             # product_id -> shop product JSON
  if [ -n "${PRINTIFY_FIXTURE_PRODUCT:-}" ]; then cat "$PRINTIFY_FIXTURE_PRODUCT"
  else need_shop; api_get "shops/$SHOP/products/$1.json"; fi
}

# --- shared write helpers ----------------------------------------------------

# Upload a PNG (local path -> base64, or URL) and echo the resulting image id.
upload_image() {                     # <path|url> -> image id (stdout)
  local src="$1" name b64f body resp id
  name="$(basename "$src")"
  case "$src" in
    http://*|https://*)
      body="$(jq -n --arg fn "$name" --arg url "$src" '{file_name:$fn, url:$url}')" ;;
    *)
      # -r not just -f: an existing-but-unreadable file would otherwise encode to
      # EMPTY and be sent as contents:"" -- Printify then answers with a confusing
      # 400/422 that never mentions the real cause. errexit does not propagate out
      # of a pipeline inside a function called via command substitution, so capture
      # the rc explicitly (same pattern api_get/api_send use) and refuse empty.
      [ -f "$src" ] || die "create: --png file not found: $src" 4
      [ -r "$src" ] || die "create: --png file not readable: $src" 4
      b64f="$(mktemp)"
      if ! base64 < "$src" | tr -d '\n' > "$b64f"; then
        rm -f "$b64f"; die "create: could not read or encode --png file: $src" 4
      fi
      [ -s "$b64f" ] || { rm -f "$b64f"; die "create: --png encoded to empty content: $src" 4; }
      body="$(jq -n --arg fn "$name" --rawfile c "$b64f" '{file_name:$fn, contents:$c}')"
      rm -f "$b64f" ;;
  esac
  resp="$(api_send POST "uploads/images.json" "$body")"
  id="$(printf '%s' "$resp" | jq -r '.id // empty')"
  [ -n "$id" ] || die "create: upload returned no image id: $(printf '%s' "$resp" | head -c 200)" 5
  printf '%s' "$id"
}

# Report a freshly-created DRAFT: id, edit URL, real mockup src URLs from the
# response (never fabricated), and the explicit publish reminder.
print_created() {                    # product_id response-json
  local pid="$1" resp="$2" mocks
  printf 'Created DRAFT product: %s\n' "$pid"
  printf 'Printify edit URL:     %s\n' "$(edit_url "$pid")"
  printf 'Status: DRAFT — not published. After human approval run: publish --product %s --yes\n' "$pid"
  mocks="$(printf '%s' "$resp" | jq -r '[.images[]? | select(.src) | .src] | .[0:4] | .[]' 2>/dev/null || true)"
  if [ -n "$mocks" ]; then
    printf 'Mockup image(s) (from API response):\n'
    printf '%s\n' "$mocks" | sed 's/^/  /'
  fi
}

# Build the Printify create-product body (shared by create + migrate so the
# draft-product schema lives in exactly one place).
product_draft_body() {               # title desc blueprint provider variants print_areas tags
  jq -n --arg t "$1" --arg d "$2" --argjson bp "$3" --argjson pp "$4" \
    --argjson v "$5" --argjson pa "$6" --argjson tags "$7" \
    '{title:$t, description:$d, blueprint_id:$bp, print_provider_id:$pp, variants:$v, print_areas:$pa, tags:$tags}'
}

# POST a product body as a DRAFT and report it; die on a missing id. The label
# ("create"/"migrate") prefixes the error so the caller's context is preserved.
submit_draft() {                     # body label
  local body="$1" label="$2" resp pid
  resp="$(api_send POST "shops/$SHOP/products.json" "$body")"
  pid="$(printf '%s' "$resp" | jq -r '.id // empty')"
  [ -n "$pid" ] || die "$label: no product id in response: $(printf '%s' "$resp" | head -c 200)" 5
  print_created "$pid" "$resp"
}

# --- doors: create -----------------------------------------------------------
door_create() {
  need_jq
  local png="" title="" blueprint="" provider="" price="" desc="" tags="" \
        position="front" envars="all" dry=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --png)             png="${2:?}";       shift 2 ;;
      --title)           title="${2:?}";     shift 2 ;;
      --blueprint)       blueprint="${2:?}"; shift 2 ;;
      --provider)        provider="${2:?}";  shift 2 ;;
      --price)           price="${2:?}";     shift 2 ;;
      --desc)            desc="${2:?}";       shift 2 ;;
      --tags)            tags="${2:?}";       shift 2 ;;
      --position)        position="${2:?}";  shift 2 ;;
      --enable-variants) envars="${2:?}";     shift 2 ;;
      --dry-run)         dry=1;               shift ;;
      *) die "create: unknown arg '$1'" 64 ;;
    esac
  done
  if [ -z "$png" ] || [ -z "$title" ] || [ -z "$blueprint" ] || [ -z "$provider" ] || [ -z "$price" ]; then
    die "create: --png --title --blueprint --provider --price are all required" 64
  fi
  is_uint "$blueprint" || die "create: --blueprint must be an integer" 64
  is_uint "$provider"  || die "create: --provider must be an integer" 64
  is_uint "$price"     || die "create: --price must be an integer (cents)" 64
  need_shop

  # 1. variants from the target catalog (fixture-injectable)
  local variants_raw variants_json enabled_ids
  variants_raw="$(fetch_target_variants "$blueprint" "$provider")"
  # Bind the variant id BEFORE piping into $ids: inside index(...) the input is
  # $ids (an array), so a bare .id there indexes the array, not the variant, and
  # jq dies with "Cannot index array with string". Only reachable when $ids is
  # non-null -- jq short-circuits `or`, so the default --enable-variants all path
  # never evaluated it. Same bind form the migrate door already uses.
  variants_json="$(printf '%s' "$variants_raw" | jq -c --argjson p "$price" --arg sel "$envars" \
    '(if $sel == "all" then null else ($sel|split(",")|map(tonumber)) end) as $ids
     | [.variants[] | .id as $vid
        | {id, price:$p, is_enabled: ($ids == null or (($ids|index($vid)) != null))}]')"
  [ "$(printf '%s' "$variants_json" | jq 'length')" -gt 0 ] \
    || die "create: no catalog variants for blueprint $blueprint / provider $provider" 5
  enabled_ids="$(printf '%s' "$variants_json" | jq -c '[.[] | select(.is_enabled) | .id]')"
  [ "$(printf '%s' "$enabled_ids" | jq 'length')" -gt 0 ] \
    || die "create: --enable-variants matched no catalog variant ids" 64

  # 2. tags
  local tags_json='[]'
  [ -n "$tags" ] && tags_json="$(jq -c -n --arg t "$tags" '$t|split(",")')"

  # 3. image id — real upload in live mode; placeholder in dry-run (no write)
  local image_id="UPLOAD_PENDING"
  [ "$dry" -eq 1 ] || image_id="$(upload_image "$png")"

  # 4. print_areas (default geometry, single placement)
  local print_areas
  print_areas="$(jq -c -n --argjson vids "$enabled_ids" --arg pos "$position" --arg img "$image_id" \
    '[{variant_ids:$vids, placeholders:[{position:$pos, images:[{id:$img, x:0.5, y:0.5, scale:1, angle:0}]}]}]')"

  # 5. product body (shared builder)
  local body
  body="$(product_draft_body "$title" "$desc" "$blueprint" "$provider" "$variants_json" "$print_areas" "$tags_json")"

  if [ "$dry" -eq 1 ]; then
    printf '# DRY-RUN — no product created. Request that WOULD be sent:\n'
    printf '# POST %s/shops/%s/products.json\n' "$BASE" "${SHOP:-<shop>}"
    printf '# (image NOT uploaded in dry-run; print_areas[].images[].id = "%s")\n' "$image_id"
    printf '%s\n' "$body"
    return 0
  fi

  # 6. create (DRAFT — never publish)
  submit_draft "$body" "create"
}

# --- doors: migrate ----------------------------------------------------------
# Print the enumerated coverage delta: retained (matched by shared variant id)
# vs dropped (no target variant). The drop decision belongs to merchandising —
# this door reports, a human decides.
print_coverage() {                   # product src_bp tobp toprov matched-json dropped-json
  local product="$1" sbp="$2" tbp="$3" tprov="$4" matched="$5" dropped="$6" nm nd
  nm="$(printf '%s' "$matched" | jq 'length')"
  nd="$(printf '%s' "$dropped" | jq 'length')"
  printf '=== migrate coverage report ===\n'
  printf 'source product:  %s  (blueprint %s)\n' "$product" "$sbp"
  printf 'target:          blueprint %s / provider %s\n' "$tbp" "$tprov"
  printf 'retained (matched by shared variant id): %s\n' "$nm"
  printf '%s' "$matched" | jq -r '.[] | "  [+] \(.id)  \(.title // "")  price=\(.price)"'
  printf 'dropped  (no target variant — COVERAGE LOSS): %s\n' "$nd"
  printf '%s' "$dropped" | jq -r '.[] | "  [-] \(.id)  \(.title // "")  DROPPED"'
  if [ "$sbp" != "$tbp" ]; then
    printf '#\n'
    printf '# !! CROSS-BLUEPRINT MIGRATE (%s -> %s) — TREAT THIS REPORT AS UNVERIFIED.\n' "$sbp" "$tbp"
    printf '# Matching is by shared numeric variant id. That premise is verified only\n'
    printf '# ACROSS PROVIDERS WITHIN ONE BLUEPRINT. Across blueprints a coincidental id\n'
    printf '# collision reads as a legitimate match, so a retained line above may be a\n'
    printf '# DIFFERENT product attribute. Verify each retained id in the Printify\n'
    printf '# catalog before accepting this migration.\n'
  fi
  printf '# Coverage loss is a MERCHANDISING decision: the door reports, a human decides.\n'
}

door_migrate() {
  need_jq
  local product="" toprov="" tobp="" pricemap="" dry=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --product)      product="${2:?}"; shift 2 ;;
      --to-provider)  toprov="${2:?}";  shift 2 ;;
      --to-blueprint) tobp="${2:?}";    shift 2 ;;
      --price-map)    pricemap="${2:?}"; shift 2 ;;
      --dry-run)      dry=1;             shift ;;
      *) die "migrate: unknown arg '$1'" 64 ;;
    esac
  done
  if [ -z "$product" ] || [ -z "$toprov" ]; then
    die "migrate: --product and --to-provider are required" 64
  fi
  is_uint "$toprov" || die "migrate: --to-provider must be an integer" 64

  # 1. source product (fixture-injectable)
  local src src_bp src_title src_desc src_tags src_variants src_pareas
  src="$(fetch_source_product "$product")"
  src_bp="$(printf '%s' "$src" | jq -r '.blueprint_id')"
  [ -n "$tobp" ] || tobp="$src_bp"
  is_uint "$tobp" || die "migrate: --to-blueprint must be an integer" 64
  # Variant-id matching is verified only ACROSS PROVIDERS WITHIN ONE BLUEPRINT.
  # Warn loudly rather than refuse: a cross-blueprint migrate is legitimate, but
  # the human must not read its coverage report as verified.
  if [ "$tobp" != "$src_bp" ]; then
    printf 'printify: WARNING — cross-blueprint migrate (blueprint %s -> %s). Variant-id\n' "$src_bp" "$tobp" >&2
    printf 'printify: matching is UNVERIFIED across blueprints; see the caveat in the report.\n' >&2
  fi
  src_title="$(printf '%s' "$src" | jq -r '.title // ""')"
  src_desc="$(printf '%s' "$src" | jq -r '.description // ""')"
  src_tags="$(printf '%s' "$src" | jq -c '.tags // []')"
  src_variants="$(printf '%s' "$src" | jq -c '[.variants[] | {id, price, is_enabled, title}]')"
  src_pareas="$(printf '%s' "$src" | jq -c '.print_areas // []')"

  # 2. target variant universe (fixture-injectable)
  local tgt tgt_ids
  tgt="$(fetch_target_variants "$tobp" "$toprov")"
  tgt_ids="$(printf '%s' "$tgt" | jq -c '[.variants[].id]')"

  # 3. map source -> target by shared variant id
  local matched dropped
  matched="$(printf '%s' "$src_variants" | jq -c --argjson ids "$tgt_ids" \
    '[.[] | select((.id) as $i | $ids | index($i))]')"
  dropped="$(printf '%s' "$src_variants" | jq -c --argjson ids "$tgt_ids" \
    '[.[] | select((.id) as $i | ($ids | index($i)) | not)]')"

  # optional per-id price override (--price-map id:cents,...)
  if [ -n "$pricemap" ]; then
    matched="$(printf '%s' "$matched" | jq -c --arg pm "$pricemap" \
      '($pm | split(",") | map(split(":") | {key:.[0], value:(.[1]|tonumber)}) | from_entries) as $m
       | [.[] | .price = (($m[(.id|tostring)]) // .price)]')"
  fi

  # 4. filter source print_areas to matched variants only (carries source image ids)
  local matched_ids new_pareas
  matched_ids="$(printf '%s' "$matched" | jq -c '[.[].id]')"
  new_pareas="$(printf '%s' "$src_pareas" | jq -c --argjson keep "$matched_ids" \
    '[.[] | .variant_ids |= map(select(. as $v | $keep | index($v))) | select((.variant_ids|length) > 0)]')"

  # 5. coverage report (always)
  print_coverage "$product" "$src_bp" "$tobp" "$toprov" "$matched" "$dropped"

  # 6. build create body for the target (variants carry only id/price/is_enabled)
  local create_variants body
  create_variants="$(printf '%s' "$matched" | jq -c '[.[] | {id, price, is_enabled}]')"
  body="$(product_draft_body "$src_title" "$src_desc" "$tobp" "$toprov" "$create_variants" "$new_pareas" "$src_tags")"

  if [ "$dry" -eq 1 ]; then
    printf '\n# DRY-RUN — no product created. Request that WOULD be sent:\n'
    printf '# POST %s/shops/%s/products.json\n' "$BASE" "${SHOP:-<shop>}"
    printf '# Source product %s is left UNTOUCHED (migrate never deletes/unpublishes the source).\n' "$product"
    printf '%s\n' "$body"
    return 0
  fi

  need_shop
  [ "$(printf '%s' "$matched" | jq 'length')" -gt 0 ] \
    || die "migrate: 0 variants matched target — nothing to create" 5
  printf '\n'
  submit_draft "$body" "migrate"
  printf 'Source product %s left UNTOUCHED — retire it separately, human-gated.\n' "$product"
}

# --- doors: publish (EXPLICIT, human-gated) ----------------------------------
door_publish() {
  local product="" yes=0 dry=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --product) product="${2:?}"; shift 2 ;;
      --yes)     yes=1;             shift ;;
      --dry-run) dry=1;             shift ;;
      *) die "publish: unknown arg '$1'" 64 ;;
    esac
  done
  [ -n "$product" ] || die "publish: --product <id> is required" 64
  need_shop
  local body='{"title":true,"description":true,"images":true,"variants":true,"tags":true,"keyFeatures":true,"shipping_template":true}'
  if [ "$dry" -eq 1 ]; then
    printf '# DRY-RUN — would publish product %s to Shopify:\n' "$product"
    printf '# POST %s/shops/%s/products/%s/publish.json\n' "$BASE" "$SHOP" "$product"
    printf '%s\n' "$body"
    return 0
  fi
  [ "$yes" -eq 1 ] \
    || die "publish is human-gated: pass --yes to confirm publishing product $product (create/migrate never auto-publish)" 64
  api_send POST "shops/$SHOP/products/$product/publish.json" "$body" >/dev/null
  printf 'Published product %s to Shopify.\n' "$product"
  printf 'Printify product: %s\n' "$(edit_url "$product")"
}

# --- dispatcher --------------------------------------------------------------
cmd="${1:-status}"
case "$cmd" in
  status)
    # The read the MCP faked with 'Mock Shop 1'. Real shop list, no fallback.
    out="$(api_get "shops.json")"
    if have_jq; then
      printf '%s' "$out" | jq --arg cur "$SHOP" '{
        current_shop_id: $cur,
        shops: [ .[] | { id, title, sales_channel, current: ((.id|tostring) == $cur) } ]
      }'
    else printf '%s\n' "$out"; fi
    ;;
  products)
    need_shop; limit="${2:-10}"
    out="$(api_get "shops/$SHOP/products.json?limit=$limit")"
    if have_jq; then
      printf '%s' "$out" | jq '{
        total: (.data | length),
        products: [ .data[] | { id, title, description_len: ((.description // "") | length), visible, tags } ]
      }'
    else printf '%s\n' "$out"; fi
    ;;
  product)
    need_shop; id="${2:?usage: printify_api.sh product <product_id>}"
    # LOSSLESS: returns the full description — the field the MCP read drops.
    out="$(api_get "shops/$SHOP/products/$id.json")"
    if have_jq; then
      printf '%s' "$out" | jq '{ id, title, description, tags, visible, variants: (.variants | length) }'
    else printf '%s\n' "$out"; fi
    ;;
  orders)
    need_shop; limit="${2:-5}"
    # Orders carry real customer PII (name/email/address) — redact before sharing.
    out="$(api_get "shops/$SHOP/orders.json?limit=$limit")"
    if have_jq; then
      printf '%s' "$out" | jq '{
        total: (.data | length),
        orders: [ .data[] | { id, shop_order_label: .metadata.shop_order_label, status, created_at, line_items: (.line_items | length) } ]
      }'
    else printf '%s\n' "$out"; fi
    ;;
  order)
    need_shop; id="${2:?usage: printify_api.sh order <order_id>}"
    # Full order incl. customer PII — redact before sharing in chat/logs.
    api_get "shops/$SHOP/orders/$id.json"
    ;;
  raw)
    path="${2:?usage: printify_api.sh raw <api/path.json>}"; api_get "$path"
    ;;
  create)   shift; door_create  "$@" ;;
  migrate)  shift; door_migrate "$@" ;;
  publish)  shift; door_publish "$@" ;;
  -h|--help|help)
    # Print the header comment block (everything above `set -euo pipefail`).
    sed -n '2,/^set -euo pipefail/p' "$0" | sed '/^set -euo pipefail/d; s/^# \{0,1\}//'
    ;;
  *)
    die "unknown command '$cmd' (try: status|products|product|orders|order|raw|create|migrate|publish|help)" 64
    ;;
esac
