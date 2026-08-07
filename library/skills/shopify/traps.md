# Shopify traps — the fields that lie

Every entry here cost a real incident. Each was **re-verified against a live Shopify
Admin API before being written down**, because several were folklore that turned out to
be wrong in the details — see `inventory_quantity`, which is the clearest example of a
trap everyone "knows" and nobody has measured.

The API calls in this skill are trivial. This file is the reason it exists.

---

## 1. `fulfillment_service` cannot identify a supplier

**The belief:** print-on-demand variants are marked with their fulfiller, so you can
filter on `fulfillment_service` to find POD products.

**Measured:** across **4,804 variants** on a store where the large majority are
fulfilled by an external POD provider, `fulfillment_service` was `"manual"` on
**4,804 of 4,804** — 100%, no exceptions.

The field records who Shopify thinks *ships* the line, and an app-created product that
syncs stock in over the API is still "manual" as far as Shopify is concerned. It carries
no supplier information at all.

**Do instead:** see trap 3 — identity comes from the fulfiller's side, not Shopify's.

---

## 2. `inventory_quantity` is not stock, and the popular version of this trap is wrong

**The folklore:** "POD variants are all set to 9999, so any quantity you see is a
placeholder."

**Measured, and it does not hold.** On the same 4,804 variants:

| value | variants |
|---|---|
| `0` | 2,158 |
| `1` | 930 |
| `-1` | 488 |
| `9999` | **341** |
| `-2` | 231 |
| `-3` | 130 |

`9999` is **7%** of variants, not the rule — and not every `9999` variant is even
untracked. Filtering on `9999` finds almost nothing.

**The real discriminator is `inventory_management`:**

| | variants | of which the quantity is misleading |
|---|---|---|
| `null` (Shopify tracks nothing) | **4,111** | 2,273 carry a non-zero quantity anyway |
| `"shopify"` (genuinely tracked) | 693 | only 7 are negative |

When `inventory_management` is `null`, Shopify is not tracking that variant, the
`inventory_quantity` integer is **not authoritative**, and it is frequently non-zero
regardless. 1,350 variants store a negative number; almost all are untracked, which is
why negatives are a red herring rather than a bug to chase.

**Do instead:** purchasability is `availableForSale` (Storefront) — never a quantity
comparison. A variant can be `availableForSale: true` at `quantity: 0` (untracked, or
`inventoryPolicy: CONTINUE`), and that is correct, not a defect.

---

## 3. The only sound unfulfillable test is set difference against the fulfiller

Since trap 1 removes the obvious signal, the answer has to come from the other system.
Every POD platform stores the Shopify product id it created on its own record (Printify
calls it `external.id`). So:

> **unfulfillable ≈ ACTIVE on Shopify AND its product id absent from the fulfiller's
> external-id set**

**Measured:** 107 ACTIVE products, 97 present in the fulfiller's set, **10 absent** —
a real, actionable list. `fulfillment_service` would have answered `"manual"` for all
107 and told you nothing.

Caveats worth stating rather than hiding: a product can be legitimately absent
(hand-fulfilled, digital, a service), and a linked product can still be unfulfillable if
the *provider* discontinued the blueprint. This is a triage filter, not a verdict.

---

## 4. Collection listings truncate silently

Storefront `collection.products(first: N)` is Relay-paginated. `first:` is a page size,
never "all", and there is no error when the collection has more — you simply receive N
and a `pageInfo.hasNextPage` you did not read.

A hard-coded `first: 50` on a catch-all collection page hid **100+ products** for months.
Nothing failed. The page rendered, ranked by `BEST_SELLING`, and everything below the cut
was invisible — including newly-added products, which have no sales and therefore sort
last, so *new products are exactly what a truncated best-seller page hides*.

**Do instead:** always loop on `pageInfo.hasNextPage` / `endCursor`. Treat any bare
`first:` with no cursor loop as a bug. If you must cap, log what was dropped.

---

## 5. "N active discount codes" is a count of the wrong noun

`codeDiscountNodes` returns **discount objects**, not codes. One node can carry a bulk
batch of thousands of individual codes.

