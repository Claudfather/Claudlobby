---
title: "P4 Lobby hook conformance from measured Codex payloads"
type: plan
status: active
owner: chrisrogers37
created: 2026-10-16
updated: 2026-10-16
epic: "#2145"
repos: [Claudfather/Claudlobby]
---

# P4 Lobby hook conformance from measured Codex payloads

## Summary

Prove that the existing receiver and vitals hook consume the native Codex C1 payloads correctly. The measured common fields already match; use the smallest conformance change supported by those captures, keeping execution/composition in companion #2149.

## Evidence

- Main `284e7f2d` reads `prompt` in `claudlobby/_runtime_scripts/plane-dispatch-in.sh`; its tracked trailer produces a Plane receipt with exact wire byte count/hash and empty stdout. Existing cases are in `tests/test_plane_dispatch_in.py`.
- `claudlobby/_runtime_scripts/bot-vitals.sh` reads `hook_event_name`, `tool_name`, `session_id`, then touches the activity marker under absolute existing BOT_DIR. `tests/test_plane_cutover_keepalive.py` proves its row and no-cwd fallback.
- The canonical [run log](2026-10-04-runtime-neutral-observability-run-log.md#2026-10-16-utc--authorized-native-measurements-and-p4-continuation) records actual C1 native stdin from Codex 0.162.0-alpha.17.2. UserPromptSubmit has a string prompt; Pre/PostToolUse use the common field names and `Bash`; observed PostToolUse response is an empty string. IDs are native UUIDs matching the house grammar.
- #2173/Q17 deliberately retains `tool_call` until P2-a2; this phase does not retire it. D1/F18(a) and D2 were ratified 2026-10-05 in the epic.

## Implementation Plan

### Dependencies

Actual C1 answers must be recorded in the single run log before this plan. Independent fixture review must verify source capture, redaction and lack of content/identifiers before publication. This phase does not depend on the seven-day telemetry canary or Half B's uncompleted timed gates.

### Blocks

This closes the Lobby payload-conformance part of P4. Companion #2149 still needs owner releases and its own launcher, composition, trust and mixed-runtime acceptance evidence.

### Steps

1. Add `tests/fixtures/codex/` containing content-redacted objects transformed directly from actual native stdin. Include provenance: installed version, harness/capture/sanitized hashes, exact retained types/keys and the transform recipe. Use obvious fake paths/IDs/text. Keep prompts, tool commands/results, native private paths, real identifiers and auth out of Git. Never reconstruct unobserved fields from producer source.
2. Extend `tests/test_plane_dispatch_in.py` using its existing isolated Plane rig. Load the captured UserPromptSubmit object, replace only its sanitized prompt with the deterministic test wire plus tracked trailer, run the actual hook and assert empty stdout, exit 0 and the one committed `received` receipt with exact bytes/hash. Include an ordinary untracked prompt/no-row control from the same captured envelope.
3. Extend `tests/test_plane_cutover_keepalive.py` using its existing rig, actual PreToolUse/PostToolUse envelopes and the real hook. Assert the bot marker, original event/tool/session fields, one legacy event through the fleet-event door and no project-cwd marker when BOT_DIR is absent. Empty-string tool_response must be accepted without a dict assumption.
4. If the captured fields already pass, leave runtime scripts byte-identical and report that outcome explicitly. Only a demonstrated captured-field mismatch warrants a narrowly tested normalizer; do not add an abstraction for a hypothetical future runtime. Claude pasted-content handling and Codex dispatch framing belong in the measured execution companion if an actual mismatch is found there.
5. Update this plan and the canonical ledger with exact before/after focused counts/names/exits, capture provenance, independent simplify/review verdict, CI and the runtime-blob identity check. Add the phase plan link to the epic's per-PR index.

## Test Plan

Prepare separate history-free exports of current main and candidate, each with its own editable venv, private HOME/TMPDIR and once-prepared resources under `documentation/test-suite.md`. Run the existing dispatch and cutover/vitals suites unsandboxed in both, comparing counts, failing/error names and exit codes. Run fixture privacy/type checks against the observed capture metadata. Automatic CI provides the broader suite/platform lanes; do not relabel older full-suite output as evidence for this head.

The actual-hook/temporary-Plane replay is executable conformance evidence for this fixture-only phase. The original native P0 capture proves the externally produced shape. No fleet activation is needed for unchanged runtime bytes. Any actual runtime edit instead requires its own independent canary-root observation before merge.

## Verification Checklist

- [x] C1 and redaction provenance independently reviewed; no invented native event or field.
- [x] Actual dispatch receipt bytes/hash and ordinary-prompt no-row control pass with captured Codex envelopes.
- [x] Actual Pre/Post vitals marker/event and missing-BOT_DIR control pass.
- [x] Separate prepared before/after affected runs record counts, names and exits.
- [x] Runtime blobs/modes remain equal to the base, or a justified runtime edit has fresh empirical evidence.
- [ ] Independent simplify/code/spec review converges and exact-head CI is recorded.
- [x] Ledger states reachability: conformance proven, Codex fleet composition/activation remains companion-owned.

## What NOT To Do

Do not implement launcher/config/trust/OTel composition here. Do not remove legacy tool_call before P2-a2, change Claude dispatch framing without a measured failure, infer session lifecycle from tool hooks, relax stdout/fail-open discipline or claim live supervision/mixed-runtime acceptance.

## Context

Area: runtime hook compatibility. Effort: S. Risk: low for tests/fixtures; runtime edits escalate validation. Priority: critical-path P4. No new product fork is introduced; existing runtime-neutral ownership and deferred retirement rulings are preserved.


## Verification results (2026-10-16)

Two separately exported/indexed/prepared legs used Python 3.11.5, identical pinned development dependencies, private HOME/TMPDIR and once-prepared resources. Command: `.venv/bin/python -m pytest --tb=no -ra tests/test_plane_dispatch_in.py tests/test_plane_cutover_keepalive.py`. Main `284e7f2d`: **60 passed, exit 0**; candidate: **83 passed, exit 0**; failed/error names empty on both, delta **+23**. All 1357 baseline/1363 candidate archived-plus-fixture source bytes/modes were unchanged through the runs. All **723 non-test/non-documentation paths** are identical across the legs, so this phase changes no runtime behavior. The initial export install was refused before tests because its Git index was missing; the corrected setup created the index before install and prepared resources once. That setup failure is retained separately from the successful test evidence.

The capture-based envelopes broaden the existing real send-path/receiver/temporary-Plane and vitals rigs. Prompt substitution is deterministic test input; native Codex fleet dispatch, composition, service acceptance and mixed-runtime rollout remain companion-owned. Broader exact-head CI and independent convergence review are recorded when terminal.

Independent full-path evidence/spec/code/simplify review returned **NO FINDINGS** after the two P0 reporting corrections recorded in the canonical run log. Current test/fixture bytes match the verified export; exact-head CI remains pending.
