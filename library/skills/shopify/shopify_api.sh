#!/usr/bin/env bash
# shopify — direct Shopify Admin API actuator, built around the fields that lie.
#
# The API calls here are trivial; anyone can curl Shopify. The value is that each
# door answers the question people ACTUALLY ask using the field that is sound,
# rather than the obvious field that silently gives a wrong answer. Every such
# choice is documented and measured in traps.md — read it before extending this.
#
# Env contract (parameterised; nothing store-specific is baked in):
#   SHOPIFY_STORE_DOMAIN    myshop.myshopify.com  (also honors SHOPIFY_SHOP_DOMAIN)
#   SHOPIFY_ACCESS_TOKEN    Admin API access token (also honors
#                           SHOPIFY_ADMIN_ACCESS_TOKEN, the name most apps export)
#   SHOPIFY_API_VERSION     optional, defaults below
#
# Doors:
#   health-check              store-wide sanity sweep over the trap-aware checks
#   orders [limit]            recent orders  [PII — redact before sharing]
#   catalog [--unfulfillable] product inventory truth, not the placeholder fields
#   discounts                 CODE count, not the node count everyone reports
#   collections [handle]      membership WITH full pagination (never a bare first:)
#   webhooks                  registered topics AND the critical ones that are MISSING
#   copy                      description defects, including the ones invisible in JSON
#   redirects                 301 destinations that 404, and sources that came back
#   orphans                   sitemap entries nothing links to  (triage, not a delete list)
#   consent                   marketing-consent tally, fully counted or bound stated
#   raw <path>                authenticated GET passthrough (escape hatch)
#   help
#
# Read-only, enforced in code rather than asserted in this comment: the HTTP
# method is allowlisted, POST is reachable only for the GraphQL endpoint, and a
# GraphQL document carrying a mutation or subscription is refused. The guards are
# assert_read_only and assert_query_only below; their comments carry the why.
#
# There is no write door, deliberately — a status flip is destructive in ways
# traps.md 7 explains, and belongs behind a human, not behind a convenience
# wrapper.
#
# Surfaces the REAL HTTP error on failure. Never fabricates data.
set -euo pipefail

DOMAIN="${SHOPIFY_STORE_DOMAIN:-${SHOPIFY_SHOP_DOMAIN:-}}"
TOKEN="${SHOPIFY_ACCESS_TOKEN:-${SHOPIFY_ADMIN_ACCESS_TOKEN:-}}"
VERSION="${SHOPIFY_API_VERSION:-2026-04}"

die() { printf 'shopify: %s\n' "$1" >&2; exit "${2:-1}"; }
have_jq() { command -v jq >/dev/null 2>&1; }
need_jq() { have_jq || die "jq is required for this command (JSON parse)" 2; }
need_env() {
  [ -n "$DOMAIN" ] || die "SHOPIFY_STORE_DOMAIN is not set" 2
  [ -n "$TOKEN" ]  || die "SHOPIFY_ACCESS_TOKEN (or SHOPIFY_ADMIN_ACCESS_TOKEN) is not set" 2
}

# Authenticated request. The token goes through a curl --config file so it never
# appears in the process list (argv) or in shell history.
#
# Explicit temp-file cleanup, NOT a RETURN trap: a RETURN trap re-fires on the
# CALLER's return too, where $cfg is out of scope -> "unbound variable" under
# set -u as soon as a door calls this more than once in its own scope. Same
# footgun the printify actuator documents.
# ---------------------------------------------------------------------------
# The read-only guarantee, as code (#998 review).
#
# It previously lived in a header comment claiming "every door issues GET only",
# which was false about this very file: gql() POSTs, so two doors already did
# not. A safety claim that exists only as a comment is worse than no claim,
# because it gets trusted. Two independent findings landed on it — a wired REST
# write door walked past both the bash and pytest guards, because those grepped
# the source for a literal "-X DELETE" that this file never spells (it passes
# -X "$method"); and a GraphQL mutation rides POST, so no method check alone can
# see one.
#
# Hence two guards, not one. assert_read_only is NOT the load-bearing half:
# without assert_query_only, POST-to-GraphQL is a single open door through which
# the entire store can be mutated.
#
# Exit 7 is reserved for a refusal so a test can tell "the guard stopped it"
# apart from "it failed for some other reason". A guard indistinguishable from a
# network error is a guard nobody can prove.
# ---------------------------------------------------------------------------
GUARD_RC=7

