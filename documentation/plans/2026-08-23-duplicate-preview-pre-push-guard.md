# Duplicate-preview pre-push guard — measurement, mechanism, and design proposal

**Status:** SPEC ONLY. Nothing implemented, no canary run. Requested by a downstream consumer
fleet after a shared Vercel deployment cap was tripped.
**Branch:** `spec/duplicate-preview-pre-push-guard` off fresh `main` @ `454c3ff`.
**Asks of this repo:** review the shape, and rule on the three forks in §7 — one of them
(F1) requires a small change to `composer.py` that widens what `GIT_CONFIG_GLOBAL` means.

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

### 2.4 The carrier already exists here, and is dormant

`composer.py::compose_bot_gitconfig` composes a **per-bot `.gitconfig`**, and `compose_bot_conf`
exports `GIT_CONFIG_GLOBAL="$BOT_DIR/.gitconfig"`. That is a composer-owned, per-bot git config
that applies to **every repo the bot ever clones**, is regenerated by `generate`, and needs no
per-checkout installation.

Three measured facts about it:

- `grep -rn hooksPath claudlobby/ lib/ library/` → **zero hits**. `core.hooksPath` is net-new
  to this package.
- `compose_bot_gitconfig` returns `None` unless `bot.git_credentials` is declared. The
  consumer fleet declares none, so **no `.gitconfig` and no `GIT_CONFIG_GLOBAL` exist there
  today**. The carrier is real but currently gated on an unrelated concern. F1 is about that.
- In a live `<app>` checkout: `core.hooksPath` unset both local and global, `.git/hooks` holds
  only `.sample` files, no husky. Nothing would be displaced by adopting a shared hooks dir.

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

**Prerequisite, and it is real.** `compose_bot_gitconfig` returns `None` unless
`bot.git_credentials` is set (§2.4), so on a fleet declaring no per-org credentials there is no
`.gitconfig` at all. Hanging the guard off that file as-is would make it present or absent
according to an unrelated credential setting. The file's existence must be **decoupled**:
compose it when *either* credential routing *or* the guard is enabled. That is fork **F1**.

**Second-order effect, flagged because it is fleet-wide.** A global `core.hooksPath`
**replaces** `.git/hooks` for every repo without a local override. Measured today: nothing is
displaced (§2.4). Husky is safe — it sets `core.hooksPath` at repo level, and local config
wins. But a repo that later adds a plain `.git/hooks/pre-push` would find it silently ignored.
The shared hook should therefore **chain** to `.git/hooks/<name>` when one exists, so the guard
adds behavior rather than replacing it.

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

- **F1 — decouple the composed `.gitconfig` from `git_credentials`.**
  Options: (a) compose it whenever credential routing **or** the push guard is enabled;
  (b) always compose it; (c) leave it coupled and find another carrier.
  **Lean: (a)** — smallest change that makes the guard's presence independent of an unrelated
  credential setting. (b) widens `GIT_CONFIG_GLOBAL`'s blast radius for every fleet with no
  reason to want it. (c) has no candidate carrier that survives §2.2.
  **Ratifier:** this package's maintainers.

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

---

## 8. Explicitly not in this spec

- No implementation and no canary. Per the dispatch, a canary comes after the deploy hold
  lifts, on **one** worker, not a roll — and it produces a real deployment, so it is gated on
  that hold.
- No merge of this PR by a bot.
- No reduction figure, before or after (§4/Q4).
