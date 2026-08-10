# Merge-policy consolidation — one policy, one place, rendered from config

**Issue:** [Claudfather/Claudlobby#1159](https://github.com/Claudfather/Claudlobby/issues/1159)
**Status:** plan only. No implementation in this PR.
**Branch:** `plan/1159-merge-policy-consolidation` off `main` @ `560c3c9`.
**Scope:** design + phasing + canary contract. Implementation lands as separate reviewed PRs.

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
| Delete both fleet forks | **AGREE, but split** — one fork needs no templating at all and can die first, at near-zero risk. §5 |

Two additions #1159 does not raise: the **shared base guardrail is itself the more dangerous
artifact** (§6), and consolidation without a drift detector re-forks (§9).

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

## 5. Phasing — the protocol fork dies first, with no new machinery

#1159 treats both forks as one deletion. They are not. **One needs no templating at all.**

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

### Phase 4 — drift detection (§9)

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

Checks 9-10 are the ones that matter and the ones unit tests cannot reach: composition proves the
text landed; only a dispatch proves the bot *reads it as policy*. Cite the observation in each
PR body — claimed evidence is not evidence.

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

**Proposed Phase 4 (separate issue, not this plan's scope):** `claudlobby doctor` warns when a
fleet overlay shadows a shared library file whose content has diverged. Cheap, uses existing
machinery (`conformance.py` already does rename-map drift and boundary invariants), and it makes
the *class* visible rather than this instance. Without it, the fix's half-life is one ratified
policy that config cannot express.

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