assert_read_only() {
  local method="$1" url="$2"
  case "$method" in
    GET) ;;
    POST)
      # Reachable only for GraphQL. A REST write is a non-GET verb at a REST
      # path; pinning POST to graphql.json means a wired REST write door cannot
      # reach the network even if someone adds one.
      case "${url%%\?*}" in
        */graphql.json) ;;
        *) die "refusing POST to a non-GraphQL endpoint ($url). This actuator is read-only; see the header." "$GUARD_RC" ;;
      esac
      ;;
    *)
      die "refusing HTTP method $method. This actuator is read-only: GET, or POST to graphql.json only." "$GUARD_RC"
      ;;
  esac
}

# A GraphQL mutation is a POST with a query-shaped envelope, so assert_read_only
# cannot see it. Only the operation type can, which is why this exists.
#
# Fails CLOSED and over-rejects on purpose: a document is refused if the words
# mutation or subscription appear as bare tokens ANYWHERE in it, and it must
# begin with query, {, or fragment. String literals and # comments are blanked
# first so neither can smuggle a token in or trip the scan from inside a value.
#
# STATED BOUND: a legitimate query selecting a FIELD named "mutation" or
# "subscription" would be refused. Shopify's Admin QueryRoot exposes no such
# field, and for a read-only guarantee the safe direction is to reject a valid
# query rather than admit an invalid one. Checking every operation rather than
# only the first is deliberate — a document may carry several, and
# 'query { a } mutation { b }' is one POST.
assert_query_only() {
  local doc="$1" stripped head
  stripped="$(printf '%s' "$doc" | sed -e 's/"[^"]*"/""/g' -e 's/#[^\n]*//g')"

  if printf '%s' "$stripped" | grep -qiE '(^|[^A-Za-z0-9_])(mutation|subscription)([^A-Za-z0-9_]|$)'; then
    die "refusing a GraphQL document containing a mutation or subscription. This actuator is read-only." "$GUARD_RC"
  fi

  head="$(printf '%s' "$stripped" | tr '\n' ' ' | sed -e 's/^[[:space:]]*//')"
  case "$head" in
    query[[:space:]]*|query\{*|'{'*|fragment[[:space:]]*) ;;
    *) die "refusing a GraphQL document that does not begin with a query, { or fragment. This actuator is read-only." "$GUARD_RC" ;;
  esac
}

#
# A 4th argument names a file to dump response headers into. It is requested
# through the curl CONFIG FILE (dump-header) rather than as another curl
# invocation, deliberately: every request in this script must go through this one
# call site, or the read-only guarantee stops being auditable by looking at one
# function. test.sh counts curl invocations for exactly that reason.
api() {
  local method="$1" url="$2" body="${3:-}" hdr_out="${4:-}" cfg out http payload rc=0
  # BEFORE need_env, deliberately: a forbidden method is refused whether or not
  # credentials happen to be present, and the refusal stays provable without them.
  assert_read_only "$method" "$url"
  need_env
  cfg="$(mktemp)"
  {
    printf 'header = "X-Shopify-Access-Token: %s"\n' "$TOKEN"
    printf 'header = "Content-Type: application/json"\n'
    [ -n "$hdr_out" ] && printf 'dump-header = "%s"\n' "$hdr_out"
  } > "$cfg"
  if [ -n "$body" ]; then
    out="$(curl -sS --max-time 60 --config "$cfg" -X "$method" -d "$body" -w $'\n%{http_code}' "$url")" || rc=$?
  else
    out="$(curl -sS --max-time 60 --config "$cfg" -X "$method" -w $'\n%{http_code}' "$url")" || rc=$?
  fi
  rm -f "$cfg"
  [ "$rc" -eq 0 ] || die "network error calling $url"
  http="${out##*$'\n'}"; payload="${out%$'\n'*}"
  case "$http" in
    2*) printf '%s' "$payload" ;;
    401|403) die "HTTP $http — token rejected or missing scope. Response: $(printf '%s' "$payload" | head -c 200)" 3 ;;
    404) die "HTTP 404 — not found: $url" 4 ;;
    429) die "HTTP 429 — rate limited. Back off and retry; do not tight-loop." 5 ;;
    *)   die "HTTP $http — $(printf '%s' "$payload" | head -c 300)" 6 ;;
  esac
}

