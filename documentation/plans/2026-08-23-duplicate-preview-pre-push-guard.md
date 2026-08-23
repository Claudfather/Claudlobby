# Duplicate-preview pre-push guard — measurement, mechanism, and design proposal

**Status:** SPEC ONLY. Nothing implemented, no canary run. Requested by a downstream consumer
fleet after a shared Vercel deployment cap was tripped.
**F1 is answered — decouple** (§7), on a stronger basis than this document originally gave: the
gitconfig composition path is dormant estate-wide, so coupling would compose the guard onto zero
bots (§2.4). F2 and F3 remain open. That answer is deliberately narrow — it unblocks the fork
without ratifying or scheduling the rest of the spec, and nothing here carries a deadline.
**Branch:** `spec/duplicate-preview-pre-push-guard` off fresh `main` @ `454c3ff`.
**Asks of this repo:** review the shape, and rule on **F2 and F3** in §7. F1 is already
resolved (decouple), but the landing is yours: it needs a small `composer.py` change widening
what `GIT_CONFIG_GLOBAL` means, plus the `fleet.yaml` surface that enables the guard as its own
declaration.

Repo and branch names below are placeholders except where the real target matters. The
affected consumer app is `<app>`, a Vercel-connected project in a consumer fleet; this
package's own repo is named where it is the subject.

---

## 1. The phenomenon, as reported

Two branches carried the **same head commit sha**. Vercel treats each branch as its own
deployment and issued a preview for each — distinct deployment uids, distinct preview URLs,
**21 seconds apart**. Confirmed on the Vercel side by the reporting fleet; both branches were
theirs.

The cost is not the duplicate itself. Six Vercel projects share **one account-wide 100/day
rolling deployment cap**. It tripped, and blocked a customer-facing fix in an unrelated fleet.
At the time of the report previews were 78% of the window and `<app>` alone accounted for 58
of 76 previews.

Those figures are the reporting fleet's measurements at dispatch time and are cited, not
re-derived. They are a snapshot of a rolling window and will not re-run equal.

---

## 2. What was measured for this spec

Four premises in the original framing were checked. Two do not hold, and both change the
design.

### 2.1 The mechanism is one commit under two refs — not two workers colliding

The original framing was "two workers branch from the same base, both push, both heads land on
the same sha." The evidence does not support that reading.

Evidenced, from the GitHub API:

| fact | value |
|---|---|
| commit | `d80f927264b3…`, authored `2026-08-21T22:22:19Z` |
| tree | `cb39577d…` — 3 files, one script, its test, and a changelog entry |
| subject | `fix(#942): reachability hits must be differential, and the controls must ship` |
| branch A | `<bot>/942-<slug>` — head ref of **PR #972**, merged `2026-08-21T23:03:24Z`, head sha at merge `57dbb255…` (i.e. later commits followed) |
| branch B | `fix/972-<slug>` — `pulls?state=all&head=…` returns **empty**: it never had a PR, and was deleted |

Branch B is named for **PR #972**, and PR #972's own head branch is branch A. The commit's
subject names issue #942, which is what PR #972 implements.

The reading that fits: a fix branch was cut to address review on the PR, the commit was then
also pushed onto the PR's own head branch 21 seconds later, and the side branch was abandoned
without ever opening a PR of its own.

**Evidenced vs inferred, stated because the difference matters:** the refs, shas, timestamps
and the absent PR are measured. The intent — "cut a side branch, then realised it belonged on
the PR branch" — is the reading that fits them, and is not recoverable from the API. It is
worth separating, because the two-independent-workers reading implies a *coordination* fix
(who may branch from what), while this one implies a *hygiene* fix (a redundant ref was left
pointing at a commit that had a home).

**Design consequence.** The duplicate ref was a **newly created branch** at a commit that also
reached an existing branch. A guard scoped to new-branch creation catches exactly the ref that
should have been held and never evaluates the legitimate push to the PR branch. §3 uses this.

### 2.2 The bots do not share one working tree

The dispatch stated "hooks are per-checkout and 21 bots share one working tree." Measured on
the host: **8 independent clones of `<app>`**, one per bot, each with
`git-dir == git-common-dir == .git` — separate clones, not shared worktrees.

So the per-checkout problem is worse than assumed rather than mitigated: a hand-installed hook
is N installs per repo per bot across 9 in-scope repos, and a bot's next `git clone` starts
unguarded. This is what moves F1 away from "install a hook" and toward "carry it in config."

