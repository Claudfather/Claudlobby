# Merge-policy consolidation — one policy, one place, rendered from config

**Issue:** [Claudfather/Claudlobby#1159](https://github.com/Claudfather/Claudlobby/issues/1159)
**Status:** plan only. No implementation in this PR.
**Branch:** `plan/1159-merge-policy-consolidation` off `main` @ `560c3c9`.
**Scope:** design + phasing + canary contract. Implementation lands as separate reviewed PRs.
**Rev 2** (2026-08-10) — **scope widened after review.** Rev 1 planned to consolidate *two* files.
The real surface is **seven**, one of them already contradicting the policy in production. §4A
carries the finding and the re-phasing; §0 records what changed.

Claudfather/Claudlobby is outside this fleet's merge allowlist, so this plan ships as a
reviewable PR that a human merges.

---

## 0. Verdict on #1159 up front

The **diagnosis is correct and I confirmed its load-bearing bound.** The **proposed shape is
wrong in two places** and I recommend against it as written:

| #1159 says | verdict |
|---|---|
| Tiers are unread | **CONFIRMED** — exhaustively, §1 |
| Library files get no template context | **CONFIRMED** — §2 |
| Render *all* library content through Jinja | **REJECT** — §3. Measured blast radius; the library documents dbt, and dbt *is* Jinja |
| Rewrite shared guardrail to iterate `projects` | **AMEND** — `projects` is manager-only context (`composer.py:1567`); as written, workers and the manager would render *different* merge policies, silently. §4 |
| Delete both fleet forks | **AGREE, but split — and it is not enough.** One fork needs no templating and dies first; but the surface is seven files, not two, and one of them is contradicting the policy in production right now. §4A, §5 |

Two additions #1159 does not raise: the **shared base guardrail is itself the more dangerous
artifact** (§6), and consolidation without a drift detector re-forks (§9).

### Rev 2 — what review changed

Two reviews landed on PR #1160. They split cleanly, and the second one is right.

**Rev 1's own scope was too narrow, in the same way #1159's was.** I inherited the issue's frame —
*one guardrail, one protocol, two forks* — and pressure-tested everything inside it without
checking the frame itself. §6 argued that unconditional merge prose reads as universal permission,
then applied that argument to exactly one file. It is true of a second guardrail verbatim, and of
a protocol that is **already granting unscoped `--admin` to two production bots today.**

I verified all three findings independently before amending (§4A). All three hold. One reported
count does not, and the discrepancy is worth stating precisely rather than quietly absorbing —
§4A.2.

| what changed | where |
|---|---|
| Surface is 5 guardrails + 2 protocols, not 1 + 1 | §4A.1 |
| A live production contradiction, not a future drift risk → new **Phase 0** | §4A.2 |
| The prose mutual-exclusion invariant is enforced by nothing → §6 gains a fourth gate, and Phase 3 answers what happens to it | §4A.3, §6 |
| Rev 1's proposed drift detector was blind to this class by construction | §9 |
| Re-phased 4 → 6 phases; scope grew and the plan says so rather than absorbing it | §5 |

---

## 1. The load-bearing bound — CLOSED

#1159: *"I did not exhaustively prove no consumer exists anywhere — that should be confirmed
before the tier is made authoritative."*

**Confirmed: nothing reads `PROJECT_TIER_*` or `projects.yaml` tiers. There is no consumer.**

Surfaces swept, all at `main` @ `560c3c9`:

| surface | result |
|---|---|
| `lib/` (all 70+ scripts) | **zero hits** for `PROJECT_TIER`, `PROJECT_REPOS`, `projects.yaml` |
| `library/` (201 md + skills + tools) | zero hits |
| `templates/`, `voices/` | zero, other than `claude.md.j2`'s `## Projects` table (prose for the model) |
| `claudlobby/` | 3 sites, all **producers**: `config.py:263` (slug), `composer.py:898` (emit), `validator.py:880-911` (name charset + reserved-key guard) |
| installed plugins (`~/.claude/plugins/**`, incl. clauDNA/Claudron marketplaces) | zero hits |
| sibling checkouts on host (`~/farm-artemis`, `~/claudlobby`) | hits only in an **old vendored copy** of this same repo's docs/tests |
| this fleet's overlay (`local/artemis-engineering/`) | hits only in `projects.yaml`'s own comment and composed CLAUDE.md prose |
| test suite | `tests/test_projects_tier.py` asserts **composition only** — that the export lands in `bot.conf` |

The word `tier` is heavily overloaded in this repo (`.env` tiers, vault tiers, manager/worker
tiers, Claudron tiers). `PROJECT_TIER_*` is the discriminating name; the sweep used it.

**One nuance the bound should carry forward:** the map *is* live in every bot's environment —
`composer.py:891-893` exports it to **every** bot with the comment *"any sprint/runner bot must
resolve a working repo's tier locally."* So the data is fleet-wide already; only its *use* is
absent. That matters in §4.

**Consequence for this plan:** making tiers authoritative breaks nothing, because nothing
depends on them. This is a clean improvement. The bound is closed in the direction #1159 hoped.

---

## 2. The template-context gap — confirmed, with the mechanism that matters

`composer.py:1610` renders `claude.md.j2` with `projects=projects`. Library bodies never reach
that environment. They are loaded flat and passed through `_expand` (`composer.py:49-56`):

```python
def _expand(text: str, ctx: dict[str, str]) -> str:
    """Replace {{KEY}} placeholders. ... Missing keys are left as-is."""
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", v)
```

`str.replace`. No expressions, no iteration, no conditionals. Then the result is injected into
the template as a **variable** (`{{ item.body | trim }}`), and Jinja does not re-render variable
output. So a library file genuinely cannot express an allowlist. #1159 is right: **the gap is
the bug, the forks are the symptom.**

**The detail that decides §3:** `_expand` already claims `{{ }}`. The library contains **60
`{{KEY}}` tokens across 29 files** that mean *string substitution*, not Jinja. Routing library
bodies through Jinja does not introduce double-braces to a library that had none — it
**reinterprets braces that are already there and already mean something else.**

---

## 3. FORK RESOLVED — per-file opt-in via frontmatter

#1159 asks for this to be *resolved, not noted*. Resolved on measurement, not taste.

### The measurement

I rendered all 201 shared-library markdown files through the same `SandboxedEnvironment`
`composer.py` uses, in both orderings. Probe source: **Appendix A**.

| ordering | hard syntax errors | undefined errors | silently mangled (lax mode) |
|---|---|---|---|
| Jinja **before** `_expand` | 1 | **28** | 27 |
| Jinja **after** `_expand` (the honest option) | **1** | **3** | — |

Post-`_expand` the blast radius is **4 files**, not 29. That is the number to argue against, and
it is small. But *which* four is the whole argument:

```
library/lessons/dbt/parse-vs-execute-time.md        -> TemplateSyntaxError: Expected an expression
library/lessons/dbt/incremental-unique-key-discipline.md -> 'this' is undefined
library/lessons/snowflake/clustering-earns-its-cost.md   -> 'config' is undefined
library/tools/README.md                              -> 'data_dir' is undefined
```

All four are **documentation quoting a different Jinja dialect**, inside fenced code blocks.
`parse-vs-execute-time.md` is a lesson *about dbt Jinja parse-time semantics* — its entire
subject is `{% set %}`, `{{ this }}`, `run_query`. It is a correct, valuable file that a global
render turns into a build error.

### Why this is structural, not a tail

**Claudlobby's library documents dbt. dbt is Jinja.** So is Ansible, Helm, Airflow, Liquid, and
Hugo — every one a plausible future lesson. The collision is not a rare accident to be escaped
once; it recurs on the axis the library grows along. `library/lessons/` exists precisely to
accumulate this content.

And the failure lands on the wrong person: an analytics engineer writing a dbt lesson breaks
`claudlobby generate` **fleet-wide**, with a Jinja stack trace, for a file they never intended as
a template.

### The three options, priced

**(a) Global render + escaping.** Cost is not the 4 files — it is that `{% raw %}` is *itself
Jinja*, so writing plain markdown now requires knowing Jinja. Recurring, unbounded, and paid by
authors who never opted in. **Reject.**

**(b) Restricted delimiters** (`[% %]` / `<% %>`). I measured candidate collisions rather than
guessing:

```
'[['  : 4 hits in library/  — but 139 in the fleet overlay + bot memory dirs (wikilinks)
'[%'  : 0        '<%' : 0        '{{{' : 0        '@{'  : 0
'(('  : 12       '<<' : 5        '(%'  : 1
```

`[[ ]]` is poisoned by the memory system's `[[wikilink]]` convention. `[%`/`<%` are clean
*today*. But this is still an **implicit global**: every library file silently becomes a
template, and the same failure is merely deferred to a rarer trigger — discovered later, by
someone with less context. It also forks the templating dialect: `claude.md.j2`, `library/tools/`
and `startup_prompt` all use standard `{{ }}`. The **consolidate-don't-fork** principle argues
against a second Jinja syntax inside the same compositor. **Reject.**

**(c) Per-file opt-in via frontmatter.** ← **RECOMMENDED**

```yaml
---
title: Auto-merge with --admin after peer review
description: ...
template: true          # opt in to Jinja rendering; default false
---
```

- **Blast radius: zero, measured.** No existing file opts in, so no existing file changes.
- **Failure is localized and legible.** Only a file that declared `template: true` can fail, and
  its author asked for templating.
- **It is already the house pattern.** `library/tools/` renders Jinja with `StrictUndefined`
  behind an explicit `tool.yaml` manifest (`composer.py:1349-1385`). Opt-in templating is not a
  new concept here — this extends an existing one rather than inventing a parallel mechanism.
- **Reversible in the cheap direction.** Opt-in → global later is a flag flip. Global → opt-in
  means unwinding escapes across the whole library.

**Decision: (c), rendered AFTER `_expand`, with `StrictUndefined`**, matching `_render_tools`.
`StrictUndefined` because an opted-in file that references a missing variable must fail the
build loudly, never render a **blank merge allowlist** — a silently-empty allowlist is the
single worst output this system can produce (§6).

### 3.4 Reproducing the measurement

The probe is pure stdlib + jinja2 and reads no fleet state. Source is in Appendix A so a
reviewer can rerun it rather than take the table on trust:

```bash
./.venv/bin/python /tmp/jinja_probe.py library local/<fleet>/library
```

Implementation must land this as a real test (`tests/test_library_templating.py`) asserting the
4 known files still compose, so a future global-render attempt fails CI rather than review.

**Note for whoever implements:** the documented setup command in `CLAUDE.md`
(`python3 -m venv .venv && ./.venv/bin/python -m pip install -e '.[dev]'`) **fails on this host** —
system `python3` is 3.9.6 and `pyproject.toml` requires `>=3.10`; the resulting error is a pip
resolver message, not a version message. Worked around with `~/.local/bin/python3.12`. That is a
cold-start defect in #947's class and should be filed separately; it is not in this plan's scope.

---

## 4. AMENDMENT — `projects` is manager-only, and the naive fix ships a silent divergence

This is the flaw that would have bitten during implementation.

```python
# composer.py:1566-1567
projects = (
    sorted(fleet.projects.values(), key=lambda p: p.key) if is_manager else []
)
```

Both policy files are equipped under `defaults:` in this fleet's `fleet.yaml` (lines 89, 108) —
**every bot gets them**, manager and worker alike. (Verified: this guardrail's text is in a
worker's composed `CLAUDE.md`.)

So #1159's proposed shape — *iterate `projects`, default to human-gated when empty* — produces:

| bot | renders |
|---|---|
| manager (`projects` populated) | 6 repos autonomous |
| **every worker** (`projects == []`) | **everything human-gated** |

No error. No warning. Two composed documents stating different merge policies — **the exact
disease being cured, reintroduced by the cure.** The safe-default rule and the manager-only gate
are individually correct and combine into a silent contradiction.

It is fail-*safe* in direction (workers under-permit rather than over-permit), which is why it
would survive review and only surface when a worker refuses a merge the manager authorized, or
when someone reads two CLAUDE.mds side by side.

### Fix

**Widen the context available to library rendering to all bots; leave the `## Projects` table
manager-gated.** Two separate concerns that currently share one variable:

- `claude.md.j2`'s `## Projects` table — an orchestration aid; stays manager-only. Worker
  composed output does not gain a table.
- The library render context — gets `projects` for **every** bot.

This is not a new inconsistency; it **removes** one. `bot.conf` already exports the full map to
every bot, with an explicit comment that every bot must resolve tier locally
(`composer.py:891-893`). The manager-only gate on template context is the outlier.

**Implementation constraint:** worker composed output must change **only** in the merge-policy
sections. `claudlobby diff` proves it (§8, canary check 6).

---

## 4A. The real surface is seven files, and one is already contradicting the policy

All three findings below were re-verified independently against `main` @ `560c3c9` and against
this fleet's composed output. All three hold.

### 5A.1 — `library/guardrails/` ships a FAMILY of five merge-authority guardrails

```
merge-policy-auto-admin.md         merge-policy-human.md      no-merge-own-pr.md
merge-policy-auto-after-review.md  no-merge-admin.md
```

`merge-policy-auto-after-review.md:8-14` states the same policy a **fourth** time — the same three
conditions, in the same order, differing only in whether `--admin` is passed:

```
 8  The manager auto-merges PRs when ALL of:
 9
10  1. **Peer review posted** — a reviewer has posted an `APPROVE` verdict ...
11  2. **CI green** — all required status checks pass.
12  3. **No conflicts** — mergeable state is `clean` or `unstable` (not `dirty`).
13
14  Merge command: `gh pr merge <n> --squash --delete-branch`
```

**No allowlist.** §6's argument — *unconditional prose reads as universal permission* — applies to
this file verbatim. Rewriting `auto-admin` while its unscoped twin sits in the same directory
does not remove the fork; it **relocates** it. The next fleet needing a scoped policy equips the
twin and hand-copies an allowlist into it, and we are back where #1159 started.

And §9's proposed drift detector **cannot catch that**, because it compares overlay-versus-shared.
These are two *shared* files. The detector is blind to this class by construction (§9).

### 5A.2 — LIVE IN PRODUCTION: `review-flow.md:24` grants unscoped `--admin` to both reviewers

Not a future risk. Shipping now.

```
review-flow.md:24
- `--admin` merge is allowed: `gh pr merge <n> --squash --admin --delete-branch`.
  Branch protection's "requires approvals" only counts formal `APPROVE`. Red lines:
  never `--no-verify`, never force-push main, never `--admin` without an actual peer
  review on the PR.
```

Equipped at `fleet.yaml:272` (ramanujan) and `:285` (damodaran). Verified in composed output by
**content**, not filename:

```
$ grep -c -- '`--admin` merge is allowed' local/artemis-engineering/runtime/bots/*/CLAUDE.md
damodaran:1
ramanujan:1        # every other bot: 0
```

An unscoped `--admin` grant, with no allowlist and no repo scoping, sitting in the same composed
document as the six-repo allowlist. **The contradiction §9 treats as a future drift risk is live
today.**

The sharpest form of it: both bots carrying this grant have the mission
*"Code reviewer. ... No commits, no merges."* (`fleet.yaml:270-271`, `:283-284`). The fleet hands
an unscoped merge-bypass grant to precisely the two bots it tells not to merge.

**One correction to the reported count, because the distinction is the whole point.** The review
reported the instruction appearing *"twice in ramanujan, twice in damodaran, once in mine."*
Measured, the **unscoped** grant appears **once** in each reviewer and **zero** times in
takahashi — who does not equip `review-flow` at all. The reported figures are consistent with
counting the *scoped* allowlisted guardrail's merge command (`Merge command: gh pr merge <n>
--squash --admin --delete-branch`, present in all 9 bots) alongside the unscoped grant. That
conflates the scoped instruction with the unscoped one, which is exactly the distinction that
makes this a defect.

**Total live unscoped grants: 2, both on reviewers.** Smaller than reported, and worse placed.
The finding stands entirely; only the arithmetic needed correcting, and it needed correcting in
the direction of *precision*, not of dismissal — a fix that removed 5 occurrences would have
removed 3 correct ones.

### 5A.3 — the mutual-exclusion invariant is enforced by nothing

```
merge-policy-auto-admin.md:28
**This guardrail replaces both `merge-policy-human` and `no-merge-admin`.** Do not stack with either.
```

An invariant, in prose, about which guardrails may coexist on one bot. Searched `validator.py`,
`config.py` and `known_values.py`: **no guardrail-conflict check exists.** The only `conflict`
logic in `validator.py` (`:630-639`) is tools-deny-versus-expertise — unrelated. A fleet may equip
`merge-policy-auto-admin` and `no-merge-admin` together today and `claudlobby validate` passes.

§6 proposed three new `validate` gates and **the one the library already asks for in prose was not
among them.** Corrected in §6; what happens to line 28 itself is answered in Phase 3.

---

## 5. Phasing — the live contradiction dies first, then the machinery

#1159 treats both forks as one deletion. They are not — and after §4A, they are not the whole
job either. **Six phases. Two of them need no new machinery and should ship immediately.**

### Is this now too big to ship safely?

**The plan is bigger; no individual phase is.** The scope grew by one file that must be fixed now
(Phase 0), one `validate` gate (folded into Phase 3), and one genuine design question about the
guardrail family (Phase 4). The phase boundaries from Rev 1 survive review unchanged — Phases 1
and 2 are byte-identical to Rev 1's Phases 1 and 2.

What I will **not** do is widen Phase 3 into "rewrite five guardrails at once." That is where the
size becomes real, so it gets its own phase and its own ratification (Phase 4), and it lands
*after* the live defect is gone rather than being blocked behind it.

The honest summary: **Phase 0 is urgent and small. Phase 4 is large and not urgent.** Rev 1
conflated them by seeing neither.

### Phase 0 — remove the live unscoped `--admin` grant (URGENT, no new machinery)

**Ships first, independently, before anything else in this plan.** §4A.2 is a defect in
production, not a plan item.

`library/protocols/review-flow.md:24` grants unscoped `--admin` to two bots whose mission says
*"No commits, no merges."* The fix is deletion, not scoping: a **review-flow** protocol has no
business granting merge authority at all — same overreach as Phase 1's, in a different file.

**Change:** strike the `--admin` grant from `review-flow.md:24`, leaving the same-identity
*verdict* mechanics (lines 19-23) intact. Merge authority lives in the merge-policy guardrail,
full stop.

- No Jinja, no config, no compositor change.
- Does not depend on Phases 1-5 and must not wait for them.
- **Reviewers are the correct place to fix first**: they have no merge duty, so removing the grant
  cannot regress any workflow the fleet actually runs.
- Carrier caveat (§8): both reviewers keep the grant until they restart. For this phase that is
  the *argument for* a prompt restart, not a reason to defer.

### Phase 1 — collapse `same-identity-fallback` (no compositor change)

The fleet fork's own header states it: *"Identical to the shared library version except for the
merge stance."* Confirmed by diff — the only substantive divergence is that the shared base
asserts merge authority it has no business asserting:

```
- Use `--admin` to merge over the block. The human merges.
...
**Why this matters:** the merge policy is human-gated regardless.
```

A *same-identity comment fallback* protocol should describe **how to post a verdict you are
blocked from posting formally.** Merge authority is a different decision, owned by the
merge-policy guardrail. The shared protocol overreaches into a neighbouring concern, and that
overreach is the sole reason the second fork exists.

**Change:** edit the shared protocol to defer merge authority to whichever merge-policy guardrail
is equipped, rather than asserting human-gating. Then delete the fleet fork.

- No Jinja. No config. No new failure mode. Ships and canaries independently.
- Removes fork #2 and **halves the drift surface before the risky work starts.**
- Correct on its own merits even if Phase 2 is never built.

### Phase 2 — opt-in templating (§3)

`template: true` frontmatter → post-`_expand` Jinja render with `StrictUndefined`. Context:
`projects`, `bot`, `fleet` (§4 widening). Plus `tests/test_library_templating.py` pinning the 4
dbt/Snowflake files.

**No policy change ships in this phase.** Mechanism only, with zero opted-in files. Isolates a
compositor change from a merge-authority change — if Phase 2 regresses, no bot's merge
permissions moved.

### Phase 3 — parameterize the shared guardrail, delete fork #1

Rewrite `library/guardrails/merge-policy-auto-admin.md` with `template: true`, rendering its
allowlist from `projects` where `validation.tier == "review"`. Delete the fleet overlay fork.

**Preserve the fork's prose.** The 79-line fork is not 51 lines of duplication — it carries
ratified reasoning the 28-line base lacks: *why* `--admin` is required rather than a shortcut,
the outside-the-allowlist stop-and-flag instruction, the `projects.yaml`-is-source-of-truth note,
the extended red lines. **Parameterize the allowlist; port the prose to the shared file.**
"Delete the forks" must not mean "discard the ratified policy text" — a real risk in a
mechanical reading of #1159 step 3.

**Also lands the mutual-exclusion gate** (§4A.3, §6): `validate` fails when a bot equips two
merge-policy guardrails. It ships here rather than earlier because until Phase 3 there is nothing
new to conflict *with*, and it must exist before Phase 4 can safely be deferred — while the family
still has five members, the invariant is the only thing standing between a fleet and two
contradictory merge policies on one bot.

**Scoping note:** Phase 3 rewrites **one** file. It does **not** touch
`merge-policy-auto-after-review.md`. That is deliberate — see Phase 4 — but it does mean Phase 3
alone leaves an unscoped twin in the library. Phase 3 must therefore add a one-line pointer in
`auto-after-review.md` naming the scoped guardrail, so the twin is at minimum *signposted* rather
than silently equippable, even if Phase 4 never ships.

### Phase 4 — the guardrail family: collapse or scope? (NEEDS RATIFICATION)

The largest phase and the only one with an unresolved design question. **Not urgent** — Phase 0
removes the live defect and Phase 3's gate contains the rest — so this can be ratified slowly.

**The observation:** the five guardrails are not five policies. They are **two axes**:

| file | axis A — who closes | axis B — is `--admin` needed to physically merge |
|---|---|---|
| `merge-policy-auto-admin` | peer review | yes |
| `merge-policy-auto-after-review` | peer review | no |
| `merge-policy-human` | human | n/a |
| `no-merge-admin` | (negates B) | forbidden |
| `no-merge-own-pr` | role constraint, orthogonal | n/a |

The top two differ **only on axis B** — and axis B is not a policy choice at all. Whether
`--admin` is required is a **fact about the repo's branch protection** under a shared identity, as
`auto-admin`'s own §"Why `--admin` is required, not a shortcut" argues at length. A fleet does not
*decide* to need `--admin`; it discovers it.

**Fork 4a — collapse to one parameterized guardrail (recommended).** Allowlist from `projects`;
axis B stated as the mechanical fact it is; `merge-policy-human` becomes the *empty-allowlist
rendering* of the same file; `no-merge-admin` becomes a property of that rendering rather than a
separate policy; `no-merge-own-pr`'s role constraint is already carried as a red line inside
`auto-admin` ("never merge a PR the manager itself authored without a separate reviewer") and is
absorbable. **Five files → one.** This is `consolidate-dont-fork` applied to the surface that
motivated the principle.

**Fork 4b — keep the family, add scoping to each.** Lower risk per file, but it preserves five
places for one policy to be stated and five chances for them to drift. It is the shape that
produced this defect.

**I recommend 4a and am flagging it rather than assuming it**, because collapsing a
merge-authority family is a bigger call than anything else in this plan and deserves an explicit
yes rather than arriving inside an implementation PR.

**What happens to `auto-admin.md:28` — the question asked directly.**

Line 28 is a prose invariant that exists *only because five stackable files exist*. Its fate is
therefore decided by this fork, and it is never left as prose either way:

- **Under 4a:** line 28 is **deleted along with the files it names.** One file cannot conflict with
  itself, so the invariant stops being needed rather than being enforced — the collapse *is* the
  enforcement. The Phase 3 gate then becomes dead code and is removed with it.
- **Under 4b:** line 28 is **deleted as prose and promoted to the Phase 3 `validate` gate**, which
  becomes permanent. A machine-checked invariant, not a sentence asking to be obeyed.

Either way the rule is executable before Phase 4 closes. What must **not** happen is Phase 3
rewriting the file and carrying line 28 forward as prose into a rendered guardrail — that would
preserve an unenforced invariant through the very change meant to make policy authoritative.

### Phase 5 — drift detection (§9)

---

## 6. Second-order effect: tiers become load-bearing

### What validates a tier

Already present, and the foundation is better than expected:

- tier enum (`auto|review|preview|human`) — `config.py` `VALID_TIERS`, with did-you-mean
- project key → env-name charset — `validator.py:880-911`
- **`PROJECT_TIER_*` is already reserved** against `fleet.yaml` `env:` override
  (`tests/test_projects_tier.py:386-400`, comment: *"would silently flip a human-tier project"*).
  Someone already anticipated exactly this escalation. That guard becomes load-bearing now.

**Must be added before tiers bind:**

1. **Repo-slug shape** — `owner/name`, validated. A typo'd slug currently fails silently by
   never matching; once it gates merge authority it must fail at `validate`.
2. **Duplicate repo across projects → hard error.** A repo listed in a `review` project and a
   `human` project is *ambiguous merge authority*. Must not resolve last-wins or dict-order.
   This is the one new failure mode the change creates, and it is a hard `validate` error.
3. **No network validation.** `validate` must not hit GitHub. (`projects.yaml`'s comment records
   a real 301-redirect miss — `artemis-quality-hub` → `artemis-data-hub`. That check belongs in
   a separate opt-in door, not in `validate`.)
4. **Guardrail mutual exclusion — the gate the library already asks for.** A bot equipping two
   merge-policy guardrails is a hard `validate` error. This is not a new rule: it is
   `merge-policy-auto-admin.md:28` (*"replaces both `merge-policy-human` and `no-merge-admin`. Do
   not stack with either"*), which today is prose enforced by nothing (§4A.3). Rev 1 proposed
   gates 1-3 and missed the one already written down — which is its own small lesson about
   inventing validation while an unenforced invariant sits in the file being rewritten. Ships in
   Phase 3; its fate under the family collapse is settled in Phase 4.

### A repo in no project

**Human-gated, by construction — allowlist inversion.** The rendered guardrail must *state the
catch-all in prose*, not merely omit the repo. Precedent: `mention-rewrite.py`'s allowlist
inversion — the harm class is "anything not explicitly cleared," which no denylist can enumerate.

`projects.yaml` already documents this as a KNOWN GAP the guardrail covers in prose. Phase 3
keeps that sentence — now rendered, still stated.

### A fleet with no `projects.yaml` at all

`load_projects` returns `{}` for an absent file (`config.py:347-351`). So the allowlist renders
empty → **everything human-gated.** Safe.

This is the right soft-fail under the #1146 rule, and worth stating precisely, because #1146
warns against exactly this shape: *"a soft-fail is right wherever empty means do nothing, and
wrong wherever empty licenses a write or a delete."* Here **empty means refuse to merge** — do
nothing. Soft-fail is correct.

**But it must be visible, not silent.** Render an explicit line —
*"No `projects.yaml` declared: every repo is human-gated"* — rather than an empty table that
reads as an oversight. `brief.py`'s LABELED-vs-OMITTED discipline: a field neither present nor
disclosed does not exist.

### The inversion is the actual live hazard — and it is the SHARED file

Worth stating plainly, because it reframes the priority:

**The shared base guardrail is the more dangerous artifact of the three.** It ships **no
allowlist**, and its unconditional prose — *"The manager auto-merges PRs using `--admin` when ALL
of..."* — reads as universal permission. `fleet.yaml:75-77` records this explicitly: *"base ships
NO repo allowlist, so it would have authorized autonomous merge on every Artemis repo."*

That is the #1146 defect in its dangerous orientation: **absence of an allowlist read as
permission for everything.** Today only this fleet's fork prevents it — meaning the mitigation
for a shared-library hazard is a fleet-local file that #1159 proposes to delete.

**Ordering constraint: fork #1 cannot be deleted before the shared file renders a real
allowlist.** A window where the fork is gone and the base is unchanged is a window where every
Artemis repo is autonomously mergeable. Phase 3 must land the rewrite and the deletion in **one
commit**, not two PRs.

---

## 7. What we are NOT doing

- **Not building closure gates.** Tiers become authoritative *for the composed merge-policy
  guardrail only*. `lib/` still consumes nothing; goal-aware-fleet P6 is unchanged and unblocked.
- **Not globally templating the library.** §3.
- **Not adding `preview`/`auto` tier semantics** to the guardrail. Only `review` is selected;
  everything else is human-gated. Their merge semantics are undefined and inventing them here
  would be exactly the unratified policy expansion this plan exists to prevent.
- **Not changing which repos are autonomous.** Output must be the same 6 repos, from config
  instead of prose. Any diff in the *set* is a bug, not an improvement.
- **Not extending the allowlist.** Chris's decision, per the guardrail's own red line.
- **Not rewriting five guardrails in Phase 3.** Phase 3 touches one file plus a signpost; the
  family question is Phase 4 and needs ratification (§5, Phase 4). Widening Phase 3 to cover the
  family is exactly the "just widen the blast radius" move this revision was asked not to make.
- **Not deferring Phase 0 behind any of this.** The unscoped `--admin` grant on two reviewers is a
  live defect and ships on its own timeline (§5, Phase 0; §10 q5).

---

## 8. Canary contract — what must be demonstrated before fleet rollout

Per `canary-rollout` and the mandatory empirical-validation gate: unit tests prove composition,
only running it proves behavior. **Canary one production bot before the fleet.**

**Safety precondition:** the canary never merges anything. It asserts on *rendered text* and
*refusals*. The throwaway bot gets no PAT capable of merging into a live Artemis repo. A canary
for a merge-authority change must not be able to exercise merge authority.

| # | check | pass condition |
|---|---|---|
| 1 | Compose throwaway fleet **with** `projects.yaml` | guardrail renders exactly the 6 `tier: review` repos |
| 2 | **Manager vs worker agreement** (§4 regression) | allowlist section **byte-identical** in manager and worker `CLAUDE.md` |
| 3 | Compose with **no** `projects.yaml` | renders "all human-gated" + explicit disclosure line; no empty table, no crash |
| 4 | Repo in **no** project | absent from allowlist **and** catch-all stated in prose |
| 5 | **Duplicate repo, conflicting tiers** | `claudlobby validate` **fails** with both project keys named |
| 6 | `claudlobby diff` on real `artemis-engineering` after fork deletion | **only** merge-policy sections differ; nothing else moves — this is the proof consolidation is behavior-preserving |
| 7 | Full-library generate with a bot equipping the dbt/Snowflake lessons | build succeeds — proves opt-in spared the 4 files |
| 8 | Negative: file with `template: true` and a bad tag | build **fails loudly, naming the file** — never a blank allowlist |
| 9 | **Behavioral read** — dispatch the canary bot: "which repos may you merge autonomously?" | names exactly the 6; names `dbt`/`modal-pipelines` as human-gated |
| 10 | **Behavioral refusal** — ask it to merge an unlisted repo | refuses, cites human-gating |
| 11 | Rollback | restore forks, regenerate → **byte-identical to pre-change** composed output |
| 12 | **Phase 0 — no unscoped `--admin` survives anywhere in composed output** | `grep -rc -- '\`--admin\` merge is allowed'` over every bot's `CLAUDE.md` returns **0**, and the only surviving `--admin` instruction is the allowlist-scoped one |
| 13 | **Phase 0 behavioural** — ask a restarted reviewer to `--admin` merge a PR | refuses; cites having no merge duty. Run against **both** reviewers, since both carry the grant |
| 14 | **Stacked guardrails rejected** (§6 gate 4) | a fleet equipping `merge-policy-auto-admin` + `no-merge-admin` **fails** `validate`, naming both |

Checks 9-10 are the ones that matter and the ones unit tests cannot reach: composition proves the
text landed; only a dispatch proves the bot *reads it as policy*. Cite the observation in each
PR body — claimed evidence is not evidence.

**Check 12 must grep by content, not by filename.** That is how §4A.2 was found and how Rev 1
missed it: the unscoped grant lives in `review-flow.md`, a file whose name contains no hint that
it grants merge authority. A filename-scoped audit of "the merge-policy files" returns clean
while the contradiction sits in a protocol two directories away.

### The carrier constraint — the canary must restart before checks 9-10

This change ships by the **composed-file** carrier, which
[`documentation/fleet-update-lifecycle.md`](../fleet-update-lifecycle.md) flags as *"the one that
misleads"*: a running bot does **not** have the new `CLAUDE.md` until it restarts. Generating and
then immediately asking the canary bot which repos it may merge measures the **old** policy and
reads as a pass. Restart the canary between check 7 and check 9, or the behavioural half of this
contract proves nothing.

It also defines the rollout window: after merge, every already-running bot keeps the **fork's**
allowlist until its next restart, while freshly-restarted bots carry the rendered one. That window
is safe **only because check 6 pins the two to the same 6 repos.** If the rendered set ever differs
from the fork's, the fleet spends that window split across two merge policies — which is why "same
6 repos, from config instead of prose" (§7) is an invariant and not an aspiration. No rolling
restart is required if check 6 holds; if it does not, the change is wrong, not the rollout.

---

## 9. The risk this plan does not close

**Consolidation without drift detection re-forks.** #1159's own framing: *"Forks drift silently
... and nothing reports the divergence."* Deleting two forks does not stop a third. The next
fleet with an unexpressible policy will hand-copy prose again, because that path is still open
and still undetected.

**Proposed Phase 5 (separate issue, not this plan's scope):** `claudlobby doctor` warns when a
fleet overlay shadows a shared library file whose content has diverged. Cheap, uses existing
machinery (`conformance.py` already does rename-map drift and boundary invariants), and it makes
the *class* visible rather than this instance. Without it, the fix's half-life is one ratified
policy that config cannot express.

### The detector Rev 1 proposed would not have caught §4A — it was blind by construction

Review caught this and it is the sharper version of the risk. An overlay-versus-shared detector
compares a fleet file against the shared file it shadows. **Both defects in §4A are
shared-versus-shared:** `merge-policy-auto-after-review` restating `auto-admin`'s policy, and
`review-flow` granting what `no-merge-admin` forbids. Neither is an overlay. Neither shadows
anything. The detector returns clean on both, forever.

So the detector needs a **second, differently-shaped** check, and the second one is the harder
and more valuable of the two:

| check | catches | shape |
|---|---|---|
| overlay shadows shared, content diverged | a fleet re-forking a shared file | pairwise, by filename |
| **one policy asserted in N shared files** | the family in §4A.1, and `review-flow` in §4A.2 | **by content, across the whole library, filename-blind** |

The second cannot key on filenames — `review-flow.md` grants merge authority and its name says
nothing about merge. A plausible first cut: flag any library file outside
`guardrails/merge-policy-*` that contains a merge-authority instruction (`gh pr merge`, `--admin`),
and require an explicit frontmatter acknowledgement to keep it. Crude, but it fails in the safe
direction and it is the check that would have caught a live defect two reviewers were carrying.

**This is now the most valuable part of Phase 5, not a footnote to it.** Rev 1 had it as a
nice-to-have; it is the only proposed mechanism that generalizes past the instance.

---

## 10. Open questions for the reviewer

1. **Frontmatter key name** — `template: true` vs `render: jinja` vs `templated: true`. I have no
   strong preference; `template: true` reads best beside `title:`/`description:`. Bikeshed now,
   not after it is in 200 files.
2. **Should Phase 1 ship as its own PR?** I think yes — it is independently correct and removes
   half the drift surface at near-zero risk. A reviewer who disagrees should say so before
   implementation splits.
3. **Does widening `projects` to worker library context need its own ratification?** It changes
   no composed worker output today (`## Projects` stays manager-gated), but it does widen what
   worker-equipped library files *can* express. I read it as mechanism, not policy. Flagging it
   rather than assuming.
4. **Phase 4 fork 4a vs 4b — collapse the guardrail family to one file, or scope all five?**
   The one question in this plan I am flagging for an explicit yes rather than deciding. I
   recommend 4a; it is `consolidate-dont-fork` applied to the surface that motivated the
   principle, but it is also a five-file merge-authority redesign and should not arrive inside an
   implementation PR.
5. **Should Phase 0 ship ahead of this plan's merge entirely?** It is a one-line deletion fixing a
   live contradiction on two production bots and depends on nothing here. My view: yes — file it
   as its own issue and fix it today rather than letting it inherit this plan's review latency.
   The only reason it sits in this document is that this document is where it was found.

---

## Appendix A — the blast-radius probe

Applies `composer._expand` first (as composition already does), then renders each library
markdown file through the same `SandboxedEnvironment` the compositor uses. Save as
`/tmp/jinja_probe.py`; run against `library/` and any fleet overlay library.

```python
"""Blast-radius probe for routing library markdown through Jinja (#1159).

Order matters: composer._expand runs BEFORE the body reaches the template, so
the honest measurement expands {{KEY}} placeholders first and only then renders.
Measures what a global Jinja render would break TODAY.
"""
import pathlib
import sys

import jinja2
from jinja2.sandbox import SandboxedEnvironment

# Mirrors composer._bot_template_context keys, with placeholder values.
CTX = {
    "BOT_ID": "b", "BOT_ID_UPPER": "B", "BOT_NAME": "b", "BOT_NAME_UPPER": "B",
    "FLEET_NAME": "f", "SERVICE_PREFIX": "com.x", "CLAUDLOBBY_ROOT": "/r",
    "BOT_DIR": "/r/b", "TELEGRAM_GROUP_CHAT_ID": "-100", "SHARED_DOCS_PATH": "/s",
    "MANAGER_NAME": "m", "MANAGER_BOT_ID": "m",
}


def expand(text):
    """Stand-in for composer._expand — plain str.replace, missing keys left as-is."""
    for k, v in CTX.items():
        text = text.replace("{{" + k + "}}", v)
    return text


strict = SandboxedEnvironment(
    undefined=jinja2.StrictUndefined, keep_trailing_newline=True
)
# The context #1159 proposes handing to library files.
render_ctx = {"projects": [], "bot": None, "fleet": None}

for root_arg in sys.argv[1:]:
    root = pathlib.Path(root_arg)
    if not root.exists():
        print(f"\n### {root_arg}: ABSENT")
        continue
    files = sorted(root.rglob("*.md"))
    syntax, undef = [], []
    for f in files:
        try:
            strict.from_string(expand(f.read_text())).render(**render_ctx)
        except jinja2.exceptions.TemplateSyntaxError as e:
            syntax.append((f, str(e)))
        except Exception as e:
            undef.append((f, f"{type(e).__name__}: {e}"))
    print(f"\n### {root_arg}  ({len(files)} md files)")
    print(f"  SYNTAX errors (no context can fix these): {len(syntax)}")
    for f, e in syntax:
        print(f"     {f}\n       -> {e}")
    print(f"  UNDEFINED/other (survive _expand)       : {len(undef)}")
    for f, e in undef:
        print(f"     {f}  -> {e}")
```

Observed at `main` @ `560c3c9`:

```
### library  (201 md files)
  SYNTAX errors (no context can fix these): 1
     library/lessons/dbt/parse-vs-execute-time.md
       -> Expected an expression, got 'end of print statement'
  UNDEFINED/other (survive _expand)       : 3
     library/lessons/dbt/incremental-unique-key-discipline.md  -> 'this' is undefined
     library/lessons/snowflake/clustering-earns-its-cost.md  -> 'config' is undefined
     library/tools/README.md  -> 'data_dir' is undefined

### local/artemis-engineering/library  (3 md files)
  SYNTAX errors (no context can fix these): 0
  UNDEFINED/other (survive _expand)       : 0
```

Dropping the `expand()` call reproduces the pre-`_expand` column of the §3 table (1 syntax
error, 28 undefined) — the number to quote only if someone proposes rendering *before*
substitution.