rest() { api GET "https://$DOMAIN/admin/api/$VERSION/$1"; }

# Cursor-paginated REST, following the Link header to exhaustion.
#
# This exists because REST pagination is INVISIBLE in the body: page 1 of a
# 15,000-row collection is a well-formed 200 that looks exactly like a complete
# answer. The only signal that more exists is a `Link: <...>; rel="next"` header,
# which api() throws away. Every "we have N customers" figure derived from a
# single customers.json call is page 1 of the truth (traps.md 12).
#
# Emits one JSON object per page on stdout; the caller reduces. Prints the page
# count to stderr so a caller that stops early can DISCLOSE the bound rather
# than silently reporting a sample as a total.
rest_paged() {
  local path="$1" max_pages="${2:-0}" url hdr pages=0 next
  url="https://$DOMAIN/admin/api/$VERSION/$path"
  while [ -n "$url" ]; do
    pages=$((pages + 1))
    hdr="$(mktemp)"
    # Through api(), so the method allowlist and the error mapping apply here
    # exactly as they do everywhere else.
    printf '%s\n' "$(api GET "$url" "" "$hdr")"
    # Only rel="next" advances. A Link header commonly carries rel="previous"
    # too, and matching the URL without checking the rel walks backwards forever.
    next="$(tr -d '\r' < "$hdr" | grep -i '^link:' | tr ',' '\n' \
            | grep 'rel="next"' | sed -e 's/.*<//' -e 's/>.*//' | head -1)"
    rm -f "$hdr"
    url="$next"
    if [ "$max_pages" -gt 0 ] && [ "$pages" -ge "$max_pages" ] && [ -n "$url" ]; then
      printf 'shopify: STOPPED at %s pages with more remaining — result is a SAMPLE, not a total\n' "$pages" >&2
      printf '%s\n' '{"__truncated__":true}'
      break
    fi
  done
  printf 'shopify: fetched %s page(s)\n' "$pages" >&2
}

# GraphQL errors arrive inside a 200, so they must be checked separately —
# treating any 200 as success is how a failed query becomes "zero results".
gql() {
  local q="$1" out
  need_jq
  assert_query_only "$q"
  out="$(api POST "https://$DOMAIN/admin/api/$VERSION/graphql.json" "$(jq -cn --arg q "$q" '{query:$q}')")"
  if printf '%s' "$out" | jq -e '.errors' >/dev/null 2>&1; then
    die "GraphQL error: $(printf '%s' "$out" | jq -c '.errors')" 6
  fi
  printf '%s' "$out"
}

# ---------------------------------------------------------------------------
# catalog — inventory truth
#
# traps.md 1 + 2: fulfillment_service is "manual" on everything, and
# inventory_quantity is not authoritative unless inventory_management says the
# variant is tracked. So this reports the DISCRIMINATOR, not the placeholder.
# ---------------------------------------------------------------------------
door_catalog() {
  need_jq
  local unfulfillable=0 ids_file=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --unfulfillable) unfulfillable=1; shift ;;
      --fulfiller-ids) ids_file="${2:?--fulfiller-ids needs a file of product ids, one per line}"; shift 2 ;;
      *) die "catalog: unknown option $1" 2 ;;
    esac
  done

  local data; data="$(rest 'products.json?limit=250&fields=id,title,handle,status,product_type,tags,variants')"

  if [ "$unfulfillable" -eq 1 ]; then
    [ -n "$ids_file" ] || die "catalog --unfulfillable needs --fulfiller-ids <file>: the set of Shopify product ids your fulfiller has created. traps.md 3 explains why Shopify alone cannot answer this." 2
    [ -r "$ids_file" ] || die "cannot read $ids_file" 2
    printf '%s' "$data" | jq --rawfile ids "$ids_file" '
      ($ids | split("\n") | map(select(length>0)) | map(tostring)) as $known |
      [ .products[] | select(.status == "active") | . as $p
        | select(($known | index($p.id|tostring)) == null)
        | {id, handle, title, product_type} ] as $orphans |
      { active_products: [.products[] | select(.status=="active")] | length,
        linked: (([.products[] | select(.status=="active")] | length) - ($orphans|length)),
        not_in_fulfiller_set: $orphans|length,
        note: "Triage filter, not a verdict — a product may be legitimately hand-fulfilled, digital, or a service. See traps.md 3.",
        candidates: $orphans }'
    return
  fi

  printf '%s' "$data" | jq '
    [.products[].variants[]] as $v |
    { products: (.products|length),
      variants: ($v|length),
      fulfillment_service_values: ($v|map(.fulfillment_service)|unique),
      fulfillment_service_note: "If this is [\"manual\"] it tells you nothing about the supplier — traps.md 1.",
      tracked_variants:   ($v|map(select(.inventory_management != null))|length),
      untracked_variants: ($v|map(select(.inventory_management == null))|length),
      untracked_with_nonzero_quantity: ($v|map(select(.inventory_management == null and .inventory_quantity != 0))|length),
      quantity_note: "For untracked variants inventory_quantity is NOT authoritative and is often non-zero anyway. Purchasability is availableForSale (Storefront) — traps.md 2." }'
}

