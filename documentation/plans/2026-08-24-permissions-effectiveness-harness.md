---
title: "Permissions-effectiveness harness — design for review"
type: design
status: active
owner: ravi
created: 2026-08-24
updated: 2026-08-24
resolved_blocker: "section 3 — seeder alarm refuted by measurement 2026-08-24"
tags: [970, 1312, 1168, permissions, harness, false-clean, positive-control, trust, claudlobby]
---

# Permissions-effectiveness harness — design for review

Design only, per dispatch `t-1787585864-859e`. **No code written, nothing run.**
Held for vera's review, specifically for the false-clean failure mode.

## 1. The problem, stated as the thing that must not happen

"The bot was not blocked" has four causes and only one is good news:

| # | cause | today's status |
|---|---|---|
| 1 | settings file ignored wholesale — workspace not trusted | live: 21/21 bots, #970 |
| 2 | file read, rule never matched — bare-absolute path | live, #1312 |
| 3 | rule matched, permission mode approved anyway | not established either way |
| 4 | genuinely enforced | the only good news |

A harness that cannot separate these **reports SUCCESS for 1, 2 and 3**. That is
what last night's canary did, and the failure is not that it got a wrong answer —
it is that a dead instrument and a working one produced identical output.

**The design rule that follows:** a gate whose failure mode is silence cannot be
validated by observing silence. Every "not blocked" must be *earned* by an
instrument that has demonstrated, in the same run, that it can produce "blocked".

## 2. Where it lives — extend, do not fork

`lib/freshbox-boot-gate.sh` already owns most of the machinery: a fresh empty
`CLAUDE_CONFIG_DIR`, `seed_claude_auth_and_trust()`, headless boot, transcript
capture, and — importantly — a **teeth check** that strips the trust seed and
asserts the composed settings go inert.

It also already has a **direct observable for cause 1** that I had assumed would
need to be inferred:

```
ADVISORY_RE='has not been trusted|Ignoring [0-9]+ permissions'
```

Claude Code *announces* an untrusted workspace. Cause 1 is therefore readable, not
inferred — strictly better than probing for it.

**Proposal:** a sibling script, not an extension. `freshbox-boot-gate.sh` answers
an allow-side question (over-grant / orphan / transcript ⊆ allow) with its own
exit semantics; this is a deny-side effectiveness question. Bolting a second
verdict onto one gate muddies both. Sibling precedent is well established here
(`rehearse-*`, `run-discrimination.sh`).

- `lib/perm-effectiveness-gate.sh` — orchestration, boots, safety
- `lib/perm-verdict.py` — standalone stdlib scorer, unit-testable
  (`dispatch-overdue.py` / `boot-strand-summary.py` / `exit-token-mixture.py`
  precedent)

Shared helpers are **called, never re-implemented** — `seed_claude_auth_and_trust`,
`declared_bots_strict`, `bot_conf_get`. A private copy of a shared predicate is how
a fleet-wide fact quietly forks.

## 3. Pre-implementation blocker — RESOLVED, and my alarm did not survive it

**Status: I raised this and the evidence refutes it. The seeder is NOT demonstrably inert.**
Measured 2026-08-24, Claude Code 2.1.240, four throwaway arms, disposable
`CLAUDE_CONFIG_DIR`, isolation asserted (no probe path reached the operator config).

Answering dara's three questions separately, as asked:

| question | answer | evidence |
|---|---|---|
| does the binary READ `$cfg/.claude.json`? | **yes — it WRITES it** | md5 of the seeded file changed across the boot in both arms that had one (A, B) |
| does it CREATE `$cfg/.config.json`? | **no** | absent after boot in A, B, D; present in C only because I put it there |
| does the workspace end up TRUSTED? | **cannot tell — the run does not discriminate** | see below |

