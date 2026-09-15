---
title: "Manager Check-in — Chunk 1: The Record and the Run — Implementation Plan"
type: plan
status: draft
owner: fleet owner
created: 2026-09-14
---

# Manager Check-in — Chunk 1: The Record and the Run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Reforge cycle 3 (2026-09-15).** Cycle 2 of `/ironclad` (PR #1550, second review comment; log `scratch/ironclad-2026-09-15_cycle2/verified.md`, 52 claims confirmed) returned 10 blockers against the cycle-2 body: seven mechanical (a door edit placed before the variable it tests, a flag-guard test that could never fire, a test with colliding kwargs, two grant shapes that cannot match their invocations, three README count pins, a harness control that passes on an empty id) and three design (the degraded rule re-created the blocker it replaced, the chunk never produced a real row, and the record could not be judged). All are folded here, plus twelve risks and the remaining YAGNI cuts the cost-benefit lens named. The cycle-2 body is at `273a3db` and `scratch/ironclad-2026-09-15_cycle2/snapshot-cycle2/plan.md`.

**Goal:** One leaf manager, equipped by hand (`protocols: [checkin]`, `skills: [checkin]` in its `fleet.yaml`), runs `/checkin` for real after this chunk merges and deploys: it reads the SSOT through named doors, decides `dispatch | ask | nothing`, records the decision as one plane row BEFORE acting, and any dispatch it makes is joined to that row. `claudlobby checkins --last` shows that row. That real run — not a dry run — is the deploy's positive control and is pasted on the PR.

**Architecture:** One bash door and one CLI read door over the plane, plus a skill and a protocol. The decision is one `checkin_decision` system event (no migration; one severity line). The dispatch join is a second system event, `checkin_dispatch`, that `dispatch-task.sh --checkin` appends to the dispatch batch, atomic with the assignment — the `--supersedes` precedent. Nothing composes differently on the estate: no template change, no `bot.conf` change, no protocol edit beyond one additive new file, no defaults, no trigger. `lib/` changes (the two dispatch flags, one alias fix) are additive and byte-identical without the flags; the canary manager is declared by the operator, by hand, after merge.