**Measured:** **250 nodes** carrying **24,115 codes**. The largest single node held
**10,928**. 142 of the 250 held more than one.

Reporting the node count as "active discount codes" understates the real exposure by two
orders of magnitude — which matters, because that number is the answer to "how much
discounting is live on this store?"

**Do instead:** read `codesCount { count }` per node and sum it. Say which noun you are
counting, every time.

---

## 6. Unlisted-but-buyable is a two-part convention, and both parts are load-bearing

The pattern for a direct-link-only drop: `product_type = "Hidden"` **and** the tag
`hidden`.

**Measured:** 7 products carried both, **0** carried only one — the convention is intact
in practice. Both are needed because different consumers key on different halves:
Shopify smart-collection rules match on product type, tag-based rules and most storefront
filters match on the tag. Setting one and not the other yields a product that is hidden
from some surfaces and browsable on others, which is the worst outcome — it looks
unlisted while being publicly listed somewhere you did not check.

**Do instead:** set and check both. A product with one and not the other is a defect;
report it rather than "fixing" it silently, because which half is wrong is a
merchandising decision.

---

## 7. Drafting a product silently breaks every redirect pointing at it

`status = DRAFT` is the correct way to retire a product — it keeps order history, unlike
deletion. But a drafted product's `/products/<handle>` returns **404**, and Shopify does
not touch redirects that target it. Any `301` you previously created to that handle now
sends shoppers to a dead page.

This is strictly worse than the 404 the redirect replaced: the shopper invests a click
before hitting the same wall, and search engines follow the 301 and index the failure.

It is invisible to unit tests, because both facts live in the store rather than the code.
A redirect map is a snapshot; the catalogue is not.

**Do instead:** after any status change, re-verify every redirect whose destination is
that handle. A CI check that resolves redirect destinations against the live catalogue
catches it; nothing else does.

Related: `DRAFT` also unpublishes from **all** sales channels, and returning to `ACTIVE`
does **not** restore them — republishing is a separate call. A product can therefore be
ACTIVE, correct-looking in the admin, and reachable by nobody.

---

## 8. The Shopify MCP's `getProduct` is broken; use REST

**Verified live, this session:**

```
mcp__shopify__getProduct(id: "gid://shopify/Product/…")
  -> Error fetching product: Field 'weight' doesn't exist on type 'ProductVariant'
```

The MCP requests a `ProductVariant.weight` field that the current Admin API schema does
not have, so the call fails outright for every product. Weight moved to
`InventoryItem.measurement.weight` in recent versions.

**Do instead:** direct REST or GraphQL for product reads. This is not a preference —
the MCP door does not function. Do not build a flow on it, and do not report its failure
as "the product could not be found".

---

## 9. A missing webhook is silent, total, and indistinguishable from no activity

Shopify does not report the absence of a webhook. There is no error, no empty-result marker,
no degraded mode. The events simply never arrive.

The consequence is that **absence and inactivity produce identical downstream evidence.** A
store with no `orders/create` subscription looks exactly like a store that has taken no
orders: every consumer reports a clean zero, every dashboard renders an honest-looking empty
state, and nothing anywhere raises a flag.

**Measured on a live store, 2026-08-04:** no orders webhook had ever fired. Not "was failing"
— had never existed. The visible symptom was that 0 of 25 real, paid orders were attributable
to any session or channel, which read for weeks as an attribution-modelling problem rather
than a missing subscription.

**Do instead:** ask what is MISSING, not what is registered. Listing registered topics is the
query anyone writes and it cannot find this; the answer has to be a set difference against
the topics you require. `webhooks` does that.

**And know the bound:** registration is not delivery. Shopify exposes no last-delivered
timestamp on the subscription, so a registered endpoint that 500s on every call looks
identical to a healthy one from the Admin API. Confirm receipt at the consumer, not here.

---

## 10. The `.:` marker is invisible in API output and renders literally on the page

Supplier-imported descriptions can carry a literal `.:` sequence. In JSON it reads as
punctuation — the eye takes it for a bullet or a typo in the escaping and moves on. On the
rendered product page it prints as-is, in the copy the customer reads.