**Where my alarm went wrong.** I inferred "the seeder writes a dead filename" from
the *host HOME* layout, where `~/.claude/.config.json` is live and
`~/.claude/.claude.json` is absent. But the harnesses do not use the HOME layout —
they set an explicit `CLAUDE_CONFIG_DIR`, and **in that layout the binary reads and
writes `<cfg>/.claude.json`**, which is exactly what the seeder writes. Two
different layouts; I conflated them. The `~/.claude.json` staleness that
invalidated last night's canary is real and unchanged — it just does not transfer
to the seeder.

**What is still unanswered, and it is the part that matters.** Trust made **no
observable difference in any arm.** An allow-listed command took effect and a
deny-listed command was blocked in all four — *including arm D, which had no config
file at all*. So in headless `-p` on a scratch project, `settings.local.json`
appears to be honoured without any trust key, and this setup therefore cannot
discriminate trusted from untrusted. Per the rule this whole design is built on, I
report that rather than reporting arm A.

**Three detectors died before this one produced anything** — the untrusted-workspace
advisory never fired (not even in the no-trust control), stdout-scraping missed tool
results because `-p` prints only the final message, and `factor 12` turned out to be
*ungranted as well as denied*, so it was declined identically in every arm. Each was
caught by a control that refused to move. **That is this design's rc-3 and rc-5 gates
firing on their own author, three times, before any verdict existed** — which is the
strongest argument I can make that those gates belong in it.

**Two follow-ons this opens, neither claimed as established:**

1. If `settings.local.json` is honoured without trust in headless mode, then #970's
   "ignored wholesale" premise may be **mode-specific**, and `freshbox-boot-gate.sh`'s
   teeth check (strip trust ⇒ settings inert) should be re-verified on 2.1.240. If
   that check no longer bites, it passes vacuously — a check that cannot operate
   returns its negative verdict.
2. The advisory regex `has not been trusted|Ignoring [0-9]+ permissions` did not fire
   in a state that should have produced it. freshbox asserts its **absence** as proof
   of trust. If it cannot fire on this version, that assertion is vacuous too.

**Consequence for this design:** §5's trust rung cannot use "no advisory" as its
signal, and cannot assume the seeder establishes trust. It needs a trust detector
that has been shown to move — which is what C3 (trust-strip) exists to prove, and C3
is now the *only* rung that can establish it. That raises C3 from a nice-to-have to
load-bearing.

## 3b. Original blocker text, kept for the record

### The alarm as originally filed — the seeder may already be inert

**This blocks implementation and I have not resolved it.**

`seed_claude_auth_and_trust()` (lib-common.sh) writes:

```
$CLAUDE_CONFIG_DIR/.claude.json
```

Measured on this host, the default config dir is `~/.claude`, and:

| path | present? | evidence |
|---|---|---|
| `~/.claude/.config.json` | **yes** — 73,943 B, mtime advancing live | plus three `.config.json.tmp.*` atomic-write leftovers |
| `~/.claude/.claude.json` | **absent** | — |

So a real config dir contains **no file by the name the seeder writes**.

**Two live readings, and I have measured neither:**

- **(a) benign** — a fresh dir containing `.claude.json` is *migrated* on first
  run. `~/.claude.json` going stale "since ~Aug 5" is consistent with exactly such
  a migration at some version. Seeder still works.
- **(b) inert** — the binary no longer reads that name. Then
  `freshbox-boot-gate.sh`, `boot-strand-sampler.sh` and `ab-comms-eval.sh`
  (4 call sites) have been seeding trust into a dead file and running **untrusted
  workspaces while reporting clean** — last night's failure, one layer down, in the
  test infrastructure itself.

**Resolution is one throwaway boot**, and freshbox already implements the check:
seed as today, boot, grep for `ADVISORY_RE`. Advisory present despite seeding ⇒ (b).

I am not spending that boot without a go. **If (b), this design's trust gate cannot
use the shared seeder and the three sibling harnesses need re-verification before
their past verdicts are cited again.**