**Tech Stack:** Python 3.11 (stdlib + the package's existing pydantic), bash 3.2-compatible `lib/` scripts sourcing `lib-common.sh`, SQLite plane (schema 11), pytest.

**Spec:** `documentation/plans/2026-09-13-manager-checkin-design.md` — this plan implements its §12 chunk 1 as re-sequenced in cycle 3. Executors read both.

## Scope

**In chunk 1 (this plan):** the schema-1 decision contract; `lib/checkin-record.sh`; `dispatch-task.sh --project` and `--checkin`; the `tg-post.sh` sender-alias fix; the two severity lines; `claudlobby checkins` (rows, `--last`, `--bot`, `--since`, `--json`); `library/protocols/checkin.md` (additive) and `library/skills/checkin/SKILL.md`; the harness block; the gauntlet; the post-merge real run on the canary manager.

**Deferred, each to the chunk that consumes it** (§Decision Forks):

| Deferred | To | Why not now |
|---|---|---|
| `planning.initiative`; `lib/checkin-propose.sh`; `dispatch-task.sh --work-item` (looking its id up, as `--supersedes` does); the proposals projection; the `propose` action; the proposal cap enforced by the door | **1d — intake**, after the canary (F1) | the empty-backlog branch of a fleet whose framework repos carry thousands of open issues |
| `requires:` frontmatter with the grant union; the `leaf-manager` role with the cross-fleet direction of spec §10 | **5 — default** (F2) | its only consumer is the registry line; the canary declares by hand |
| the cadence-retirement edits, with a **grep-derived** sweep (`milestone\|beacon\|2.3 min\|10.15 min\|Idle silence\|never go silent` over `library/`, incl. `expertise/orchestration.md:108`, `protocols/comms-topology.md:74`, `skills/lifecycle/SKILL.md:37`, `protocols/telegram-routing.md:29`) | **5 — default** (F3) | an estate-wide change whose replacement must be trusted on one fleet first |
| an `ask` door and `targets.msg_id` | **3 — read door + outcome join** (F4) | asks join by alias + time window; `tg-post.sh`'s alias is fixed here so that join can work |
| `sprint` action; `focus_*` fields | **1c**, **1b** (schema 2) | not read by this chunk's skill |
| `checkins --summary`, `--limit`, SQL-bound `--since` | **3** | no consumer before the outcome join |

## Decision Forks

- **F1 — Defer `propose` and the intake store past the canary.** *Options:* defer to 1d gated on canary evidence / build now. *Lean:* defer. *Ratifier:* operator. **Status: locked** (2026-09-15) — evidence: operator: "okay sure. some fleets on my personal projects would have less issues on their given domains. but yes these work systems we're testing on have thousands." The canary measures `dispatch` on a deep backlog; 1d ships `propose` for the thin-backlog fleets, gated on the canary's `ask`-for-tasks rows on an empty project.
- **F2 — Defer `requires:` linking and the `leaf-manager` role to chunk 5.** *Options:* defer / build now. *Lean:* defer. *Ratifier:* operator. **Status: locked** — evidence: operator instruction ("NOT overbuilding, YAGNI"); no consumer before chunk 5; cycle-1 B8 (linked but never granted) and R2 (the cross-fleet direction) are written into spec §10 so chunk 5 builds them right.
- **F3 — Where the cadence-retirement edits land.** *Options:* (a) chunk 5 estate-wide with the default; during chunk 4 the canary fleet declares `checkin` in `defaults.protocols`, whose preamble states that it governs where it composes beside an older cadence rule; (b) chunk 2; (c) chunk 4. *Lean:* (a). *Ratifier:* operator. **Status: locked** (2026-09-15) — evidence: operator: "Yes I agree with this approach. Lets retire those and make check in the beat."
- **F4 — `ask` has no door in chunk 1.** *Options:* (a) asks join to decisions by alias + time window in chunk 3; a door only if that proves ambiguous; (b) build now. *Lean:* (a). *Ratifier:* operator. **Status: locked** — evidence: YAGNI instruction; `targets.msg_id` was an unfillable field (cycle-1 B11). Consequence folded (cycle-2 R2): `tg-post.sh:81` writes the sender alias from `BOT_NAME` while every other door uses `BOT_ID` — fixed in Task 2 so the chunk-3 join can work.
- **F5 — A cross-fleet `manages:` target does NOT make a manager leaf.** *Options:* count it / do not. *Lean:* do not. *Ratifier:* operator. **Status: locked** — evidence: `config.py:736-748`; spec §10 and §13 corrected. Consumed by chunk 5.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| The skill text is wrong on first contact with a real manager (a refused decision, a door the model cannot drive) | the first real run fails | the dry run precedes the real run (Task 7 steps 8–9); skill text is library content, live on the next `generate`, so a fix is a follow-up PR against `library/` with no restart |
| `lib/dispatch-task.sh` reaches every fleet on pull with no per-bot canary | a regression in the door hits the estate | the flags are additive; behaviour without them is pinned byte-identical by `tests/test_task_id_dispatch.py` + `tests/test_dispatch_task.sh`; the harness runs the real door; the real run on the canary is the positive control |
| The manager reads the record's `considered` list as licence to write essays | the 16 KiB DIAGNOSTIC cap truncates the record | every list is capped in the contract (≤ 10 × ≤ 200 chars); `rationale` and `raise.reason` ≤ 600; a truncated row is still LISTED by the read door |
| GitHub or Claudron is unreachable at check-in time | the manager cannot see the backlog | the degraded rule is per input: plane-known open work is still dispatchable; only backlog-sourced new work is withheld |
| The chunk-1 baselines drift before chunk 4 judges against them | the outcome measure is not attributable | Task 0 records them fleet-scoped and instant-compared; spec §12.4 names both as the judging inputs; chunk 4 re-takes them the same way before arming |

## Global Constraints

Every task's requirements include these. Exact values are copied from the spec and from `CLAUDE.md`.

- **The repo is PUBLIC.** No PII, real chat ids, user ids, handles, tokens, tailnet names or fleet-specific paths in any committed asset, test, fixture or commit message. Fixtures are shape-verbatim with faked identifiers. Never `@`-mention a bot name in GitHub-bound text.
- **bash 3.2 target.** `set -euo pipefail`; source `lib-common.sh`; quote every variable; `printf '%s'` for values; **no apostrophes in comments inside `$( )`** (`tests/test_bash_parse.py` gates `lib/` and every `library/**/*.sh`). **Never guard a flag value with `${2:?…}`** — an expansion fault exits rc 0 through lib-common's EXIT trap (`lib-common.sh:337`; `dispatch-task.sh:94-95` documents it and uses `_flag_val`).
- **New `system` event kinds need NO migration and NO contract change** (`contracts.py:389-418`). Register severity with one line per kind in `claudlobby/plane/registries.py:54`.
- **The `Assignment` payload is `_Strict` (`contracts.py:344`)** — the join is its own system event in the same batch.
- **The plane is always on.** `plane_armed` (`lib-common.sh:495-524`) is opt-OUT: `PLANE_EMIT_DISABLED=1` is the only silencer.
- **Emit from bash through `plane_emit_events <door> <<<"$batch"`** (a here-string, never a pipeline). Every `system` event is actor-anchored `"subject_kind":"actor","subject":"bot:<fleet>/<bot_id>"` — **`BOT_ID`, never `BOT_NAME`** (`lib-common.sh:1420`; `config.py:521`). Ids are minted by `plane_mint_id <prefix>` (`lib-common.sh:526-533`), never a private copy.
- **Doors' rc ladder follows `task-act.sh:55-56`:** 0 acted · 1 usage · 2 refused · 3 the plane could not record.
- **Skill grants** follow the shipped shapes: `Bash(<cmd> *)` for a command (`status/SKILL.md:6-8`), `Bash(*<script>*)` star-bounded with **no space** for a lib script (`restart/SKILL.md:4`), `mcp__plugin_telegram_telegram__reply` for posting. A pipeline is matched per subcommand (`permissions-model.md:52`), so a door is invoked directly with a here-doc, never through `cat |`. **Never `Bash(claudron *)`** — the boundary allows verbs only (`claudron-integration.md:29`; Invariant 5 `tests/test_boundary_invariants.py:275-292`): `Bash(claudron lookup *)`.
- **Read doors:** unreachable ≠ empty — `refuse_unreachable("checkins", note)` (`commands/_helpers.py:160`, prints `UNREACHABLE` upper-case, rc 3). The shared plane session's connection **yields tuples** (`plane-readers.py:53-58`; `status.py:218`) — `plane_session` for the reachability/roster refusal, then `plane.db.open_ro(root)` (`commands/task.py:51`) for named rows.
- **Line numbers** are as of `main` @ `a96b47f`. Re-anchor by the symbol named beside a line, never by the number.
- **Tests run unsandboxed; the baseline is red** (~46 failed / 2 errors on macOS). Under the sandbox, macOS `mktemp` ignores `TMPDIR` and `lib-common.sh`'s source-time `mktemp -d` fails — every bash-door test fails spuriously (the ~250-phantom mode CLAUDE.md documents). The gate is *names + counts* (Task 7), never `pytest | grep`; never pipe `claudlobby validate` or `diff` into `head`/`grep` — redirect to a file, read rc, then read the file.
- **Test command form:** `./.venv/bin/pytest tests/<file>.py -q` from the worktree, whose `.venv` Task 0 creates. Pre-existing test files this plan names, all present on main: `tests/test_skill_ref_resolution.py`, `tests/test_task_id_dispatch.py`, `tests/test_dispatch_type.py`, `tests/test_dispatch_task.sh`, `tests/test_bash_parse.py`, `tests/test_plane_system_events.py`, `tests/test_main.py`, `tests/test_readme_library_counts.py`, `tests/test_boundary_invariants.py`.
- **Operator config (`local/<fleet>/fleet.yaml`) is REPORTED, never edited by the executor**, and **no unmerged code ever runs on the live host**: the canary manager is equipped after merge and pull (Task 7 step 7).
- **Commits:** message via `git commit -F <file>`; end every message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File structure

**Create**
| Path | Responsibility |
|---|---|
| `lib/checkin-contract.py` | Stdlib contract for the schema-1 decision record: `normalize(obj, checkin_id)` lists every defect; CLI filter stdin → normalized JSON, rc 2 with reasons. |
| `lib/checkin-record.sh` | The decision door: mint `ck_` id → validate → ONE `checkin_decision` system event, `source_ref checkin:<id>`. No flags but `--dry-run`/`--help`. |
| `claudlobby/commands/checkins.py` | `claudlobby checkins` — rows newest first, `--last`, `--bot`, `--since`, `--json`; rc 3 on an unreachable plane. |
| `library/protocols/checkin.md` | The check-in protocol: preamble (the precedence sentence), `## Manager` (the surfacing judgment; one line, one ask, one pointer), `## Worker` (one thin line on start/done/blocked). No `requires:`; no self-fire clause. |
| `library/skills/checkin/SKILL.md` | The `/checkin` reasoning contract: READ 0–5 through named doors, DECIDE one project then one of `dispatch \| ask \| nothing`, RECORD before ACT, the per-input degraded rule, the surfacing judgment. |
| `tests/test_checkin_contract.py` | The contract + the two severity registrations. |
| `tests/test_checkin_doors.py` | `checkin-record.sh` (Task 1), `dispatch-task.sh --project/--checkin` and the `tg-post.sh` alias (Task 2) — stub transport, real plane. |
| `tests/test_checkins_cli.py` | The read door over a seeded plane. |
| `tests/test_checkin_library.py` | The protocol composes both sections; the skill is coupled to its doors by name; its grants match its own command lines and contain no forbidden wildcard. |

**Modify**
| Path | Change |
|---|---|
| `lib/dispatch-task.sh:5-24, 78-82, 104-117, 350-354, 395-405, 660-667, 681-689` | `--project KEY`, `--checkin ck_<32hex>`; `DISPATCH_PROJECT` opens the envelope gate; `project_key` on the work item; a `checkin_dispatch` system event appended to the batch AFTER the `emit_triple` gate; disclosure when `--checkin` rides an untracked dispatch. |
| `lib/tg-post.sh:81` | sender alias `bot:$FLEET_NAME/$BOT_ID` (was `BOT_NAME`). |
| `library/protocols/dispatch.md:29-34, 126` | `project:<key>` row in the envelope field table; `--project` in the tracked-dispatch recipe. |
| `claudlobby/plane/registries.py:54-123` | two severity lines. |
| `claudlobby/plane/queries.py` (append) | `CHECKIN_ROWS_SQL`. |
| `claudlobby/commands/_parsers.py:189-197` + imports | registers `checkins` beside `workstreams` (a standalone import line after the `from .core import (…)` block). |
| `lib/validate-bot-change.sh` (append a block) | the empirical gate for the record door and the read door. |
| `README.md:145-146` | library counts 54 skills / 40 protocols / 93 lib scripts (`tests/test_readme_library_counts.py` pins them). |
| `documentation/guides/observability.md` (question→door table), `CLAUDE.md` (lib table; `commands/` line; Key Commands), `CHANGELOG.md` | rows for the two lib scripts and `checkins`. |

**Sizing:** Task 0 S · Task 1 L · Task 2 M · Task 3 M · Task 4 M · Task 5 S · Task 6 S · Task 7 L in wall time (the gauntlet costs roughly Tasks 1–6 combined).

---

### Task 0: Worktree, venv, baseline — and the two measurements this feature is judged on

**Files:** none changed. Produces the branch, the venv, the *before* leg every later gate diffs against, and the two pre-change baselines (fleet-scoped, outbound-only, instant-compared — cycle-2 R1).

- [ ] **Step 1: Worktree on a fresh branch off main**

```bash
cd /Users/chris/Projects/Claudlobby
git fetch -q origin main
git worktree add -b checkin/chunk1-record "$SCRATCH/ck1-wt" origin/main
cd "$SCRATCH/ck1-wt" && git log --oneline -1
```
Expected: one line, the tip of `origin/main`.

- [ ] **Step 2: A venv IN the worktree**

```bash
python3 -m venv .venv && ./.venv/bin/python -m pip install -q -e '.[dev]'
./.venv/bin/python -c "import claudlobby, pathlib; print(pathlib.Path(claudlobby.__file__).resolve())"
```
Expected: a path under `$SCRATCH/ck1-wt/claudlobby/`. Any other path: stop — the editable finder is shadowing the worktree.

- [ ] **Step 3: The before leg, names + counts (unsandboxed)**

```bash
./.venv/bin/pytest --tb=no -ra > "$TMPDIR/run_before.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$TMPDIR/run_before.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$TMPDIR/before.txt"
wc -l < "$TMPDIR/before.txt"; tail -1 "$TMPDIR/run_before.txt"
```
Expected: `rc=1` (the baseline is red) and a `N failed, M passed` line. rc 2/4/5/127 means the run did not complete.

- [ ] **Step 4: The two baselines for the canary fleet, read-only, pasted into `$TMPDIR/baseline.md`**

Fleet-active % (any bot of THIS fleet BUSY per observed minute, last 7 days — the spike's definition, scoped) and the fleet's outbound Telegram volume (the fatigue number: messages SENT by this fleet's bots, never the operator's inbound). `<canary-fleet>` is the fleet name the operator named (ruling 13) — never `ls | head -1`. Instants are compared as instants (`strftime('%s', …)`), the rule `_epoch()` exists for. A plain `sqlite3` open is read-safe on the WAL db (`-readonly` is the CANTOPEN case `plane-readers._open` works around).

```bash
ssh -o BatchMode=yes mini 'bash -s' <<'EOF' | tee "$TMPDIR/baseline.md"
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
DB=~/Projects/claudlobby/state/plane/plane.db
FLEET="<canary-fleet>"
echo "## baseline $(date -u +%F) fleet=$FLEET"
sqlite3 "$DB" "
WITH hb AS (
  SELECT strftime('%Y-%m-%d %H:%M', m.occurred_at) AS minute,
         MAX(json_extract(m.value, '\$.state') = 'BUSY') AS busy
  FROM metric_samples m
  WHERE m.metric = 'bot.heartbeat'
    AND m.fleet_uid = (SELECT uid FROM identity_registry WHERE kind = 'fleet' AND alias = '$FLEET')
    AND strftime('%s', m.occurred_at) >= strftime('%s', 'now', '-7 days')
  GROUP BY minute)
SELECT 'fleet_active_pct', ROUND(100.0 * SUM(busy) / COUNT(*), 1), SUM(busy) || '/' || COUNT(*) || ' minutes' FROM hb;"
sqlite3 "$DB" "
SELECT 'telegram_posts_out_7d', COUNT(*) FROM communications c
WHERE c.sender_alias >= 'bot:$FLEET/' AND c.sender_alias < 'bot:$FLEET' || '0'
  AND strftime('%s', c.occurred_at) >= strftime('%s', 'now', '-7 days')
  AND EXISTS (SELECT 1 FROM events t WHERE t.kind = 'transmission' AND t.msg_id = c.msg_id AND t.carrier = 'telegram-bridge');"
EOF
```
Expected: two rows with non-zero denominators. If the first query returns `0/0`, the fleet alias spelling is not the bare name — `sqlite3 "$DB" "SELECT alias FROM identity_registry WHERE kind='fleet'"` lists the spellings; fix the literal and re-run. If the heartbeat query still returns nothing, that fleet's keepalive is not emitting `bot.heartbeat`: record that fact as the baseline rather than a number. Both rows go into the PR body (Task 7).

---

### Task 1: The decision record — the contract, the record door, the severities

**Files:**
- Create: `lib/checkin-contract.py`, `lib/checkin-record.sh`
- Modify: `claudlobby/plane/registries.py:54-123`
- Test: `tests/test_checkin_contract.py`, `tests/test_checkin_doors.py` (the record half)

**Interfaces:**
- Produces: `checkin-contract.py`: `normalize(obj, *, checkin_id=None) -> dict` (raises `ContractError(reasons)`; the door supplies `checkin_id`, minted by `plane_mint_id ck`; the contract validates its shape and never mints), CLI `python3 checkin-contract.py [--checkin-id ck_<32hex>] < decision.json` rc 0/2. `checkin-record.sh [--dry-run] < decision.json` → stdout `ck_<32hex>`; rc 0 recorded · 1 usage (unknown flag, no identity) · 2 contract refused · 3 plane could not record (or silenced). Identity is `BOT_ID` + `FLEET_NAME` from the environment only (no flags — nothing consumed them; cycle-2 gap). Plane row: `kind='system'`, `event='checkin_decision'`, `source_ref='checkin:<ck_id>'`, `subject_alias='bot:<fleet>/<bot_id>'`, `severity='notice'`, `detail` = the normalized record.
- Schema 1 (the whole of it; `propose`, `sprint`, `focus_*` arrive as schema 2):

```json
{ "schema": 1, "checkin_id": "ck_<32hex>", "prev_checkin_id": "ck_<32hex> | null",
  "inputs_seen": { "open_tasks": 0, "stalls": 0, "unacked": 0, "issues_considered": 0,
                   "knowledge_hits": 0,
                   "considered": ["<candidate> — <why not chosen>", "..."],
                   "unavailable": ["gh"] },
  "delta": { "tasks_opened": 0, "tasks_completed": 0, "stalls_appeared": 0, "stalls_cleared": 0,
             "issues_new": 0, "messages_new": null, "held_pending": 0 },
  "action": "dispatch | ask | nothing",
  "project_key": "<slug or null>",
  "rationale": "<= 600 chars",
  "raise": { "decided": false, "reason": "<non-empty, <= 600>", "held": ["<= 10 items"] } }
```
`considered` is the losers list — what was weighed and passed over, one line each — because "a selector can only be judged against what it did NOT pick" (`lib/sprint-selection-record.py:11-24`, Phase 0 of #974; cycle-2 B8). Every `delta` count is `int | null`, **null = could not measure, never 0** (cycle-1 R17). `raise.reason` is required non-empty and capped like `rationale` (cycle-2 R8). Every list ≤ 10 strings of ≤ 200 chars, so the record stays far under the 16 KiB cap.

- [ ] **Step 1: Write the failing contract tests**

```python
# tests/test_checkin_contract.py
"""The schema-1 decision record (manager check-in spec §7): lib/checkin-contract.py
(stdlib, the dispatch-overdue.py precedent) and the two severity registrations."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "lib" / "checkin-contract.py"
CK = "ck_" + "a" * 32
PREV = "ck_" + "b" * 32

_spec = importlib.util.spec_from_file_location("checkin_contract", CONTRACT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def _decision(**over) -> dict:
    d = {
        "inputs_seen": {"open_tasks": 2, "stalls": 0, "unacked": 1, "issues_considered": 4,
                        "knowledge_hits": 1, "considered": ["#12 flaky test — not mission work"],
                        "unavailable": []},
        "delta": {"tasks_opened": 0, "tasks_completed": 1, "stalls_appeared": 0,
                  "stalls_cleared": 0, "issues_new": 1, "messages_new": None, "held_pending": 0},
        "action": "nothing",
        "project_key": None,
        "rationale": "All work in flight; nothing new worth starting.",
        "raise": {"decided": False, "reason": "no delta the operator would want", "held": []},
    }
    d.update(over)
    return d


def test_a_decision_normalizes_with_the_door_supplied_id():
    out = cc.normalize(_decision(), checkin_id=CK)
    assert out["schema"] == 1 and out["checkin_id"] == CK
    assert out["prev_checkin_id"] is None
    assert out["delta"]["messages_new"] is None          # null survives: could not measure
    assert out["delta"]["tasks_completed"] == 1
    assert out["inputs_seen"]["considered"] == ["#12 flaky test — not mission work"]
    assert "targets" not in out and "focus_declared" not in out["inputs_seen"]


def test_the_contract_never_mints():
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision())                         # no id from the door, none in the body
    assert any("checkin_id" in r for r in exc.value.reasons)


def test_prev_is_kept():
    out = cc.normalize(_decision(prev_checkin_id=PREV), checkin_id=CK)
    assert out["prev_checkin_id"] == PREV


@pytest.mark.parametrize("over, needle", [
    ({"action": "dispatch"}, "must name project_key"),
    ({"action": "ask"}, "raise.decided"),
    ({"action": "propose"}, "action must be one of"),        # schema 2, not this chunk
    ({"action": "coffee"}, "action must be one of"),
    ({"rationale": "x" * 601}, "rationale must be <= 600"),
    ({"rationale": ""}, "rationale"),
    ({"project_key": "Not-A-Slug"}, "project_key"),
    ({"prev_checkin_id": "nope"}, "prev_checkin_id"),
    ({"inputs_seen": {"open_tasks": -1}}, "inputs_seen.open_tasks"),
    ({"inputs_seen": {"considered": ["x"] * 11}}, "inputs_seen.considered"),
    ({"inputs_seen": {"considered": ["x" * 201]}}, "inputs_seen.considered"),
    ({"delta": {"tasks_opened": -1}}, "delta.tasks_opened"),
    ({"raise": {"decided": False, "reason": ""}}, "raise.reason"),
    ({"raise": {"decided": False, "reason": "r" * 601}}, "raise.reason must be <= 600"),
    ({"raise": {"decided": "yes", "reason": "r"}}, "raise.decided"),
    ({"raise": {"decided": False, "reason": "r", "held": ["h"] * 11}}, "raise.held"),
])
def test_defects_are_listed_by_name(over, needle):
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(**over), checkin_id=CK)
    assert any(needle in r for r in exc.value.reasons), exc.value.reasons


def test_every_defect_is_reported_not_just_the_first():
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(action="coffee", rationale=""), checkin_id=CK)
    assert len(exc.value.reasons) >= 2


def test_the_cli_is_a_filter():
    ok = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK],
                        input=json.dumps(_decision()), capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["checkin_id"] == CK
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK], input="not json",
                         capture_output=True, text=True)
    assert bad.returncode == 2 and "not JSON" in bad.stderr
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK],
                         input=json.dumps(_decision(action="coffee")), capture_output=True, text=True)
    assert bad.returncode == 2 and "checkin-contract: action must be one of" in bad.stderr and bad.stdout == ""
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", "nope"],
                         input=json.dumps(_decision()), capture_output=True, text=True)
    assert bad.returncode == 2 and "checkin_id" in bad.stderr


@pytest.mark.parametrize("kind", ["checkin_decision", "checkin_dispatch"])
def test_the_two_kinds_carry_notice_severity(tmp_path, kind):
    emit_batch(tmp_path, [{
        "event_type": "system", "emitter": "t", "fleet": "f",
        "payload": {"event": kind, "subject_kind": "actor", "subject": "bot:f/mgr", "data": {"schema": 1}}}])
    conn = connect(db_path(tmp_path))
    row = conn.execute("SELECT severity FROM events WHERE kind='system' AND event=?", (kind,)).fetchone()
    conn.close()
    assert row["severity"] == "notice"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_contract.py -q`
Expected: collection fails on the missing `lib/checkin-contract.py`; the severity test alone would fail with `None`.

- [ ] **Step 3: The severities**

In `claudlobby/plane/registries.py`, inside `SYSTEM_EVENT_SEVERITY` before its closing `}`:

```python
    # manager check-in (spec §7): the decision record, and the join row a
    # `dispatch-task.sh --checkin` appends to its batch. notice — the record
    # IS the point; nothing here pages.
    "checkin_decision": "notice",
    "checkin_dispatch": "notice",
```

- [ ] **Step 4: The contract module**

```python
#!/usr/bin/env python3
# lib/checkin-contract.py
"""The check-in decision record contract (manager check-in spec §7), schema 1.

Stdlib only (the dispatch-overdue.py precedent): checkin-record.sh pipes the
manager's decision JSON through `normalize` before anything reaches the plane,
so a malformed decision is refused AT THE DOOR with every reason named, never
landed as a row no reader can join.

    python3 checkin-contract.py --checkin-id ck_<32hex> < decision.json
    exit 0 ok (normalized JSON on stdout) / 2 contract violation (reasons on stderr)

The DOOR mints the id (lib-common's plane_mint_id, the one mint every door
uses); this module validates its shape and never mints. Schema 1 is the record
ONE hand-equipped manager can produce this chunk: actions dispatch | ask |
nothing. Later chunks ADD (propose, sprint, focus fields) as schema 2. Every
`delta` count is int | null -- null means "could not measure" and is never
collapsed to 0, because an unchanged delta is the skill's primary argument for
silence. `inputs_seen.considered` is the losers list: a selector can only be
judged against what it did NOT pick (lib/sprint-selection-record.py, #974).
"""
from __future__ import annotations

import json
import re
import sys

SCHEMA = 1
ACTIONS = ("dispatch", "ask", "nothing")
TEXT_MAX = 600
LIST_MAX = 10
ITEM_MAX = 200
INPUTS_COUNTS = ("open_tasks", "stalls", "unacked", "issues_considered", "knowledge_hits")
INPUTS_LISTS = ("considered", "unavailable")
DELTA_COUNTS = ("tasks_opened", "tasks_completed", "stalls_appeared", "stalls_cleared",
                "issues_new", "messages_new", "held_pending")
ID_RE = re.compile(r"^ck_[0-9a-f]{32}$")     # the one spelling: plane_mint_id ck
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")   # a projects.yaml key


class ContractError(ValueError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _str_list(v) -> bool:
    return (isinstance(v, list) and len(v) <= LIST_MAX
            and all(isinstance(s, str) and len(s) <= ITEM_MAX for s in v))


def _text(v) -> bool:
    return isinstance(v, str) and v.strip() != "" and len(v) <= TEXT_MAX


def normalize(obj, *, checkin_id: str | None = None) -> dict:
    """Return the schema-1 record, or raise ContractError listing EVERY defect."""
    if not isinstance(obj, dict):
        raise ContractError(["decision must be a JSON object"])
    bad: list[str] = []
    out: dict = {"schema": SCHEMA}

    cid = checkin_id or obj.get("checkin_id")
    if not cid:
        bad.append("checkin_id required (the door mints it: plane_mint_id ck)")
    elif not ID_RE.match(str(cid)):
        bad.append("checkin_id must be ck_<32 hex>")
    out["checkin_id"] = cid
    prev = obj.get("prev_checkin_id")
    if prev is not None and not ID_RE.match(str(prev)):
        bad.append("prev_checkin_id must be ck_<32 hex> or null")
    out["prev_checkin_id"] = prev

    seen = obj.get("inputs_seen")
    if not isinstance(seen, dict):
        bad.append("inputs_seen must be an object")
        seen = {}
    out["inputs_seen"] = {}
    for k in INPUTS_COUNTS:
        v = seen.get(k, 0)
        if not _count(v):
            bad.append(f"inputs_seen.{k} must be a non-negative integer")
        out["inputs_seen"][k] = v
    for k in INPUTS_LISTS:
        v = seen.get(k, [])
        if not _str_list(v):
            bad.append(f"inputs_seen.{k} must be a list of <= {LIST_MAX} strings of <= {ITEM_MAX} chars")
        out["inputs_seen"][k] = v

    delta = obj.get("delta")
    if not isinstance(delta, dict):
        bad.append("delta must be an object")
        delta = {}
    out["delta"] = {}
    for k in DELTA_COUNTS:
        v = delta.get(k)
        if v is not None and not _count(v):
            bad.append(f"delta.{k} must be a non-negative integer or null (null = could not measure)")
        out["delta"][k] = v

    action = obj.get("action")
    if action not in ACTIONS:
        bad.append(f"action must be one of {', '.join(ACTIONS)}")
    out["action"] = action

    pk = obj.get("project_key")
    if pk is not None and not (isinstance(pk, str) and SLUG_RE.match(pk)):
        bad.append("project_key must be a projects.yaml slug or null")
    out["project_key"] = pk
    if action == "dispatch" and pk is None:
        bad.append("action dispatch must name project_key")

    rationale = obj.get("rationale")
    if not _text(rationale):
        bad.append(f"rationale must be a non-empty string, rationale must be <= {TEXT_MAX} characters")
    out["rationale"] = rationale

    raise_ = obj.get("raise")
    if not isinstance(raise_, dict):
        bad.append("raise must be an object")
        raise_ = {}
    decided = raise_.get("decided", False)
    if not isinstance(decided, bool):
        bad.append("raise.decided must be true or false")
    reason = raise_.get("reason")
    if not _text(reason):
        bad.append(f"raise.reason must be a non-empty string (why it surfaced, or why not), raise.reason must be <= {TEXT_MAX} characters")
    held = raise_.get("held", [])
    if not _str_list(held):
        bad.append(f"raise.held must be a list of <= {LIST_MAX} strings of <= {ITEM_MAX} chars")
    out["raise"] = {"decided": decided, "reason": reason, "held": held}
    if action == "ask" and decided is not True:
        bad.append("action ask requires raise.decided = true (an ask IS a surfacing)")

    if bad:
        raise ContractError(bad)
    return out


def main(argv: list[str]) -> int:
    checkin_id = None
    if len(argv) == 3 and argv[1] == "--checkin-id":
        checkin_id = argv[2]
    elif len(argv) != 1:
        print("usage: checkin-contract.py [--checkin-id ck_<32hex>] < decision.json", file=sys.stderr)
        return 2
    try:
        obj = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"checkin-contract: not JSON: {exc}", file=sys.stderr)
        return 2
    try:
        out = normalize(obj, checkin_id=checkin_id)
    except ContractError as exc:
        for r in exc.reasons:
            print(f"checkin-contract: {r}", file=sys.stderr)
        return 2
    json.dump(out, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 5: Run the contract tests**

Run: `./.venv/bin/pytest tests/test_checkin_contract.py tests/test_plane_system_events.py -q`
Expected: all pass.

- [ ] **Step 6: Write the failing record-door tests**

```python
# tests/test_checkin_doors.py
"""The check-in's write doors (spec §7): checkin-record.sh (Task 1), and
dispatch-task.sh --project / --checkin plus the tg-post.sh alias (Task 2).
Two rigs: a STUB lib-common that captures the batch (tests/test_briefing_trigger.py's
shape), and the REAL shim landing rows in a scratch plane through the cold CLI
rung (tests/test_task_id_dispatch.py's _fake_lib / plane_env)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.conftest import constructed_env
from tests.plane_fixtures import plane_root
from tests.plane_fixtures import ro as _ro

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

STUB_LIB_COMMON = """\
#!/bin/bash
install_error_trap() { :; }
trap 'true' EXIT
plane_armed() { [ "${PLANE_EMIT_DISABLED:-0}" != "1" ]; }
plane_mint_id() { printf '%s_%s' "$1" "${STUB_MINT_HEX:-0123456789abcdef0123456789abcdef}"; }
json_escape() { printf '%s' "$1" | python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read())[1:-1])'; }
show_help() { awk 'NR == 1 { next } /^[^#]/ { exit } { sub(/^# ?/, ""); print }' "$1"; }
plane_emit_events() { cat > "$EMIT_CAPTURE"; PLANE_EMIT_LAST_RC="${STUB_EMIT_RC:-0}"; }
PLANE_EMIT_LAST_RC=0
"""
STUB_CK = "ck_0123456789abcdef0123456789abcdef"


def _decision() -> dict:
    return {"inputs_seen": {"open_tasks": 1}, "delta": {}, "action": "nothing", "project_key": None,
            "rationale": "nothing worth starting",
            "raise": {"decided": False, "reason": "no delta", "held": []}}


def _stub_rig(tmp_path: Path) -> tuple[Path, dict]:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "lib-common.sh").write_text(STUB_LIB_COMMON)
    for name in ("checkin-record.sh", "checkin-contract.py"):
        shutil.copy(LIB / name, lib / name)
        (lib / name).chmod(0o755)
    env = {"EMIT_CAPTURE": str(tmp_path / "emit.json"), "FLEET_NAME": "f", "BOT_ID": "mgr",
           "PATH": os.environ["PATH"]}
    return lib, env


def _run(lib: Path, script: str, env: dict, stdin: str = "", *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(lib / script), *args], input=stdin, capture_output=True,
                          text=True, env=constructed_env(**env), timeout=60)


def _captured(env: dict) -> dict:
    return json.loads(Path(env["EMIT_CAPTURE"]).read_text())


# --- checkin-record.sh, stub transport ----------------------------------------

def test_record_lands_one_actor_anchored_decision_with_the_shared_mint(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == STUB_CK                   # minted by plane_mint_id ck, never a private mint
    (e,) = _captured(env)["events"]
    assert e["event_type"] == "system" and e["emitter"] == "checkin-record"
    assert e["source_ref"] == f"checkin:{STUB_CK}"
    assert e["payload"]["event"] == "checkin_decision"
    assert e["payload"]["subject_kind"] == "actor" and e["payload"]["subject"] == "bot:f/mgr"
    assert e["payload"]["data"]["checkin_id"] == STUB_CK and e["payload"]["data"]["action"] == "nothing"


def test_record_prefers_bot_id_over_bot_name(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "BOT_NAME": "Display Name"}, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    assert _captured(env)["events"][0]["payload"]["subject"] == "bot:f/mgr"


def test_record_refuses_a_malformed_decision_at_rc_2_and_records_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad))
    assert r.returncode == 2
    assert "action must be one of" in r.stderr and "nothing recorded" in r.stderr
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_says_rc_3_when_the_plane_did_not_record(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "STUB_EMIT_RC": "5"}, json.dumps(_decision()))
    assert r.returncode == 3 and "NOT recorded" in r.stderr and r.stdout == ""


def test_record_silenced_only_by_the_harness_exemption_is_rc_3(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "PLANE_EMIT_DISABLED": "1"}, json.dumps(_decision()))
    assert r.returncode == 3 and "PLANE_EMIT_DISABLED" in r.stderr
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_needs_an_identity_rc_1(tmp_path):
    lib, env = _stub_rig(tmp_path)
    env = {k: v for k, v in env.items() if k not in ("FLEET_NAME", "BOT_ID")}
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 1 and "identity" in r.stderr


def test_record_unknown_flag_is_rc_1(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--bot", "x")
    assert r.returncode == 1 and "unknown flag" in r.stderr and r.stdout == ""


def test_record_dry_run_validates_and_writes_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--dry-run")
    assert r.returncode == 0 and r.stdout.strip() == STUB_CK
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_help_prints_the_whole_header(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, "", "--help")
    assert r.returncode == 0 and "exit:" in r.stdout and "3 the plane could not record" in r.stdout


# --- the REAL spine: the row lands in a scratch plane -----------------------------

REAL_DOOR_FILES = ("checkin-record.sh", "checkin-contract.py", "lib-common.sh",
                   "plane-emit.sh", "plane-socket-client.py")


def _real_rig(tmp_path: Path) -> tuple[Path, dict]:
    root = plane_root(tmp_path)
    lib = root / "lib"
    lib.mkdir()
    for name in REAL_DOOR_FILES:
        (lib / name).symlink_to(LIB / name)
    env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_NAME": "f", "BOT_ID": "mgr",
           "PLANE_EMIT_CLI": str(CLI), "PLANE_SOCKET": str(root / "no-daemon.sock"),
           "HOME": str(root), "PATH": os.environ["PATH"]}
    return lib, env


def test_the_decision_lands_on_a_real_plane(tmp_path):
    lib, env = _real_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    ck = r.stdout.strip()
    assert ck.startswith("ck_") and len(ck) == 35
    with _ro(Path(env["CLAUDLOBBY_ROOT"])) as conn:
        row = conn.execute(
            "SELECT severity, source_ref, subject_alias, detail, detail_truncated FROM events"
            " WHERE kind='system' AND event='checkin_decision'").fetchone()
    assert row is not None
    assert row["severity"] == "notice" and row["source_ref"] == f"checkin:{ck}"
    assert row["subject_alias"] == "bot:f/mgr" and row["detail_truncated"] == 0
    assert json.loads(row["detail"])["action"] == "nothing"
```

- [ ] **Step 7: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q` (unsandboxed)
Expected: every test fails on the missing `lib/checkin-record.sh`.

- [ ] **Step 8: The record door**

```bash
#!/bin/bash
# checkin-record.sh -- THE write door for a manager check-in decision
# (manager check-in spec §7). The skill hands it the decision JSON on stdin;
# it mints the checkin_id (plane_mint_id ck -- the one mint every door uses),
# validates the schema-1 contract (checkin-contract.py), and lands ONE
# actor-anchored system event `checkin_decision`, stamped source_ref
# checkin:<checkin_id> -- the task-recheck stamp idiom: the ref is the address
# a reader joins on.
#
# Usage: checkin-record.sh [--dry-run] < decision.json
#   stdout: the checkin_id (one line)
#   exit:   0 recorded (committed or spooled)
#           1 usage (unknown flag; no identity)
#           2 contract refused (every reason on stderr; nothing recorded)
#           3 the plane could not record (disclosed; nothing recorded)
#
# Identity is BOT_ID + FLEET_NAME from the environment the manager session
# sources from bot.conf -- never BOT_NAME (a display field), and no flags: no
# caller supplies them. RECORD BEFORE ACT is the skill's rule, so for THIS door
# the record IS the action: an unrecorded decision is a failure it says so
# about (rc 3), never a silent 0. The plane is always on; PLANE_EMIT_DISABLED=1
# (the harness exemption) is the one silencer, also rc 3.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        -h|--help) show_help "$0"; exit 0 ;;
        *) printf 'checkin-record: unknown flag %s\n' "$1" >&2; exit 1 ;;
    esac
done
BOT="${BOT_ID:-}"
FLEET="${FLEET_NAME:-${CLAUDLOBBY_FLEET:-}}"
if [ -z "$BOT" ] || [ -z "$FLEET" ]; then
    printf 'checkin-record: no identity -- BOT_ID and FLEET_NAME must be set (a manager session sources them from bot.conf)\n' >&2
    exit 1
fi

checkin_id=$(plane_mint_id ck)
raw=$(cat)
if ! normalized=$(printf '%s' "$raw" | python3 "$LIB_DIR/checkin-contract.py" --checkin-id "$checkin_id"); then
    printf 'checkin-record: decision refused (nothing recorded)\n' >&2
    exit 2
fi

if [ "$DRY" = "1" ]; then
    printf '%s\n' "$checkin_id"
    exit 0
fi
if ! plane_armed checkin-record; then
    printf 'checkin-record: plane silenced (PLANE_EMIT_DISABLED=1) -- decision %s NOT recorded\n' "$checkin_id" >&2
    exit 3
fi
utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
safe_fleet=$(json_escape "$FLEET")
printf -v batch '{"events":[{"event_type":"system","emitter":"checkin-record","source_ref":"checkin:%s","fleet":"%s","occurred_at":"%s","payload":{"event":"checkin_decision","subject_kind":"actor","subject":"bot:%s/%s","data":%s}}]}' \
    "$checkin_id" "$safe_fleet" "$utc" "$safe_fleet" "$(json_escape "$BOT")" "$normalized"
plane_emit_events checkin-record <<<"$batch"
if [ "${PLANE_EMIT_LAST_RC:-0}" -ne 0 ]; then
    printf 'checkin-record: plane record failed rc=%s -- decision %s NOT recorded\n' "$PLANE_EMIT_LAST_RC" "$checkin_id" >&2
    exit 3
fi
printf '%s\n' "$checkin_id"
```

`chmod 0755 lib/checkin-record.sh lib/checkin-contract.py`. The header is one contiguous `#` block from line 2 (`show_help` prints from line 2 to the first non-`#` line).

- [ ] **Step 9: Run the door tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py tests/test_bash_parse.py -q` (unsandboxed)
Expected: all pass. `test_the_decision_lands_on_a_real_plane` proves the shim's cold rung records; its "daemon unavailable — falling back to cold CLI" stderr line is expected.

- [ ] **Step 10: Commit**

```bash
git add lib/checkin-contract.py lib/checkin-record.sh claudlobby/plane/registries.py tests/test_checkin_contract.py tests/test_checkin_doors.py
printf '%s\n' 'feat(checkin): the decision record — schema-1 contract and the record door' '' 'lib/checkin-contract.py (stdlib) refuses a malformed decision with every reason;' 'the door mints the id via plane_mint_id, the contract only validates it. The' 'record carries the losers (inputs_seen.considered — a selector is judged by' 'what it did not pick, #974), null delta counts (could not measure), and a' 'capped, required raise.reason. lib/checkin-record.sh lands ONE actor-anchored' 'checkin_decision (BOT_ID alias, source_ref checkin:<id>), task-act rc ladder.' 'Spec §7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c1.txt"
git commit -q -F "$TMPDIR/c1.txt" && git log --oneline -1
```

---

### Task 2: `dispatch-task.sh --project` and `--checkin`; the `tg-post.sh` alias; the envelope docs

**Files:**
- Modify: `lib/dispatch-task.sh:5-24` (flag docs), `:78-82` (init), `:104-117` (parse), `:350-354` (the envelope gate), `:395-405` (envelope), `:660-667` (after the `emit_triple` gate — the join block), `:681-689` (the batch); `lib/tg-post.sh:81`; `library/protocols/dispatch.md:29-34, 126`
- Test: `tests/test_checkin_doors.py` (append the Task 2 half)

**Interfaces:**
- Produces: `--project KEY` (slug; `| project:KEY` in the envelope; opens the envelope gate; `project_key` on the work item — the fix for the measured 0/374). `--checkin ck_<32hex>`: one `system` event `checkin_dispatch` appended to the dispatch batch AFTER the `emit_triple` gate (`:660-663`) — `source_ref` = the dispatch ref, actor = `safe_sender` (the same alias the batch writes as `assigned_by`), `data = {checkin_id, assignment_id, work_item_id, task_id}` — atomic with the assignment. On an untracked dispatch (a control type, or no ref) it is **disclosed** on stderr, never silently dropped (the `--supersedes` precedent at `:645`). `tg-post.sh` writes `bot:$FLEET_NAME/$BOT_ID` so every door shares one alias rule. `checkin_dispatch`'s reader is chunk 3's outcome join (`claudlobby events` prefix-filters `fleet-events:`); the real-plane test below proves the row lands.

- [ ] **Step 1: Append the failing tests**

```python
# append to tests/test_checkin_doors.py

# --- dispatch-task.sh --project / --checkin; tg-post.sh alias (Task 2) ----------

from tests.test_task_id_dispatch import _bash, _fake_lib

DISPATCH_STUB = "#!/bin/bash\nprintf '%s\\n' \"$2\" > \"$DISPATCH_CAPTURE\"\nexit 0\n"
CK = "ck_" + "a" * 32


def test_dispatch_project_alone_opens_the_envelope_and_stamps_the_work_item(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    sent = (tmp_path / "sent.txt").read_text()
    assert sent.startswith("[BOTCOMMAND]") and "| project:shop" in sent     # --project ALONE opens the gate
    with _ro(tmp_path) as conn:
        row = conn.execute("SELECT project_key FROM work_items").fetchone()
    assert row["project_key"] == "shop"


def test_dispatch_refuses_a_non_slug_project(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" --project "Not Slug" w1 "x"', env=env)
    assert r.returncode == 1 and "project" in r.stderr


def test_dispatch_checkin_appends_the_join_row_to_the_same_batch(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "sent.txt").read_text().startswith("[BOTCOMMAND]")   # the send happened
    with _ro(tmp_path) as conn:
        asg = conn.execute("SELECT assignment_id, work_item_id, source_ref, assigned_by_uid FROM assignments").fetchone()
        link = conn.execute("SELECT detail, subject_uid, source_ref, severity FROM events"
                            " WHERE kind='system' AND event='checkin_dispatch'").fetchone()
    assert asg is not None and link is not None
    d = json.loads(link["detail"])
    assert d["checkin_id"] == CK and d["assignment_id"] == asg["assignment_id"]
    assert d["work_item_id"] == asg["work_item_id"] and d["task_id"].startswith("t-")
    assert link["source_ref"] == asg["source_ref"] and link["severity"] == "notice"
    assert link["subject_uid"] == asg["assigned_by_uid"]      # the dispatcher, by the plane's own alias rule


def test_dispatch_checkin_on_an_untracked_dispatch_is_disclosed_not_dropped(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --type query --checkin {CK} w1 "where are you?"', env=env)
    assert r.returncode == 0, r.stderr
    assert "--checkin ignored" in r.stderr and "query" in r.stderr
    with _ro(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event='checkin_dispatch'").fetchone()[0] == 0


def test_dispatch_refuses_a_malformed_checkin_id(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin nope w1 "x"', env=env)
    assert r.returncode == 1 and "--checkin" in r.stderr


def test_dispatch_checkin_without_a_value_is_rc_1_never_0(tmp_path):
    # flag FIRST and alone: the parse loop stops at the first positional
    # (dispatch-task.sh:115), so a trailing flag would be task text. Through
    # _fake_lib the REAL lib-common (and its EXIT trap) is in play.
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin', env=env)
    assert r.returncode == 1 and "--checkin needs a value" in r.stderr and r.stdout == ""


def test_tg_post_anchors_the_sender_on_bot_id():
    text = (LIB / "tg-post.sh").read_text()
    assert 'bot:$FLEET_NAME/$BOT_ID' in text
    assert 'bot:$FLEET_NAME/$BOT_NAME' not in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q -k "dispatch or tg_post"` (unsandboxed)
Expected: 7 failed — `unknown flag '--project'` / `'--checkin'`, and the alias assertion.

- [ ] **Step 3: The flags**

(a) Flag docs, after the `--ref URL` line (`:10`):
```bash
#   --project KEY      projects.yaml project (adds project:<KEY> to the envelope and
#                      project_key to the plane work item -- the well-defined bar)
#   --checkin ID       The check-in decision this dispatch acts on (ck_<32hex>):
#                      appends a checkin_dispatch join row to the plane batch, atomic
#                      with the assignment. Disclosed and ignored on an untracked
#                      dispatch (a control type mints no assignment to join).
```
(b) Init beside `DISPATCH_REPO=""` (`:78`): `DISPATCH_PROJECT=""` and `DISPATCH_CHECKIN=""`.
(c) Parse loop (`:104-117`), beside `--repo`:
```bash
        --project)      DISPATCH_PROJECT=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --checkin)      DISPATCH_CHECKIN=$(_flag_val "$1" "${2:-}"); shift 2 ;;
```
and directly after the loop:
```bash
if [ -n "$DISPATCH_PROJECT" ] && ! printf '%s' "$DISPATCH_PROJECT" | grep -Eq '^[a-z][a-z0-9-]*$'; then
    echo "dispatch-task: --project must be a projects.yaml slug ([a-z][a-z0-9-]*), got '$DISPATCH_PROJECT'" >&2; exit 1
fi
if [ -n "$DISPATCH_CHECKIN" ] && ! printf '%s' "$DISPATCH_CHECKIN" | grep -Eq '^ck_[0-9a-f]{32}$'; then
    echo "dispatch-task: --checkin must be a check-in id (ck_<32hex>), got '$DISPATCH_CHECKIN'" >&2; exit 1
fi
```
(d) **The envelope gate** (`:352-353`) — the project opens it, or a `--project`-only dispatch sends freeform:
```bash
if [ -n "$FORCE_ENVELOPE" ] || [ -n "$DISPATCH_REPO" ] || [ -n "$DISPATCH_PRIORITY" ] \
   || [ -n "$DISPATCH_REF" ] || [ -n "$DISPATCH_WORKSTREAM" ] || [ -n "$DISPATCH_PROJECT" ]; then
```
(e) Envelope (`:395-400`), after the `repo:` line:
```bash
    [ -n "$DISPATCH_PROJECT" ]    && DISPATCH_MSG="$DISPATCH_MSG | project:$DISPATCH_PROJECT"
```
(f) **The join block — AFTER the `emit_triple` gate, beside `link_frag`** (`:660-667`; cycle-2 B1: placed before `:660`, `emit_triple` is unbound under `set -u` and the whole dispatch is lost). Replace the two lines `local link_frag="" ws_frag="" repo_frag="" deadline_frag="" iso_deadline=""` / `if [ -n "$emit_triple" ]; then … fi` with:
```bash
    local link_frag="" ws_frag="" repo_frag="" proj_frag="" deadline_frag="" iso_deadline="" ck_ev=""
    if [ -n "$emit_triple" ]; then
        link_frag="\"work_item_id\":\"$PLANE_WI_ID\",\"assignment_id\":\"$PLANE_ASG_ID\","
    fi
    # The check-in join (spec §7): the decision row was recorded BEFORE this
    # dispatch (RECORD before ACT), and the Assignment payload is strict, so
    # the link is its own system event in the SAME batch -- the supersede
    # precedent below. Only a tracked dispatch has an assignment to join; an
    # untracked one says so, like a --supersedes that found nothing.
    if [ -n "$DISPATCH_CHECKIN" ]; then
        if [ -n "$emit_triple" ]; then
            ck_ev="{\"event_type\":\"system\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"event\":\"checkin_dispatch\",\"subject_kind\":\"actor\",\"subject\":\"$safe_sender\",\"data\":{\"checkin_id\":\"$DISPATCH_CHECKIN\",\"assignment_id\":\"$PLANE_ASG_ID\",\"work_item_id\":\"$PLANE_WI_ID\",\"task_id\":\"$(json_escape "$TASK_ID")\"}}}"
        else
            echo "dispatch-task: --checkin ignored: a $DISPATCH_TYPE dispatch mints no assignment to join" >&2
        fi
    fi
```
(`safe_sender` is the json-escaped sender alias `bot:<fleet>/<id>` built at `:585-586`, the same value the batch writes as `assigned_by`.)
(g) The batch (`:681-689`): the project fragment beside `repo_frag`, the join beside `sup_ev`:
```bash
        [ -n "$DISPATCH_PROJECT" ] && proj_frag=",\"project_key\":\"$(json_escape "$DISPATCH_PROJECT")\""
        wi_ev="{\"event_type\":\"work_item\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$PLANE_WI_ID\",\"title\":\"$safe_task\",\"created_by\":\"$safe_sender\"${ws_frag}${repo_frag}${proj_frag}}}"
```
and the `printf -v _batch` line becomes:
```bash
        printf -v _batch '{"events":[%s,%s,%s%s%s]}' "$wi_ev" "$asg_ev" "$comm" "${sup_ev:+,$sup_ev}" "${ck_ev:+,$ck_ev}"
```
(`asg_ev`, `comm` and the `plane_emit_events` line stay as they are.)

- [ ] **Step 4: The alias fix and the envelope docs**

`lib/tg-post.sh:81`: `--arg sender "bot:$FLEET_NAME/$BOT_NAME"` → `--arg sender "bot:$FLEET_NAME/$BOT_ID"`; update the comment at `:64` (`BOT_NAME` → `BOT_ID`).

`library/protocols/dispatch.md`: in the envelope field table (`:29-34`) add a row after `workstream:`:
```markdown
| `project:<key>` | projects.yaml key | The project this task belongs to — the well-defined bar; lands `project_key` on the plane work item |
```
and in the recipe sentence at `:126` extend the flag list: ``(or any envelope flag: `--repo`, `--priority`, `--ref`, `--workstream`, `--project`)``. Then `grep -n 'workstream:' library/protocols/worker-lifecycle.md`: if the inbound `[BOTCOMMAND]` field list there names `workstream:`, add `project:<key>` beside it in the same form (both files restate the envelope; `dispatch-task.sh:86-92` names them as the prose source).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py tests/test_task_id_dispatch.py tests/test_dispatch_type.py tests/test_bash_parse.py -q && bash tests/test_dispatch_task.sh` (unsandboxed)
Expected: all pass (`test_dispatch_type.py` parses the protocol docs' `[BOTCOMMAND]` TYPE list — untouched by a field-table row).

- [ ] **Step 6: Commit**

```bash
git add lib/dispatch-task.sh lib/tg-post.sh library/protocols/dispatch.md library/protocols/worker-lifecycle.md tests/test_checkin_doors.py
printf '%s\n' 'feat(dispatch-task): --project stamps project_key and opens the envelope; --checkin appends the join row' '' 'project_key on the work item fixes the measured 0/374. The checkin_dispatch' 'system event rides the SAME batch as the assignment, after the emit_triple gate' '(the supersede precedent); an untracked dispatch discloses the ignored flag.' 'tg-post.sh anchors its sender on BOT_ID like every other door. Spec §7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c2.txt"
git commit -q -F "$TMPDIR/c2.txt" && git log --oneline -1
```

(If `worker-lifecycle.md` needed no edit, drop it from `git add`.)

---

### Task 3: `claudlobby checkins` — the read door

**Files:**
- Modify: `claudlobby/plane/queries.py` (append `CHECKIN_ROWS_SQL`), `claudlobby/commands/_parsers.py:189-197` + its imports
- Create: `claudlobby/commands/checkins.py`
- Test: `tests/test_checkins_cli.py`

**Interfaces:**
- Consumes: `brief.plane_session(paths, fleet) -> (plane, note)` (`brief.py:252`; its `conn` yields TUPLES), `plane.db.open_ro(root) -> (conn | None, reason)` (`db.py:28`, named rows), `_helpers.refuse_unreachable(command, note) -> int` (`:160`, prints `UNREACHABLE`), `queries.fleet_alias_range` / `fleet_range_params` / `_epoch`.
- Produces: `CHECKIN_ROWS_SQL` (binds: fleet, fleet) ordered `occurred_at DESC, ingest_seq DESC`, truncated rows returned with NULL fields; `cmd_checkins(args) -> int`: `--fleet` (dest `checkins_fleet`), `--bot`, `--since 7d` (the report-back grammar `24h, 7d, 30m, ISO`, parsed inline — not a new helper; cycle-2 gap), `--last` (ignores `--since`), `--json`; rc 0 · 2 no fleet / bad since · 3 plane unreachable. No `--limit` (chunk 3). `--since` filters in Python over the fleet's rows — intentional through chunk 3, which binds it in SQL with `--summary`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkins_cli.py
"""`claudlobby checkins` — the read door (spec §11), minimal form. Seeded through
the real emit spine. The plane session's connection yields tuples (status.py:218);
the door reads named rows through open_ro."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby.commands import checkins as cmd
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import plane_root

REPO_ROOT = Path(__file__).resolve().parent.parent
F = "ck-fleet"
CK1, CK2, CK3 = ("ck_" + c * 32 for c in "abc")


@pytest.fixture(autouse=True)
def root(tmp_path: Path) -> Path:
    r = plane_root(tmp_path)
    (r / "lib").mkdir()
    for name in ("dispatch-overdue.py", "plane-readers.py", "plane-lookup.py"):
        shutil.copy(REPO_ROOT / "lib" / name, r / "lib" / name)
    return r


class _Args:
    def __init__(self, root, **kw):
        self.root, self.fleet, self.seed = str(root), None, False
        self.checkins_fleet = kw.get("fleet", F)
        self.bot = kw.get("bot")
        self.since = kw.get("since", "7d")
        self.last = kw.get("last", False)
        self.json = kw.get("json", False)


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _record(ck: str, **over) -> dict:
    d = {"schema": 1, "checkin_id": ck, "prev_checkin_id": None,
         "inputs_seen": {"open_tasks": 0, "considered": [], "unavailable": []}, "delta": {},
         "action": "nothing", "project_key": None, "rationale": f"r-{ck[-4:]}",
         "raise": {"decided": False, "reason": "quiet", "held": []}}
    d.update(over)
    return d


def _decision(root, bot: str, ck: str, *, age_h: float, **over):
    emit_batch(root, [{
        "event_type": "system", "emitter": "checkin-record", "fleet": F,
        "source_ref": f"checkin:{ck}", "occurred_at": _ago(hours=age_h),
        "payload": {"event": "checkin_decision", "subject_kind": "actor",
                    "subject": f"bot:{F}/{bot}", "data": _record(ck, **over)}}])


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_rows_are_newest_first_by_occurred_at_scoped_to_fleet_and_bot(root, capsys):
    _decision(root, "mgr", CK1, age_h=5)          # arrival order CK1, CK2, CK3;
    _decision(root, "mgr", CK2, age_h=1, prev_checkin_id=CK1, action="dispatch", project_key="shop")
    _decision(root, "other", CK3, age_h=2)        # occurred order CK2 (1h), CK3 (2h), CK1 (5h)
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert [r["checkin_id"] for r in rows] == [CK2, CK3, CK1]
    assert rows[0]["prev_checkin_id"] == CK1 and rows[0]["action"] == "dispatch" and rows[0]["bot"] == "mgr"
    assert rows[0]["raise"] == {"decided": False, "reason": "quiet"}
    assert cmd.cmd_checkins(_Args(root, bot="mgr", json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK1]


def test_last_ignores_the_window(root, capsys):
    _decision(root, "mgr", CK1, age_h=24 * 30)    # a manager idle for a month
    assert cmd.cmd_checkins(_Args(root, bot="mgr", last=True, json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK1]   # the chain is not broken by --since


def test_since_window(root, capsys):
    _decision(root, "mgr", CK1, age_h=30)
    _decision(root, "mgr", CK2, age_h=1)
    _decision(root, "mgr", CK3, age_h=2)
    assert cmd.cmd_checkins(_Args(root, since="24h", json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK3]
    assert cmd.cmd_checkins(_Args(root, since="yesterday")) == 2


def test_a_truncated_record_is_listed_and_marked_never_dropped(root, capsys):
    _decision(root, "mgr", CK1, age_h=1, rationale="x" * 20000)       # over the 16 KiB DIAGNOSTIC cap
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert len(rows) == 1 and rows[0]["truncated"] is True and rows[0]["checkin_id"] is None


def test_an_empty_fleet_plane_answers_no_checkins_at_rc_0(root, capsys):
    # a plane that has SEEN the fleet (one identity row — the roster the session
    # opens on) but holds no decision is EMPTY, not unreachable
    emit_batch(root, [{"event_type": "system", "emitter": "t", "fleet": F,
                       "payload": {"event": "report_status", "subject_kind": "actor",
                                   "subject": f"bot:{F}/w1", "data": {"status": "progress"}}}])
    assert cmd.cmd_checkins(_Args(root)) == 0
    out = capsys.readouterr().out
    assert F in out and "no check-ins" in out


def test_unreachable_plane_refuses_at_rc_3(tmp_path, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert cmd.cmd_checkins(_Args(bare)) == 3
    assert "UNREACHABLE" in capsys.readouterr().err       # refuse_unreachable's own token, upper-case
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkins_cli.py -q`
Expected: `ImportError: cannot import name 'checkins'`.

- [ ] **Step 3: The query**

Append to `claudlobby/plane/queries.py`:

```python

# --- the manager check-in (manager check-in spec §7, §11) ---------------------
# A check-in is ONE system event, `checkin_decision`, actor-anchored on the
# manager, stamped source_ref checkin:<checkin_id>; the record is its detail
# (schema 1). Ordered by WHEN IT HAPPENED (`occurred_at`, the clock every other
# read of this door uses), ingest_seq as the tiebreak -- under the shim's spool
# rung the two clocks diverge, and "the previous check-in" means the one that
# happened last, not the one that landed last. A truncated detail is NOT a JSON
# document (detail_truncated=1 IS the parse guard), so the CASE short-circuits
# before json_type sees it (measured: no error, NULL fields) and the row is
# still returned -- the read door exists to show every decision, and dropping
# the over-cap ones would hide exactly the records that most need looking at.
# Binds: fleet, fleet.
CHECKIN_ROWS_SQL = (
    "SELECT"
    " CASE WHEN e.detail_truncated = 0 AND json_type(e.detail, '$.checkin_id') = 'text'"
    "      THEN json_extract(e.detail, '$.checkin_id') END AS checkin_id,"
    " CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.prev_checkin_id') END AS prev_checkin_id,"
    " e.subject_alias AS subject_alias, e.occurred_at AS occurred_at,"
    " CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.action') END AS action,"
    " CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.project_key') END AS project_key,"
    " CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.raise.decided') END AS raise_decided,"
    " CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.raise.reason') END AS raise_reason,"
    " CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.rationale') END AS rationale,"
    " e.detail AS detail, e.detail_truncated AS detail_truncated, e.ingest_seq AS ingest_seq"
    " FROM events e"
    " WHERE e.kind = 'system' AND e.event = 'checkin_decision'"
    f" AND {fleet_alias_range('e.subject_alias')}"
    f" ORDER BY {_epoch('e.occurred_at')} DESC, e.ingest_seq DESC"
)
```

- [ ] **Step 4: The command**

```python
# claudlobby/commands/checkins.py
"""`claudlobby checkins` — the check-in's read door (manager check-in spec §11),
minimal form: the decision rows, newest first. `--summary`, `--limit` and the
outcome join land in chunk 3.

Two connections, on purpose: `brief.plane_session` is THE reachability door for
the package (no db / no fleet / a plane that has never seen the fleet all refuse
with a note -- unreachable is not empty), but its connection yields TUPLES
(plane-readers.py:53-58; status.py:218). The rows are read through
`plane.db.open_ro`, which sets sqlite3.Row -- the `commands/task.py` pattern."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from ..plane.db import open_ro
from ..plane.queries import CHECKIN_ROWS_SQL, fleet_range_params
from ._helpers import _resolve_paths, refuse_unreachable


def _since(text: str) -> datetime:
    """The `--since` grammar the read doors share: 24h, 7d, 30m, or an ISO
    instant (`cmd_report_back` applies the same rule inline, core.py)."""
    raw = (text or "").strip()
    now = datetime.now(timezone.utc)
    try:
        if raw.endswith("h"):
            return now - timedelta(hours=int(raw[:-1]))
        if raw.endswith("d"):
            return now - timedelta(days=int(raw[:-1]))
        if raw.endswith("m"):
            return now - timedelta(minutes=int(raw[:-1]))
        got = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"cannot parse --since '{raw}' (use e.g. 24h, 7d, 30m, or ISO date)") from None


def _bot_of(alias: str | None) -> str:
    return alias.rsplit("/", 1)[-1] if alias else ""


def _row(r) -> dict:
    truncated = bool(r["detail_truncated"])
    return {
        "checkin_id": r["checkin_id"], "prev_checkin_id": r["prev_checkin_id"],
        "bot": _bot_of(r["subject_alias"]), "occurred_at": r["occurred_at"],
        "action": r["action"], "project_key": r["project_key"],
        "raise": {"decided": bool(r["raise_decided"]), "reason": r["raise_reason"]},
        "rationale": r["rationale"], "truncated": truncated,
        "record": None if truncated else json.loads(r["detail"]),
    }


def collect_checkins(conn, fleet: str, *, since: datetime | None, bot: str | None,
                     last: bool) -> list[dict]:
    """Newest first by occurred_at. `since` None means no window (--last)."""
    out: list[dict] = []
    for r in conn.execute(CHECKIN_ROWS_SQL, fleet_range_params(fleet)):
        if bot and _bot_of(r["subject_alias"]) != bot:
            continue
        if since is not None and datetime.fromisoformat(r["occurred_at"]) < since:
            continue
        out.append(_row(r))
        if last:
            break
    return out


def cmd_checkins(args) -> int:
    paths = _resolve_paths(args)
    from ..brief import plane_session, resolve_fleet_name

    fleet = getattr(args, "checkins_fleet", None) or resolve_fleet_name(paths)
    if not fleet:
        print("checkins: no fleet is named (--fleet <name>, or a fleet.yaml naming one)"
              " — the plane's rows are per fleet", file=sys.stderr)
        return 2
    try:
        since = None if args.last else _since(args.since)
    except ValueError as exc:
        print(f"checkins: {exc}", file=sys.stderr)
        return 2
    plane, note = plane_session(paths, fleet)
    if plane is None:
        return refuse_unreachable("checkins", note)
    plane.close()
    conn, reason = open_ro(paths.root)
    if conn is None:
        return refuse_unreachable("checkins", reason or "plane db unreadable")
    try:
        rows = collect_checkins(conn, fleet, since=since, bot=args.bot, last=args.last)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"schema": 1, "fleet": fleet,
                          "since": since.isoformat() if since else None,
                          "checkins": rows}, indent=2))
        return 0
    scope = f"fleet {fleet}" + (f", bot {args.bot}" if args.bot else "") + \
        (" (newest only)" if args.last else f", last {args.since}")
    if not rows:
        print(f"no check-ins — {scope}")
        return 0
    print(f"check-ins — {scope}: {len(rows)}")
    for r in rows:
        if r["truncated"]:
            print(f"  {r['occurred_at']}  {r['bot']}  (record over the size cap — truncated at ingest)")
            continue
        raised = " · raised" if r["raise"]["decided"] else ""
        proj = f" [{r['project_key']}]" if r["project_key"] else ""
        print(f"  {r['occurred_at']}  {r['bot']}  {r['action']}{proj}{raised}  {r['checkin_id']}")
        print(f"      {r['rationale']}")
        print(f"      surfacing: {r['raise']['reason']}")
    return 0
```

Register it in `claudlobby/commands/_parsers.py` directly after the `workstreams` block (ends `:197`); the import is a **standalone** line placed after the `from .core import (…)` block (never inside it — `cmd_checkins` is not in `core`):

```python
from .checkins import cmd_checkins
```

```python
    pck = sub.add_parser("checkins", help="The manager check-in's decisions, newest first (plane read)")
    pck.add_argument("--fleet", dest="checkins_fleet", default=None,
                     help="fleet whose rows to read (default: the fleet.yaml this root names)")
    pck.add_argument("--bot", default=None, help="one manager's rows only")
    pck.add_argument("--since", default="7d", help="window: 24h, 7d, 30m, or an ISO instant (default 7d)")
    pck.add_argument("--last", action="store_true", help="only the newest row, ignoring --since")
    pck.add_argument("--json", action="store_true", help="machine-facing envelope")
    pck.set_defaults(func=cmd_checkins)
```

(`dest="checkins_fleet"`, not `fleet`: a subparser copies its namespace over the parent's — `_parsers.py:223-225`.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkins_cli.py tests/test_main.py -q`
Expected: all pass. The truncation test depends on the 16 KiB DIAGNOSTIC cap truncating at ingest (`ingest.py:286-294`); if `emit_batch` rejects the oversize record instead, that is a contract change on main — stop and re-read `FIELD_POLICY[("system","data")]`.

- [ ] **Step 6: Commit**

```bash
git add claudlobby/plane/queries.py claudlobby/commands/checkins.py claudlobby/commands/_parsers.py tests/test_checkins_cli.py
printf '%s\n' 'feat(cli): claudlobby checkins — the decision rows, newest first by occurred_at' '' 'Reachability through brief.plane_session (unreachable is not empty, rc 3 via' 'refuse_unreachable), rows through plane.db.open_ro (the session yields tuples).' '--last ignores the window so an idle month never reads as a first check-in;' 'truncated records are listed and marked, never dropped. Spec §11.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c3.txt"
git commit -q -F "$TMPDIR/c3.txt" && git log --oneline -1
```

---

### Task 4: The protocol and the skill

**Files:**
- Create: `library/protocols/checkin.md`, `library/skills/checkin/SKILL.md`
- Test: `tests/test_checkin_library.py`

**Interfaces:**
- The protocol is additive, declares no `requires:`, carries **no self-fire clause** (cycle-2 B7: "run it when you reach a natural idle point" fired the check-in with none of chunk 2's throttles). Its preamble carries the precedence sentence (both audiences — the live collisions are Manager-side; cycle-2 R12).
- The skill consumes, by name: `claudlobby checkins --bot $BOT_ID --last --json` and `claudlobby checkins --bot $BOT_ID --since 7d --json` (READ 0), `claudlobby brief --bot $BOT_ID --json`, `claudron lookup --limit 5 <project>`, `PROJECT_TIER_<SLUG>` / `PROJECT_REPOS_<SLUG>`, `gh issue list`, `bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<'EOF'` (a here-doc on the door, never `cat |`), `bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --project … --checkin …`, and for `ask` the Telegram reply tool or `bash "$CLAUDLOBBY_ROOT/lib/tg-post.sh"` (the tmux-injected case has no chat to reply to).
- Grants follow the shipped shapes exactly (Global Constraints): `Bash(claudlobby *)`, `Bash(claudron lookup *)`, `Bash(gh *)`, `Bash(*checkin-record.sh*)`, `Bash(*dispatch-task.sh*)`, `Bash(*tg-post.sh*)`, `mcp__plugin_telegram_telegram__reply`, `Read`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkin_library.py
"""The check-in protocol (spec §9) composes both audiences from one file; the
skill (§6) is coupled to its doors by name, and its grants MATCH the command
lines it writes (permissions-model.md:48-52: a space is a word boundary; a
pipeline is matched per subcommand) and contain no forbidden wildcard
(claudron-integration.md:29; boundary Invariant 5)."""

import fnmatch
import re
import shutil
from pathlib import Path

from claudlobby.composer import compose_claude_md
from claudlobby.config import load_fleet
from claudlobby.loader import parse_frontmatter
from claudlobby.paths import Paths
from tests.conftest import install_real_template

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"
SKILL = LIB / "skills" / "checkin" / "SKILL.md"


def test_the_protocol_declares_no_requires_and_no_self_fire():
    text = (LIB / "protocols" / "checkin.md").read_text()
    fm, body = parse_frontmatter(text)
    assert fm["title"] == "Check-in" and "requires" not in fm     # equipment linking is chunk 5
    assert "natural idle point" not in body                       # the trigger is chunk 2's, with its throttles
    assert "governs where it composes beside" in body.split("## Manager")[0]   # precedence in the PREAMBLE


def test_both_sections_compose_for_a_hand_equipped_manager(fleet_dir):
    install_real_template(fleet_dir)
    shutil.copy(LIB / "protocols" / "checkin.md", fleet_dir / "library" / "protocols" / "checkin.md")
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n", "    lead:\n      protocols: [checkin]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    md = compose_claude_md(fleet.bots["lead"], fleet, Paths(root=fleet_dir, fleet_dir=fleet_dir))
    assert "### Manager" in md and "### Worker" in md
    assert "Silence is the default" in md


def test_the_cadence_rules_are_untouched_in_this_chunk():
    # fork F3: retiring them is chunk 5's, with a grep-derived sweep
    assert "Idle silence is a bug" in (LIB / "protocols" / "proactivity-discipline.md").read_text()
    assert re.search(r"2.3 min", (LIB / "protocols" / "worker-lifecycle.md").read_text())


DOORS = ["claudlobby checkins --bot $BOT_ID --last --json", "claudlobby checkins --bot $BOT_ID --since 7d --json",
         "claudlobby brief --bot $BOT_ID --json", "claudron lookup --limit 5", "gh issue list",
         'bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<\'EOF\'', "lib/dispatch-task.sh", "--checkin", "--project",
         "lib/tg-post.sh", "PROJECT_TIER_", "PROJECT_REPOS_", "PROJECT_MISSION.md"]


def test_the_skill_is_coupled_to_its_doors():
    text = SKILL.read_text()
    for d in DOORS:
        assert d in text, d
    for action in ("dispatch", "ask", "nothing"):
        assert f"**{action}**" in text, action
    assert "RECORD before ACT" in text and "considered" in text and "could not measure" in text
    assert "propose" not in text.split("## Not in this chunk")[0]   # the enum the contract accepts
    assert "cat <<" not in text                                      # a pipeline is matched per subcommand


def test_the_degraded_rule_is_per_input_and_ignores_the_standing_utilization_entry():
    text = SKILL.read_text()
    assert "utilization" in text and "not an input" in text        # brief's degraded[] is never empty (#891)
    assert "plane-known" in text                                     # gh/claudron down ≠ no dispatch


# --- the grants match the command lines the skill itself writes --------------------

FORBIDDEN = ("Bash", "Bash(bash *)", "Bash(cat *)", "Bash(claudron *)", "Bash(sh *)")


def _bash_grant_matches(grant: str, command: str) -> bool:
    """permissions-model.md:48-51 — the pattern inside Bash(...) is a glob over
    the command line; a trailing ' *' requires a space (a word boundary)."""
    assert grant.startswith("Bash(") and grant.endswith(")")
    return fnmatch.fnmatchcase(command, grant[5:-1])


def _skill_command_lines() -> list[str]:
    """Every `bash …`, `claudlobby …`, `claudron …`, `gh …` line inside a fenced block
    or an inline code span of SKILL.md, first pipeline stage only (the skill must
    not use pipelines)."""
    text = SKILL.read_text()
    cmds = re.findall(r"`((?:bash|claudlobby|claudron|gh) [^`\n]+)`", text)
    for block in re.findall(r"```bash\n(.*?)```", text, re.S):
        for line in block.splitlines():
            line = line.strip().split("$(", 1)[-1].rstrip(")")
            if line.startswith(("bash ", "claudlobby ", "claudron ", "gh ")):
                cmds.append(line)
    return cmds


def test_the_skill_grants_cover_its_own_commands_and_nothing_forbidden():
    fm, _ = parse_frontmatter(SKILL.read_text())
    grants = fm["tool_grants"]
    assert not [g for g in grants if g in FORBIDDEN], grants
    assert "mcp__plugin_telegram_telegram__reply" in grants           # ask posts through the reply tool
    bash_grants = [g for g in grants if g.startswith("Bash(")]
    cmds = _skill_command_lines()
    assert cmds, "no command lines found in the skill"
    for c in cmds:
        assert any(_bash_grant_matches(g, c) for g in bash_grants), f"ungranted: {c!r}"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_library.py -q`
Expected: all fail on the missing files except `test_the_cadence_rules_are_untouched_in_this_chunk` (the guard that F3 stays honoured).

- [ ] **Step 3: The protocol**

```markdown
---
title: Check-in
description: The idle-manager check-in — the manager's own re-engagement cycle — and the thin human edge it enforces. Silence at the human edge is the default; a post is the exception the surfacing judgment must justify and record.
---

# Check-in

The fleet is loud on the inside and quiet at the human edge. Bot-to-bot traffic is
the work happening; the operator hears from the manager in clean updates and
requests for direction, and from workers in one thin line each. Every line, on any
carrier, is a recorded communication — the plane recreates every occurrence. Where
this protocol composes beside an older cadence rule (milestones every few minutes,
wait-point beacons, "never go silent"), **this protocol governs where it composes
beside it**; those rules retire estate-wide when the check-in ships as a default.

## Manager

**The check-in is yours, not the operator's.** It is injected into your session —
by the operator, or by the `manager-checkin` job once that is armed — as
`/checkin`. Run it then: read the SSOT, decide one project and one action, record
the decision, then act. The operator never sees a check-in; they see only what the
surfacing judgment decides they should.

**Whether to post at all is the surfacing judgment — the skill's DECIDE step.
Silence is the default; a post is the exception you must justify and record**
(`raise.decided`, `raise.reason`, `raise.held` in the decision). A check-in that
dispatches or chooses `nothing` posts **nothing** — it is in the plane
(`claudlobby checkins`).

**When the judgment does say post, the shape is fixed:**

- **one status line** — what changed, in plain terms; never a restatement;
- **one ask with named options** — the fork only the operator can resolve, or, when
  the backlog holds nothing worth starting, "nothing worth starting here — anything
  you want first?";
- **one pointer** — the plane URL, the PR, the task id.

At most **one post per check-in**, covering the whole portfolio — never one per
project; what does not fit is `held` for the next justified post. Never restate a
project's rigor tier in the message; point to it. **Asks are rate-limited, not
nagged:** two raised in the last week (`claudlobby checkins --bot $BOT_ID --since
7d`) means you proceed on your best tier-gated judgment or wait quietly — never a
third, unless the urgency floor breaks through.

## Worker

**One thin line on start, done and blocked — to Telegram where you are configured
for it**, plane-only where you are not. Detail goes to your manager and the plane
through `$CLAUDLOBBY_ROOT/lib/report-back.sh`, never to the channel. Shape —
`<verb>: <what>`: `start: #123 price feed` · `done: #123 PR #130` · `blocked: #123
needs the API key`.
```

- [ ] **Step 4: The skill**

```markdown
---
name: checkin
description: "The idle-manager check-in: read the SSOT (the plane through checkins and brief, Claudron, the mission with each project's tier and repos, the GitHub backlog), decide ONE project and ONE action, record the decision BEFORE acting, and let the surfacing judgment decide whether the operator hears anything at all. Silence is the default."
argument-hint: "[--dry-run]"
tool_grants:
  - "Bash(claudlobby *)"
  - "Bash(claudron lookup *)"
  - "Bash(gh *)"
  - "Bash(*checkin-record.sh*)"
  - "Bash(*dispatch-task.sh*)"
  - "Bash(*tg-post.sh*)"
  - "mcp__plugin_telegram_telegram__reply"
  - "Read"
---

# Check-in

Your own re-engagement cycle. Nobody is watching it; what they may see is only what
the surfacing judgment (DECIDE, below) lets through. **Every read goes through a
named door and every write through a named door** — never a hand-rolled query,
never a hand-built plane envelope, never a pipeline. That coupling is what makes
your reasoning inspectable (`claudlobby checkins`) and the edges deterministic.

`$BOT_ID`, `$FLEET_NAME`, `$CLAUDLOBBY_ROOT` and the `PROJECT_*` map come from your
`bot.conf`. `SLUG` below is a project key upper-cased with `-` → `_`. If no
`PROJECT_TIER_*` variable exists, this fleet has no `projects.yaml`: `dispatch` is
not available to you (it needs `--project`), and the check-in ends in `ask` or
`nothing` — say so in the rationale.

## Arguments

Parse `$ARGUMENTS`:
- `--dry-run`: do every READ and the DECIDE, print the decision JSON, validate it
  with `checkin-record.sh --dry-run`, record nothing, act on nothing.

## READ — in order, all cheap, all SSOT

A step that fails is **recorded, never guessed around**: add its name to
`inputs_seen.unavailable` and continue. A count you could not measure is `null`
(**could not measure**), never `0`.

0. **The previous check-in, and this week's asks** —
   `claudlobby checkins --bot $BOT_ID --last --json` (its `record.inputs_seen` is
   *the state at the last check-in*; keep its `checkin_id` for `prev_checkin_id`;
   none → `null`) and `claudlobby checkins --bot $BOT_ID --since 7d --json` (count
   the rows with `raise.decided` true: asks already raised this week). rc 3 means
   the plane is unreachable — record `checkins` as unavailable.
1. **The fleet's present** — `claudlobby brief --bot $BOT_ID --json`: `dispatches`
   (`open` / `overdue` / `orphaned` / `dispatched`, each row with `escalated`,
   `nudged`, `last_progress_at`), `workstreams` (`active`, `stalled`),
   `reports.unacked`, `alerts` (last 24h critical), `mission`. Read `degraded[]`
   **for the fields you use**: an entry naming `dispatches`, `workstreams`,
   `reports` or `alerts` (or a dotted child of one) makes that section
   unavailable — treat it as unavailable, never as zero. The standing
   `utilization` entry (#891) is always present and is **not an input** of this
   skill; ignore it.
2. **Knowledge** — `claudron lookup --limit 5 <project>` for each project with open
   work. Count the hits (`knowledge_hits`); read what is relevant.
3. **The goal and each project's rigor** — `PROJECT_MISSION.md` (via
   `mission.charter` / `$FLEET_MISSION_FILE`), and per project from your env:
   `PROJECT_TIER_<SLUG>` (how work CLOSES) and `PROJECT_REPOS_<SLUG>` (its repos).
4. **The external backlog** — `gh issue list --repo <owner/name> --state open
   --limit 50 --json number,title,labels,updatedAt` per repo in `PROJECT_REPOS_*`;
   filter to mission-aligned items; group by project; count `issues_considered`.
5. **The delta** — now versus step 0, per project: tasks opened / completed /
   stalled / cleared, new issues, new messages, held items still pending. **The
   delta is the primary signal** for both the action and the surfacing judgment: an
   unchanged world argues for `nothing` and silence.

**Everything above is read per project.** The check-in is a portfolio decision.

## DECIDE — one project, then exactly one action

**The allocation rule.** Attention is a weighting, never a ranking: every project
with open work or a blocker is checked on every check-in (the floor); *new* effort
— picking backlog issues — goes preferentially to projects with recent attention,
never exclusively (the tilt); no attention + no open work = left alone — a dormant
project is revived only by explicit direction (never stalest-first). Mission
alignment and backlog depth weigh in. **Record the losers:** every candidate you
weighed and passed over goes into `inputs_seen.considered` as one line, `<candidate>
— <why not>` (at most ten) — a decision is judged by what it did not pick.

| action | when | through |
|---|---|---|
| **dispatch** | an open or backlog item fits an idle worker; you choose the worker and the rationale says why | `bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --project <key> [--repo <owner/name>] [--ref <issue-url>] --checkin <the ck_ id the record door printed> <worker> "<task>"` — `--project` is the well-defined bar (a projects.yaml key); `--checkin` joins the dispatch to this decision |
| **ask** | the surfacing judgment (below) concludes the operator should hear something — a fork only they can resolve, or the backlog holds nothing worth starting ("ask for tasks") | one Telegram post shaped by the check-in protocol (one line, one ask with named options, one pointer): the reply tool when this check-in arrived on Telegram; `bash "$CLAUDLOBBY_ROOT/lib/tg-post.sh" "<the post>"` when it was injected into your pane (there is no chat to reply to). Either way it is recorded as your communication |
| **nothing** | all work in flight, nothing worthwhile — **recorded**, so "checked and chose nothing" is a fact, not silence | — |

**The surfacing judgment.** Its default answer is **no**. Weigh, at minimum: does
this genuinely need a human (a `requires-approval` boundary in the mission, a tier
that mandates sign-off, conflicting priorities)? · would the operator want to know (a
deliverable ready, a blocker that stalls the fleet, a failure with cost)? · what
changed since they were last told (the delta against step 0 and the last post's
`held`)? · has enough accumulated to be worth one message? · have two asks already
been raised this week (step 0 — then proceed on your best tier-gated judgment or
wait quietly, never a third)? · what Claudron says about how the operator wants to
be engaged · the urgency floor (a `blocked` that stalls everything breaks through
regardless). Record the judgment in `raise`: `decided`, `reason` (always, in both
directions), and what you `held`.

**Degraded inputs, per input.** The plane unreachable (`checkins` rc 3, or
`dispatches`/`workstreams`/`reports`/`alerts` degraded in `brief`) narrows the
actions to `ask | nothing` — never dispatch blind. `gh` or `claudron` unavailable
narrows only the **source** of new work: you may still dispatch open, **plane-known**
tasks; you may not pick fresh backlog issues you could not read. Record what was
unavailable either way.

## RECORD before ACT

Build the decision as JSON (schema 1) and record it FIRST — the decision exists even
if the action then fails. The door is invoked directly with a here-doc (never
through `cat |`):

```bash
bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<'EOF'
{"prev_checkin_id": <from step 0, or null>,
 "inputs_seen": {"open_tasks": N, "stalls": N, "unacked": N, "issues_considered": N,
                 "knowledge_hits": N, "considered": ["<candidate> — <why not>"], "unavailable": []},
 "delta": {"tasks_opened": N|null, "tasks_completed": N|null, "stalls_appeared": N|null,
           "stalls_cleared": N|null, "issues_new": N|null, "messages_new": N|null, "held_pending": N|null},
 "action": "dispatch|ask|nothing",
 "project_key": "<slug or null>",
 "rationale": "<your words, <= 600 chars: the weighing, the worker, the why>",
 "raise": {"decided": false, "reason": "<why it surfaced, or why not, <= 600>", "held": []}}
EOF
```

The door prints the `checkin_id` on its last line — **read it from the output and
pass it literally** to `--checkin` (a shell variable does not survive between your
tool calls). rc 2: the decision was refused — every reason is on stderr; fix and
re-record. rc 3: the plane did not record it — do **not** act on an unrecorded
`dispatch`; say so in your next justified post. Then ACT through the door in the
table.

## Not in this chunk

`propose` (generating new work into an intake store, gated by a project's
`planning.initiative`) and `sprint` (batch execution) arrive with later chunks and
widen the `action` enum then. Until they do, "the backlog holds nothing worth
starting" is an `ask`, not an invention.

## Rules

- One project, one action, one record per check-in. Never two actions.
- Never freelance a read (no ad-hoc SQL, no reading state files) or a write (no
  hand-built plane envelopes): the doors above are the whole contract.
- Never restate a project's tier in a post; point to it.
- `--dry-run` never records and never acts.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_library.py tests/test_skill_ref_resolution.py tests/test_dispatch_type.py tests/test_boundary_invariants.py -q`
Expected: all pass. Then the grant shapes on a real validate, rc read first, never piped:

```bash
./.venv/bin/claudlobby --root "$(pwd)" validate > "$TMPDIR/validate.out" 2>&1; echo "rc=$?"
grep -i 'checkin' "$TMPDIR/validate.out" || echo "no checkin findings"
```
Expected: the same rc `main` gives on this root (compare) and `no checkin findings`.

- [ ] **Step 6: Commit**

```bash
git add library/protocols/checkin.md library/skills/checkin/SKILL.md tests/test_checkin_library.py
printf '%s\n' 'feat(library): the check-in protocol (additive) and the /checkin skill' '' 'One file, two sections (Manager: the surfacing judgment; Worker: one thin line),' 'precedence in the preamble, no self-fire clause (the trigger is chunk 2 with its' 'throttles). The skill is a thin reasoning wrapper coupled to its doors by name:' 'actions dispatch | ask | nothing, the losers recorded, a per-input degraded' 'rule, grants in the shipped shapes and matched against its own command lines' 'by test. Spec §6, §9.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c4.txt"
git commit -q -F "$TMPDIR/c4.txt" && git log --oneline -1
```

---

### Task 5: The empirical gate — the record door and the read door on a real plane through the harness

**Files:**
- Modify: `lib/validate-bot-change.sh` (append a scenario block before the harness summary; helpers `val_plane_ready`, `val_sql` at `:163,171`, `harness_check` at `lib-common.sh:4634`, `$VAL_CLI`, `$VAL_REPO`, `$LIB_DIR`, `$ROOT`; the harness already exports a SHORT `PLANE_SOCKET` at `:135`)

**Interfaces:** consumes Tasks 1 and 3. It proves the two doors under the identity env a manager session carries and through the installed CLI, with the **positive control gated on a non-empty id** (cycle-2 B10: `grep -c ""` counts every line).

- [ ] **Step 1: Append the block**

```bash
# ===========================================================================
# manager check-in chunk 1 -- the record door and the read door, end to end
# on a real plane. Unit tests pin the contract and the envelopes; what only
# running the real doors proves is that a decision LANDS as a row the read
# door can join, through the real shim, under the identity env a manager
# session carries (BOT_ID, FLEET_NAME), and that "not listed" is a real
# negative -- the positive control runs FIRST and is gated on a non-empty id,
# so a failed record or an unreachable read door can never read as a clean
# answer (an empty grep pattern matches every line).
# ===========================================================================
echo ""
echo "=== validate manager check-in: the decision lands and the read door joins it ==="
CK_FLEET="valckf"
val_plane_ready "$ROOT" "$CK_FLEET"
ck_decision='{"inputs_seen":{"open_tasks":0,"considered":[],"unavailable":[]},"delta":{},"action":"nothing","project_key":null,"rationale":"harness: nothing worth starting","raise":{"decided":false,"reason":"no delta","held":[]}}'
ck_id=$(printf '%s' "$ck_decision" | env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET" BOT_ID="valckmgr" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$PLANE_SOCKET" \
    bash "$VAL_REPO/lib/checkin-record.sh" 2> "$ROOT/ck-record.err" || true)
case "$ck_id" in ck_??????????????????????????????????) r=yes ;; *) r=no ;; esac
harness_check "checkin: the record door returns a ck_<32hex> id" "$r"
ck_row=$(val_sql "$ROOT" "SELECT json_extract(detail,'\$.action') || '|' || severity || '|' || subject_alias FROM events WHERE kind='system' AND event='checkin_decision' AND source_ref='checkin:$ck_id'")
[ -n "$ck_id" ] && [ "$ck_row" = "nothing|notice|bot:$CK_FLEET/valckmgr" ] && r=yes || r=no
harness_check "checkin: ...and the decision LANDED as one actor-anchored notice row (source_ref checkin:<id>, BOT_ID alias)" "$r"
ck_other=$(printf '%s' "$ck_decision" | env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET" BOT_ID="valckother" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$PLANE_SOCKET" \
    bash "$VAL_REPO/lib/checkin-record.sh" 2>> "$ROOT/ck-record.err" || true)
# The CLI reaches the plane through <root>/lib/dispatch-overdue.py -- linked for
# THIS scenario and removed after it (the #1481 neighbour rule at :652/:682).
ln -sfn "$LIB_DIR" "$ROOT/lib"
CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$CK_FLEET" --json \
    > "$ROOT/ck-read.out" 2> "$ROOT/ck-read.err" || true
{ [ -n "$ck_id" ] && grep -q "$ck_id" "$ROOT/ck-read.out"; } && r=yes || r=no
harness_check "checkin: the read door LISTS the decision (positive control, gated on a non-empty id)" "$r"
CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$CK_FLEET" --bot valckmgr --last --json \
    > "$ROOT/ck-read2.out" 2> "$ROOT/ck-read2.err" || true
rm -f "$ROOT/lib"
{ [ -n "$ck_id" ] && [ -n "$ck_other" ] && grep -q "$ck_id" "$ROOT/ck-read2.out" && ! grep -q "$ck_other" "$ROOT/ck-read2.out"; } && r=yes || r=no
harness_check "checkin: ...--bot --last returns THIS manager's newest row and not the other manager's (a real negative)" "$r"
```

- [ ] **Step 2: Run the harness unsandboxed and read the four lines**

Run: `bash lib/validate-bot-change.sh > "$TMPDIR/vbc.txt" 2>&1; echo "rc=$?"; grep -E 'checkin:' "$TMPDIR/vbc.txt"`
Expected: four `PASS` lines beginning `checkin:`; the harness's overall verdict unchanged from main's (run main's harness once for the baseline if unsure — a pre-existing failure in another block is out of scope and is named, not fixed). The two `critical` `script_error` rows a bare-root dispatch emits are pre-existing on main (measured against unpatched `dispatch-task.sh`) — not a chunk-1 regression. **Paste the four lines into the PR body** (Task 7).

- [ ] **Step 3: Commit**

```bash
git add lib/validate-bot-change.sh
printf '%s\n' 'test(harness): the check-in record lands and the read door joins it' '' 'validate-bot-change.sh gains the chunk-1 scenario: record -> row under a manager' 'identity env; read door lists it (positive control first, gated on a non-empty' 'id); --bot --last excludes the other manager (a real negative). Uses the' 'harness short socket path; $ROOT/lib linked around the CLI calls like #1481.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c5.txt"
git commit -q -F "$TMPDIR/c5.txt" && git log --oneline -1
```

---

### Task 6: `README.md`, `CLAUDE.md`, `CHANGELOG.md`, the observability guide

**Files:** `README.md:145-146`; `CLAUDE.md` (the `lib/` table; the `commands/` line; `## Key Commands` → `# Operations`); `CHANGELOG.md` (`[Unreleased]`); `documentation/guides/observability.md` (the question→door table).

- [ ] **Step 1: README counts** — `tests/test_readme_library_counts.py` pins them: `53 skills` → `54 skills`; `39 protocols` → `40 protocols`; `91 bash lifecycle scripts` → `93 bash lifecycle scripts` (measure with `ls -d library/skills/*/ | wc -l`, `ls library/protocols/*.md | grep -vc README`, and the test's own lib rule before editing — paste the numbers, never recall them). Run: `./.venv/bin/pytest tests/test_readme_library_counts.py -q` → pass.

- [ ] **Step 2: The lib rows** — append to the `lib/` table in `CLAUDE.md`, in the house style:

```markdown
| `checkin-record.sh` | THE write door for a manager check-in decision (manager check-in spec §7). Mints the `ck_` id through `plane_mint_id` (the one mint), validates the schema-1 record through `checkin-contract.py`, lands ONE actor-anchored `checkin_decision` system event stamped `source_ref checkin:<id>` — the task-recheck stamp idiom, so a reader joins on the ref. **For this door the record IS the action**: rc 3 when the plane did not record or is silenced (never a silent 0), rc 2 when the contract refuses (every reason named), rc 1 for usage including a missing identity — the `task-act.sh` ladder. Identity is `BOT_ID` + `FLEET_NAME` from the environment, never `BOT_NAME` (a display field) and never a flag (nothing supplies one) |
| `checkin-contract.py` | The schema-1 decision record, stdlib (the `dispatch-overdue.py` precedent): `normalize()` lists EVERY defect rather than the first; keeps a `delta` count `null` when the manager could not measure it (never collapsed to 0 — an unchanged delta is the skill's argument for silence); requires `raise.reason` in both directions; carries the losers in `inputs_seen.considered`, because a selector is judged by what it did NOT pick (`sprint-selection-record.py`, #974). Actions this chunk: `dispatch \| ask \| nothing`; `propose`/`sprint` widen the enum as schema 2. Also the CLI filter the door pipes through |
```

and one sentence each in the existing `dispatch-task.sh` and `tg-post.sh` rows: ``Since the check-in chunk: `--project <key>` stamps `project_key` on the work item and opens the envelope; `--checkin ck_<32hex>` appends a `checkin_dispatch` join row to the SAME batch as the assignment (a strict `Assignment` payload cannot carry the id), disclosed and ignored on an untracked dispatch.`` / ``Its sender alias is `bot:$FLEET_NAME/$BOT_ID`, the rule every door shares (it was `BOT_NAME`, which silently broke any alias join for a renamed bot).``

- [ ] **Step 3: The command and the guide** — in the package structure's `commands/` line add `checkins`; under `## Key Commands` → `# Operations`:

```bash
claudlobby checkins [--bot B] [--since 7d] [--last] [--json]   # the manager check-in's decisions, newest first (plane read)
```

In `documentation/guides/observability.md`'s question→door table add: `| What did a manager decide at its last check-in, and why? | claudlobby checkins --bot <b> --last |`.

- [ ] **Step 4: CHANGELOG** — under `[Unreleased]`, one bullet per landed piece (the record door + contract; `dispatch-task --project/--checkin`; the `tg-post.sh` alias; `checkins`; the protocol + skill).

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md CHANGELOG.md documentation/guides/observability.md
printf '%s\n' 'docs: README counts, CLAUDE.md rows, CHANGELOG and the observability guide for the check-in record and run (chunk 1)' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c6.txt"
git commit -q -F "$TMPDIR/c6.txt" && git log --oneline -1
```

---

### Task 7: The gauntlet, the merge, the deploy, the real run

The operator's standing loop for every chunk. Nothing merges without all of it, and the PR body cites each observation — claimed evidence is not evidence. The real-boot gate for the skill and protocol (the CLAUDE.md-mandated runtime gate) runs **after merge and pull, on main's code, on the canary manager** — no unmerged code ever runs on the live host (cycle-2 R4), and a skill-text finding there is a follow-up PR against `library/` (live on the next `generate`, no restart), which is why the ordering no longer invalidates the gates below (cycle-2 R3).

- [ ] **Step 1: Review lenses** — `/simplify`, then `/review-work`, then `/verify-completion` on the branch (the phase-finalization gate). Fold every finding as its own commit; re-run the touched test files.

- [ ] **Step 2: Committed-code mutants, in a detached worktree** — never against uncommitted code. For each: apply, run the named tests, expect ≥ 1 failure, restore with `git checkout -- <file>`. Every anchor must occur exactly once (`assert text.count(old) == 1`); a surviving mutant is a missing test — add the test, never a weaker mutant. Bash-door tests unsandboxed.

```python
# $TMPDIR/mut-ck1-defs.py — (name, file, old, new, [killing test files])
MUTANTS = [
    ("rationale-cap-off", "lib/checkin-contract.py",
     "return isinstance(v, str) and v.strip() != \"\" and len(v) <= TEXT_MAX", "return isinstance(v, str) and v.strip() != \"\"",
     ["tests/test_checkin_contract.py"]),
    ("null-delta-collapsed", "lib/checkin-contract.py",
     "v = delta.get(k)\n        if v is not None and not _count(v):", "v = delta.get(k) or 0\n        if not _count(v):",
     ["tests/test_checkin_contract.py"]),
    ("considered-uncapped", "lib/checkin-contract.py",
     "return (isinstance(v, list) and len(v) <= LIST_MAX", "return (isinstance(v, list)",
     ["tests/test_checkin_contract.py"]),
    ("contract-mints", "lib/checkin-contract.py",
     "if not cid:\n        bad.append(\"checkin_id required (the door mints it: plane_mint_id ck)\")",
     "if not cid:\n        cid = \"ck_\" + \"f\" * 32",
     ["tests/test_checkin_contract.py"]),
    ("record-swallows-failure", "lib/checkin-record.sh",
     "if [ \"${PLANE_EMIT_LAST_RC:-0}\" -ne 0 ]; then", "if false; then", ["tests/test_checkin_doors.py"]),
    ("record-uses-bot-name", "lib/checkin-record.sh",
     "BOT=\"${BOT_ID:-}\"", "BOT=\"${BOT_NAME:-${BOT_ID:-}}\"", ["tests/test_checkin_doors.py"]),
    ("project-never-opens-gate", "lib/dispatch-task.sh",
     "|| [ -n \"$DISPATCH_WORKSTREAM\" ] || [ -n \"$DISPATCH_PROJECT\" ]; then", "|| [ -n \"$DISPATCH_WORKSTREAM\" ]; then",
     ["tests/test_checkin_doors.py"]),
    ("join-row-dropped", "lib/dispatch-task.sh",
     "\"${sup_ev:+,$sup_ev}\" \"${ck_ev:+,$ck_ev}\"", "\"${sup_ev:+,$sup_ev}\" \"\"", ["tests/test_checkin_doors.py"]),
    ("join-dropped-silently", "lib/dispatch-task.sh",
     "echo \"dispatch-task: --checkin ignored: a $DISPATCH_TYPE dispatch mints no assignment to join\" >&2", ":",
     ["tests/test_checkin_doors.py"]),
    ("tg-post-bot-name", "lib/tg-post.sh",
     "--arg sender \"bot:$FLEET_NAME/$BOT_ID\"", "--arg sender \"bot:$FLEET_NAME/$BOT_NAME\"", ["tests/test_checkin_doors.py"]),
    ("rows-by-ingest-order", "claudlobby/plane/queries.py",
     "ORDER BY {_epoch('e.occurred_at')} DESC, e.ingest_seq DESC", "ORDER BY e.ingest_seq DESC", ["tests/test_checkins_cli.py"]),
    ("last-bounded-by-since", "claudlobby/commands/checkins.py",
     "since = None if args.last else _since(args.since)", "since = _since(args.since)", ["tests/test_checkins_cli.py"]),
    ("truncated-rows-dropped", "claudlobby/plane/queries.py",
     "\" WHERE e.kind = 'system' AND e.event = 'checkin_decision'\"", "\" WHERE e.kind = 'system' AND e.event = 'checkin_decision' AND e.detail_truncated = 0\"",
     ["tests/test_checkins_cli.py"]),
    ("claudron-wildcard-sneaks-in", "library/skills/checkin/SKILL.md",
     "  - \"Bash(claudron lookup *)\"\n", "  - \"Bash(claudron *)\"\n", ["tests/test_checkin_library.py"]),
    ("record-grant-needs-a-space", "library/skills/checkin/SKILL.md",
     "  - \"Bash(*checkin-record.sh*)\"\n", "  - \"Bash(*checkin-record.sh *)\"\n", ["tests/test_checkin_library.py"]),
    ("degraded-any-field", "library/skills/checkin/SKILL.md",
     "is always present and is **not an input** of this\n   skill; ignore it.", "is an input like any other.",
     ["tests/test_checkin_library.py"]),
]
```

- [ ] **Step 3: The two-leg full-suite gate** — `before.txt` is Task 0's; the after leg runs on the FINAL committed tip, unsandboxed:

```bash
./.venv/bin/pytest --tb=no -ra > "$TMPDIR/run_after.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$TMPDIR/run_after.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$TMPDIR/after.txt"
comm -13 "$TMPDIR/before.txt" "$TMPDIR/after.txt"     # failures YOU introduced — must be empty
tail -1 "$TMPDIR/run_before.txt"; tail -1 "$TMPDIR/run_after.txt"
./.venv/bin/pytest --collect-only -q tests/test_checkin_contract.py tests/test_checkin_doors.py tests/test_checkins_cli.py tests/test_checkin_library.py | tail -1   # the expected passed-count delta, measured
```
Both rc must be 1 (the red baseline). Known load flakes on this host: `test_boot_capture.sh:203 (dur=1)` and the `test_github_app_wrapper` refresh loop — re-run a flake alone before calling it a regression. The after leg's `passed` must equal before's plus the collected count printed by the last line.

- [ ] **Step 4: Push, open the PR, CI on Linux**

```bash
git push -u origin checkin/chunk1-record
gh pr create --title "feat(checkin): chunk 1 — the record and the run (contract, record door, dispatch join, read door, skill + protocol)" --body-file "$TMPDIR/pr-body.md"
```
The PR body carries, in order: what landed (one line per task); **the empirical observations** — the four `checkin:` harness lines from Task 5 and the names of the two real-plane tests (`test_the_decision_lands_on_a_real_plane`, `test_dispatch_checkin_appends_the_join_row_to_the_same_batch`); the two-leg gate result (`comm -13` empty; before/after count lines pasted); the mutant table (16 names, each with the test that killed it); the two baselines from Task 0; **the canary-rollout posture** ("`lib/dispatch-task.sh` and `lib/tg-post.sh` reach every fleet on pull; the flags are additive and behaviour without them is byte-identical, pinned by `tests/test_task_id_dispatch.py` + `tests/test_dispatch_task.sh`; the real run on the canary manager is the positive control"); the spec link and the five locked forks. End with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Wait for CI green; a `test_boot_capture.sh` load flake is re-run, not waved through.

- [ ] **Step 5: Admin-merge with the explicit squash body** (the operator's standing authorization for gauntleted work) — `gh pr merge --squash --admin --body-file "$TMPDIR/pr-body.md"`; delete the branch.

- [ ] **Step 6: Deploy to the Mini: pull, then validate every fleet**

Chunk 1 composes nothing differently for any bot that does not declare `checkin`; `lib/` and the `checkins` subcommand are live on pull. Fleets are enumerated the way `lib/setup-fleets:18` does (flat or nested); nothing is piped.

```bash
ssh -o BatchMode=yes mini 'bash -s' <<'EOF'
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd ~/Projects/claudlobby
git status --porcelain | grep -q . && { echo "dirty checkout — stop"; exit 1; }
git fetch -q origin main && git pull --ff-only
for fy in local/*/fleet.yaml local/*/*/fleet.yaml; do
  [ -f "$fy" ] || continue
  fleet=$(basename "$(dirname "$fy")")
  .venv/bin/claudlobby --fleet "$fleet" validate > "/tmp/ck-validate-$fleet.out" 2>&1; echo "validate $fleet rc=$?"
  grep -i 'checkin' "/tmp/ck-validate-$fleet.out" || echo "  no checkin findings"
  .venv/bin/claudlobby --fleet "$fleet" diff > "/tmp/ck-diff-$fleet.out" 2>&1; echo "diff $fleet rc=$? lines=$(wc -l < "/tmp/ck-diff-$fleet.out")"
done
.venv/bin/claudlobby checkins --fleet "<canary-fleet>"; echo "checkins rc=$?"
EOF
```
Expected: `validate` rc unchanged from before the pull on every fleet, no `checkin` findings, `diff` empty on every fleet (nothing composes differently yet), and `claudlobby checkins` printing `no check-ins — fleet …` at rc 0 (rc 3 means the install's `lib/` predates the door — pull again).

- [ ] **Step 7: Operator action — equip the canary manager** (operator config is never edited by the executor)

The operator adds two lines to the canary leaf manager's entry in `local/<system>/<canary-fleet>/fleet.yaml` — `protocols: [checkin]` and `skills: [checkin]` (appended to any existing lists; `projects.yaml` must exist for that fleet, or `dispatch` is unavailable to the skill by its own rule) — and runs `claudlobby --fleet <canary-fleet> generate --bot <manager>`. The skill symlink is live instantly; the protocol lands at the manager's next session start.

- [ ] **Step 8: The dry run — the mandatory runtime gate for the skill text**

Inject from the host, through the socket-aware helper (the slash payload reaches the pane bare — `dispatch.sh:26-42`): `"$CLAUDLOBBY_ROOT/lib/dispatch.sh" <manager> "/checkin --dry-run"`. Capture from the pane (`tmux -L <socket> capture-pane -p -S -200`) and the manager's transcript: (a) the decision JSON the model emitted, (b) `checkin-record.sh --dry-run`'s rc and printed `ck_` id, (c) which of READ 0–5 answered and which landed in `unavailable`. A refused decision (rc 2) or a door the model could not drive is a finding about the skill text: a follow-up PR against `library/`, live on the next `generate`, before step 9.

- [ ] **Step 9: The real run — the deploy's positive control** (cycle-2 B7)

`"$CLAUDLOBBY_ROOT/lib/dispatch.sh" <manager> "/checkin"`. Then, on the host: `claudlobby checkins --fleet <canary-fleet> --bot <manager> --last --json > /tmp/ck-first.json; echo "rc=$?"`. Expected: rc 0 and one row whose `record.action` is one of `dispatch | ask | nothing` with a non-empty `rationale` and `raise.reason`. If the action was `dispatch`, also `sqlite3 state/plane/plane.db "SELECT COUNT(*) FROM events WHERE event='checkin_dispatch'"` → 1. **Paste the row (identifiers faked if the PR is public-facing) and the dry-run's three captures as a PR comment.** That comment closes the chunk; the manager's next check-in waits for chunk 2's trigger.

---

## Self-review (run against the spec and the cycle-2 review after writing; findings folded above)

**Cycle-2 blockers → where each is resolved.** B1 (join block before `emit_triple`) → Task 2 step 3(f), placed after the gate beside `link_frag`; a test asserts the send happened. B2 (flag-guard test unreachable; mutant unkilled) → the record door has no value-taking flags (the `--bot`/`--fleet` cut), and the dispatch test puts `--checkin` first and alone through `_fake_lib` (the real lib-common); the `flag-guard` mutant is gone with its subject. B3 (kwarg collision) → `_record(ck, **over)` takes the id positionally. B4 (grant shape) → `Bash(*checkin-record.sh*)` etc.; the RECORD block is a here-doc on the door; the library test matches every command line the skill writes against its grants with the space-boundary rule. B5 (`Bash(claudron *)`) → `Bash(claudron lookup *)`; a forbidden list and a mutant. B6 (degraded rule) → per-input scoping; the standing `utilization` entry named as not-an-input; a test and a mutant. B7 (no real run; self-fire) → Task 7 steps 8–9 after merge; the self-fire clause cut and pinned absent. B8 (winner-only record) → `inputs_seen.considered`, capped, in schema 1; a contract test and a mutant. B9 (README pins) → Task 6 step 1. B10 (empty-id control) → `[ -n "$ck_id" ]` gates both read-door checks.

**Cycle-2 risks.** R1 → Task 0 step 4 rewritten (fleet-scoped, outbound, `strftime`, plain open, named fleet, PATH). R2 → the reply tool granted; `tg-post.sh` named for the injected case; its alias fixed in Task 2. R3 → the real-boot gate is post-merge, so nothing invalidates the gates; spec §12 re-sequenced. R4 → merge → pull → equip → run; the posture stated in the PR body. R5 → the disclosure line + test + mutant. R6 → the field-table row and the recipe line in Task 2. R7 → READ 0 reads `--since 7d`; the protocol's rule is a rate cap. R8 → `raise.reason` ≤ 600. R9 → spec edits in the same commit (rc 3; §13; frontmatter). R10 → "read it from the output and pass it literally". R11 → `UNREACHABLE`. R12 → the precedence sentence in the preamble; chunk 5's grep gains `never go silent`.

**Cycle-2 gaps (the YAGNI cuts).** `parse_since` → inlined as `_since` (no new helper); `--limit` → gone; `--bot`/`--fleet` flags and the `flag_val` lift → gone; the dispatch tests → Task 2's own step 1; `mint_checkin_id` → the door mints via `plane_mint_id`; `plane_root` fixture used; the deploy enumerates fleets like `setup-fleets`, pipes nothing; the harness uses the exported short socket path; `checkin_dispatch`'s reader is chunk 3 (said in Task 2's Interfaces); `## Risks` and sizing added; `observability.md` row added; `test_dispatch_task.sh` in the exists-on-main list.

**Placeholder scan.** No TBD/TODO; every code step carries code. Two verification instructions name a file and a pattern rather than an edit: Task 2 step 4's `grep -n 'workstream:' worker-lifecycle.md` (mirror the row only if the field list is there) and Task 6 step 1's count measurement (paste, never recall).

**Type consistency.** `normalize(obj, *, checkin_id=None)` matches the door's `--checkin-id` and every contract test. `checkin-record.sh` rc ladder 0/1/2/3 is identical in the header, the tests, the CLAUDE.md row and the skill. `CHECKIN_ROWS_SQL` binds `(fleet, fleet)` — matched in `collect_checkins`. `_Args.checkins_fleet` matches `dest="checkins_fleet"`; no `limit` anywhere. `STUB_CK` matches the stub's `plane_mint_id`. The skill's door strings match `DOORS` verbatim, and the grant test's glob rule is the documented one.
