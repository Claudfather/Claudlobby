# #729 Stage-C — A/B comms-eval harness: design checkpoint (WIP)

**Status:** BUILT in this PR (was briefly PAUSED 2026-07-24, then resumed at Chris's direction).
This doc is the design rationale; the locked decisions and the real-run blocker below remain the
reference for F2. Shipped: `lib/ab-comms-eval.sh`, `lib/ab-comms-verdict.py`,
`tests/test_ab_comms_eval.py`.

**Re-verified 2026-08-28 — claims still accurate, file grew well past this doc's original scope.**
All three shipped files still exist (`lib/ab-comms-eval.sh` is now ~55KB, `lib/ab-comms-verdict.py`
~10KB, `tests/test_ab_comms_eval.py` ~7.5KB — grown considerably as it picked up two more
pre-registered experiments beyond the base scaffolding this doc describes: `--experiment
coverage-honesty` for #866 and a `channel-brevity` cell for #728/#729's own P1 gate). The **real-run
blocker is still open**: `grep -rn "message.id\|dedup" lib/transcript-usage.py` finds no
message-id dedup logic, so the "parser over-counts on interactive transcripts (content-block
multiplicity)" defect this doc flags is unfixed. Consistent with that, `AB_EVAL_REAL=1` is still
hard-refused in the shipped script (`lib/ab-comms-eval.sh:823-824`: *"real mode (AB_EVAL_REAL=1) is
REFUSED by this scaffolding: the task battery is an F2 stub..."*) — no real (non-`--dry-run`) A/B
run has been unblocked for the token-efficiency protocol this doc targets. No `status:` frontmatter
exists on this doc to update; leaving as informal WIP/checkpoint framing, which still fits.

**Branch:** `feat/729-stage-c-ab-comms-eval-scaffold` (off fresh `main` @ `c8aa31f`).
**Issue:** [#729](https://github.com/Claudfather/Claudlobby/issues/729) — P2 A/B comms-eval harness + transcript-usage parser (#727 epic; source spec #716).

**Scope of THIS PR (per ari):** the **F2/F4-independent scaffolding only**. Build the skeleton +
token axes; leave task CONTENT and the quality rubric as pluggable stubs (F2-dependent, wait for
Chris). No fleet change, no protocol rollout.

## Recovered artifacts (already on main / the branch)

- **Stage A — `lib/transcript-usage.py`** (merged, #736). stdlib parser. `--json` emits
  `aggregate.main.{protocol_sensitive, cost_weighted_total, turns, input_tokens, output_tokens,
  cache_creation_input_tokens, cache_read_input_tokens, models}` and a `weights` object. Splits
  sidechains (recurses dirs for nested `subagents/**/agent-*.jsonl`). This is the measurement
  instrument the harness wires.
- **Stage B — `lib/interactive-claude-spike.sh`** (commit `d7246ff`, likely unmerged). PROVED the
  mechanic end-to-end: throwaway root + freshbox auth/trust seed on a fresh `CLAUDE_CONFIG_DIR` ->
  `start-bot.sh` boot -> `lib/dispatch.sh` -> await the `report-back.jsonl` ledger row (the
  completion signal) -> recover the transcript from `CONFIG_DIR/projects/` -> attribute via the
  parser. The harness owns a COPY of this recipe; it does not call the spike.
- **Scaffolding source — `lib/validate-bot-change.sh`** (four pieces to own-copy, provenance-commented):
  - tmux socket-isolation shim `vsock()`/`tmux()` + `unset FLEET_NAME` (:36-51)
  - `ROOT` mktemp + per-run private `TMUX_TMPDIR` (literal `/tmp` for sun_path <108) (:60-74)
  - cleanup trap: per-bot `kill-server` + `rm -rf ROOT` (:80-91)
  - `check` pass/fail counter (:126-134)
  - the `claude` stub pattern (`printf 'remote-control is active'; exec cat`) is dry-run-only (:528-533)
  - start-bot env recipe: `CLAUDE_BIN`, `HOME`, `PATH`, `TMPDIR`, `CLAUDLOBBY_ROOT`, `BOOT_LOCK_HOLD_S=0` (:545-557)

## Locked design decisions

1. **`--dry-run` = deterministic synthetic transcripts through the REAL measure+verdict path.**
   Zero model calls, no `CLAUDE_CONFIG_DIR` auth touch, CI-safe. The matrix loop (task x rep x
   variant, paired) is SHARED between modes; only the per-cell body swaps: real mode boots+dispatches
   +collects (spike recipe); dry-run synthesizes a clean one-line-per-message transcript with known
   token numbers (WITH lower than WITHOUT on `protocol_sensitive`). This exercises exactly the
   fork-independent wiring: parser -> axes -> paired deltas -> pass-bar -> `verdict.json`.
2. **Default verdict is INCONCLUSIVE by construction.** With no F2-ratified threshold T and a stub
   quality scorer, the skeleton can NEVER emit PASS. `--threshold` unset -> every task INCONCLUSIVE
   (reason: no ratified threshold). This is the correct safety posture: the harness cannot green a
   real gate until F2 lands.
3. **Real mode (`AB_EVAL_REAL=1`) REFUSES pending F2/P1.** The boot/dispatch/collect functions are
   wired and reviewable (owned spike recipe), but entry is gated: the battery content is a stub and
   `library/protocols/token-efficiency.md` is unmerged (P1), so a real run aborts with a clear
   F2/P1-pending message. Mirrors the placeholder-protocol refusal. No meaningful real run happens
   pre-F2 — faithful to the PAUSE.
4. **Variant fixture** = one throwaway root, `library/`/`templates/`/`voices/` symlinked in, a
   heredoc `fleet.yaml` with two identical `code-review` worker bots `ab-with` / `ab-without`
   differing ONLY in `protocols: [token-efficiency]` on WITH, composed by real
   `claudlobby --root generate` (root mode, `include_defaults: false`). Per-bot `account` ->
   per-bot `CLAUDE_CONFIG_DIR` + `HOME` (`$ROOT/home-<bot>`) so transcript attribution stays
   directory-scoped even on the two-bot task.
   - **Protocol injection:** `token-efficiency.md` is unmerged. Build `$ROOT/library` as a dir of
     symlinks to each real `library/*` entry, EXCEPT `protocols` -> a real dir symlinking each real
     protocol file + an injected minimal **placeholder** `token-efficiency.md`. Real gate runs
     refuse the placeholder (check the real file is present; abort if we fell back).
5. **Verdict stats** = paired per-rep relative reductions per task type; **seeded** bootstrap CI
   (`random.seed(0)`, B=2000, 90% CI — documented header constants so dry-run/tests are
   deterministic). Co-primary axes: PASS iff `protocol_sensitive` reduction CI-low >= `--threshold`
   (T, F2) AND `cost_weighted_total` reduction CI-low >= `--cost-threshold` (default 0.0 =
   no-regression; F2 may raise to a true co-primary bar per the stage-A read-out correction) AND
   quality gate passes (stub) AND zero mechanical failures. Straddle at `--reps-max` -> INCONCLUSIVE
   (never rounded up to PASS). Adaptive stopping rule: run `--reps` (default 3), extend one rep at a
   time to `--reps-max` (default 5) while any task straddles.
6. **F2 seams (stubs):** `score_quality()` returns `{"gate":"pass","scorer":"stub","note":"F2-pending"}`;
   `battery_dispatch_text()`/`battery_must_persist()` return placeholders; threshold T defaults to
   the INCONCLUSIVE sentinel. Battery is T1..T6 labels with the issue shapes as COMMENTS only.
7. **F4** (not yet pinned in recon) — keep anything it might govern as a `--flag` parameter, not a
   baked-in constant, so the scaffolding stays fork-independent.

## The one real blocker for real runs (surfaced by stage B)

**The parser over-counts on INTERACTIVE transcripts.** Interactive Claude Code writes one assistant
message as N content-block lines, EACH repeating the flat `message.usage`. `transcript-usage.py`
sums per-line -> inflation = (content-block multiplicity). Dry-run is UNAFFECTED (synth writes one
line per message). But before ANY real A/B number is trustworthy the parser needs a
`--dedup-by-message-id` mode (sum once per unique `message.id`). This is a change to the merged
stage-A parser (it moves the published read-out numbers), so it belongs in its OWN PR with a
read-out re-run — NOT this scaffolding PR. Flag loudly; do not silently wire real mode to an
over-counting measurement.

## Deliverables (when resumed)

- `lib/ab-comms-eval.sh` — `set -euo pipefail`, sources `lib-common.sh`, bash-3.2-safe (no
  `declare -A`; no apostrophes in `$( )` comments). Flags: `--dry-run`, `--tasks` (count or list),
  `--reps` (3), `--reps-max` (5), `--threshold`, `--cost-threshold` (0.0), `--keep`,
  `--compute-verdict FILE` (test seam: compute verdict from a given results.jsonl and exit).
  Emits `$ROOT/results.jsonl` + `$ROOT/verdict.json` + a markdown table; prints `ROOT=`/`RESULTS=`/
  `VERDICT=` lines for the test to parse.
- `tests/test_ab_comms_eval.py` — subprocess-wraps `--dry-run` (the shell-tests-not-in-CI lesson:
  wrap in Python so CI enforces it): exit 0, `results.jsonl` has both variants, `verdict.json`
  well-formed with both axes + pins + INCONCLUSIVE default; plus a `--compute-verdict` stats case
  (fixture deltas -> known PASS/FAIL and a straddle -> INCONCLUSIVE).
- Register `ab-comms-eval.sh` in the root + `lib/` CLAUDE.md tables.

## Verification when resumed

- `bash lib/ab-comms-eval.sh --dry-run` exits 0, zero model calls, `verdict.json` INCONCLUSIVE.
- `pytest tests/test_ab_comms_eval.py` green.
- PR body cites the stage-A read-out (#727), the stage-B spike note, `--dry-run` output, and the
  parser-dedup blocker as the gating item for a real run.