## 4. Data source — composed artifacts and observed behaviour only

Per dara's constraint, and it corrects a hole in my first draft.

**Effective state comes from what COMPOSED, never from what was asked for.**
`config.py:1832` merges `DEFAULT_GUARDRAILS` unconditionally; the defaults registry
has an **empty permissions slot pending #1168 Phase 2**. So a harness reading
manifests is correct today and silently wrong — in the under-reporting direction —
the day a default permission lands.

Consequences for this design:

1. Throwaway variants are composed by **real `claudlobby generate`**; the declared
   `fleet.yaml` is an *input*, never evidence of what the bot carries.
2. The run-blocking assertion is **not** `composed == declared`. That would fail
   spuriously the day defaults land. It is: *composed CONTAINS the probe rules.*
3. **The complete composed deny set is recorded in every verdict** — including
   rules nobody declared.

**Rung 3 is load-bearing and is new since dara's message.** An undeclared default
deny could block a probe, and the harness would attribute that block to the rule
under test. A blocked probe is only evidence about *this* rule if no other composed
rule could match the same target. So: **attribution, not just outcome** — on any
block, assert no other composed rule matches the probe target, or report
`BLOCKED-UNATTRIBUTED`.

## 5. Cells — each is one composed variant, one real boot

Probes cannot share a cell when one must be denied and another must not.

| cell | composed denies | probes | role |
|---|---|---|---|
| **C0** | none | `factor 12`; Read target; MCP tool | **negative control** — all must SUCCEED. Proves every probe action is executable here at all. |
| **C1** | `Bash(factor *)`, `Read(/abs/target/**)`, `mcp__<harmless>` | same three | `factor` = **positive control (Bash)**; Read = measurement, bare form; MCP = proxy measurement |
| **C2** | `Read(//abs/target/**)` | Read target | measurement, `//` form — **and the per-tool positive control for Read** |
| **C3** | C1 rules, **trust seed stripped** | `factor 12` | **positive control on the trust gate itself** — must go from BLOCKED to not-blocked, with the advisory present |

**C2 does double duty and that is the point.** A `Bash` control proves deny works
for *Bash*. It says nothing about `Read`. #1312's own table pairs `Bash(factor *)`
with `//`-form `Read`/`Edit` denies for exactly this reason. If C2 blocks, cause 3
is retired **for Read** under the recorded mode.

**C3 is the rung last night lacked.** It converts "trust was seeded" from an
assumption into a measurement: remove the seed, and behaviour must change. If it
does not, the seed was never load-bearing and nothing downstream is interpretable.

## 6. Refuse gates — ordered, each exits nonzero and names its cause

The harness **must never report clean from a dead instrument.** Every gate below
refuses rather than degrading to a verdict.

| rc | gate | refuses when |
|---|---|---|
| 3 | **positive control** | C1 `factor 12` not blocked ⇒ deny layer dead; nothing measurable |
| 4 | **trust** | trust key absent in the resolved live store, **or** C3 shows no behavioural difference, **or** the config path was not verified live |
| 5 | **probe exercised** | no tool-call record for a probe. *"Not blocked" and "never attempted" are the same observable unless you check.* |
| 6 | **negative control** | any C0 probe fails ⇒ blanket failure would read as enforcement |
| 7 | **composition** | probe rules absent from composed `settings.local.json` |
| 8 | **isolation** | `CLAUDE_CONFIG_DIR` redirect did not hold, or a target path resolves inside a production runtime dir |

**Config-path liveness is asserted, not assumed** — the resolved store must be
observed to be *written by the binary* (mtime advance across the boot). This is
the one rung that exists purely because of last night: a write verified by
re-reading its own output confirms the write, never the target.

## 7. Verdict table — Read measurement, given all gates passed

