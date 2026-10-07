---
title: "P1 — the boundary-spec amendment, one SNIPPET_EVENTS, the ops-log id regex (Claudron)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
updated: 2026-10-05
epic: documentation/plans/2026-10-04-runtime-neutral-observability-plan.md
spec: documentation/plans/2026-07-20-claudfather-boundary-separation.md
issue: "#2145"
repos: Claudfather/Claudron
---

# P1 — the boundary-spec amendment, one SNIPPET_EVENTS, the ops-log id regex (Claudron)

> **Status:** draft, authored by `/claudna:forge` on 2026-10-04 from epic plan §6 P1 (the three Claudron
> bullets) and §10, where this PR is **order 4**: P0's Claude batch (order 1), P2-b (order 2) and the per-bot
> `CLAUDNA_STATE_DIR` line (order 3) precede it. Code references are to Claudron `6ca2b94` (the `v0.9.0` tag;
> `origin/main` `8e895cb` adds only `.github/workflows/tests.yml` and three CHANGELOG lines). `spec:` above is
> a path in the **Claudron** repo. Depends on: no code in another repo; its Codex values rest on the mission
> decision **D1, ratified**: F18 (a) locked 2026-10-05
> ([FORK-LOCK F18](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000050806)) — Claudlobby
> composes Claude Code and, by name, Codex. Waits on canaries: **C1, for Task 3 only**; Tasks 1, 2 and 4 do not
> wait. Reforged 2026-10-05 from ironclad cycle 1 (directives C1–C7; cross-cutting X1, X7, X14) and its interim
> fold (the P4 flag's naming caveat, the `session_alias` default, row 10's flipper named as P1 Claudlobby's Half A,
> row 11's `session.agent_cli`), then from ironclad cycle 2 (the §10 order, the D1 dependency, row 10 names no
> attribute), then the operator's rulings of 2026-10-05 were applied: F18 (a) locked, so the Codex names are written
> plainly; A-F5 ratified ([FORK-LOCK F5](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000051858)),
> so §10.2 names no semantic-convention attribute and gives the vendor→house mapping to the `plane-otel` intake.
> No step waits on a ruling, and no mission decision is pending.

## Summary