# ---------------------------------------------------------------------------
# discounts — count CODES, not nodes (traps.md 5)
# ---------------------------------------------------------------------------
door_discounts() {
  need_jq
  gql 'query { codeDiscountNodes(first: 250) { pageInfo { hasNextPage }
         edges { node { codeDiscount { __typename
           ... on DiscountCodeBasic        { status codesCount { count } }
           ... on DiscountCodeBxgy         { status codesCount { count } }
           ... on DiscountCodeFreeShipping { status codesCount { count } } } } } } }' \
  | jq '[.data.codeDiscountNodes.edges[].node.codeDiscount] as $d | {
      discount_nodes: ($d|length),
      total_codes: ($d|map(.codesCount.count // 0)|add // 0),
      largest_single_node: ($d|map(.codesCount.count // 0)|max // 0),
      nodes_holding_multiple_codes: ($d|map(select((.codesCount.count // 0) > 1))|length),
      active_nodes: ($d|map(select(.status=="ACTIVE"))|length),
      more_pages: .data.codeDiscountNodes.pageInfo.hasNextPage,
      note: "discount_nodes is NOT the number of live codes — one node can hold thousands. Quote total_codes. traps.md 5." }'
}

# ---------------------------------------------------------------------------
# collections — membership with REAL pagination (traps.md 4)
# ---------------------------------------------------------------------------
door_collections() {
  need_jq
  local handle="${1:-}"
  if [ -z "$handle" ]; then
    gql 'query { collections(first: 250) { edges { node { handle title productsCount { count } } } } }' \
    | jq '{ collections: [.data.collections.edges[].node
              | {handle, title, products: .productsCount.count}] | sort_by(-.products),
            note: "productsCount LAGS after collection writes — enumerate members to confirm. traps.md 4." }'
    return
  fi

  local cursor="null" page=0 all="[]" out has
  while :; do
    page=$((page + 1))
    [ "$page" -gt 40 ] && die "collections: refusing to page past 10,000 products — narrow the query"
    out="$(gql "query { collectionByHandle(handle: \"$handle\") { title products(first: 250, after: $cursor) {
             pageInfo { hasNextPage endCursor } edges { node { id handle status } } } } }")"
    printf '%s' "$out" | jq -e '.data.collectionByHandle' >/dev/null 2>&1 \
      || die "collection not found: $handle" 4
    all="$(jq -cn --argjson a "$all" --argjson b "$(printf '%s' "$out" | jq -c '[.data.collectionByHandle.products.edges[].node]')" '$a + $b')"
    has="$(printf '%s' "$out" | jq -r '.data.collectionByHandle.products.pageInfo.hasNextPage')"
    [ "$has" = "true" ] || break
    cursor="\"$(printf '%s' "$out" | jq -r '.data.collectionByHandle.products.pageInfo.endCursor')\""
  done
  jq -n --argjson m "$all" --arg h "$handle" --argjson p "$page" \
    '{collection: $h, pages_fetched: $p, members: ($m|length), products: $m,
      note: "Fully paginated. A bare first:N here silently truncates and new products sort last — traps.md 4."}'
}

# ---------------------------------------------------------------------------
# orders — PII-light by default (never dump address_to into a log)
# ---------------------------------------------------------------------------
door_orders() {
  need_jq
  local limit="${1:-10}"
  case "$limit" in ''|*[!0-9]*) die "orders: limit must be a number" 2 ;; esac
  rest "orders.json?status=any&limit=$limit&fields=id,name,created_at,financial_status,fulfillment_status,line_items" \
  | jq '{ orders: [.orders[] | {name, created_at, financial_status, fulfillment_status,
                                line_items: (.line_items|length)}],
          note: "Deliberately excludes customer fields. Full order objects contain PII — redact before sharing." }'
}