**This survives careful human review.** Three people read the same descriptions in API
output and none of them saw it; it was caught only by rendering the page in a browser. That
is what makes it worth a door rather than a checklist item: the defect is invisible in the
medium where the reviewing happens.

**Do instead:** match it by machine (`test("\\.:")`), and treat a human "I read them and
they looked fine" as no evidence either way. If a defect class is invisible in the medium
you inspect, more inspection in that medium buys nothing.

Generalises past this one marker: **before trusting a read-through, ask whether the defect
you are looking for is even visible in the representation you are reading.**

---

## 11. A redirect breaks in two directions, and the second one is worse

Verifying a redirect map means checking both ends. Only one is obvious.

**Dead destination** — the target was drafted, archived or deleted, so the `301` now delivers
the shopper to a 404 *they had to click through to reach*. Strictly worse than the original
404: the click is wasted, and search engines follow the redirect and index the failure. See
trap 7 for why drafting causes this silently.

**Revived source** — the product came back at its own handle while a redirect still points
away from it. Redirects run at the edge **before** routing, so the live, sellable product is
unreachable at its canonical URL — from every link ever shared, every index entry, every
saved bookmark. Nothing in the catalogue looks wrong; the product is ACTIVE and correct.

The second direction is rarer and costs more, and a checker written from the obvious
direction alone will never see it.

**Do instead:** check destinations resolve AND that no source is live. `redirects` does both.
A redirect map is a snapshot; the catalogue is not — so this needs re-running against the
live store, not asserting once in a unit test.

---

## 12. "Orphaned" is a symptom with several different correct answers

A page nothing links to is not automatically a page to delete. Observed on one sweep, three
orphans with three different correct actions:

| what it was | correct action |
|---|---|
| a container that should not be publicly listed | remove it from the sitemap |
| a good page nobody had linked yet | **link to it** — a discovery gap, not stale content |
| a drop container deliberately kept unlinked | leave it exactly as it is |

Two of those three are actively harmed by deletion, and the middle one is the opposite of
deletion. So **a tool that emits "orphans" as a delete list is wrong more often than it is
right**, and it is wrong in the expensive direction — removing a page that needed promoting.

**Do instead:** return candidates with the question attached, never a verdict. Separate the
ones that are unlinked *on purpose* (an explicit hidden convention — trap 6) from the ones
that need a human decision. `orphans` does that.

**And know the proxy:** collection membership is a stand-in for inbound links, not a
measurement of them. Something linked only from the nav, a blog post or a hand-built page
reads as an orphan and is not one. A true inbound-link audit needs a crawl of the rendered
storefront.

---

## 13. `customersCount` saturates at 10,000, and REST hides the rest behind a header

Two ways to get a confidently wrong customer total.

**GraphQL `customersCount`** returns an object carrying `precision: AT_LEAST`, and on any
store past ten thousand it reports `10000` — permanently, and without ever looking like an
error. The field name promises a count; the value is a floor.

**REST `customers.json`** is authoritative but paginates through the `Link` response header.
Page one of a fifteen-thousand-customer store is a well-formed `200` containing 250 rows that
looks *exactly* like a complete answer. The body carries no marker that more exists — the
only signal is a header most clients discard.

**Measured 2026-08-04:** a marketing-consent rate was computed from 3,000 of 15,704 customers
and reported as the store rate. Nothing in the response indicated the shortfall.

**Do instead:** follow `Link: <…>; rel="next"` to exhaustion, and match on the **rel**, not
just the URL — the header commonly also carries `rel="previous"`, and advancing on that walks
backwards forever. If you must stop early, **say so in the output**: a sample reported as a
total is the failure this trap is about, and adding a caveat in conversation does not travel
with the number. `consent` counts fully by default and marks itself incomplete when bounded.

---

## How to add to this file

One entry per trap, and every entry needs a **measurement or a reproduction**, not a
recollection. If you cannot verify it against a live API, say so in the entry. The point
of the file is that it can be trusted without re-deriving it — an unverified entry
poisons the rest.