| C1 bare | C2 `//` | verdict |
|---|---|---|
| SUCCEED | BLOCK | **Cause 2 confirmed** — #1312 live on composed output |
| BLOCK | BLOCK | **Cause 4** for this rule — enforced under both forms |
| SUCCEED | SUCCEED | **NOT #1312.** Rules load (C1 control fired) but neither form matches ⇒ a distinct matching defect, **or** cause 3 scoped to `Read`. The harness **cannot separate those two** and says so — it does not pick one |
| BLOCK | SUCCEED | **Anomaly** — contradicts #1312. Refuse and flag; do not narrate |

Every verdict records: the **actual `--permission-mode`** read from composed
`bot.conf` (a verdict that does not name the mode is not a verdict), the complete
composed deny set, the binary version, and each probe's attributed rule.

## 8. MCP coverage — proxy only, and the bound is stated

**`mcp__github__merge_pull_request` is NOT tested.** Per dara's hard constraint, the
only direct test is merging a real PR.

Covered by proxy with a **harmless read-only MCP tool deny** (candidate:
`mcp__github__search_repositories` — read-only, public data, no side effects;
final choice to be confirmed at implementation).

**Stated bound, carried into every verdict:** a matching result for MCP tool A does
not establish matching for tool B. Different tool name, different string, and if
MCP denies match by pattern the shapes may differ. The proxy establishes that *MCP
denies are enforced at all* — it does not establish that this specific merge deny is.

## 9. Scope, cost, safety

**In this PR:** harness + this design doc + unit tests + `--dry-run`.

- **`--dry-run` drives the real scorer** on synthetic transcripts at zero model
  cost, CI-safe. Not a mock — a dry run that calls a parallel path certifies a dead
  path.
- **Real runs behind `PERM_GATE_REAL=1`** — deliberately **not** `AB_EVAL_REAL`, so
  a permissions run can never ride in on an eval gate (`ab-recoverability-judge.py`
  precedent).
- **Never seeds trust on a live bot.** Refused by path check. All work happens in a
  `git archive` export with its own `CLAUDE_CONFIG_DIR`; the operator's config is
  never opened for write.
- **Isolation is asserted, not assumed** (rc 8) — a harness that silently fell back
  to the real host config would pass by coincidence.
- **No production runs.** Canary comes after review and dara gates it.

## 10. What this will NOT establish

- Whether `merge_pull_request` specifically is enforced (§8).
- Whether interactive sessions behave as headless ones do. Every cell is
  `claude -p`. #1312's mode claim is likewise headless. Untested, and named.
- Whether a *fixed* path form restores isolation on a **production** bot — this runs
  on throwaway composed bots. #1312's bound survives this harness.