# ---------------------------------------------------------------------------
# health-check — the sweep, every check trap-aware
# ---------------------------------------------------------------------------
door_health_check() {
  need_jq
  local prod disc findings
  prod="$(rest 'products.json?limit=250&fields=id,title,handle,status,product_type,tags,variants')"
  disc="$(door_discounts)"

  findings="$(printf '%s' "$prod" | jq '
    [.products[]] as $p | [$p[].variants[]] as $v |
    def hidden_tag: (.tags // "") | test("(^|, *)hidden( *,|$)");
    { products: ($p|length),
      active:   ($p|map(select(.status=="active"))|length),
      draft:    ($p|map(select(.status=="draft"))|length),
      archived: ($p|map(select(.status=="archived"))|length),
      supplier_signal_usable: (($v|map(.fulfillment_service)|unique) != ["manual"]),
      untracked_variants: ($v|map(select(.inventory_management==null))|length),
      untracked_with_nonzero_quantity: ($v|map(select(.inventory_management==null and .inventory_quantity!=0))|length),
      hidden_convention_broken: [ $p[]
        | select((.product_type=="Hidden") != (hidden_tag))
        | {handle, product_type, has_hidden_tag: hidden_tag} ] }')"

  jq -n --argjson f "$findings" --argjson d "$disc" '{
    catalog: $f,
    discounts: {nodes: $d.discount_nodes, codes: $d.total_codes},
    warnings: ([
      (if $f.supplier_signal_usable | not then
        "fulfillment_service is \"manual\" store-wide — it cannot identify a supplier (traps.md 1). Use the fulfiller set-difference instead: catalog --unfulfillable --fulfiller-ids <file>." else empty end),
      (if $f.untracked_with_nonzero_quantity > 0 then
        "\($f.untracked_with_nonzero_quantity) untracked variants carry a non-zero inventory_quantity. That number is not stock (traps.md 2) — read availableForSale." else empty end),
      (if ($f.hidden_convention_broken|length) > 0 then
        "\($f.hidden_convention_broken|length) product(s) set only HALF the unlisted convention (type Hidden XOR tag hidden). Hidden on some surfaces, browsable on others (traps.md 6)." else empty end),
      (if $d.total_codes > $d.discount_nodes then
        "\($d.discount_nodes) discount nodes carry \($d.total_codes) actual codes — quote the code count (traps.md 5)." else empty end),
      (if $f.draft > 0 then
        "\($f.draft) DRAFT product(s). Any 301 pointing at a drafted handle now redirects to a 404, and DRAFT unpublishes from all channels irreversibly (traps.md 7)." else empty end)
    ])
  }'
}

# ---------------------------------------------------------------------------
# webhooks — what is registered, and more importantly what is NOT
#
# traps.md 9. Shopify never tells you a webhook is absent. There is no error, no
# empty-result marker, no degraded mode: the events simply never arrive, and
# every downstream consumer reports a clean zero. An orders webhook that was
# never created looks identical to a store that has taken no orders.
#
# So the answer this door gives is the MISSING list, not the registered list.
# Listing what exists is the query anyone would write; naming what is absent is
# the one that would have caught it.
# ---------------------------------------------------------------------------

# Topics whose absence silently breaks attribution or fulfilment. Not "all
# useful topics" — the ones where nothing anywhere reports the gap.
SHOPIFY_CRITICAL_TOPICS="orders/create orders/paid orders/updated orders/cancelled"

