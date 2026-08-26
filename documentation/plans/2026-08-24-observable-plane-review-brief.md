# Review brief — observable-plane design v2 + Phase-1 plan (second independent review)

**Requesting:** implementation-grade review of the *current* design before Phase-1 execution. The prior external review (2026-08-18) examined spec v1; everything below postdates it and has had no independent eyes.
**Materials:** `documentation/plans/2026-08-18-observable-plane-design-v2.md` (the spec — §9d is the column-level reference) · `documentation/plans/2026-08-19-observable-plane-phase1-kernel.md` (full-code implementation plan, 12 TDD tasks — **never executed**) · branch `design/observable-plane` (commit history = the decision trail) · v1 at `2026-08-17-…` (audit trail only).
**Posture:** read-only review; do not treat RATIFIED/fork labels as immutable where correctness requires reopening. Everything is pre-cutover and un-executed — re-rulings cost plan-editing, not migration. This is the cheapest review this program will ever get.

## What changed since the last review

The v1→v2 reconciliation (your prior findings adopted: intent/attempt split, minted uids, envelope, privacy-first, workstream migration) · a three-week conversational model walk (every table, column-level, ~25 rulings — forks F16–F21 + §9b/§9d) · **F16-v2**: a first-principles physical re-ruling from ~13 typed tables to **constructs + one events log** (9 tables; kinds transmission/task/workstream/system/declaration; per-kind conditional CHECKs; partial indexes; JSON detail tails) · renames (assignments, workstreams, transmissions, communications) · §9d: consolidated column reference, cardinalities, mutation surface, door choreography — whose self-audit found and fixed 10 defects.

## Locked vs open

**Locked (forks F1–F21):** two-lane rule · crash-correctness (construct-before-transmission) · identity model · vocabularies (12 closed + 2 seed registries) · F16-v2 physical shape · privacy defaults · retention (samples 30d; disposable-until-cutover regime) · backfill (clean epoch + selective import).
**Open:** Phase-0 callsite/state-store re-inventory against current main (outstanding) · Pi ingest benchmark (gates direct-writer vs socket daemon; also the F16-v2 flip condition) · Claudron#145 (vault `fleets/` namespace + multi-vault contract) · canonical-bytes golden fixtures (generated at implementation).

## Review asks, ranked

1. **F16-v2 stress test.** The events stream: per-kind conditional CHECK (one large OR-expression), per-kind requiredness enforced in Pydantic only, promoted columns vs JSON `detail` split (carrier promoted; destination/carrier_ref/error/progress/pr_url/renewed_until/plan_ref in detail), partial indexes per hot kind. Is the promoted set right? Does anything in the attention-queue/task-status derivations need a detail field at SQL speed that we've buried in JSON? Is the conditional CHECK testable/maintainable at this size?
2. **Crash-correctness under the new shape.** Re-derive the §7 windows with transmissions as stream rows: does sharing a table with task events open any window the typed-table version lacked? The dual-ack choreography (transmission ack → task ack, causation-linked) — sound, or double-count risk in derivations?
3. **Session identity.** `session_uid = sess_ + sha256(platform_session_id)[:32]`, deterministic, registry-free. **Open question we could not settle: does a resumed Claude Code session retain its session id?** If not, one logical session shards across resumes — assess impact on transcript/OTel joins and propose the fix (chain field? resume events?).
4. **§9d holes.** Cardinalities, nullability, the mutation surface (retention-DELETE exception included), soft-ref posture, wire-naming (the `workstream_event` collision rule) — anything missing or contradictory against §§7–9b.
5. **Phase-1 plan executability.** The plan's code is complete but has NEVER run. Desk-check Tasks 3/4/6 hardest (contracts ↔ events DDL ↔ ingest params must agree column-for-column after the F16-v2 rework); flag any parameter-count/column-order mismatches, the nested-transaction assumption in ingest (its Task-6 note), and the parity test's regex approach against the real DDL text.
6. **Migration/shim realism.** §13's door-shim + dual-write canary against the actual `lib/` callsites (pane_send_verified net, dispatch-task/report-back/tg-post/workstream-update, bridge in/out) — given Phase 0's inventory is still outstanding, what does the inventory need to disprove?
7. **Privacy §11 completeness** before the first canary stores bodies (operator estate opts into full bodies day one).
8. **What did the two-party walk miss entirely?** The standing completeness ask: a concern class with no section — name it.

## Falsifiable claims worth checking directly

- "Partial scans are self-healing; no completeness table needed" (§9).
- "create-vs-update is derivable first-row-in-partition" (SCD windowing over `occurred_at, ingest_seq`).
- "The samples retention DELETE cannot disturb other families" (own table, F16-v2).
- "Every walk ruling survived F16-v2 unchanged" (the F16-v2 fork row's parenthetical — verify against §§7–9b, don't trust it).
- "No `delivered` state exists anywhere" (grep both docs).