- Anything about allow-side handling under `auto` (#913 thread).

## 11. Open questions for review

1. **The §3 blocker.** Spend one boot now to settle it, or design around the
   seeder entirely?
2. Is C3 (trust-strip) worth its boot every run, or a pre-flight run once per
   binary version?
3. Should `BLOCKED-UNATTRIBUTED` (§4) refuse, or report as a distinct verdict?
   I lean report — it is a real state, not a broken instrument.
4. MCP proxy tool choice — is `search_repositories` acceptable, or is there a
   deny already composed that could be reused?

## 12. Adversarial review (vera, 2026-08-24T15:48:50Z)

**Mandate: can this design ever report ENFORCED (Cause 4) when nothing is enforced? I tried hard to make it lie and could not, at the structural level.** The verdict table's Cause-4 row requires C1 *and* C2 to both BLOCK, gated by rc3 (positive control), rc4 (trust, via read-back), rc6 (negative control), rc7 (composition), rc8 (isolation). I could not construct a path to that row firing falsely without also tripping one of those gates. That is a real result, stated plainly, not a hedge — but it comes with four implementation-precision requirements below, because the structure only holds if these are built as specified rather than as their nearest-neighbor shortcut.

### Your three self-flagged points, attacked directly

**§6 rc5, "probe exercised" — you're right that this is the deepest hole, and it needs two specific tightenings, not general vigilance:**

1. **The tool-call record must match the exact probe string, not "a Bash/Read/MCP call occurred."** A headless model asked to run `factor 12` could run `bash -c "factor 12"`, add flags, split it into steps, or run something adjacent — a loose "any tool call of this type happened" check is satisfied by all of those without ever exercising the composed rule. Assert the specific command/path/tool-name appears in the tool_use record, not merely the tool category.
2. **An invocation with no corresponding result is exactly as uninformative as no invocation.** If the transcript shows a tool_use for the probe but the run crashes or truncates before a tool_result lands, that's not "probe exercised" either — it's a third unresolved state, and rc5 should refuse on it the same as on zero tool calls.

Reuse, don't reinvent: this is the same problem `boot-strand-sampler.sh` / `exit-token-mixture.py` already solved (submitted-vs-unsubmitted from a transcript, shipped `pane_trace_render`). Shelling out to that instrument is the established precedent for exactly this class of check in this codebase — a private transcript parser here re-forks a problem that's already been gotten right once.

**§4 rung 3, rule attribution — sound as stated, one thing to make explicit rather than implicit:** the mechanism (assert no other composed deny matches the probe target) is correct and sufficient, since only deny rules can be the reason something blocked — allow/ask can't explain a BLOCK, so you don't need to check those. But Layer-0 sibling isolation composes unconditionally onto *every* throwaway bot against every other throwaway bot in the same test fleet (per #1312's own quoted loop). If two cells' throwaway bots ever share a fleet and a probe target happens to resolve inside another cell's bot directory, that's a live, composed deny nobody declared for the test — exactly rung 3's own worry, from a source you haven't named. #1312's own methodology dodged this by using a scratch project outside any bot tree, not a bot directory. Worth one explicit sentence: probe targets must resolve outside every composed bot's runtime tree, not just outside "the bot under test."

**§5 C3, trust-strip — not redundant (confirmed: it and C0 vary orthogonal axes, rule-presence vs trust-presence, and #970's whole thesis is that those are separable), but there's a real methodological question the design doesn't yet answer: is C3 the *same* config dir as C1, trust key removed and re-booted, or an independently-composed dir that was never seeded?** If it's strip-and-rebook, you're relying on nothing about "was ever trusted" being cached anywhere across that transition — a session cache, a memoized dialog-acceptance, anything CLI-internal that outlives the key's removal. That's not a hypothetical for this estate specifically; it's the generic shape of tonight's whole saga (a thing that should have changed and didn't, discovered too late to matter). The clean version composes C3 fresh, never-seeded, identical rules to C1 — same test, zero caching surface. If you specifically want to test *revocation* behavior (trust granted then pulled), that's a legitimate and different question and deserves its own cell rather than riding on C3's back.

**And the connection between your two flagged points, which compounds them:** C3's *informative* outcome is "not blocked" — the exact ambiguous shape rc5 exists to disambiguate. C1's informative outcome is "blocked," which doesn't need rc5 nearly as much (a genuine denial message is a positive, specific signal). So rc5's fix isn't a general nice-to-have — it's the one thing standing between C3 and being unfalsifiable in exactly the direction dara's canary was.

### The five attacks, checked against the actual text

1. **Kill the positive control** — rc3, clean. Refuses nonzero, names the cause ("deny layer dead").
2. **Feed it an untrusted workspace** — rc4, covered three redundant ways (live-store key check, C3 behavioral evidence, config-path liveness). More robust than the incident that motivated it.
3. **Wrong trust-check file** — this *is* §3, and it's the most careful section in the document: measured, not assumed, both readings named, the one boot that resolves it specified, and implementation explicitly blocked pending it. Nothing to add; this is the bar.
4. **Vacuity** ("checked and fine" vs "nothing to check") — doesn't map cleanly onto this design's shape, and worth saying why rather than silently passing over it: every cell here is a harness-constructed rule under test, so there's no "bot with zero denies" case *inside* this design's scope. It would matter if this harness is later pointed at auditing a real bot's actual rule set rather than a throwaway probe — different tool, flag it as a scope boundary if that reuse is ever proposed, not a gap in this one.
5. **Every verdict names its permission mode** — §7, explicit, in your own words almost exactly. Confirmed.

### dara's mid-turn addition — assert trust via read-back, not helper-call

Already met, and better than a simple read-back: rc4's "config path was not verified live" requires the resolved store's **mtime to advance across the boot** — proof the binary itself is touching that file, not just that your own write landed in it. That's a stronger claim than "I read back what I wrote" — it can't be satisfied by a seeder that successfully writes to a file nobody consults. Paired with the separate "trust key absent in the resolved live store" check (verifying the *specific* key, not just file liveness), the two together cover both halves dara was worried about. Nothing to add here either.

**Net: hold implementation on §3 as scoped (correctly your call, not mine to second-guess), and fold in the four items above** (rc5's exact-match + no-orphaned-invocation; rung 3's explicit outside-every-bot-tree constraint; C3's fresh-vs-strip clarification) **before code.** Everything else stands as designed.

---

## 13. MODE IS AN AXIS, NOT A CONSTANT — added after the boot, and it re-cuts §5

**This supersedes the implicit assumption in §5 that every cell is `claude -p`.**

Found by searching the backlog *after* running the boot, which is the wrong order
and cost about forty seconds to get right.

**#863** (open since 2026-08-21, claude 2.1.220) already records that an untrusted
workspace no longer drops composed `settings.local.json`. My 2.1.240 arms reproduce
it and widen it from hooks to the `allow`/`deny` rules themselves. Filed as
comments on #863 and #970.

**Both measurements are headless. The bots are not.** They run interactive in tmux
with the TUI. So the position on #970's central consequence is:

- **contradicted** in the mode we can cheaply test,
- **unmeasured** in the mode the bots actually run in,
- known to agree only by assumption.

#1312's mode claim (*"deny is honoured under `auto`"*) inherits the same boundary.

**Consequence for this design.** A headless-only harness would have produced a
clean, well-controlled, fully-gated verdict **about a mode no bot uses** — a
false-clean at the level of the experiment rather than the instrument, and one that
every rung in §6 would have passed. So:

1. **Mode is a declared axis.** Cells run headless *and* interactive; a verdict
   names which mode produced it. A verdict that does not name its mode is not a
   verdict — the same rule §7 already applies to `--permission-mode`, one level up.
2. **The interactive arm needs a scoring mechanism that does not exist yet.**
   Scoring "was this blocked" off a TUI pane is the submitted-vs-unsubmitted problem;
   vera's steer toward `exit-token-mixture.py` / `pane_trace_render` may be the
   answer rather than an obstacle. **Open — see §11 q5.**
3. **If interactive proves unmeasurable**, a headless-only harness ships only with
   the bound stated at verdict level, not in a footnote. Whether that is worth
   building is a judgment call and it is dara's, not mine.

**Open question 5 (new):** is a deterministic interactive probe achievable, or does
the harness ship headless-only with a loud bound?

## 14. Review status

- **vera** reviewed and posted findings as §12. Four points, **all accepted**:
  rc5 exact-match on the probe string; invocation-without-result treated as
  uninformative; reuse `exit-token-mixture.py`'s mechanism rather than a private
  parser; probe targets outside **every** composed bot's tree (Layer-0 composes
  sibling denies across the throwaway cells too); C3 as a fresh never-seeded dir
  rather than a strip-and-reboot, to avoid a caching surface.
- The §3 alarm was raised by me and **refuted by my own measurement**.
- Three probe detectors died before one worked (§3). Those were this design's rc-3
  and rc-5 gates firing on their author, which is the best evidence available that
  they belong in it.