door_webhooks() {
  need_jq
  local data registered missing
  data="$(rest 'webhooks.json?limit=250')"
  registered="$(printf '%s' "$data" | jq -c '[.webhooks[].topic] | unique')"

  missing='[]'
  local t
  for t in $SHOPIFY_CRITICAL_TOPICS; do
    printf '%s' "$registered" | jq -e --arg t "$t" 'index($t)' >/dev/null 2>&1 \
      || missing="$(jq -cn --argjson m "$missing" --arg t "$t" '$m + [$t]')"
  done

  jq -n --argjson d "$data" --argjson r "$registered" --argjson m "$missing" '{
    registered_count: ($d.webhooks | length),
    registered_topics: $r,
    critical_missing: $m,
    subscriptions: [ $d.webhooks[] | {topic, address, created_at, api_version} ],
    warnings: ([
      (if ($m|length) > 0 then
        "MISSING critical topic(s): \($m|join(", ")). Shopify reports absence as nothing at all — no error, no empty marker. Downstream this reads as \"no orders\", not \"no webhook\" (traps.md 9)." else empty end),
      (if ($d.webhooks | length) == 0 then
        "ZERO webhooks registered. Every event-driven integration on this store is silently inert." else empty end)
    ]),
    bound: "REGISTRATION ONLY. This proves a subscription exists, NOT that it has ever delivered. Shopify exposes no last-delivered timestamp here, so a registered-but-failing endpoint looks identical to a healthy one — confirm receipt at the consumer."
  }'
}

