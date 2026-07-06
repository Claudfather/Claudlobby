---
title: Session Reflection — Orchestration Trust Loop + clauDNA single-sink + validation ethos
type: knowledge
status: current
owner: claude
created: 2026-05-28
tags: [process, retrospective, claudlobby, claudna]
---

# Session Reflection — 2026-05-28

## Context

Long multi-arc session: product-vision sweep on claudlobby → clauDNA `/publish` single-sink refactor (PR #115, merged) → claudlobby Orchestration Trust Loop (activity_stuck #311, dispatch watchdog #318, validation harness) shipped as a 3-PR stack (#321 → #331 → #323), all merged via admin-squash. Plus `/simplify max` review that caught a critical bug, plus 6 follow-up issues filed.

## Worked

- **Empirical validation harness as a non-negotiable step.** `lib/validate-bot-change.sh` runs the real `fleet-pulse.sh` against a scratch bot + tmux stub and asserts events fire. On its **first invocation** it caught a `fleet-pulse.sh` sweep-abort (a `bot.conf` missing `BOT_SERVICE` killed the whole sweep under `set -euo pipefail`). No composer unit test could have seen it — the bug only existed at runtime.
- **`uv run --with-editable . --with pytest python -m pytest tests/`** is the working invocation for claudlobby tests when the system Python (3.14) lacks pytest. The repo expects `pip install -e '.[dev]'` but uv-ephemeral works without modifying the environment. Worth memorizing or making a `make test`.
- **Reading actual composer output (not assuming) caught the headline bug.** The `/simplify max` reviewer traced what `claudlobby/composer.py` *writes* to `bot.conf` (`MANAGER_TMUX`) vs what the new `notify_manager` *reads* (`MANAGER_BOT_NAME`) — two different strings. Silent failure on every real fleet. Lesson: when adding code that reads a composed value, grep the composer to confirm what's actually written. Don't trust mental model.
- **`git diff main...HEAD` (three-dot)** captured the whole stacked-branch work for `/simplify` review. Two-dot would have only shown the current branch's commits.

## Failed

- **Planned against stale code after `git pull` mid-session.** I read `lib/keepalive.sh` in plan mode while on my session's original branch; later did `git checkout main && git pull` (which brought in #224 fleet observability) and branched for implementation. My plan would have built `.lastbeat` + new hooks duplicating the just-merged `bot-vitals.sh` + `fleet-pulse.sh`. Caught by re-reading the file before writing code, not by anything systematic. **Root cause:** mental model treated planning-time reads as ground truth across a pull.
- **`--delete-branch` on bottom of a 3-PR stack closed the middle PR.** `gh pr merge 321 --admin --squash --delete-branch` auto-closed #322 (GitHub closes PRs against a deleted base; it does **not** retarget to the default branch). Recovery cost ~10 min: rebase the orphaned branch onto new `main` (commits dropped out via patch-id), create a fresh PR (#331), repeat for #323. **Lesson:** for a stack of N PRs, only the topmost merge should `--delete-branch`. Or rebase the next PR onto main between merges.
- **Invented `MANAGER_BOT_NAME` next to existing `MANAGER_TMUX`** (the critical bug). Three duplicate-primitive incidents in one session (also: inline `BOT_SERVICE` grep vs `bot_conf_get` *in the same file*; `strptime` vs `fromisoformat` already used 6 places). **Root cause:** my default is to write fresh rather than grep for existing helpers. Needs an external prompt (review) to find them.
- **Subagents over-claimed mid-investigation.** One Explore agent reported `claudlobby/uptime.py` and a `data/events` JSONL stream that don't exist (it conflated the post-pull main state with the planning-time branch). I caught it by reading the actual file. **Lesson:** treat subagent file references as candidates, not facts; verify file:line by direct Read for anything load-bearing.

## Would Change

- **Plan-mode reads expire on `git pull`.** Add a personal rule: after any `git pull` in a session that has an approved plan, re-Read every file the plan names before writing code. Better still — pull *before* planning, never between.
- **Default to checking what already exists.** Before adding a helper that reads bot.conf / parses a timestamp / matches a config key, grep `lib-common.sh` and the file being edited. Codify in claudlobby `CLAUDE.md` "Working on This Repo."
- **Stacked-PR merges:** use the bottom-up admin-merge sequence WITHOUT `--delete-branch` until the topmost PR. Delete leftover branches in a final cleanup pass.

## Reusable

**Fleet-pulse stuck-detection facts** (anyone touching trust-loop code):
- `MANAGER_TMUX` is the canonical bot.conf key for the manager's tmux session name (set by composer.py:481-489, read by report-back.sh + notify_manager). Do not invent alternates.
- `fleet-pulse.sh` resolves bots via `resolve_bots_dir <fleet>` = `local/<fleet>/runtime/bots`. The `runtime/bots` root-mode path only kicks in with **no** fleet arg. Harnesses/tests must use the local overlay path.
- The `.last-tool-call` marker (touched by `bot-vitals.sh` on every Pre/PostToolUse) is the activity signal for `activity_stuck`. Stale marker + non-idle pane + threshold exceeded = stuck.
- `dispatch-log.jsonl` ↔ `report-back.jsonl` matching is by `(bot, status in {completed,failed,blocked}, ts >= dispatched_at)` — case-insensitive bot match. No task-id correlation; latest terminal report closes all open dispatches for that bot.

**Bash + `set -euo pipefail` trap:** any `VAR=$(grep ... | ... )` where grep can return non-zero will abort the whole script. Always `|| true` at the end of such pipelines, or use a helper that handles no-match safely (`bot_conf_get` in `lib/fleet-pulse.sh` is the canonical example).

**clauDNA + claudlobby sibling boundary:** pre-merge change validation (the Deliver→Add config→Recompose→Observe loop) lives in **claudlobby** as a dev/operator discipline. Longitudinal "trials and combat" scoring of behaviors over real runs is **Claudosseum's** job (mission item #5). claudlobby only *emits* the structured telemetry; do not bolt scoring into the validation loop.