Three small things in one PR, so the boundary spec names the agent runtimes before any code assumes them.
(a) Amend `documentation/plans/2026-07-20-claudfather-boundary-separation.md`: §10.2 gives Claudlobby the
*bought layer* (the normalization layer and the normalized attribute names; the vendor→house mapping is the
`plane-otel` intake's — F4 and F5 as re-locked 2026-10-05 — and the register names no semantic-convention
attribute, so a change in how Claudlobby implements the mapping never re-amends it);
§10.4 gains rule R8 (the epic's §2.3 rule — runtime-specific signals are enrichment only) and register rows
10–12 for the `(agent_cli, session_id)` join key (Claudlobby), clauDNA's export-contract additions and
`entrypoint.json` (clauDNA), and the Codex session-loop snippet (Claudron, landing in P4) — each unshipped
surface worded as *planned* (R6) and naming the PR that flips it to shipped; row 10 states the `session_id`
character class once, for both regex copies; the stale pre-C2 sentences on the lines being amended are fixed (the `hooks.py:62` sniff is
gone, the CLI contract exists). It lands **before** P2 writes the attribute contract, so R1 precedes R2
precedes any R3 consumer. (b) `merge_settings` stops carrying a private copy of the event→verb map and
keys on `SNIPPET_EVENTS`, pinned by a test that a map entry added nowhere else still merges. (c) Confirm
the ops-log id regex admits a Codex `session_id` once C1 records one; widen it — with a doc-parity table so
`docs/CLI_CONTRACT.md` and `test_ops.py` move together — only if it does not. Deliberately left to later
PRs: any Codex adapter, snippet, `hooks install --host codex` flag (name settled in P4: `--host` clashes with
Claudron's existing `host` term; `--front-end`/`--agent` are candidates) or capability (P4); any attribute
name or normalization text (P2, Claudlobby); the Claudlobby `claudron` pin bump (P3).

## Evidence (Claudron at `6ca2b94`)

- `claudron/hooks.py:232-234` the `SNIPPET_EVENTS` comment names two readers ("`settings_snippet` renders
  from it and doctor's hook checks (#204) walk it"); `:235-239` the dict (`SessionStart`/`PreCompact`/
  `SessionEnd` → `session-start`/`pre-compact`/`session-end`). Readers: `settings_snippet` `:242`
  (renders `:264`), `settings_shape_error` `:273` (iterates `:287`; its docstring `:277` says "each of the
  three events the install writes"), `claudron/doctor.py:51-52` (import), `:420`, `:434`.
- `claudron/hooks.py:381-401` `merge_settings`; `:388-392` a byte-identical local `event_cmds`, used once
  at `:394` (`event_cmd = event_cmds[event]`) to feed `_is_claudron_hook` (`:370-378`, the `hook <event>`
  suffix identity rule, `docs/CLI_CONTRACT.md:366-370`). An event added to `SNIPPET_EVENTS` alone raises
  `KeyError` at `:394` on `hooks install --write` (`claudron/cli.py:1099`, the one production caller).
- The test files keep their own `EVENT_CMD` copies (`claudron/tests/test_hooks.py:467`,
  `test_doctor_hooks.py:25`) — the *expected* side of the pins, not a production duplicate.
- `claudron/ops.py:32` comment "what a Claude Code session id is"; `:33` `_ID_RE =
  [A-Za-z0-9][A-Za-z0-9._-]{0,127}`; `:36-40` `_dir` returns `None` on a non-`fullmatch`, so the event is
  silently not logged (`record` never raises). A canonical UUID matches; `:`, `/`, `\`, whitespace, a
  leading `.`/`-`/`_` and >128 chars do not. `claudron/runs.py:25` `RUN_ID_RE` is the same class capped at
  `{0,63}`, enforced at the CLI with exit 2 (`:53`; prose `docs/CLI_CONTRACT.md:695-697`) — a different
  contract, untouched here.
- `claudron/tests/test_ops.py:53-57` pins only the unsafe side (`../escape`, `a/b`, `..`, `""`,
  `.hidden`); `:46-50` writes and reads one `recall.served` event through the `_events` helper (`:36-38`);
  fixture `vault_dir` (`conftest.py:43-44`) is a non-git vault, so logs are written.
- `docs/CLI_CONTRACT.md:675-691` the ops-log bullet under `## Command-specific contracts` (`:422`);
  `:686` "an id that isn't a safe directory name isn't logged"; `:690-691` the gate sentence. No
  `<!-- doc-parity -->` marker pins the regex (the four markers are `:117`, `:229`, `:390`, `:458`). The
  precedent for a marker + table *inside* a bullet is `DOCTOR_CODES` at `:458-469`.
- `claudron/tests/doc_parity.py` is the single reader (`:6-9`): `doc_table(doc, marker)` `:20-37`,
  `section` `:40-57`, `fenced_block` `:60-89` (first fence of a `##` section only), `code_values` `:92-94`.
- The spec (763 lines; two commits — `640621f` #78, `6968dc8` license): frontmatter `:1-9` has no
  `updated:`; the status sentence is `:19-20`; §4.1 `:172` still says `hooks.py:62 _claudna_installed()`;
  §10.2 `:399-434` (Claudron *Never* `:408-411`: "today it sniffs both siblings"; Claudlobby *Owns*
  `:424-427`); §10.3 Q1 `:447-452` ("Claudron's `hooks.py` is the engine's Claude Code adapter"
  `:449-450`); §10.4 rules `:471-486`, table header `:488-489`, rows `:490-497` (row 5 `:493`:
  "**none — changelog lore**" / "the deferral is a name-sniff (`hooks.py:62`)"); §10.5.1 `:518-522`;
  §10.5.5 `:623-626`; §10.7 `:671`; §10.8 `:688-716` (items 1–11); `---` `:718`; `## Appendix Z` `:720`
  (sealed, "read this last"). No test reads this file.
- Facts the amended lines are fixed to: `_claudna_installed()` removed in 0.4.0 (`CHANGELOG.md:466-470`,
  PR #84, issue #85); `docs/CLI_CONTRACT.md:220-400` §Session-loop protocol exists (structural claim,
  `:276-302`; normative snippet `:304-323`); the tree-shape walk is `claudron/cli.py:146-157`
  `_detect_claudlobby_root`, silent since #102; Claudlobby composes the loop per bot behind
  `tests/test_claudron_loop.py` (Claudlobby, F7).
- Amendment conventions in the directory: `2026-07-18-decision-c-mcp-demand-gated.md` (`updated:` `:8`;
  inline stamps `:38-40`, `:46-47`; appended `## Amendment — 2026-07-20: …` `:59-66` opening "Recorded by
  … grounds in … **The gate is affirmed; the framing is revised.**"); `2026-07-20-boundary-rearchitecture/
  00-overview.md:197-211` (Amendment A1 with `*Ratifier:* chris (date)` and the standing rule `:208-210`:
  "amend the fork first; never ratify code by editing the contract it violates"; `:56-58` "the ironclad
  review cycle is the ratification venue").
- Placement: `docs/CLAUDE.md:24-27` keeps repo-internal records out of `docs/`; the spec is
  `documentation/plans/` material, so it may *name* planned surfaces but asserts none as shipped (R6).
- CHANGELOG on `origin/main`: `## Unreleased` `:3` already holds `### Added` (#223, `:5-6`); format is a
  bold-lead bullet ending "Tests: `…`". `pyproject.toml:7` `0.9.0`; `:14-16` PyYAML only.
- CI (`.github/workflows/tests.yml:22-33`): Python 3.12, `pip install -e '.[dev]'`, a git identity,
  `pytest -q`.
- Sibling paths the register rows cite: Claudlobby `claudlobby/plane/ids.py:79-89` `derive_session_uid`
  (Claude-only today), `documentation/architecture/observable-plane.md`, `tests/test_claudron_loop.py`;
  clauDNA `documentation/specs/2026-09-28-session-store-design.md` §8 `:420-440` (`claudna.export/1`,
  shipped), `lib/claudna/session_store/filing.py:97-99` `evidence_ref` → `session:<sid>:<seg>`,
  `contracts/claudron.ref` = `v0.9.0`; Claudron's two opaque provenance channels `claudron/engine.py:160-161`
  (`source_url`) and `claudron/amend.py:37,63,84` (evidence ref; only `·`, `<!--`, `-->` refused).

## Implementation Plan

### Dependencies
None in code. F1–F18 are locked (F1–F17 ratified 2026-10-04; F3, F4, F5 and F17 re-locked and F18 locked on
2026-10-05); this is §10 order 4 (after P0's Claude batch, P2-b and the order-3 per-bot `CLAUDNA_STATE_DIR` line)
and plan 1 of §10.1's six. **The mission decision it rests on, D1, is ratified** (Claudlobby#2145 §16): F18 (a),
[FORK-LOCK F18](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000050806), 2026-10-05 —
Claudlobby composes and supervises agent CLIs, Claude Code today and Codex through #2149, a further CLI by its
own fork. So every clause that names Codex — Step 3's §10.2 *Owns* sentence, R8's two parentheticals, row 10's
`agent_cli` set, row 12, the amendment section's items 3 and 5 and Task 4's CHANGELOG bullet — names it plainly.
Claudron's own mission (`PROJECT_MISSION.md:17`, "any agent fleet") needs no amendment. §10.2's attribute wording
follows F5 as re-locked by A-F5 ([FORK-LOCK F5](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000051858)):
the GenAI semantic-convention names are the intake's internal mapping, so the register names none. Task 3 waits on
C1, a Codex-install canary.

### Blocks
P2 (Claudlobby) — the attribute contract needs its owner named first (R1/R2). P4 Claudron — row 12 fixes
the owner and the parity-reader constraint the Codex snippet must satisfy; Task 2 is the shape its event
map extends. The companion (Claudlobby#2149) — reads rows 10–12 under R6. Row flips: P1 Claudlobby Task 10,
Half A (row 10 — after Claudlobby's release N, which ships everything the cell cites; Half B changes none of
it), P3 clauDNA Task 9 (row 11) and P4 Claudron (row 12) each open the one-line Claudron PR that turns
a *planned* cell into the shipped text — never before the owning release exists.

### Steps

Tasks follow as H3 siblings.

### Task 0: worktree, before-leg, evidence

- [ ] Worktree `~/Projects/claudron-worktrees/p1-boundary` on branch `docs/p1-runtime-neutral-boundary`
  from `origin/main` (`8e895cb`). Evidence dir `~/Projects/claudron-worktrees/p1-boundary-out/`.
- [ ] Before leg, on the branch base, CI's own recipe (`tests.yml:22-33`): `python3 -m venv .venv &&
  .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q | tee ../p1-boundary-out/before.txt`. Record
  the passed count; every later run compares against it.
- [ ] Record `git rev-parse HEAD`, and, when it exists, the C1 run-log entry Task 3 reads (the epic keeps
  the run log beside the epic plan in the Claudlobby repo, P0).

### Task 1: the boundary-spec amendment

**Files:** `documentation/plans/2026-07-20-claudfather-boundary-separation.md` (modified; nothing else).
Line numbers are pre-edit; edit bottom-up or re-grep. Every touched sentence carries an inline stamp
(decision-C `:46-47` form); the full account is one appended section (decision-C `:59` form).

- [ ] **Step 1 — frontmatter and status.** After `created: 2026-07-20` (`:6`) add `updated: 2026-10-04`.
  Set `status: draft` (`:4`) to `status: active` in the same touch (C5): the `plan` vocabulary is `draft |
  active | completed | superseded | archived` (clauDNA `skills/_shared/output-guide.md` §3 — `ratified` is a
  `decision` status), and `active` is the in-force value for a spec whose §10 was ratified 2026-07-20
  (`2026-07-20-boundary-rearchitecture/00-overview.md:56-58`, program gate 1 discharged) and is now being
  amended; the stale `draft` is what the epic's §4 read as "a draft plan with no formal amendment process".
  Rewrite the status sentence (`:19-20`): "**Status: §10 ratified 2026-07-20 (program gate 1,
  `2026-07-20-boundary-rearchitecture/00-overview.md:56-58`); deliverable (b) lives in
  `2026-07-20-boundary-rearchitecture/` (overview + 9 phase docs, this directory). Amended 2026-10-04 — the
  agent runtimes enter the frame; see §Amendment — 2026-10-04 (after §10.8).**"
- [ ] **Step 2 — §10.2 Claudron *Never* (`:408-411`), the stale sentence.** Replace "— today it sniffs
  both siblings (`hooks.py:62` globs the plugin cache for `claudna`; `cli.py:104–113` walks for a
  `library/`+`lib/` tree shape)." with: "— *(amended 2026-10-04: at authoring it sniffed both siblings; the
  `hooks.py:62` plugin-cache glob for `claudna` was removed in 0.4.0 (#84, issue #85) and the capture-prompt
  claim is structural, `docs/CLI_CONTRACT.md` §Session-loop protocol; the `library/`+`lib/` tree-shape walk
  remains at `cli.py:146–157`, silent since #102 — the one open R5 item; §10.8 item 12)*." Keep "Consumers
  declare themselves … never goes looking." and append one sentence: "**Nor does it implement telemetry:**
  the engine emits no OpenTelemetry and takes no SDK (single dependency); what a session did inside its
  runtime is the bought layer, Claudlobby's (below; Claudlobby#2145 §2.1, §11)."
- [ ] **Step 3 — §10.2 Claudlobby *Owns* (`:424-427`), the bought layer.** Append: "*(Added 2026-10-04)*
  **And the bought layer:** the agent runtimes (Claude Code, Codex) export their own intra-session
  telemetry; Claudlobby owns **the normalization layer** that turns it into the plane's vocabulary and **the
  normalized attribute names** that come out — the plane's registry names, which are the reader surface. The
  normalized session attribute carries the plane's house key (the join key, #10); the vendor→house mapping is
  the intake's — the `plane-otel` intake's one mapping dict is the F5 artefact (Claudlobby#2145 F4/F5) — and this
  register names no semantic-convention attribute. That is a contract with one owner (R1 here; R2 when P2 lands
  its text in Claudlobby); neither sibling implements OpenTelemetry. See §Amendment — 2026-10-04."
  The wording **names where the mapping lives, not how it is built** (X7, as ruled): F4 was re-locked on
  2026-10-05 (A-F4 — no Collector; each bot exports OTLP/HTTP-JSON straight to the opt-in `plane-otel` intake) and
  F5 with it (A-F5 — the GenAI semconv names are the intake's internal vendor→house mapping; the plane's registry
  names are the reader surface; the Claudron register names no semconv attribute), so the register says the
  mapping is the intake's and names no attribute; how the intake implements it is Claudlobby's, under its own
  plans, and never re-amends this register. The word "Collector" does not enter the spec.
- [ ] **Step 4 — §10.3 Q1 (`:449-450`).** "(Claudron's `hooks.py` is the engine's Claude Code adapter;
  *amended 2026-10-04:* one adapter per agent runtime, and nothing past the adapter is runtime-specific —
  R8)."
- [ ] **Step 5 — §10.4 rule R8, after R7 (`:485-486`):**

```markdown
- **R8 — runtime-specific signals are enrichment only** *(added 2026-10-04, Claudlobby#2145 §2.3).* Each
  system meets an agent CLI (Claude Code, Codex) through one adapter — hook payloads, transcript
  reader, launcher, telemetry mapping — and everything past the adapter consumes one runtime-neutral
  model joined on `(agent_cli, session_id)` (#10). A signal only one agent_cli provides (Claude subagent span
  nesting, `TRACEPARENT`, `CLAUDE_CODE_CHILD_SESSION`, `$CLAUDE_PID`, Codex `PostCompact`) may enrich a
  view; it is never load-bearing for liveness, task state, rollups, paging or summaries. It is a register
  rule rather than a §10.3 placement because it bounds what *any* system may build on a vendor surface no
  sibling owns — R1–R7 say who owns a contract; R8 says what may rest on one that has no owner here.
```

  *Answered (ironclad C4):* R8 stays in the register, with the last sentence above carrying the reason;
  §10.3 Q1 (Step 4) already points at it, so placement guidance reaches it without a second copy.

- [ ] **Step 6 — §10.4 table: refresh row 5 (`:493`), append rows 10–12 after row 9 (`:497`).** Same five
  columns (`:488-489`). The "Authoritative text (today)" cell of every unshipped surface begins
  **planned** and names the phase (R6; `docs/CLAUDE.md:24-27`).

```markdown
| 5 | **Session-loop protocol** (roles, ordering, single-prompt rule, claim mechanism) | Claudron (knowledge roles) | `docs/CLI_CONTRACT.md` §Session-loop protocol *(amended 2026-10-04 — was "none — changelog lore"; C2 #84 landed it, 0.4.0)* | ✓ the claim is structural — a front-end defers on the registered `hook pre-compact` entry; the `hooks.py:62` sniff is gone (R5 met); Claudlobby composes the loop per bot behind an R3 gate (`tests/test_claudron_loop.py`); a second host's snippet is row 12 |
| 10 | **Session join key** `(agent_cli, session_id)` — `agent_cli ∈ {claude, codex}`; `session_id` the id the agent CLI hands its hooks, in the class `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` — stated **here, once**: clauDNA `paths.py:32` (`_SID_RE`) and Claudron `ops.py:33` (`_ID_RE`) are its two copies and their tests cite this row (C1 may widen it; Task 3 leg B and the clauDNA mirror move together); plane uid `sess_` + sha256(`session_alias(id, agent_cli)`)[:32], where `session_alias(platform_session_id, agent_cli="claude")` is the raw id for `claude` (the default) and `"<agent_cli>:" + id` otherwise (Claudlobby#2145 F2) | Claudlobby | **planned** (P1 Claudlobby Task 4; P1 Claudlobby Task 10, Half A, opens the Claudron PR that flips this cell): `ids.session_alias(platform_session_id, agent_cli="claude")` composed into `derive_session_uid(platform_session_id, agent_cli="claude")` in `claudlobby/plane/ids.py` (today `:79-89`, Claude-only) — the F2 material rule exists in that one function and nowhere else; its text in `documentation/architecture/observable-plane.md`; until then Claudlobby#2145 §2.2 | consumers conform: clauDNA keys sessions by `session_id` and records `agent_cli` at open (P1 clauDNA); P2's intake sets its session `subject = session_alias(id, agent_cli)` and its test pins the composition, not a coincidence; Claudron treats provenance `session:<agent_cli>/<sid>:<seg>` (F9) as an opaque string in both channels (`engine.py:160-161`, `amend.py:37,63,84`) — no engine change; the normalized session attribute carries the same key (§10.2) |
| 11 | **clauDNA export contract** — `claudna.export/1` and its planned additions: item `session.agent_cli` (P1), `--include-skipped` status items and `segment{sealed_at,sealed_by,counts}` (P3), and the `entrypoint.json` record (`claudna.entrypoint/1`, F16, P3) | clauDNA | `documentation/specs/2026-09-28-session-store-design.md` §8 (the envelope: shipped, v0.26.0); the additions **planned** there (P1/P3 clauDNA; P3 clauDNA Task 9 opens the Claudron PR that flips this cell) | consumer: Claudlobby's `session-export` job (P3) conforms to §8 and reads `entrypoint.json` for the door's path — never Claude Code's `installed_plugins.json` (F16); R6: Claudlobby asserts no field before the clauDNA release that ships it |
| 12 | **Codex session-loop snippet** — the normative Codex `hooks.json` shape for the engine's roles; same `<executable> --vault <root> hook <event>` command form and `hook <event>` identity suffix (`CLI_CONTRACT.md:320-323,366-370`) | Claudron | **planned** (P4 Claudron, after C1/C3 — P4 itself flips this cell): its own `##` section in `docs/CLI_CONTRACT.md` with a parity test (`doc_parity.fenced_block` reads one fence per `##` section) and a capability (e.g. `codex-session-loop`) gated per §Capability probe | R6: no composer renders it before the release that ships it; the companion (Claudlobby#2149) bumps the pin afterwards. If C3 shows Codex cannot block at PreCompact, the P4 text records R-capture-prompt as not held on Codex |
```

  Each *planned* cell names the PR that flips it to the shipped text (X14): row 10 — P1 Claudlobby Task 10,
  Half A (that plan's release N carries everything the cell cites); row 11 — P3 clauDNA Task 9; row 12
  — P4 Claudron. Until that PR merges the cell stays *planned* (R6), whatever the sibling has shipped.
  Row 10 names **no attribute** (ironclad cycle 2), and since A-F5 was ratified (2026-10-05) §10.2 (Step 3) names
  none either: the attribute names are the plane's registry — Claudlobby's text under R2 — and the vendor→house
  mapping is the intake's, so neither the join-key row nor §10.2 moves when a name does.

- [ ] **Step 7 — §10.8 item 12, after item 11 (`:716`):**

```markdown
12. **The session-loop contract exists and the plugin sniff is gone** (found 2026-10-04): C2 (#84, 0.4.0)
    landed `docs/CLI_CONTRACT.md` §Session-loop protocol and removed `_claudna_installed()`; the claim is
    structural (a registered `hook pre-compact` entry), not the declared env or install marker §10.5.1
    envisaged. §4.1's "partly reconciled" sentence, §10.5.1's claim mechanism, §10.5.5 and §10.7's
    `hooks.py` row describe the pre-C2 state and stay as history; the tree-shape walk is now
    `cli.py:146–157` (silent since #102). Row 5 and §10.2 were refreshed by the 2026-10-04 amendment.
```

- [ ] **Step 8 — the amendment section**, inserted after item 12 and before the `---` (`:718`) so Appendix
  Z stays last and sealed:

```markdown
## Amendment — 2026-10-04: the agent runtimes enter the frame

Recorded for Claudlobby#2145 (`2026-10-04-runtime-neutral-observability-plan.md`, Claudlobby repo; forks
F1–F17 ratified 2026-10-04, F18 — Claudlobby composes Codex by name — locked 2026-10-05); grounds in its §2.1
(layers and owners), §2.2 (the join key) and §2.3 (enrichment only). **The triad stands; the frame gains a
fourth participant that owns nothing here — the agent runtimes — and the register names who owns what we buy
from them.**

1. **§10.2 — the bought layer is Claudlobby's.** Both runtimes export OpenTelemetry natively; the fleet's
   own per-tool-call events duplicated it (84% of system-event volume, read by nothing — #2145 §1). The
   normalization layer and the normalized attribute names are a contract: one owner now (R1), text
   when P2 writes it in Claudlobby (R2), consumers by gate after that (R3). The vendor→house mapping is the
   `plane-otel` intake's (#2145 F4/F5); the register names no semantic-convention attribute.
2. **§10.4 R8** — #2145 §2.3, made a register rule because it bounds what *any* system may build on from a
   surface no sibling owns.
3. **§10.4 rows 10–12** — the join key (Claudlobby), the export-contract additions (clauDNA), the Codex
   snippet (Claudron, P4). Named now so owners precede text and text precedes consumers; every unshipped
   surface is worded *planned* (R6), with the phase that ships it.
4. **Stale lines fixed where touched** (§10.2 Claudron *Never*, §10.3 Q1, row 5); §10.8 item 12 ledgers
   the fact. §1–§9 and the other §10 mentions stay as written — this file's own convention (§10 preamble:
   facts that amend earlier sections are ledgered, not edited).
5. **What this does not do.** No fork of #2145 changes; nothing unshipped is asserted as shipped; no
   contract *text* lives here (`docs/CLAUDE.md`); the Codex adapter, the attribute names and the export
   fields land in their owners' repos under their own plans.

*The standing rule (00-overview.md, A1) runs the right way here: the decision is amended before the code
(P2, P4) exists.* *Ratifier:* chris — by approving the Claudron PR that carries this section (number and
date filled before merge).
```

  (The `## Amendment —` line inside this fence is the spec's heading, not this plan's: a grep-based skeleton
  check of this file counts one H2 more than §4.1 lists; fence-aware parsers do not. It stays `##` because
  Step 9's `grep -n "^## "` on the *spec* depends on it — C6.)

- [ ] **Step 9 — verify.** `grep -n "^## " <spec>` shows `## Amendment — 2026-10-04 …` immediately before
  `## Appendix Z …`, which is still the last `##`; `grep -c "2026-10-04" <spec>` ≥ 10; `grep -n
  "^updated:" <spec>` prints line 7; `grep -n "^| 1[0-2] |" <spec>` prints three rows; `grep -n "R8 —"
  <spec>` prints one line inside §10.4. `.venv/bin/pytest -q` unchanged from the before leg (no test reads
  the file). Commit: `docs(boundary): amend the spec for the agent runtimes — bought layer, R8, register rows
  10–12 (Claudfather/Claudlobby#2145 P1)`.

### Task 2: one `SNIPPET_EVENTS`

**Files:** `claudron/hooks.py`, `claudron/tests/test_hooks.py`.

- [ ] **Step 1 (tests first):** in `TestGauntletPins` (`test_hooks.py:169`), after
  `test_moved_executable_replaces_not_duplicates` (`:198-220`) — the other replace-not-append pin:

```python
    def test_merge_keys_on_the_one_event_map(self, monkeypatch):
        """`merge_settings` keys on SNIPPET_EVENTS, not a private copy of it:
        an event added to the map alone used to raise KeyError on install."""
        from claudron.hooks import merge_settings

        monkeypatch.setitem(hooks_mod.SNIPPET_EVENTS, "Probe", "probe")   # a synthetic event, not a real host's
        snippet = settings_snippet("/EXE", "/VAULT")
        assert "Probe" in snippet["hooks"]  # the renderer already follows the map (hooks.py:264)
        stale = {"hooks": {"Probe": [{"matcher": "", "hooks": [
            {"type": "command", "command": "/old/venv/claudron --vault /VAULT hook probe"}]}]}}
        merged = merge_settings(stale, snippet)
        assert merged["hooks"]["Probe"] == snippet["hooks"]["Probe"]  # replaced by the identity rule, not appended
        assert set(merged["hooks"]) == set(hooks_mod.SNIPPET_EVENTS)
```

  Run `.venv/bin/pytest claudron/tests/test_hooks.py -q -k one_event_map`: it fails with `KeyError:
  'Probe'` from `hooks.py:394`. (`monkeypatch.setitem` mutates the one dict `doctor.py:51` imported too,
  and restores it; the autouse `_isolated_home` fixture, `conftest.py:20-21`, keeps `Path.home()` away.)
- [ ] **Step 2 — the change.** `claudron/hooks.py:388-394`, before:

```python
    event_cmds = {
        "SessionStart": "session-start",
        "PreCompact": "pre-compact",
        "SessionEnd": "session-end",
    }
    for event, entries in snippet["hooks"].items():
        event_cmd = event_cmds[event]
```

  after:

```python
    for event, entries in snippet["hooks"].items():
        event_cmd = SNIPPET_EVENTS[event]  # the one event map: the snippet was rendered from it
```

  The `SNIPPET_EVENTS` comment (`:232-234`) names its third reader: "`settings_snippet` renders from it,
  `merge_settings` keys its replace-not-append rule on it, and doctor's hook checks (#204) walk it, so all
  three agree on which events the loop needs." `settings_shape_error`'s docstring (`:277`) says "each
  event in `SNIPPET_EVENTS` (when present)" instead of "each of the three events the install writes". The
  `merge_settings` docstring, `_is_claudron_hook` and the snippet itself are untouched, so the contract's
  identity rule (`CLI_CONTRACT.md:366-370`) and normative block (`:310-318`) are unchanged.
- [ ] **Step 3 — verify.** `.venv/bin/pytest claudron/tests/test_hooks.py claudron/tests/test_doctor_hooks.py
  -q`: the new test passes; `TestSessionProtocolDocParity::test_snippet_shape_matches_the_installer`
  (`:246-257`) and `TestHooksInstall::test_write_merges_and_is_idempotent` (`:311`) still pass. Live smoke
  (the one production caller, `cli.py:1099`): `.venv/bin/claudron --vault <tmp vault> hooks install --write
  --settings $TMPDIR/s.json` twice; the second run reports the install as already present and the file is
  byte-identical. Commit: `refactor(hooks): merge_settings keys on SNIPPET_EVENTS — one event map for the
  snippet, the merge and doctor`.

### Task 3: the ops-log id regex — conditional on C1

**Files:** `claudron/ops.py`, `claudron/tests/test_ops.py`; **leg B only:** `docs/CLI_CONTRACT.md`,
`CHANGELOG.md`.

- [ ] **Step 0 — read C1** from the run log: does the recorded Codex `session_id` `fullmatch`
  `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`? (The same question for clauDNA's `paths._SID_RE` is the clauDNA P4
  plan's.) If C1 is not in the log when Tasks 1, 2 and 4 are review-ready, **ship them without this task**
  and open Task 3 as its own follow-up PR: the register amendment gates P2 and must not wait on a regex.
- [ ] **Leg A — it matches** (expected: Codex rollouts are `rollout-<ts>-<uuid>.jsonl` *(doc)*, and a UUID
  matches). **Step A1 (test):** after `test_ops.py:57`:

```python
@pytest.mark.parametrize("ident", [
    "550e8400-e29b-41d4-a716-446655440000",   # Claude Code: a UUID
    "<the C1-recorded Codex id, every hex digit replaced, separators kept>",   # Codex: C1, run log <date>
])
def test_a_hosts_session_id_is_a_safe_directory_name(vault_dir, ident):
    ops.record(detect(vault_dir), "recall.served", session_id=ident, trusted=[])
    (ev,) = _events(vault_dir, "sessions", ident)
    assert ev["session_id"] == ident
```

  **Step A2:** `ops.py:32` → `#: A safe directory name: what a run id already is (runs.RUN_ID_RE), and
  what a host's hook session_id is.` **Step A3:** verify `.venv/bin/pytest claudron/tests/test_ops.py -q`;
  no CHANGELOG line (nothing a user sees changes). Commit: `test(ops): pin that a host's session id is a
  safe log-directory name`.
- [ ] **Leg B — it does not match** (an id with `:`, `/`, a leading `_`, or >128 chars). **Step B1 (tests
  first):** the A1 test with the real C1 shape, failing; plus the parity gate, beside it:

```python
def test_the_session_id_class_is_the_documented_one():
    from claudron.tests.doc_parity import code_values, doc_table
    (row,) = doc_table("docs/CLI_CONTRACT.md", "OPS_LOG_IDS")
    assert row[0] == "`session_id`" and code_values(row[1]) == (ops._ID_RE.pattern,)
```

  **Step B2:** widen `_ID_RE` (`ops.py:33`) by exactly the characters C1's id needs and nothing else; `/`,
  `\`, whitespace and a leading `.` stay out, so `_dir` (`:36-40`) still yields a safe path segment. Add the
  nearest unsafe neighbours of the new class to the parametrize at `:53` (e.g. `"a/b:c"`, `":lead"`).
  Never touch `runs.RUN_ID_RE` (`runs.py:25`): run ids are a CLI contract with exit 2 (`CLI_CONTRACT.md:695-697`).
  **Step B3 — contract text, `docs/CLI_CONTRACT.md:686`:** "an id that isn't a safe directory name — the
  class below — isn't logged", followed by an indented marker + table (the in-bullet `DOCTOR_CODES` shape,
  `:458-469`; `doc_table` reads it, `code_values` extracts the backticked pattern):

```markdown
    <!-- doc-parity: OPS_LOG_IDS -->
    | Id | Admitted (`fullmatch`) | Since |
    |---|---|---|
    | `session_id` | `<the widened pattern>` | `0.<next>` — earlier engines admit `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` and silently skip the rest |
```

  The "Since" cell is R7's version window; `run_id` stays described by the prose at `:695-697`.
  **Step B4:** CHANGELOG (Task 4) gains the `### Changed` bullet below. Commit: `feat(ops): the operations
  log admits Codex session ids; CLI_CONTRACT pins the class (doc-parity OPS_LOG_IDS)`. Additive relaxation,
  not breaking; no capability — an older engine just logs nothing for that session and the loop runs on.

### Task 4: CHANGELOG

**Files:** `CHANGELOG.md`.

- [ ] Under `## Unreleased` (`:3` on `origin/main`, which already has `### Added` for #223, `:5-6`), add a
  `### Changed` section after it:

```markdown
### Changed
- **`hooks install` merges by the one event map.** `merge_settings` carried a private copy of the
  event→verb map `SNIPPET_EVENTS` already holds; an event added to the map alone raised `KeyError` on
  install. One map now serves the snippet, the merge and doctor's hook checks (#204). No change for the
  three events the loop installs. Tests: `claudron/tests/test_hooks.py`.
- **Boundary spec amended for the agent runtimes** (`documentation/plans/2026-07-20-claudfather-boundary-separation.md`
  §Amendment — 2026-10-04, for Claudfather/Claudlobby#2145): Claudlobby owns the bought telemetry layer;
  register rule R8 (runtime-specific signals are enrichment only) and rows 10–12 (session join key,
  clauDNA export additions, Codex session-loop snippet — planned, P4). Nothing shipped changes.
```

  Leg B adds: "**The operations log admits Codex session ids.** `.claudron/sessions/<session_id>/` accepted
  only `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`; it now admits `<class>`. `docs/CLI_CONTRACT.md` §Operations log
  carries the class under `<!-- doc-parity: OPS_LOG_IDS -->`. Tests: `claudron/tests/test_ops.py`."
- [ ] Commit: `docs(changelog): P1 Claudron — one event map, the boundary amendment` (folded into the task
  commits is also fine; one PR either way).

### Task 5: gate, PR body, the release question

- [ ] Whole suite `.venv/bin/pytest -q | tee ../p1-boundary-out/after.txt`: passed count = before + 1
  (Tasks 1–2) or + 2/3 (with Task 3 leg A/B). Mutants, shown then restored: (1) restore the `event_cmds`
  dict → `test_merge_keys_on_the_one_event_map` fails with `KeyError`; (2) leg B: change the pattern in
  `ops.py` only → `test_the_session_id_class_is_the_documented_one` fails. CI (`tests.yml`) green on the PR.
- [ ] No runtime-behavior change in the Claudlobby sense (no composed bot changes), so no canary-root
  observation; the Task 2 Step 3 double-install is this PR's live smoke. Claudron's gate is `pytest`.
- [ ] PR body in the #78 precedent's form: enumerate the spec sections touched (§frontmatter, §10.2 ×2,
  §10.3, §10.4 R8 + rows 5/10–12, §10.8 item 12, §Amendment), say the sibling halves are **not** in this PR
  (rows 10–12 point at text that lands under the Claudlobby P1/P2 and clauDNA P1/P3 plans), link
  Claudfather/Claudlobby#2145 and Claudron#178/#179 (prior art the P4 row depends on), and ask the operator
  to ratify the amendment by approving — the merge is the ratification (00-overview.md `:56-58`); fill the
  `*Ratifier:*` line's PR number and date on the last push.
- [ ] **Release: none from this PR.** The spec amendment is a `documentation/plans/` record, not shipped,
  not in the package. The `hooks.py` change alters no contract (the snippet, the identity rule and the
  `hook` verbs are unchanged) and rides the next release — P4's "Release Claudron" — whose companion pin
  bump (Claudlobby, after P3's `v0.6.1` → `v0.9.0`) picks it up; clauDNA's daily `claudron-release.yml`
  opens its own bump PR when that release exists. Leg B is contract text but relaxes, not breaks; its first
  consumer is P4's Codex adapter, which ships in the same release. `pyproject.toml:7` stays `0.9.0`.

## Test Plan

- New: `test_hooks.py::TestGauntletPins::test_merge_keys_on_the_one_event_map` (red on main, green after);
  `test_ops.py::test_a_hosts_session_id_is_a_safe_directory_name` (leg A and B);
  `test_ops.py::test_the_session_id_class_is_the_documented_one` (leg B).
- Existing gates that must stay green and prove nothing moved: `TestSessionProtocolDocParity` (the
  snippet block, `test_hooks.py:246-257`), `TestHooksInstall` idempotence (`:311`), `TestInstallRecordsTheVault`
  (`:471`, the `EVENT_CMD` pins), `test_doctor_hooks.py` (D009 walks `SNIPPET_EVENTS`), `test_ops.py:53-57`.
- Whole suite twice (before leg on the base, after leg on the branch), counts recorded in the evidence dir.

## Verification Checklist

- [ ] `grep -n "^updated: 2026-10-04" <spec>` prints line 7; `grep -n "^status: active" <spec>` prints line
  4; `grep -n "^## " <spec>` lists `## Amendment — 2026-10-04 …` directly before `## Appendix Z …`, the last
  `##`.
- [ ] `grep -n "^| 1[0-2] |" <spec>` prints three rows; each "Authoritative text" cell of rows 10–12
  contains `planned` and names its flipping PR; row 10 contains `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`,
  `session_alias(platform_session_id, agent_cli="claude")` and `Task 10, Half A`, and no attribute name; row 11
  contains `session.agent_cli`. **A-F5:** `grep -c "gen_ai\.\|agent\.agent_cli" <spec>` prints 0 — neither row 10 nor
  §10.2 names an attribute — and `grep -ci "collector" <spec>` prints 0; §10.2's Claudlobby *Owns* sentence says
  the vendor→house mapping is the intake's and its mapping dict the F5 artefact;
  `grep -c "amended 2026-10-04" <spec>` = 3 (the three case-sensitive stamps the steps
  write: §10.2 Claudron *Never*, §10.3 Q1, row 5) and `grep -ci "amended 2026-10-04" <spec>` = 4 (adds the
  status sentence's "Amended"); the Claudlobby *Owns* "Added" and R8's "added" stamps are counted by neither.
- [ ] D1 ratified (F18 (a), Dependencies): the Codex clauses (§10.2 Claudlobby *Owns*, R8, rows 10 and 12, the
  amendment section's items 3 and 5, Task 4's CHANGELOG bullet) name Codex plainly — `grep -c "second composed
  runtime" <spec>` prints 0 — and the amendment section's opening line names F18; the PR body links the
  [FORK-LOCK F18](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000050806) comment. Claudron's
  `PROJECT_MISSION.md` is untouched.
- [ ] `grep -n "event_cmds" claudron/hooks.py` prints nothing; `grep -n "SNIPPET_EVENTS" claudron/hooks.py`
  prints five lines (the definition `:235`, `settings_snippet` `:264`, `settings_shape_error`'s docstring
  `:277` and loop `:287`, `merge_settings`).
- [ ] With the dict restored, `pytest claudron/tests/test_hooks.py -q -k one_event_map` fails with
  `KeyError: 'Probe'` (shown, restored); as committed it passes.
- [ ] `claudron --vault <tmp> hooks install --write --settings $TMPDIR/s.json` twice → second run reports
  no changes; `shasum` of the file identical across the two runs.
- [ ] Leg A: the Codex id in `test_ops.py` carries a comment naming the C1 run-log entry. Leg B:
  `python -c 'from claudron.tests.doc_parity import *; print(doc_table("docs/CLI_CONTRACT.md","OPS_LOG_IDS"))'`
  prints one row whose pattern equals `claudron.ops._ID_RE.pattern`.
- [ ] `pytest -q` passed count = before + 1 (or + 2/3); `pyproject.toml:7` unchanged; `CHANGELOG.md`
  `## Unreleased` has the `### Changed` bullets.

## What NOT To Do

- Do not add a Codex event map, snippet, `hooks install --host codex` (name settled in P4: `--host` clashes
  with Claudron's existing `host` term; `--front-end`/`--agent` are candidates), doctor-per-host text or a
  `codex-session-loop` capability here — P4, after C1/C3. Do not parameterize `merge_settings` on a map
  "for P4": a Codex `hooks.json` has its own shape and `_is_claudron_hook` is bound to Claude's; P4 decides.
- Do not "unify" the tests' `EVENT_CMD` copies (`test_hooks.py:467`, `test_doctor_hooks.py:25`) with
  `SNIPPET_EVENTS` — a test that imports the map it checks pins nothing.
- Do not put R8, the rows or the bought layer into `docs/` (`docs/CLAUDE.md:24-27`); contract *text* for
  rows 10–12 lands in each owner's repo when it ships (R2). Do not word any of them as existing (R6). Do not
  name a semantic-convention or registry attribute anywhere in the spec (A-F5: the mapping is the intake's,
  the names are Claudlobby's text), and do not write "Collector".
- Do not refresh every stale fact in the spec (the `@v0.2.0` pin at `:429`, §10.5.1's envisaged claim
  mechanism, §10.5.5, §10.7, the "25 lessons"): only the lines this amendment touches, plus the §10.8 item
  that tells a reader which sections are history. Do not edit §1–§9 (§10 preamble, `:358-362`).
- Do not widen `_ID_RE` before C1 is in the run log, do not widen it beyond the characters the recorded id
  needs, and do not touch `runs.RUN_ID_RE`. Do not add a second markdown reader to `doc_parity.py`
  (`:6-9`) — the in-bullet table + `doc_table` is the existing shape.
- Do not bump `pyproject.toml`, tag, or release. Do not edit the clauDNA or Claudlobby repos from this PR.

## Context

area: boundary spec · hooks adapter · ops log — effort: **S** — risk: **Low** (a plan-tier document, a
refactor pinned by the existing parity tests, a conditional regex pin; on leg A no contract text changes) —
priority: P1, §10 order 4 (plan 1 of 6 in epic §10.1) — mission decision: none pending (D1 ratified: F18 (a),
locked 2026-10-05, Codex named plainly) — related: Claudfather/Claudlobby#2145 (epic), Claudlobby#2149
(companion, reads rows 10–12), clauDNA#404, Claudron#84/#85/#102 (the facts the stale lines are fixed to),
Claudron#178/#179 (prior art row 12 and C3 carry) — reforged 2026-10-05 (ironclad cycle 1: C1–C7, X1, X7,
X14, the bought layer worded without a mechanism; cycle 2: the §10 order, D1, row 10 attribute-free; the
2026-10-05 rulings: F18 (a) locked, and A-F5 ratified — §10.2 names no attribute and gives the mapping to the
`plane-otel` intake).

## Canary answers this PR waits on

| Canary | Question | If yes | If no |
|---|---|---|---|
| C1 | Does the recorded Codex `session_id` `fullmatch` `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (`ops.py:33`)? | Task 3 leg A: pin it, fix the comment, no contract text | Task 3 leg B: widen by the recorded class, `OPS_LOG_IDS` table in `CLI_CONTRACT.md:686`, parity test, CHANGELOG `### Changed` |

Tasks 1, 2 and 4 wait on nothing. If C1 lags the review, they merge as this PR and Task 3 follows as its
own PR; the amendment must not wait on a regex.