# ---------------------------------------------------------------------------
# copy — description defects, including the class humans cannot see
#
# traps.md 10. The `.:` marker is the reason this door exists rather than a
# human reading descriptions. In JSON it reads as punctuation and the eye skips
# it; on the rendered page it prints literally. Three reviewers read the same
# descriptions in API output and none saw it — it was only caught by rendering
# in a browser. A defect that survives careful reading is a defect that has to
# be matched by machine.
# ---------------------------------------------------------------------------
door_copy() {
  need_jq
  local data
  data="$(rest 'products.json?limit=250&fields=id,title,handle,status,body_html')"

  printf '%s' "$data" | jq '
    def body: (.body_html // "");
    [ .products[] | . as $p | {
        handle, title, status,
        empty:        (body | gsub("<[^>]*>";"") | gsub("\\s";"") | length == 0),
        dot_colon:    (body | test("\\.:")),
        size_table:   (body | test("(?i)<table|size chart|size guide")),
        em_dash:      (body | test("—")),
        boilerplate:  (body | test("(?i)made to order|machine wash|100% cotton|please allow|due to the nature"))
      } ] as $rows |
    { products_checked: ($rows|length),
      defects: {
        empty_description: [ $rows[] | select(.empty)       | .handle ],
        dot_colon_marker:  [ $rows[] | select(.dot_colon)   | .handle ],
        size_table:        [ $rows[] | select(.size_table)  | .handle ],
        em_dash:           [ $rows[] | select(.em_dash)     | .handle ],
        supplier_boilerplate: [ $rows[] | select(.boilerplate) | .handle ]
      },
      warnings: ([
        (if ([$rows[]|select(.dot_colon)]|length) > 0 then
          "\([$rows[]|select(.dot_colon)]|length) description(s) contain a literal \".:\" marker. This is INVISIBLE when reading API output — it reads as punctuation in JSON and renders literally on the page (traps.md 10). Do not ask a human to re-check by reading; render it." else empty end),
        (if ([$rows[]|select(.empty)]|length) > 0 then
          "\([$rows[]|select(.empty)]|length) product(s) have no description text at all — markup only, or nothing." else empty end)
      ]),
      bound: "Pattern-matched, not judged. Boilerplate and size-table patterns are heuristics and will both miss and over-flag; the dot-colon and empty checks are exact."
    }'
}

# ---------------------------------------------------------------------------
# redirects — both directions, because each one breaks differently
#
# traps.md 11 (and 7). A redirect map is a snapshot; the catalogue is not. Two
# independent failure directions, and checking only the first is the common
# mistake:
#
#   dead DESTINATION — the target was drafted/deleted, so the 301 now delivers
#                      shoppers to a 404 they had to click through to reach
#   revived SOURCE   — the product came back at its own handle, and the redirect
#                      runs BEFORE routing, so a live sellable product is
#                      unreachable at its canonical URL
# ---------------------------------------------------------------------------
door_redirects() {
  need_jq
  local reds prods colls
  reds="$(rest_paged 'redirects.json?limit=250' 2>/dev/null | jq -sc '[.[].redirects[]?]')"
  prods="$(rest 'products.json?limit=250&fields=id,handle,status' | jq -c '[.products[]]')"
  colls="$(rest 'custom_collections.json?limit=250&fields=id,handle' | jq -c '[.custom_collections[]?]')"

  jq -n --argjson r "$reds" --argjson p "$prods" --argjson c "$colls" '
    ([ $p[] | select(.status == "active") | .handle ]) as $live_products |
    ([ $p[] | .handle ]) as $all_products |
    ([ $c[] | .handle ]) as $collections |
    def handle_of($path): ($path | capture("^/(?<kind>products|collections)/(?<h>[^/?#]+)") // null);
    [ $r[] | . as $red
      | (handle_of($red.target)) as $t
      | select($t != null)
      | select( if $t.kind == "products" then ($live_products | index($t.h)) == null
                else ($collections | index($t.h)) == null end )
      | {path: $red.path, target: $red.target, reason:
          (if ($all_products | index($t.h)) != null then "target exists but is NOT active (drafted/archived)"
           else "target handle does not exist" end)} ] as $dead |
    [ $r[] | . as $red
      | (handle_of($red.path)) as $s
      | select($s != null and $s.kind == "products")
      | select(($live_products | index($s.h)) != null)
      | {path: $red.path, target: $red.target} ] as $revived |
    { redirects_checked: ($r|length),
      dead_destinations: $dead,
      revived_sources: $revived,
      warnings: ([
        (if ($dead|length) > 0 then
          "\($dead|length) redirect(s) point at something that no longer resolves. Drafting a product does NOT clear redirects aimed at it (traps.md 7) — the shopper now clicks through to the same 404." else empty end),
        (if ($revived|length) > 0 then
          "\($revived|length) redirect SOURCE(s) are live active products. Redirects run before routing, so these products are unreachable at their own canonical URL — from every link ever shared (traps.md 11)." else empty end)
      ]),
      bound: "Checks handles against the catalogue, not HTTP. A target outside /products/ or /collections/ (a page, a blog, an external URL) is NOT evaluated here — resolve those over HTTP."
    }'
}

# ---------------------------------------------------------------------------
# orphans — reachability triage, and deliberately NOT a delete list
#
# traps.md 12 is the whole point of this door. "Orphaned" is a symptom with at
# least three different correct responses, and they are not interchangeable:
#
#   * a container that should be UNLISTED    — remove it from the sitemap
#   * a discovery GAP                        — the thing is good, link to it
#   * orphaned BY DESIGN                     — unlinked on purpose; leave it
#
# A door that returned "delete these" would be wrong on two of the three. So this
# returns candidates with the question attached, never a verdict.
# ---------------------------------------------------------------------------
door_orphans() {
  need_jq
  local prods colls smart membership
  prods="$(rest 'products.json?limit=250&fields=id,handle,title,status,product_type,tags' | jq -c '[.products[]]')"
  colls="$(rest 'custom_collections.json?limit=250&fields=id,handle' | jq -c '[.custom_collections[]?]')"
  smart="$(rest 'smart_collections.json?limit=250&fields=id,handle' | jq -c '[.smart_collections[]?]')"

  membership='[]'
  local cid
  for cid in $(jq -r '.[].id' <<<"$colls" 2>/dev/null) $(jq -r '.[].id' <<<"$smart" 2>/dev/null); do
    membership="$(jq -cn --argjson m "$membership" \
      --argjson x "$(rest "collects.json?collection_id=$cid&limit=250&fields=product_id" | jq -c '[.collects[]?.product_id]')" \
      '$m + $x')"
  done

  jq -n --argjson p "$prods" --argjson m "$membership" '
    ($m | map(tostring) | unique) as $in_collection |
    def hidden_tag: (.tags // "") | test("(^|, *)hidden( *,|$)");
    [ $p[] | select(.status == "active")
      | select(($in_collection | index(.id|tostring)) == null)
      | {handle, title, product_type,
         intentionally_unlinked: ((.product_type == "Hidden") or hidden_tag)} ] as $orphans |
    { active_products: ([$p[] | select(.status=="active")] | length),
      orphan_candidates: ($orphans | length),
      by_likely_action: {
        leave_alone_orphaned_by_design: [ $orphans[] | select(.intentionally_unlinked) | .handle ],
        needs_a_decision:              [ $orphans[] | select(.intentionally_unlinked | not) | .handle ]
      },
      warnings: ([
        (if ($orphans|length) > 0 then
          "\($orphans|length) active product(s) belong to no collection. This is a TRIAGE list, not a delete list — the correct action differs per item and at least one of them is usually \"link to it\", not \"remove it\" (traps.md 12)." else empty end)
      ]),
      bound: "Collection membership is a PROXY for inbound links, not a measurement of them. A product linked only from a blog post, the nav, or a hand-built page reads as an orphan here and is not one. A true inbound-link audit needs a crawl of the rendered storefront, which this door does not do."
    }'
}

# ---------------------------------------------------------------------------
# consent — a real count, or an explicitly bounded one. Never a silent sample.
#
# traps.md 13. The obvious query is GraphQL customersCount, which returns
# `precision: AT_LEAST` and saturates at 10000 — so on any store past that size
# it reports 10000 forever and reads like a real number. The REST list endpoint
# is authoritative but paginates through the Link header, and page 1 of a large
# store is a well-formed 200 that looks complete.
#
# Both failure modes produce a confident wrong total. So this counts every page,
# and if it is told to stop early it says so in the output rather than reporting
# a sample as a rate.
# ---------------------------------------------------------------------------
door_consent() {
  need_jq
  local max_pages=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --max-pages) max_pages="${2:?--max-pages needs a number}"; shift 2 ;;
      *) die "consent: unknown option $1" 2 ;;
    esac
  done
  case "$max_pages" in ''|*[!0-9]*) die "consent: --max-pages must be a number" 2 ;; esac

  local pages
  pages="$(rest_paged "customers.json?limit=250&fields=id,email_marketing_consent" "$max_pages" 2>/dev/null)"

  printf '%s' "$pages" | jq -s '
    (map(select(.__truncated__ == true)) | length > 0) as $truncated |
    [ .[] | .customers[]? ] as $c |
    { customers_counted: ($c|length),
      by_state: ( $c
        | group_by(.email_marketing_consent.state // "null")
        | map({key: (.[0].email_marketing_consent.state // "null"), value: length})
        | from_entries ),
      subscribed_rate: (if ($c|length) > 0
        then (([ $c[] | select(.email_marketing_consent.state == "subscribed") ] | length) / ($c|length))
        else null end),
      complete: ($truncated | not),
      warnings: (if $truncated then
        ["STOPPED EARLY — this is a SAMPLE, not the store. The rate above describes the customers counted, not all customers. Re-run without --max-pages before quoting it (traps.md 13)."]
        else [] end),
      bound: (if $truncated
        then "SAMPLE of the first pages only."
        else "Complete: every page followed to exhaustion via the Link header. Do NOT cross-check against GraphQL customersCount — it saturates at 10000 with precision AT_LEAST (traps.md 13)." end)
    }'
}

usage() {
  # Derived, not a hardcoded line range. It used to be `sed -n '2,28p'`, which
  # silently truncated the help the moment the header changed length — which
  # editing this file for the read-only fix did. Print the leading comment block
  # up to the first non-comment line, then drop that line.
  sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# Dispatch only when EXECUTED, not when sourced. This is what lets the guards be
# tested by behaviour — a test can source this file and call api/gql directly to
# prove a refusal actually happens. The old guards grepped the source text for a
# write verb instead, and a wired write door walked straight past them because
# this file never spells the verb literally. A guard you cannot execute is a
# guard you cannot trust.
if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
  return 0
fi

cmd="${1:-help}"; shift || true
case "$cmd" in
  health-check) door_health_check "$@" ;;
  orders)       door_orders "$@" ;;
  catalog)      door_catalog "$@" ;;
  discounts)    door_discounts "$@" ;;
  collections)  door_collections "$@" ;;
  webhooks)     door_webhooks "$@" ;;
  copy)         door_copy "$@" ;;
  redirects)    door_redirects "$@" ;;
  orphans)      door_orphans "$@" ;;
  consent)      door_consent "$@" ;;
  raw)          [ $# -ge 1 ] || die "raw needs a path, e.g. raw shop.json" 2; rest "$1" ;;
  help|-h|--help) usage ;;
  *) usage >&2; die "unknown command: $cmd" 2 ;;
esac