### 2.3 There is no push path to put "one command" into

The ask was "one command in the push path." `grep -rl "git push" lib/` returns **zero hits**.
No worker pushes through a helper in this package today; every push is a raw `git push` in the
bot's own session.

A check cannot be inserted into a path that does not exist. The options are to *create* the
path and then get every bot to use it — which this repo's own history rules against, since
`lib/run-discrimination.sh` exists precisely because "an instruction to run a check is not a
check" (three instructions failed on #1236 alone; #1032 is the general case) — or to intercept
at git's own layer, which needs no adoption.

### 2.4 The carrier exists — and the path that composes it is dormant estate-wide

`composer.py::compose_bot_gitconfig` composes a **per-bot `.gitconfig`**, and `compose_bot_conf`
exports `GIT_CONFIG_GLOBAL="$BOT_DIR/.gitconfig"`. That is a composer-owned, per-bot git config
that applies to **every repo the bot ever clones**, is regenerated by `generate`, and needs no
per-checkout installation.

**The gate, stated correctly.** The early return is:

```python
if not (bot.git_credentials or bot.github_app):
    return None
```

**Two** declaration surfaces, not one — `github_app` is the App-auth P3 path (#1273). An earlier
revision of this document named only `git_credentials`. That was a misreading of the source, not
a change in it: verified against `454c3ff` (this branch's base) and `origin/main`, which are
identical here, with the line dating to `c618e6e4` (2026-08-19), four days before it was read.
Recorded rather than quietly corrected, because F1 is the fork that turns on exactly this
condition and a fork stated over half a boolean is not ratifiable.

**Both surfaces are unused on this estate.** Measured across every fleet and every bot on the
host:

| declaration | fleets declaring it |
|---|---|
| `git_credentials` | **0 of 4** |
| `github_app` | **0 of 4** |

| artifact | bots carrying it |
|---|---|
| any gitconfig-shaped file, any name, in the bot dir | **0 of 21** |

So the gitconfig composition path is **entirely dormant**. This is what decides F1, and it is a
stronger reason than the one this document first gave. Coupling the guard to that gate does not
make its presence "depend on an unrelated credential setting" — it means the guard **composes
onto zero bots**. Nothing fires anywhere, and the spec reads as delivered.

> **A guard that composes onto nothing passes every test it has, because there is no bot on
> which it could fail.** Worth naming as its own class. Unit tests do not catch it — the
> composer function is correct. `generate` does not catch it — it emits what it was asked to
> emit. A green PR does not catch it. It is caught only by asking how many bots the artifact
> actually landed on, which is a denominator question of the same shape
> `selfstart-snapshot.sh` refuses to publish without.

Decoupling is therefore not an improvement over coupling. It is a **precondition for the guard
existing at all**.

Two further measured facts:

- `grep -rn hooksPath` over `claudlobby/`, `lib/`, `templates/` and `library/` → **zero hits**;
  the only occurrences under `documentation/` are this file. `core.hooksPath` is greenfield
  here — §4/Q1 on why that cuts both ways.
- In a live `<app>` checkout: `core.hooksPath` unset both local and global, `.git/hooks` holds
  only `.sample` files, no husky. Nothing would be displaced **today**.

### 2.5 What is and is not measurable historically

The dispatch said not to spend time sizing this. That conclusion is correct, and the reason is
worth recording so nobody re-runs it — but it is correct only about *GitHub*.

**GitHub's events API partly exposes it and is still not enough.** `repos/:o/:r/events` returns
`PushEvent` with `{ref, head, before}`. Over the window it served — 300 events,
`2026-08-22T00:14Z` → `19:04Z`, 63 pushes — **zero shas were pushed to more than one ref**.

That zero bounds nothing, for two independent reasons:

1. The window is ~19 hours on a repo this active, and does not reach the `2026-08-21T22:22Z`
   instance.
2. `CreateEvent` carries **no sha** — its payload keys are
   `ref, ref_type, full_ref, master_branch, description, pusher_type`. A duplicate ref that was
   *created* rather than *pushed onto* is invisible on this door, and §2.1 shows the observed
   case is exactly that shape.

So the door is both too short and structurally blind in the observed direction. Reported as a
bounded negative, not as evidence of rarity.

**Vercel can measure it, and retrospectively.** This is the one correction to the dispatch's
"you cannot size it historically." A deployment record retains `meta.githubCommitSha` and
`meta.githubCommitRef` **after the branches are deleted** — the record outlives the ref.
Grouping deployments by sha and counting groups whose ref-set has size > 1 measures the
phenomenon directly, historically, on the side that already confirmed it. §6 gives the query.
It was not run here: it is outside a spec-only scope and the Vercel credential sits with the
reporting fleet.

---

## 3. The predicate

Evaluated in `pre-push`, which receives one line per ref on stdin:

```
<local_ref> <local_sha> <remote_ref> <remote_sha>
```

- `remote_sha` all-zeros → **creating a new remote branch**. This is the only case evaluated.
- `local_sha` all-zeros → a delete. Ignored.
- otherwise → an update to an existing branch. Ignored (§2.1: the legitimate half of the
  observed pair was exactly this).

For each new-branch line:

1. `git ls-remote --heads <remote>` — one network round trip.
2. If any remote head other than `remote_ref` has sha `== local_sha` → **`COLLISION`**, naming
   the other ref.
3. If `local_sha` equals the remote default branch's sha → **`ZERO_DELTA`**: a branch carrying
   no commits of its own, whose preview would be byte-identical to production. Same waste,
   distinct label, different remedy.

**Scoping.** The guard evaluates only where a duplicate costs a deployment. Rather than a
hardcoded repo list — which re-breaks on the next project, the failure class documented on
`lib/gh-mention-guard.sh` — key it on the presence of **`.vercel/project.json`** in the repo
root, the artifact Vercel itself writes. `<app>` has one. This keeps the guard inert in this
package's own repo, in the vault, and in every non-Vercel checkout, which matters because a
global `hooksPath` fires on every push the bot ever makes.

### 3.1 Bounds, stated now rather than discovered later

- **TOCTOU.** `ls-remote` is a point-in-time read. Two pushes closer together than one round
  trip both see a clean remote and both land. The observed pair was 21 seconds apart, well
  outside; a genuinely simultaneous pair is not covered and cannot be by a client-side check.
- **Tree identity is the truer predicate and is not covered.** Two *different* shas over the
  same tree — a rebase onto an unchanged base, an amended message, a cherry-pick — build
  byte-identical output and waste a slot identically. `ls-remote` returns shas, not trees, so
  covering this means resolving a tree per remote head; `<app>` carries 87 live branches, so
  that is a fetch or an API call per ref on every push. Recorded as a named gap, deliberately
  not built. If the §6 baseline shows same-tree/different-sha pairs dominate, this spec is
  aimed at the smaller half and should be revisited.
- **`git push` only.** `mcp__github__push_files` and `create_or_update_file` write to the
  remote without git and bypass any hook. `lib/gh-mention-guard.sh` covers both the Bash and
  MCP surfaces for exactly this reason. Whether those tools create branches in practice here is
  **unmeasured**; if the guard ships and its ledger shows collisions continuing, this is the
  first place to look.
- **`--no-verify` bypasses it.** Not a hole to plug. In warn mode it is the escape hatch that
  keeps one bad interaction from getting the whole guard disabled.

---

## 4. The four design questions

### Q1 — Where it lives

**Recommend: `core.hooksPath` in the composed per-bot `.gitconfig`, pointing at a shared hook
that delegates to one `lib/` script.** Three layers, each doing only what it can:

| layer | artifact | why here |
|---|---|---|
| logic | `lib/dup-ref-guard.sh` | one copy estate-wide (`lib/` is a shared install); standalone-testable and hand-callable, the `dispatch-overdue.py` precedent |
| invocation | `core.hooksPath` → shared dir with a 2-line `pre-push` that execs the above | composer-owned, per-bot, covers every repo the bot clones now or later, regenerated by `generate`, no per-checkout install, cannot drift |
| instruction | a guardrail clause | documents *why a hold happened* — never the mechanism |

Rejected, with reasons:

- **Per-checkout hook install.** 8 `<app>` clones today, 9 in-scope repos, ~21 bots — and every
  future `git clone` starts unguarded. The drift is unbounded and silent (§2.2).
- **Worker instruction alone.** Known-insufficient in this repo's own record (§2.3).
- **A `lib/push.sh` wrapper.** It creates the path the dispatch assumed exists, but adoption
  then *is* an instruction again, and its failure mode is silent: a bot that forgets to use it
  looks identical to a bot with nothing to push.

**Prerequisite, and it is not a preference.** `compose_bot_gitconfig` returns `None` unless
`bot.git_credentials` **or** `bot.github_app` is declared — and §2.4 measures both at **0 of 4
fleets**, with **0 of 21 bots** carrying any gitconfig-shaped file. So hanging the guard off
that file as-is does not make it *conditionally* present; it composes the guard onto **zero
bots**, where it would pass every test it has because there is no bot on which it could fail.
The file's existence must be **decoupled**: compose it when credential routing *or* App auth
*or* the guard is enabled. That is fork **F1**, resolved to (a) in §7.

**Decoupling touches FOUR sites, not two.** An earlier revision of this document said two.
That was measured with a grep scoped to `composer.py` and then stated as a property of the
tree — the bound was never measured at the scope it was asserted at. Re-measured tree-wide,
four sites gate on the exact pair `git_credentials or github_app`:

| module | symbol | gates | if left unwidened |
|---|---|---|---|
| `composer.py` | `compose_bot_gitconfig` early return | whether the `.gitconfig` is **composed at all** | no file; `core.hooksPath` has nowhere to live |
| `composer.py` | `compose_bot_conf`, the `GIT_CONFIG_GLOBAL` export block | whether git ever **reads** it | the file is composed and nothing points git at it |
| `freshbox.py` | the operator-gitconfig include check | whether the fresh-box gate **asserts the `[include]` target exists** | the include risk is uncovered exactly where it was newly created |
| `validator.py` | the `_operator_git_identity_problem` guard | whether `validate` **warns** on the same prerequisite | same, at validate time |

The last two are the assertions the `compose_bot_gitconfig` docstring promises — *"asserted by
freshbox (fail) and by the validator (warn)"* — so a fix moving only the first two silently
drops the guarantee the first two depend on.

**Sites are cited by SYMBOL, not line number, and that is deliberate.** Reviewing this document
produced a disagreement over whether the validator gate sat at line 432 or 482. Both readings
were correct: the shared `lib/` install ran **three commits behind** the checkout under review,
`validator.py` differed between them, and the same gate sat at 432 in one tree and 482 in the
other. A line number is a fact about a tree, not about the code — and this package's operating
model (a hand-pulled shared install) guarantees the two trees diverge. Anchor on the symbol.

**The bound is scoped, stated at its scope.** Four sites gate on the *exact pair*. Adjacent
**single-surface** gates also exist in `validator.py` — one on `github_app` alone, one on
`git_credentials` alone — and were not part of that sweep. Under F4 none of this moves, so it
is moot for the decision; it is recorded at its true scope because this was the third bound in
one review measured narrower than it was stated.

The second is the dangerous one. Widen `679` alone and the `.gitconfig` lands on disk,
byte-correct, inspectable, obviously present — and nothing points git at it, so the hook never
runs. That is the §2.4 class one level down: a guard that composes onto a file no process reads
also passes every test it has. The composed artifact being *present and correct* is what makes
it convincing.

The comment above `:1196` will also need rewriting, not just its condition — it currently reads
*"Only when the bot declares credentials, so fleets that declare none compose byte-identically
to before,"* which stops being true the moment a fleet enables the guard without declaring
credentials. A mechanism change orphans its own self-description, and the printed surface is
the one that gets missed.

**Measured, not argued.** Both arms run locally against a real `git push` to a local bare
remote, with a composed gitconfig setting `core.hooksPath` at a hook that writes a marker:

| arm | gitconfig file present | `core.hooksPath` resolves to | hook fired |
|---|---|---|---|
| A — file composed, `GIT_CONFIG_GLOBAL` **not** exported (a 679-only fix) | **YES** | *empty* | **NO** |
| B — same file, `GIT_CONFIG_GLOBAL` exported (679 + 1196) | YES | the hooks dir | **YES** |

### 4.1 The 679-only fix would blind the instrument that found the problem

Read arm A's first and last columns together. **The file is present and the hook does not
run** — and "a gitconfig-shaped file is present" is exactly the metric §2.4 uses to establish
dormancy.

So after a 679-only fix that dormancy metric goes from **0 of 21** to **21 of 21** while
nothing functionally changes. It does not merely go blind; it **inverts**, reporting the
maximum available success at the precise moment the fix is half-landed. Anyone re-running the
§2.4 measurement to confirm the fix would read total success from a fleet on which the guard
executes nowhere.

The general form, worth stating because it outlives this fork: **a presence-of-artifact metric
measures the property only while nothing else can produce the artifact.** The fix is the thing
that produces the artifact. So a dormancy detector built on artifact presence is destroyed by
its own remedy — it starts measuring the remedy's *output* instead of the remedy's *effect*.
This package already carries the pattern in two places: `naked-bot-observe.py::_assert_compositor`
refuses to run against a stale install because such a run "comes back green having tested
nothing, a failure mode that is a PASS", and `freshbox` reports `OK — Self-contained` on a bot
carrying six undeclared protocol sections because it audits grants and never opens `CLAUDE.md`.

**So F1's acceptance gate must assert that the guard EXECUTES on a real bot, never that the
file exists.** Minimum: with `bot.conf` sourced, `git config --get core.hooksPath` is non-empty
*and* a real push fires the hook. That is a `rehearse-*` harness in this repo's existing family
(`rehearse-env-cascade.sh`, `rehearse-keepalive-swap.sh`, `rehearse-briefing-timer.sh`) — a
throwaway bot driven through the real lifecycle — not a unit test over the composer, which
passes in arm A.

*Credit: the inversion was named by the consumer fleet's manager; the two-site split it applies
to came out of an error he made and corrected in the same message.*

**Second-order effect, flagged because it is fleet-wide and greenfield.** A global
`core.hooksPath` **replaces** `.git/hooks` for every repo without a local override. Husky is
safe — it sets `core.hooksPath` at repo level, and local config wins over global (verified by
direct experiment, not assumed).

`hooksPath` appears **nowhere** in this package (§2.4), so there is nothing to break — and
equally **no precedent to lean on**. That cuts both ways: the blast radius should be treated as
fully open, which puts this squarely in `canary-rollout` territory rather than a fleet-wide
`generate`.

The caveat that matters most: **"no repo currently relies on `.git/hooks`" is a fact about
today**, and any project checkout invalidates it the moment someone runs a tool that installs
one — and a global `hooksPath` is **silent when it wins**. The failure is a hook that stops
running with no error anywhere. So the shared hook must **chain** to `.git/hooks/<name>` when
one exists, making the guard additive rather than a replacement; and that chaining is a
correctness requirement, not a courtesy.

### Q2 — Block, warn, or wait-and-retry

**Recommend: record always, warn from the start, block only per-class once the record justifies
it.**

The dispatch's own criterion decides this — "the false-positive rate decides" — and **the
false-positive rate is not knowable today**: one confirmed instance, and the only historical
GitHub door is blind in the observed direction (§2.5). Shipping a block against an unmeasured
rate is a bet whose losing side is the guard being disabled by the first worker it wrongly
stops, after which nothing is measured either.

- **Phase 0 — record.** One JSONL row per **evaluation**, not per collision, to the bot's
  `data/events/`. The denominator is the entire point: `collisions / new-branch pushes` is the
  false-positive input, and a bare collision count is unreadable without it. Precedent in this
  repo: `dispatch-supersede-hint.py` records `open_at_dispatch` so a usage gap becomes
  measurable at all, and `selfstart-snapshot.sh` refuses to publish a numerator over a
  denominator it cannot declare.
- **Phase 1 — warn.** Name the colliding ref, print the two remedies (delete the stale ref, or
  add the commit that makes this branch distinct), exit 0. The push proceeds.
- **Phase 2 — block, per class, only if the record supports it.** `ZERO_DELTA` is the candidate
  to block first: a branch with no commits of its own has no reading that a warning does not
  also serve, and it is the cheapest class to be certain about. Plain `COLLISION` stays
  warn-only until the record says otherwise.

**Wait-and-retry is rejected outright.** Retry is for a *transient* condition. A colliding ref
persists until someone merges or deletes it — minutes to days. A retrying hook would stall the
worker's turn until timeout on every occurrence and then push anyway: a cheap warning converted
into a stall with the same end state.

### Q3 — Are there legitimate same-sha pushes

**Yes — five, and they change the verdict rather than being edge cases.**

| case | legitimate? | still wastes a preview? |
|---|---|---|
| branch rename (push new name, delete old) | yes | yes, during the overlap |
| fix branch cut for a PR, then pushed to the PR's head branch — **the observed case** | yes, as work | yes |
| stacked PRs: base and first child pushed before the child diverges | yes | yes |
| asset branch (`screenshots/<date>-<topic>` off main, never merged — a documented practice in the consumer fleet) | yes | yes, and identical to production |
| release/tag-point branch cut at main's tip | yes | yes |

The pattern across all five: **every legitimate case still produces the wasteful deployment.**

So the collision is always worth *saying* and is not always worth *refusing*. That — not
caution about false positives in the abstract — is the argument for warn-first: the true
positives are themselves mostly legitimate work whose only defect is a stale ref left behind.
The right output is therefore not "you are wrong" but "this costs a preview slot; here is the
ref to delete."

The asset-branch row deserves specific attention: it collides against the **default branch's**
sha, so a `ZERO_DELTA` block would stop a practice the consumer fleet documents in its own
lessons. If Phase 2 blocks that class, `screenshots/*` needs an exemption — or, better, the
practice changes to push the branch *with* its first commit, which removes the collision and
the wasted preview together.

### Q4 — How would you know it worked

Two doors, and the honest answer is that **the check has to be its own instrument**.

- **Primary — Vercel's deployments API (§6).** Available now, retrospective, and the only door
  that can produce a **baseline**. Run it **before** the guard ships: a baseline taken
  afterwards is not a baseline.
- **Secondary — the guard's own ledger.** Collisions held, over new-branch pushes evaluated,
  broken out per class and per repo. This is the only client-side instrument that can exist,
  which is why Phase 0 ships before Phase 1.

**Before either door reads as evidence, the guard has to be executing at all.** §4.1 shows a
half-landed F1 in which every composition-side signal reports success and the hook runs
nowhere — and in which the dormancy metric inverts from 0/21 to 21/21. A ledger with no rows
would then be read as *"no collisions occurred"* when it means *"nothing ever evaluated"*. That
is the §2.4 class reaching the measurement layer, so §4.1's execution assertion gates this
section too.

**What must not be claimed.** The ledger counts *pushes held*, not *deployments prevented*.
Converting one into the other assumes every held push would have produced a preview — the
unverifiable step. Per the dispatch's constraint, no reduction figure is offered here, before
or after. The defensible statement after a month is: *"N collisions held out of M new-branch
pushes evaluated; the Vercel-side duplicate-sha group count was X before and Y after"* — with X
measured first, and the two reported side by side rather than divided into each other.

---

## 5. Is it worth building

**Yes, but only in the staged shape above, and most of the near-term value is the
measurement.**

- Evidence of frequency is **one confirmed instance**. The only historical GitHub door shows
  zero over 19 hours and 63 pushes and is structurally blind to the observed shape, so it
  neither supports nor refutes.
- The build is small: one `lib/` script, one composed hook path, one composer change (F1). The
  recurring cost is one `ls-remote` per new-branch push in a Vercel-connected repo.
- The asymmetry that decides it: this guard is the **only instrument that can exist on the
  client side**, and the phenomenon is currently unmeasured there. Even at Phase 0/1 it turns
  an invisible failure into a counted one for roughly the cost of the code.

**The condition under which the answer flips to no.** If the §6 Vercel baseline shows
duplicate-sha deployments are a handful per month against a 100/day cap, then this is not where
the cap pressure is. The honest move then is to stop after Phase 0 — keep the counter, skip the
enforcement — and spend the effort on preview *volume* instead (§6.2). That baseline should be
run before Phase 1, not after.

---

## 6. Verification and adjacent levers

### 6.1 The Vercel baseline (for whoever holds the Vercel credential)

Paginate `GET /v6/deployments?projectId=<id>&limit=100`, then:

```
group deployments by meta.githubCommitSha
count groups where { distinct meta.githubCommitRef } has size > 1
```

Each such group is one instance of the phenomenon. Records survive branch deletion, so this
reaches back over the retention window without any GitHub door. Report the count **and** the
window it covers.

### 6.2 Adjacent levers — named, not scoped

`<app>` has **no `vercel.json`** (`.vercel/project.json` exists; `vercel.json` does not), so
every preview behavior is currently the dashboard default.

The dispatch's own figures say previews are 78% of the window and `<app>` is 58 of 76 — so the
dominant cost is *how many branches get previews at all*, not how many are duplicates. Two
levers sit closer to that:

- `git.deploymentEnabled` / branch filters in `vercel.json` — e.g. previews only for branches
  with an open PR.
- Vercel's Ignored Build Step (`ignoreCommand`) — a repo-side command that can skip a build.
  **Whether a skipped build still consumes a slot against the account cap was not verified and
  should not be assumed either way.** That single question decides whether this is a real lever
  or a no-op, and it is answerable by one support question or one measured test.

Both are consumer-side and outside this spec. They are named because a duplicate-sha guard that
works perfectly still leaves the larger share of that 78% untouched.

---

## 7. Decision forks

- **F1 — decouple the composed `.gitconfig` from the credential declarations. RESOLVED: (a).**
  Options: (a) compose it whenever credential routing **or** App auth **or** the push guard is
  enabled; (b) always compose it; (c) leave it coupled and find another carrier.
  **Resolved to (a)** on the §2.4 measurement: with `git_credentials` at 0 of 4 fleets,
  `github_app` at 0 of 4, and gitconfig-shaped files at 0 of 21 bots, coupling does not weaken
  the guard — it composes it onto **zero bots**, where it would pass every test it has. So (a)
  is not the better option, it is the precondition for the guard existing. (b) widens
  `GIT_CONFIG_GLOBAL`'s meaning for every fleet with no reason to want it; (c) has no candidate
  carrier that survives §2.2.
  **Resolved by:** the consumer fleet's platform reviewer, on the estate measurement above.
  **Remaining for this repo:** the shape of the third condition in (a) — what enables "the push
  guard" as a declaration, which is a `fleet.yaml` surface this repo owns. Landing it means
  widening **four** gate sites, not two (§4/Q1), and rewriting the comment at `:1196`; §4.1 has
  why widening a subset fails silently, including that it flips the §2.4 dormancy metric to 21
  of 21 with the guard executing nowhere. F1's acceptance gate is therefore an execution
  assertion on a real bot, never a composition test.

  **But F1 may not be this spec's prerequisite at all — see F4.** It was taken as one because
  `GIT_CONFIG_GLOBAL` looked like the only carrier for `core.hooksPath`. It is not. F1 remains
  a live question for credential routing and App auth on their own merits, with their own
  owners; it should be decided there rather than by a preview guard.

- **F2 — enforcement ladder.**
  Options: (a) record → warn → per-class block, gated on the recorded rate;
  (b) warn-only permanently; (c) block from the start.
  **Lean: (a)** — §4/Q2. (c) is a bet on an unmeasured false-positive rate.
  **Ratifier:** the consumer fleet's manager, who owns the workers it would stop.

- **F3 — scope key.**
  Options: (a) `.vercel/project.json` presence; (b) a configured repo allowlist;
  (c) every repo.
  **Lean: (a)** — self-maintaining, and does not re-break on the next Vercel project
  (`gh-mention-guard.sh`'s documented failure class). (c) makes a global hook do a network
  round trip on every push in every repo.
  **Ratifier:** this package's maintainers.

- **F4 — carry `core.hooksPath` additively; compose no gitconfig at all. RECOMMENDED.**

  **The recommendation first.** Set `core.hooksPath` through git's
  `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` env triple, exported from
  `bot.conf` and gated on the guard's own declaration. It sets config **additively**, displacing
  no file. Measured twice, independently, each against a real `git push`:

  | probe | result |
  |---|---|
  | credential-less bot, injection only | `core.hooksPath` set, `user.email` **survives**, hook **fires**, **no composed file needed** |
  | injection layered on an existing `GIT_CONFIG_GLOBAL` | include, credential routing and hook **all intact** |
  | independent replication with a synthetic host + marker helper, positive control first | helper **survives** injection; `core.hooksPath` set |

  So the guard needs **one `bot.conf` export and zero of the four sites move.** No composed
  gitconfig means no `[include]`, no credential reset, no `GIT_CONFIG_GLOBAL` override — and
  `freshbox` / `validator` staying coupled becomes *correct* rather than a coverage gap, because
  the guard creates nothing new for them to assert.

  **Why every file-composing approach was abandoned — this is the reason, not a footnote.**
  Composing a gitconfig is the obvious move, and a future reader who sees only "use
  `GIT_CONFIG_COUNT`" will propose it again. The trap is invisible until you read past the
  `[include]` line. The composed body's *next* stanza is a credential **reset**:

  ```
  # ... discard whatever helper the include installed (gh auth setup-git writes its
  # own reset + helper pair in there).
  [credential "https://github.com"]
      useHttpPath = true
      helper =
  ```

  Discarding the include's helper is the **designed** behaviour — the per-org loop below it is
  meant to take over. For a credential-less bot that loop is empty. Rendering the real
  `compose_bot_gitconfig` for such a bot (gate bypassed, shipped code path) gives a conditional
  answer that neither "it breaks" nor "it is fine" captures:

  | `gh` resolvable at **compose** time | rendered tail | credential-less bot |
  |---|---|---|
  | yes | `helper = !/usr/bin/gh auth git-credential` restored after the reset | keeps authenticated git |
  | no | reset stands alone, nothing restores it | **loses authenticated git** |

  So the file's safety rests on a **host property captured at compose time and recorded nowhere
  in the file except by the absence of a stanza** — and a file missing its last two lines is
  indistinguishable, to a reader, from one that never needed them. That is a worse shape than a
  plain breakage, and it is enough to abandon the approach even where `gh` happens to resolve.

  **`git config --get-all` cannot answer this class of question**, and anyone re-checking the
  above will reach for exactly that command. It prints raw values in list order and does **not**
  apply reset semantics, so it reports the discarded helper as present — a reassuring wrong
  answer. Only a behavioural probe settles it, and it needs **both** controls, because each
  covers what the other cannot: a **positive** arm (a config that does install a marker helper →
  the probe must see it), or a null from a broken probe reads as "no helper"; and a **negative**
  arm (`GIT_CONFIG_GLOBAL=/dev/null` → the probe must see nothing), or a hit is consistent with a
  probe that returns the marker unconditionally. The first version of this probe ran with only
  the positive arm and its result was not yet evidence. Use a synthetic host and a marker helper;
  never probe `github.com` to find out. *(The `--get-all` caveat and the probe template are the
  consumer fleet's reviewers', not this author's.)*

  **Options considered and dominated:**
  - **(a) decouple all four** — protection covers the 21 bots, but composes the reset body for
    every one of them, and on a fresh box `app_identity` requires `github_app`, which 0 of 4
    fleets declare, so it also FAILs 21 times before anyone commits.
  - **(b) decouple two** — no fresh-box FAIL, but 21 bots carry a composed `[include]` whose
    existence assertion covers none of them.
  - **(d) a minimal hook-carrier file, no include, no credential block** — closest to F4 and
    still dominated: it must be reached via `GIT_CONFIG_GLOBAL`, which **replaces** the global
    config rather than adding to it. Measured: with `GIT_CONFIG_GLOBAL` at an include-less file,
    `git config --get user.email` reads **empty**. So (d) either keeps the include (the risk
    returns) or drops it (identity is destroyed). F4 touches neither, because it never sets
    `GIT_CONFIG_GLOBAL`.

  **Two bounds on F4. Both are written as constraints on new code rather than as limitations,
  because a stated limitation reads as tolerable while a constraint is checkable.**

  **B1 — the injection namespace is positional, and a collision is silent.**
  `GIT_CONFIG_COUNT` indices carry no names: a second feature injecting config the same way
  overwrites this one with no warning, one side simply winning. There are zero other injectors
  today, which is a fact about today. *Constraint: the composed value is authoritative, and any
  future injector must compose **through** the same single helper rather than emitting its own
  `GIT_CONFIG_COUNT`.* Assembly lives in exactly one composer helper, never hand-written per
  feature.

  **B2 — the guard reaches only pushes that run in a `bot.conf`-sourcing context.**
  Measured across the package: **zero executable `git push` sites.** `lib/` has none; the four
  hits in `library/` are composed *instructions* (markdown the bot reads, not code that runs);
  the three remaining non-markdown hits are permission-deny string literals, a docstring and an
  env-var description. So every real push today happens inside a bot session, which sources
  `bot.conf` — the guard reaches **every** push on this estate.

  *Constraint: any new push path must run in a `bot.conf`-sourcing context, or declare itself
  unguarded.* The route by which this stops being true is identifiable now rather than
  hypothetical — `bot-sweep-cron.sh`, `weekly-worker-restart.sh`, `dispatch.sh` and
  `rolling-restart.sh` do **not** source `bot.conf` (measured; `start-bot.sh` does). None of
  them pushes today. Add a push to any of them, or to any cron-invoked script, and F4's guard is
  silently absent there — absent exactly where nobody is watching, which is the same shape as
  §4.1 one layer out.

  This bound is distinct from §3.1's MCP-writer gap: that one bypasses **git**, this one bypasses
  **the environment**. Neither is covered by the other.
  **Ratifier:** this package's maintainers, jointly with whoever owns F1.

---

## 8. Explicitly not in this spec

- No implementation and no canary. Per the dispatch, a canary comes after the deploy hold
  lifts, on **one** worker, not a roll — and it produces a real deployment, so it is gated on
  that hold.
- No merge of this PR by a bot.
- No reduction figure, before or after (§4/Q4).
