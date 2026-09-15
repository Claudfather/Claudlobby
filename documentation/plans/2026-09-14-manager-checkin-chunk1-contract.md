---
title: "Manager Check-in — Chunk 1: The Record and the Run — Implementation Plan"
type: plan
status: draft
owner: fleet owner
created: 2026-09-14
---

# Manager Check-in — Chunk 1: The Record and the Run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Reforge cycle 2 (2026-09-15).** Cycle 1 of `/ironclad` returned 12 blockers and 18 risks against the first draft (PR #1550, review comment; verification log `scratch/ironclad-2026-09-14_192543/verified.md`). This rewrite folds every one under an adversarial, YAGNI mandate from the operator: **build only what one hand-equipped manager needs to run `/checkin` for real and leave a joinable row.** Everything else moves to the chunk that consumes it (§Decision Forks, §Deferred). The cycle-1 body is preserved at `scratch/ironclad-2026-09-14_192543/snapshot-cycle1/plan.md` and in git at `8eb3142`.

**Goal:** One leaf manager, equipped by hand (`protocols: [checkin]`, `skills: [checkin]` in its `fleet.yaml`), runs `/checkin` end to end: reads the SSOT through named doors, decides `dispatch | ask | nothing`, records the decision as one plane row BEFORE acting, and any dispatch it makes is joined to that row. `claudlobby checkins` shows the row.

**Architecture:** Two bash doors and one CLI read door over the plane, plus a skill and a protocol. The decision is one `checkin_decision` system event (no migration; one severity line). The dispatch join is a second system event, `checkin_dispatch`, that `dispatch-task.sh --checkin` appends to the dispatch batch — the `--supersedes` precedent, atomic with the assignment. Nothing composes differently on the estate: no template change, no `bot.conf` change, no protocol edits beyond one additive new file, no defaults, no trigger. The canary manager is declared by the operator, by hand.

**Tech Stack:** Python 3.11 (stdlib + the package's existing pydantic), bash 3.2-compatible `lib/` scripts sourcing `lib-common.sh`, SQLite plane (schema 11), pytest.

**Spec:** `documentation/plans/2026-09-13-manager-checkin-design.md` — this plan implements §12 chunk 1 as re-sequenced in this cycle (§12 in the spec was corrected in the same commit). Executors read both.

## Scope

**In chunk 1 (this plan):** the schema-1 decision contract; `lib/checkin-record.sh`; `dispatch-task.sh --project` and `--checkin`; the two severity lines; `claudlobby checkins` (rows, `--last`, `--bot`, `--since`, `--json`); `library/protocols/checkin.md` (additive) and `library/skills/checkin/SKILL.md`; the harness block; the gauntlet, including one real `/checkin --dry-run` on the canary manager.

**Deferred, each to the chunk that consumes it** (cycle-1 findings R16, cost-benefit, ceo, align-to-mission; see §Decision Forks):

| Deferred | To | Why not now |
|---|---|---|
| `planning.initiative` (config, known values, validator, composition, template column, docs) | **1d — intake** | gates origination only; nothing originates until `propose` ships |
| `lib/checkin-propose.sh`, `dispatch-task.sh --work-item`, `PROPOSALS_SQL`, `checkins --proposals`, the `propose` action, `CHECKIN_MAX_PROPOSALS` | **1d — intake**, gated on canary evidence that dispatching the existing backlog does not fill idle time (fork F1) | the empty-backlog branch of a fleet whose framework repos carry thousands of open issues |
| `requires:` frontmatter, `claudlobby/requires.py`, the compositor link **with the grant union** (cycle-1 B8), `list-library` annotation | **5 — default** | its only consumer is the registry line; the canary declares protocol and skill by hand |
| `leaf-manager` role, `FleetConfig.leaf_manager_bots()`, `defaults.roles_for`, the `requires.role` warning | **5 — default** | its only consumer is the registry line; the trigger (chunk 2) gates on the composed skill symlink, which by-hand declaration already scopes |
| the five cadence-retirement edits (`worker-lifecycle`, `proactivity-discipline`, `continuous-autonomous-mode`, `token-efficiency`, `inbound-acknowledgment`) **plus the residue the cycle-1 grep found** (`telegram-routing.md:29`, `worker-lifecycle.md:87,198`, `token-efficiency.md:37`, `autonomous-runner/SKILL.md:113`) | **fork F3** — lean: chunk 5, with the canary fleet carrying overlay overrides during chunk 4 | an estate-wide behaviour change whose replacement does not run until the trigger and is not trusted until the canary |
| an `ask` door and `targets.msg_id` | **3 — read door + outcome join** (`lib/checkin-ask.sh` if the join needs it) | nothing consumes the ask→message join before chunk 3; asks join by manager alias + time window until then (fork F4) |
| `sprint` action | **1c** (unchanged) | |
| `focus_declared` / `focus_empirical_top` fields | **1b** (schema 2) | the skill does not read focus in this chunk, so the record does not pretend to |

## Decision Forks

- **F1 — Defer `propose` and the intake store past the canary.** *Context:* the spec's `propose` is gated to "no well-defined work exists on a project"; the canary fleet's repos carry a deep backlog, so `dispatch` is the dominant path and `propose` the empty-backlog branch. Building it first is building the branch the canary is least likely to exercise. *Options:* (a) defer to chunk 1d, gated on the canary showing `nothing`-with-empty-backlog rows; (b) build in chunk 1 as the cycle-1 draft did. *Lean:* (a) — it is the operator's own rule ("prove they are needed, then build them robustly"), and it removes cycle-1 B9, B11-propose, R11, R12 and half of R16 at zero cost. *Ratifier:* operator. **Status: locked** (2026-09-15) — evidence: operator: "okay sure. some fleets on my personal projects would have less issues on their given domains. but yes these work systems we're testing on have thousands." So: the canary measures dispatch on a deep backlog; chunk 1d ships `propose` for the thin-backlog fleets, which are where it earns its keep — and the canary's `ask`-for-tasks rows on an empty project are the evidence that gates it.
- **F2 — Defer `requires:` linking and the `leaf-manager` role to chunk 5.** *Context:* both exist to make the check-in a default; the canary declares by hand. Cycle-1 B8 (linked but never granted) and R2 (the cross-fleet direction) are fixed in the spec now so chunk 5 builds them right. *Options:* defer / build now. *Lean:* defer. *Ratifier:* operator. **Status: locked** — evidence: operator instruction 2026-09-15 ("NOT overbuilding, YAGNI"); no consumer before chunk 5; cost-benefit and ceo lenses both recommended the move.
- **F3 — Where the cadence-retirement edits land.** *Context:* library protocol bodies compose estate-wide; the only per-fleet carrier is a `local/<fleet>/library/protocols/` override. *Options:* (a) chunk 5 estate-wide, once the check-in ships as a default; during the chunk-4 burn-in the canary fleet declares `checkin` in its `defaults.protocols` so every bot in that fleet composes the new protocol, whose Worker section states that it governs where it composes beside an older cadence rule — no library edit, no overlay override; (b) chunk 2 estate-wide when the trigger lands (before any fleet has validated the replacement); (c) chunk 4 estate-wide when the canary arms (still before the burn-in has judged it). *Lean:* (a) — the old signal is removed for every fleet only after the replacement has been trusted on one, and the canary fleet gets the thin edge for free through the protocol's own precedence sentence. Whichever lands, the sweep set is grep-derived (`milestone|beacon|2.3 min|10.15 min|Idle silence` over `library/`) and the test asserts over the grep. *Ratifier:* operator. **Status: locked** (2026-09-15) — option (a); evidence: operator: "Yes I agree with this approach. Lets retire those and make check in the beat." The retirement lands in chunk 5 with the default; the canary fleet declares `checkin` in `defaults.protocols` during chunk 4.
- **F4 — `ask` has no door in chunk 1.** *Context:* the manager posts through the Telegram reply tool it already has; the outbound hook records the reply as a communication from the manager's alias. *Options:* (a) join asks to decisions by alias + time window in chunk 3's outcome join; add `lib/checkin-ask.sh` only if that join proves ambiguous; (b) build the door now. *Lean:* (a). *Ratifier:* operator. **Status: locked** — evidence: YAGNI instruction; nothing consumes the join before chunk 3; `targets.msg_id` was an unfillable field (cycle-1 B11).
- **F5 — A cross-fleet `manages:` target does NOT make a manager leaf.** *Context:* `config.py:736-748` says `manages:` exists precisely for "a top-level coordinator whose reports are themselves managers of other fleets"; the cycle-1 draft (and spec §10) counted an unresolvable cross-fleet target as a worker report, equipping the coordinator the role was invented to exclude. *Options:* count it as a report / do not. *Lean:* do not — for a money-spending default the conservative direction is not to equip. *Ratifier:* operator. **Status: locked** — evidence: the docstring; spec §10 corrected in this commit. Consumed by chunk 5.

## Global Constraints

Every task's requirements include these. Exact values are copied from the spec and from `CLAUDE.md`.

- **The repo is PUBLIC.** No PII, real chat ids, user ids, handles, tokens, tailnet names or fleet-specific paths in any committed asset, test, fixture or commit message. Fixtures are shape-verbatim with faked identifiers. Never `@`-mention a bot name in GitHub-bound text.
- **bash 3.2 target.** `set -euo pipefail`; source `lib-common.sh`; quote every variable; `printf '%s'` for values; **no apostrophes in comments inside `$( )`** (`tests/test_bash_parse.py` gates `lib/` and every `library/**/*.sh`). **Never guard a flag value with `${2:?…}`** — an expansion fault exits rc 0 through lib-common's EXIT trap (`lib-common.sh:337`; `dispatch-task.sh:94-95` documents it); use `_flag_val`, which Task 1 lifts into lib-common.
- **New `system` event kinds need NO migration and NO contract change** (`contracts.py:389-418` — token shape `^[a-z][a-z0-9_]{0,63}$`; the DDL's `kind='system'` branch lists no vocabulary). Register severity with **one line per kind** in `claudlobby/plane/registries.py:54` (`SYSTEM_EVENT_SEVERITY`); unregistered kinds ingest with `severity NULL`.
- **The `Assignment` payload is `_Strict` (`contracts.py:344`)** — a `checkin_id` cannot ride it; the join is its own system event in the same batch.
- **The plane is always on.** `plane_armed` (`lib-common.sh:495-524`) is opt-OUT: `PLANE_EMIT_DISABLED=1` is the only silencer.
- **Emit from bash through `plane_emit_events <door> <<<"$batch"`** (a here-string, never a pipeline). Every `system` event is actor-anchored `"subject_kind":"actor","subject":"bot:<fleet>/<bot_id>"` — **`BOT_ID`, never `BOT_NAME`** (`lib-common.sh:1420`; `config.py:521` — name is a display field).
- **Doors' rc ladder follows `task-act.sh:55-56`:** 0 acted · 1 usage · 2 refused (contract / nothing to act on) · 3 the plane could not record.
- **The decision record's `data` cap is 16,384 bytes** (DIAGNOSTIC — truncates, never rejects). Keep schema 1 small (`rationale` ≤ 600 chars; lists capped in the contract).
- **Read doors:** unreachable ≠ empty — `refuse_unreachable("checkins", note)` (`commands/_helpers.py:160`) at rc 3. The shared plane session's connection **yields tuples** (`plane-readers.py:53-58`; `status.py:218`) — use `plane_session` for the reachability/roster refusal, then execute against `plane.db.open_ro(root)` (`commands/task.py:51`) for named rows.
- **Line numbers** are as of `main` @ `a96b47f`. If a file has moved, re-anchor by the symbol named beside the line, never by the number.
- **Tests run unsandboxed; the baseline is red** (~46 failed / 2 errors on macOS). The gate is *names + counts* (Task 7), never `pytest | grep`. Never pipe `claudlobby validate` into `grep` either — redirect to a file, read rc, then grep the file.
- **Test command form:** `./.venv/bin/pytest tests/<file>.py -q` from the worktree, whose `.venv` Task 0 creates. Never reuse the main checkout's `.venv`. Test files named in this plan that exist on main: `tests/test_skill_ref_resolution.py`, `tests/test_task_id_dispatch.py`, `tests/test_dispatch_type.py`, `tests/test_bash_parse.py`, `tests/test_plane_system_events.py`, `tests/test_main.py`, `tests/test_logging.py`. **`tests/test_skill_refs.py` and `tests/test_plane_queries.py` do not exist — do not name them.**
- **Operator config (`local/<fleet>/fleet.yaml`, `projects.yaml`) is REPORTED, never edited by the executor.** The canary manager's two-line equipment is the operator's action (Task 7 step 5).
- **Commits:** message via `git commit -F <file>`; end every message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File structure

**Create**
| Path | Responsibility |
|---|---|
| `lib/checkin-contract.py` | Stdlib contract for the schema-1 decision record: `normalize()` lists every defect; CLI filter stdin → normalized JSON, rc 2 with reasons. |
| `lib/checkin-record.sh` | The decision door: validate → mint `ck_` id → ONE `checkin_decision` system event, `source_ref checkin:<id>`. |
| `claudlobby/commands/checkins.py` | `claudlobby checkins` — rows, `--last`, `--bot`, `--since`, `--limit`, `--json`; rc 3 on an unreachable plane. |
| `library/protocols/checkin.md` | The check-in protocol: `## Manager` (the surfacing judgment; one line, one ask, one pointer) and `## Worker` (one thin line on start/done/blocked). No `requires:` in this chunk. |
| `library/skills/checkin/SKILL.md` | The `/checkin` reasoning contract: READ 0–5 through named doors, DECIDE one project then one of `dispatch \| ask \| nothing`, RECORD before ACT, the surfacing judgment, the failure posture. |
| `tests/test_checkin_contract.py` | The contract + the two severity registrations. |
| `tests/test_checkin_doors.py` | `checkin-record.sh` and `dispatch-task.sh --project/--checkin` — stub transport, real plane. |
| `tests/test_checkins_cli.py` | The read door over a seeded plane. |
| `tests/test_checkin_library.py` | The protocol composes both sections; the skill is coupled to its doors by name; the skill's grants are narrow. |

**Modify**
| Path | Change |
|---|---|
| `lib/lib-common.sh` (after `switch_is_on`, `:4089`) | `flag_val <flag> <value?> [<door>]` — `dispatch-task.sh`'s `_flag_val` lifted (three doors now need it). |
| `lib/dispatch-task.sh:5-24, 78-82, 104-117, 350-354, 395-405, 640-650, 681-689` | `--project KEY`, `--checkin ck_<32hex>`; `DISPATCH_PROJECT` opens the envelope gate; `project_key` on the work item; a `checkin_dispatch` system event appended to the batch. |
| `claudlobby/plane/registries.py:54-123` | two severity lines. |
| `claudlobby/plane/queries.py` (append) | `CHECKIN_ROWS_SQL`. |
| `claudlobby/commands/_helpers.py` (append) | `parse_since(text) -> datetime` — the report-back grammar (`24h, 7d, 30m, ISO`) as one function. |
| `claudlobby/commands/_parsers.py:189-197` | registers `checkins` beside `workstreams` (a standalone import line, not inside the `from .core import (…)` block). |
| `lib/validate-bot-change.sh` (append a block) | the empirical gate for the record door and the read door. |
| `CLAUDE.md` (lib table; `commands/` line; Key Commands), `CHANGELOG.md` (`[Unreleased]`) | rows for the two lib scripts and `checkins`. |

---

### Task 0: Worktree, venv, baseline — and the two measurements this feature is judged on

**Files:** none changed. Produces the branch, the venv, the *before* leg every later gate diffs against, and the pre-change baselines (cycle-1 R18: the feature's outcome measure had no baseline, and the draft moved it before measuring).

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

- [ ] **Step 3: The before leg, names + counts**

```bash
./.venv/bin/pytest --tb=no -ra > "$TMPDIR/run_before.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$TMPDIR/run_before.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$TMPDIR/before.txt"
wc -l < "$TMPDIR/before.txt"; tail -1 "$TMPDIR/run_before.txt"
```
Expected: `rc=1` (the baseline is red) and a `N failed, M passed` line. rc 2/4/5/127 means the run did not complete.

- [ ] **Step 4: The two baselines, read-only from the live plane, pasted into `$TMPDIR/baseline.md`**

Fleet-active % (any bot BUSY per observed minute, last 7 days — the spike's definition) and manager→Telegram message volume (the fatigue number), for the canary fleet. Read-only; `sqlite3 -readonly` opens the WAL db without writing.

```bash
ssh -o BatchMode=yes mini 'bash -s' <<'EOF' | tee "$TMPDIR/baseline.md"
DB=~/Projects/claudlobby/state/plane/plane.db
FLEET="$(ls ~/Projects/claudlobby/local/home | head -1)"   # replace with the canary fleet name if it is not first
echo "## baseline $(date -u +%F) fleet=$FLEET"
sqlite3 -readonly "$DB" "
WITH hb AS (
  SELECT strftime('%Y-%m-%d %H:%M', occurred_at) AS minute,
         MAX(json_extract(value, '\$.state') = 'BUSY') AS busy
  FROM metric_samples
  WHERE metric = 'bot.heartbeat' AND occurred_at >= datetime('now', '-7 days')
  GROUP BY minute)
SELECT 'fleet_active_pct', ROUND(100.0 * SUM(busy) / COUNT(*), 1), SUM(busy) || '/' || COUNT(*) || ' minutes' FROM hb;"
sqlite3 -readonly "$DB" "
SELECT 'telegram_posts_7d', COUNT(*) FROM communications c
WHERE c.occurred_at >= datetime('now', '-7 days')
  AND EXISTS (SELECT 1 FROM events t WHERE t.kind = 'transmission' AND t.msg_id = c.msg_id AND t.carrier = 'telegram-bridge');"
EOF
```
Expected: two rows. Record both in the PR body (Task 7). If the heartbeat query returns no rows, the fleet's keepalive is not emitting `bot.heartbeat` — say so in the baseline rather than inventing a number.

---

### Task 1: The decision record — `flag_val`, the contract, the record door, the severities

**Files:**
- Modify: `lib/lib-common.sh` (append after `switch_is_on`, `:4081-4089`); `lib/dispatch-task.sh:92-101` (`_flag_val` becomes a one-line shim over the lifted helper); `claudlobby/plane/registries.py:54-123`
- Create: `lib/checkin-contract.py`, `lib/checkin-record.sh`
- Test: `tests/test_checkin_contract.py`, `tests/test_checkin_doors.py`

**Interfaces:**
- Produces: `flag_val <flag> <value?> [<door>]` in lib-common — prints the value, or prints `<door>: <flag> needs a value` on stderr and `exit 1`. `checkin-contract.py`: `normalize(obj) -> dict` (raises `ContractError(reasons)`), `mint_checkin_id() -> "ck_<32hex>"`, CLI filter rc 0/2. `checkin-record.sh [--bot B] [--fleet F] [--dry-run] < decision.json` → stdout `ck_<32hex>`; rc 0 recorded · 1 usage (bad flag, missing identity) · 2 contract refused · 3 plane could not record. Plane row: `kind='system'`, `event='checkin_decision'`, `source_ref='checkin:<ck_id>'`, `subject_alias='bot:<fleet>/<bot_id>'`, `severity='notice'`, `detail` = the normalized record.
- Schema 1 (the whole of it — `focus_*`, `targets`, `propose`, `sprint` arrive with the chunks that produce them, as schema 2):

```json
{ "schema": 1, "checkin_id": "ck_<32hex>", "prev_checkin_id": "ck_<32hex> | null",
  "inputs_seen": { "open_tasks": 0, "stalls": 0, "unacked": 0, "issues_considered": 0,
                   "knowledge_hits": 0, "unavailable": ["gh"] },
  "delta": { "tasks_opened": 0, "tasks_completed": 0, "stalls_appeared": 0, "stalls_cleared": 0,
             "issues_new": 0, "messages_new": 0, "held_pending": 0 },
  "action": "dispatch | ask | nothing",
  "project_key": "<slug or null>",
  "rationale": "<= 600 chars",
  "raise": { "decided": false, "reason": "<non-empty>", "held": ["<= 10 items"] } }
```
Every `delta` count is `int | null` — **null means "could not measure", never 0** (cycle-1 R17). `raise.reason` is required non-empty in both directions (cycle-1 gap: optional in exactly the case that matters). `unavailable` and `held` are capped at 10 strings of ≤ 200 chars so the record stays far under the 16 KiB cap.

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

_spec = importlib.util.spec_from_file_location("checkin_contract", CONTRACT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def _decision(**over) -> dict:
    d = {
        "inputs_seen": {"open_tasks": 2, "stalls": 0, "unacked": 1, "issues_considered": 4,
                        "knowledge_hits": 1, "unavailable": []},
        "delta": {"tasks_opened": 0, "tasks_completed": 1, "stalls_appeared": 0,
                  "stalls_cleared": 0, "issues_new": 1, "messages_new": None, "held_pending": 0},
        "action": "nothing",
        "project_key": None,
        "rationale": "All work in flight; nothing new worth starting.",
        "raise": {"decided": False, "reason": "no delta the operator would want", "held": []},
    }
    d.update(over)
    return d


def test_a_minimal_decision_normalizes_and_mints_the_id():
    out = cc.normalize(_decision())
    assert out["schema"] == 1
    assert out["checkin_id"].startswith("ck_") and len(out["checkin_id"]) == 35
    assert out["prev_checkin_id"] is None
    assert out["delta"]["messages_new"] is None          # null survives: could not measure
    assert out["delta"]["tasks_completed"] == 1
    assert "targets" not in out and "focus_declared" not in out["inputs_seen"]


def test_a_supplied_id_and_prev_are_kept():
    prev, mine = cc.mint_checkin_id(), cc.mint_checkin_id()
    out = cc.normalize(_decision(checkin_id=mine, prev_checkin_id=prev))
    assert out["checkin_id"] == mine and out["prev_checkin_id"] == prev


@pytest.mark.parametrize("over, needle", [
    ({"action": "dispatch"}, "must name project_key"),
    ({"action": "ask"}, "raise.decided"),
    ({"action": "propose"}, "action must be one of"),        # not in this chunk — schema 2
    ({"action": "coffee"}, "action must be one of"),
    ({"rationale": "x" * 601}, "<= 600"),
    ({"rationale": ""}, "rationale"),
    ({"project_key": "Not-A-Slug"}, "project_key"),
    ({"prev_checkin_id": "nope"}, "prev_checkin_id"),
    ({"inputs_seen": {"open_tasks": -1}}, "inputs_seen.open_tasks"),
    ({"delta": {"tasks_opened": -1}}, "delta.tasks_opened"),
    ({"raise": {"decided": False, "reason": ""}}, "raise.reason"),
    ({"raise": {"decided": "yes", "reason": "r"}}, "raise.decided"),
    ({"raise": {"decided": False, "reason": "r", "held": ["h"] * 11}}, "raise.held"),
])
def test_defects_are_listed_by_name(over, needle):
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(**over))
    assert any(needle in r for r in exc.value.reasons), exc.value.reasons


def test_every_defect_is_reported_not_just_the_first():
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(action="coffee", rationale=""))
    assert len(exc.value.reasons) >= 2


def test_the_cli_is_a_filter():
    r = subprocess.run([sys.executable, str(CONTRACT)], input=json.dumps(_decision()),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["action"] == "nothing"
    r = subprocess.run([sys.executable, str(CONTRACT)], input="not json", capture_output=True, text=True)
    assert r.returncode == 2 and "not JSON" in r.stderr
    r = subprocess.run([sys.executable, str(CONTRACT)], input=json.dumps(_decision(action="coffee")),
                       capture_output=True, text=True)
    assert r.returncode == 2 and "checkin-contract: action must be one of" in r.stderr and r.stdout == ""


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
Expected: collection fails on the missing `lib/checkin-contract.py`; run alone, the severity test would fail with `None`.

- [ ] **Step 3: The severities**

In `claudlobby/plane/registries.py`, inside `SYSTEM_EVENT_SEVERITY` before its closing `}`:

```python
    # manager check-in (spec §7): the decision record, and the join row a
    # `dispatch-task.sh --checkin` appends to its batch. notice — the record
    # IS the point; nothing here pages.
    "checkin_decision": "notice",
    "checkin_dispatch": "notice",
```

- [ ] **Step 4: Lift `flag_val` into lib-common**

Append to `lib/lib-common.sh` directly after `switch_is_on` (ends `:4089`):

```bash

# flag_val <flag> <value?> [<door>] -- the explicit missing-value guard every
# door with flags uses. NOT ${2:?...}: an expansion fault exits 0 through the
# EXIT trap above (measured, bash 3.2.57), so a usage error would read as
# success. Prints the value; on a missing value prints the usage line on
# stderr and exits 1 (the task-act.sh ladder: 1 = usage).
flag_val() {
    local flag="${1:-}" value="${2:-}" door="${3:-${0##*/}}"
    if [ -z "$value" ]; then
        printf '%s: %s needs a value\n' "$door" "$flag" >&2
        exit 1
    fi
    printf '%s' "$value"
}
```

In `lib/dispatch-task.sh:96-101` replace the body of `_flag_val` with a shim so its two existing call sites and messages stay byte-identical:

```bash
_flag_val() { flag_val "$1" "${2:-}" dispatch-task; }
```

- [ ] **Step 5: The contract module**

```python
#!/usr/bin/env python3
# lib/checkin-contract.py
"""The check-in decision record contract (manager check-in spec §7), schema 1.

Stdlib only (the dispatch-overdue.py precedent): checkin-record.sh pipes the
manager's decision JSON through `normalize` before anything reaches the plane,
so a malformed decision is refused AT THE DOOR with every reason named, never
landed as a row no reader can join.

    python3 checkin-contract.py < decision.json     # prints the normalized JSON
    exit 0 ok / 2 contract violation (reasons on stderr, one per line)

Schema 1 is the record ONE hand-equipped manager can produce this chunk:
actions dispatch | ask | nothing. Later chunks ADD (propose, sprint, focus
fields) as schema 2 — additive, never a rewrite. Every `delta` count is
int | null: null means "could not measure" and is never collapsed to 0, because
an unchanged delta is the skill's primary argument for saying nothing.
"""
from __future__ import annotations

import json
import re
import secrets
import sys

SCHEMA = 1
ACTIONS = ("dispatch", "ask", "nothing")
RATIONALE_MAX = 600
LIST_MAX = 10
ITEM_MAX = 200
INPUTS_COUNTS = ("open_tasks", "stalls", "unacked", "issues_considered", "knowledge_hits")
DELTA_COUNTS = ("tasks_opened", "tasks_completed", "stalls_appeared", "stalls_cleared",
                "issues_new", "messages_new", "held_pending")
ID_RE = re.compile(r"^ck_[0-9a-f]{32}$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")   # a projects.yaml key


class ContractError(ValueError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def mint_checkin_id() -> str:
    return "ck_" + secrets.token_hex(16)


def _count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _str_list(v) -> bool:
    return (isinstance(v, list) and len(v) <= LIST_MAX
            and all(isinstance(s, str) and len(s) <= ITEM_MAX for s in v))


def normalize(obj) -> dict:
    """Return the schema-1 record, or raise ContractError listing EVERY defect."""
    if not isinstance(obj, dict):
        raise ContractError(["decision must be a JSON object"])
    bad: list[str] = []
    out: dict = {"schema": SCHEMA}

    cid = obj.get("checkin_id") or mint_checkin_id()
    if not ID_RE.match(str(cid)):
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
    unavailable = seen.get("unavailable", [])
    if not _str_list(unavailable):
        bad.append(f"inputs_seen.unavailable must be a list of <= {LIST_MAX} short strings")
    out["inputs_seen"]["unavailable"] = unavailable

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
    if not isinstance(rationale, str) or not rationale.strip():
        bad.append("rationale must be a non-empty string")
    elif len(rationale) > RATIONALE_MAX:
        bad.append(f"rationale must be <= {RATIONALE_MAX} characters (got {len(rationale)})")
    out["rationale"] = rationale

    raise_ = obj.get("raise")
    if not isinstance(raise_, dict):
        bad.append("raise must be an object")
        raise_ = {}
    decided = raise_.get("decided", False)
    if not isinstance(decided, bool):
        bad.append("raise.decided must be true or false")
    reason = raise_.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        bad.append("raise.reason must be a non-empty string (why it surfaced, or why not)")
    held = raise_.get("held", [])
    if not _str_list(held):
        bad.append(f"raise.held must be a list of <= {LIST_MAX} short strings")
    out["raise"] = {"decided": decided, "reason": reason, "held": held}
    if action == "ask" and decided is not True:
        bad.append("action ask requires raise.decided = true (an ask IS a surfacing)")

    if bad:
        raise ContractError(bad)
    return out


def main() -> int:
    try:
        obj = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"checkin-contract: not JSON: {exc}", file=sys.stderr)
        return 2
    try:
        out = normalize(obj)
    except ContractError as exc:
        for r in exc.reasons:
            print(f"checkin-contract: {r}", file=sys.stderr)
        return 2
    json.dump(out, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run the contract tests**

Run: `./.venv/bin/pytest tests/test_checkin_contract.py tests/test_plane_system_events.py -q`
Expected: all pass.

- [ ] **Step 7: Write the failing door tests**

```python
# tests/test_checkin_doors.py
"""The check-in's write doors (spec §7): checkin-record.sh, and dispatch-task.sh
--project / --checkin. Two rigs: a STUB lib-common that captures the batch
(tests/test_briefing_trigger.py's shape), and the REAL shim landing rows in a
scratch plane through the cold CLI rung (tests/test_task_id_dispatch.py's
_fake_lib / plane_env)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.conftest import constructed_env
from tests.plane_fixtures import ro as _ro
from tests.test_task_id_dispatch import _bash, _fake_lib

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

STUB_LIB_COMMON = """\
#!/bin/bash
install_error_trap() { :; }
trap 'true' EXIT
flag_val() { local f="${1:-}" v="${2:-}" d="${3:-door}"; [ -n "$v" ] || { printf '%s: %s needs a value\\n' "$d" "$f" >&2; exit 1; }; printf '%s' "$v"; }
plane_armed() { [ "${PLANE_EMIT_DISABLED:-0}" != "1" ]; }
json_escape() { printf '%s' "$1" | python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read())[1:-1])'; }
show_help() { awk 'NR == 1 { next } /^[^#]/ { exit } { sub(/^# ?/, ""); print }' "$1"; }
plane_emit_events() { cat > "$EMIT_CAPTURE"; PLANE_EMIT_LAST_RC="${STUB_EMIT_RC:-0}"; }
PLANE_EMIT_LAST_RC=0
"""


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

def test_record_lands_one_actor_anchored_decision(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    ck = r.stdout.strip()
    assert ck.startswith("ck_") and len(ck) == 35
    (e,) = _captured(env)["events"]
    assert e["event_type"] == "system" and e["emitter"] == "checkin-record"
    assert e["source_ref"] == f"checkin:{ck}"
    assert e["payload"]["event"] == "checkin_decision"
    assert e["payload"]["subject_kind"] == "actor" and e["payload"]["subject"] == "bot:f/mgr"   # BOT_ID, never BOT_NAME
    assert e["payload"]["data"]["checkin_id"] == ck and e["payload"]["data"]["action"] == "nothing"


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


def test_record_flag_without_a_value_is_rc_1_never_0(tmp_path):
    # the ${2:?} trap: an expansion fault would exit 0 through the EXIT trap
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--bot")
    assert r.returncode == 1 and "--bot needs a value" in r.stderr and r.stdout == ""


def test_record_dry_run_validates_and_writes_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--dry-run")
    assert r.returncode == 0 and r.stdout.strip().startswith("ck_")
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_help_prints_the_whole_header(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, "", "--help")
    assert r.returncode == 0 and "exit:" in r.stdout and "3 the plane could not record" in r.stdout


# --- the REAL spine: rows land in a scratch plane -------------------------------

REAL_DOOR_FILES = ("checkin-record.sh", "checkin-contract.py", "lib-common.sh",
                   "plane-emit.sh", "plane-socket-client.py")


def _real_rig(tmp_path: Path) -> tuple[Path, dict]:
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in REAL_DOOR_FILES:
        (lib / name).symlink_to(LIB / name)
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    env = {"CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "f", "BOT_ID": "mgr",
           "PLANE_EMIT_CLI": str(CLI), "PLANE_SOCKET": str(tmp_path / "no-daemon.sock"),
           "HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    return lib, env


def test_the_decision_lands_on_a_real_plane(tmp_path):
    lib, env = _real_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    ck = r.stdout.strip()
    with _ro(tmp_path) as conn:
        row = conn.execute(
            "SELECT severity, source_ref, subject_alias, detail, detail_truncated FROM events"
            " WHERE kind='system' AND event='checkin_decision'").fetchone()
    assert row is not None
    assert row["severity"] == "notice" and row["source_ref"] == f"checkin:{ck}"
    assert row["subject_alias"] == "bot:f/mgr" and row["detail_truncated"] == 0
    assert json.loads(row["detail"])["action"] == "nothing"


# --- dispatch-task.sh --project / --checkin (Task 2) ------------------------------

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
    with _ro(tmp_path) as conn:
        asg = conn.execute("SELECT assignment_id, work_item_id, source_ref FROM assignments").fetchone()
        link = conn.execute("SELECT detail, subject_alias, source_ref, severity FROM events"
                            " WHERE kind='system' AND event='checkin_dispatch'").fetchone()
    assert link is not None
    d = json.loads(link["detail"])
    assert d["checkin_id"] == CK and d["assignment_id"] == asg["assignment_id"]
    assert d["work_item_id"] == asg["work_item_id"] and d["task_id"].startswith("t-")
    assert link["source_ref"] == asg["source_ref"] and link["severity"] == "notice"
    assert link["subject_alias"] == "bot:" + env["FLEET_NAME"] + "/lead"


def test_dispatch_refuses_a_malformed_checkin_id(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin nope w1 "x"', env=env)
    assert r.returncode == 1 and "--checkin" in r.stderr


def test_dispatch_flag_without_a_value_is_rc_1_never_0(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" w1 "x" --checkin', env=env)
    assert r.returncode == 1 and r.stdout == ""
```

(The four `dispatch-task.sh` tests fail until Task 2; run the file with `-k "record or real_plane"` at this step.)

- [ ] **Step 8: Run the record tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q -k "record or real_plane"`
Expected: every test fails on the missing `lib/checkin-record.sh`.

- [ ] **Step 9: The record door**

```bash
#!/bin/bash
# checkin-record.sh -- THE write door for a manager check-in decision
# (manager check-in spec §7). The skill hands it the decision JSON on stdin;
# it validates the schema-1 contract (checkin-contract.py), mints the
# checkin_id when absent, and lands ONE actor-anchored system event
# `checkin_decision`, stamped source_ref checkin:<checkin_id> -- the
# task-recheck stamp idiom: the ref is the address a reader joins on.
#
# Usage: checkin-record.sh [--bot <bot_id>] [--fleet <name>] [--dry-run] < decision.json
#   stdout: the checkin_id (one line)
#   exit:   0 recorded (committed or spooled)
#           1 usage (unknown flag, flag without a value, no identity)
#           2 contract refused (every reason on stderr; nothing recorded)
#           3 the plane could not record (disclosed; nothing recorded)
#
# RECORD BEFORE ACT is the skill's rule, so for THIS door the record IS the
# action: an unrecorded decision is a failure it says so about (rc 3), never
# a silent 0. Identity is BOT_ID + FLEET_NAME (the bot.conf a manager session
# sources), never BOT_NAME -- that is a display field. The plane is always on;
# PLANE_EMIT_DISABLED=1 (the harness exemption) is the one silencer, rc 3.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

BOT="${BOT_ID:-}"
FLEET="${FLEET_NAME:-${CLAUDLOBBY_FLEET:-}}"
DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --bot)     BOT=$(flag_val "$1" "${2:-}" checkin-record); shift 2 ;;
        --fleet)   FLEET=$(flag_val "$1" "${2:-}" checkin-record); shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) show_help "$0"; exit 0 ;;
        *) printf 'checkin-record: unknown flag %s\n' "$1" >&2; exit 1 ;;
    esac
done
if [ -z "$BOT" ] || [ -z "$FLEET" ]; then
    printf 'checkin-record: no identity -- need BOT_ID and FLEET_NAME (a manager session sources them from bot.conf), or --bot/--fleet\n' >&2
    exit 1
fi

raw=$(cat)
if ! normalized=$(printf '%s' "$raw" | python3 "$LIB_DIR/checkin-contract.py"); then
    printf 'checkin-record: decision refused (nothing recorded)\n' >&2
    exit 2
fi
checkin_id=$(printf '%s' "$normalized" | python3 -c 'import json,sys; print(json.load(sys.stdin)["checkin_id"])')

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

`chmod 0755 lib/checkin-record.sh lib/checkin-contract.py`. The header is one contiguous `#` block from line 2, because `show_help` prints from line 2 to the first non-`#` line.

- [ ] **Step 10: Run the record tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q -k "record or real_plane" && ./.venv/bin/pytest tests/test_bash_parse.py tests/test_task_id_dispatch.py -q`
Expected: all pass (`test_the_decision_lands_on_a_real_plane` proves the shim's cold rung records; its "daemon unavailable — falling back to cold CLI" stderr line is expected). `test_task_id_dispatch.py` is the regression guard for the `_flag_val` shim.

- [ ] **Step 11: Commit**

```bash
git add lib/lib-common.sh lib/dispatch-task.sh lib/checkin-contract.py lib/checkin-record.sh claudlobby/plane/registries.py tests/test_checkin_contract.py tests/test_checkin_doors.py
printf '%s\n' 'feat(checkin): the decision record — schema-1 contract, the record door, flag_val lifted' '' 'lib/checkin-contract.py (stdlib) refuses a malformed decision with every reason;' 'null delta counts mean could-not-measure; raise.reason is required both ways.' 'lib/checkin-record.sh lands ONE actor-anchored checkin_decision (BOT_ID alias,' 'source_ref checkin:<id>) with the task-act rc ladder. flag_val moves into' 'lib-common so no door guards a flag with ${2:?} (exits 0 under the EXIT trap).' 'Spec §7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c1.txt"
git commit -q -F "$TMPDIR/c1.txt" && git log --oneline -1
```

---

### Task 2: `dispatch-task.sh --project` and `--checkin`

**Files:**
- Modify: `lib/dispatch-task.sh:5-24` (flag docs), `:78-82` (init), `:104-117` (parse), `:350-354` (the envelope gate), `:395-405` (envelope), `:640-650` (beside `sup_ev`), `:681-689` (the batch)
- Test: `tests/test_checkin_doors.py` (the four dispatch tests from Task 1 step 7)

**Interfaces:**
- Produces: `--project KEY` (slug `^[a-z][a-z0-9-]*$`; `| project:KEY` in the envelope; opens the envelope gate; `project_key` on the work item — the fix for the measured 0/374). `--checkin ck_<32hex>`: one `system` event `checkin_dispatch` appended to the dispatch batch — `source_ref` = the dispatch ref, actor = the sender, `data = {checkin_id, assignment_id, work_item_id, task_id}` — atomic with the assignment (the `sup_ev` precedent, `:640-650`). Emitted only when the tracked triple is (`emit_triple`), because a control-type dispatch mints no assignment to join.
- Consumed by the skill (Task 4) and chunk 3's outcome join.

- [ ] **Step 1: Run the four dispatch tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q -k dispatch`
Expected: 4 failed — `unknown flag '--project'` / `'--checkin'`.

- [ ] **Step 2: The flags**

(a) Flag docs, after the `--ref URL` line (`:10`):
```bash
#   --project KEY      projects.yaml project (adds project:<KEY> to the envelope and
#                      project_key to the plane work item -- the well-defined bar)
#   --checkin ID       The check-in decision this dispatch acts on (ck_<32hex>):
#                      appends a checkin_dispatch join row to the plane batch, atomic
#                      with the assignment, so the decision and its outcome meet.
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
(d) **The envelope gate** (`:352-353`) — add the project to the condition, or a `--project`-only dispatch sends freeform and `project:` never reaches the pane (cycle-1 R7):
```bash
if [ -n "$FORCE_ENVELOPE" ] || [ -n "$DISPATCH_REPO" ] || [ -n "$DISPATCH_PRIORITY" ] \
   || [ -n "$DISPATCH_REF" ] || [ -n "$DISPATCH_WORKSTREAM" ] || [ -n "$DISPATCH_PROJECT" ]; then
```
(e) Envelope (`:395-400`), after the `repo:` line:
```bash
    [ -n "$DISPATCH_PROJECT" ]    && DISPATCH_MSG="$DISPATCH_MSG | project:$DISPATCH_PROJECT"
```
(f) In `_plane_emit_intent`, beside the `sup_ev` block (`:640-650`) — declare `local ck_ev=""` with the other locals, then:
```bash
    # The check-in join (spec §7): the decision row was recorded BEFORE this
    # dispatch (RECORD before ACT), and the Assignment payload is strict, so
    # the link is its own system event in the SAME batch -- the supersede
    # precedent above. Only a tracked dispatch has an assignment to join.
    if [ -n "$DISPATCH_CHECKIN" ] && [ -n "$emit_triple" ]; then
        ck_ev="{\"event_type\":\"system\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"event\":\"checkin_dispatch\",\"subject_kind\":\"actor\",\"subject\":\"$safe_sender\",\"data\":{\"checkin_id\":\"$DISPATCH_CHECKIN\",\"assignment_id\":\"$PLANE_ASG_ID\",\"work_item_id\":\"$PLANE_WI_ID\",\"task_id\":\"$(json_escape "$TASK_ID")\"}}}"
    fi
```
`safe_sender` is the sender's json-escaped **alias** (`bot:<fleet>/<id>`), the same value the batch already writes as the assignment's `assigned_by` and the work item's `created_by` — reuse it verbatim so the join row's actor is the dispatcher by the plane's own name.
(g) The batch (`:681-689`): add the project fragment beside `repo_frag` and the link beside `sup_ev`:
```bash
        local proj_frag=""
        [ -n "$DISPATCH_PROJECT" ] && proj_frag=",\"project_key\":\"$(json_escape "$DISPATCH_PROJECT")\""
        wi_ev="{\"event_type\":\"work_item\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$PLANE_WI_ID\",\"title\":\"$safe_task\",\"created_by\":\"$safe_sender\"${ws_frag}${repo_frag}${proj_frag}}}"
        ...
        printf -v _batch '{"events":[%s,%s,%s%s%s]}' "$wi_ev" "$asg_ev" "$comm" "${sup_ev:+,$sup_ev}" "${ck_ev:+,$ck_ev}"
```
(`proj_frag` declared `local` beside `ws_frag`/`repo_frag`; the `asg_ev`, `comm` and `plane_emit_events` lines stay as they are.)

- [ ] **Step 3: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py tests/test_task_id_dispatch.py tests/test_dispatch_type.py tests/test_bash_parse.py -q && bash tests/test_dispatch_task.sh`
Expected: all pass. (`test_dispatch_type.py` parses the protocol docs against `DISPATCH_TYPES` — untouched.)

- [ ] **Step 4: Commit**

```bash
git add lib/dispatch-task.sh tests/test_checkin_doors.py
printf '%s\n' 'feat(dispatch-task): --project stamps project_key and opens the envelope; --checkin appends the join row' '' 'project_key on the work item fixes the measured 0/374. The checkin_dispatch' 'system event rides the SAME batch as the assignment (the supersede precedent):' 'a strict Assignment payload cannot carry the id, so the join is its own row.' 'Spec §7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c2.txt"
git commit -q -F "$TMPDIR/c2.txt" && git log --oneline -1
```

---

### Task 3: `claudlobby checkins` — the read door

**Files:**
- Modify: `claudlobby/plane/queries.py` (append `CHECKIN_ROWS_SQL`), `claudlobby/commands/_helpers.py` (append `parse_since`), `claudlobby/commands/_parsers.py:189-197` + its imports
- Create: `claudlobby/commands/checkins.py`
- Test: `tests/test_checkins_cli.py`

**Interfaces:**
- Consumes: `brief.plane_session(paths, fleet) -> (plane, note)` (`brief.py:252`, the reachability + roster refusal; its `conn` yields TUPLES), `plane.db.open_ro(root) -> (conn | None, reason)` (`db.py:28`, named rows), `_helpers.refuse_unreachable(command, note) -> int` (`:160`), `queries.fleet_alias_range` / `fleet_range_params` / `_epoch`.
- Produces: `CHECKIN_ROWS_SQL` (binds: fleet, fleet) → `(checkin_id, prev_checkin_id, subject_alias, occurred_at, action, project_key, raise_decided, raise_reason, rationale, detail, detail_truncated)` ordered `occurred_at DESC, ingest_seq DESC`; `_helpers.parse_since(text) -> datetime` (`24h`, `7d`, `30m`, ISO — the report-back grammar); `cmd_checkins(args) -> int`: `--fleet` (dest `checkins_fleet`), `--bot`, `--since 7d`, `--last`, `--limit 20`, `--json`; rc 0 · 2 no fleet / bad since · 3 plane unreachable. `--last` ignores `--since` (cycle-1 R13).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkins_cli.py
"""`claudlobby checkins` — the read door (spec §11), minimal form. Seeded through
the real emit spine like tests/test_plane_stale_task.py. The plane session's
connection yields tuples (status.py:218); the door must read named rows."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby.commands import checkins as cmd
from claudlobby.commands._helpers import parse_since
from claudlobby.plane.emit_api import emit_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
F = "ck-fleet"
CK1, CK2, CK3 = ("ck_" + c * 32 for c in "abc")


@pytest.fixture(autouse=True)
def root(tmp_path: Path) -> Path:
    (tmp_path / "lib").mkdir()
    for name in ("dispatch-overdue.py", "plane-readers.py", "plane-lookup.py"):
        shutil.copy(REPO_ROOT / "lib" / name, tmp_path / "lib" / name)
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return tmp_path


class _Args:
    def __init__(self, root, **kw):
        self.root, self.fleet, self.seed = str(root), None, False
        self.checkins_fleet = kw.get("fleet", F)
        self.bot = kw.get("bot")
        self.since = kw.get("since", "7d")
        self.last = kw.get("last", False)
        self.limit = kw.get("limit", 20)
        self.json = kw.get("json", False)


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _record(**over) -> dict:
    d = {"schema": 1, "checkin_id": None, "prev_checkin_id": None,
         "inputs_seen": {"open_tasks": 0, "unavailable": []}, "delta": {},
         "action": "nothing", "project_key": None, "rationale": "r",
         "raise": {"decided": False, "reason": "quiet", "held": []}}
    d.update(over)
    return d


def _decision(root, bot: str, ck: str, *, age_h: float, **over):
    rec = _record(checkin_id=ck, rationale=f"r-{ck[-4:]}", **over)
    emit_batch(root, [{
        "event_type": "system", "emitter": "checkin-record", "fleet": F,
        "source_ref": f"checkin:{ck}", "occurred_at": _ago(hours=age_h),
        "payload": {"event": "checkin_decision", "subject_kind": "actor",
                    "subject": f"bot:{F}/{bot}", "data": rec}}])


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


def test_since_window_and_limit(root, capsys):
    _decision(root, "mgr", CK1, age_h=30)
    _decision(root, "mgr", CK2, age_h=1)
    _decision(root, "mgr", CK3, age_h=2)
    assert cmd.cmd_checkins(_Args(root, since="24h", json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK3]
    assert cmd.cmd_checkins(_Args(root, since="24h", limit=1)) == 0
    text = capsys.readouterr().out
    assert CK2 in text and CK3 not in text and "1 of 2" in text        # the cap is disclosed


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
    assert "unreachable" in capsys.readouterr().err


@pytest.mark.parametrize("text, delta", [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)),
                                         ("30m", timedelta(minutes=30))])
def test_parse_since_grammar(text, delta):
    before = datetime.now(timezone.utc)
    got = parse_since(text)
    assert abs((before - delta) - got) < timedelta(seconds=5)


def test_parse_since_iso_and_garbage():
    assert parse_since("2026-09-01T00:00:00Z") == datetime(2026, 9, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_since("yesterday")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkins_cli.py -q`
Expected: `ImportError: cannot import name 'checkins'` (and `parse_since`).

- [ ] **Step 3: The query**

Append to `claudlobby/plane/queries.py`:

```python

# --- the manager check-in (manager check-in spec §7, §11) ---------------------
# A check-in is ONE system event, `checkin_decision`, actor-anchored on the
# manager, stamped source_ref checkin:<checkin_id>; the record is its detail
# (schema 1). Ordered by WHEN IT HAPPENED (`occurred_at`, the clock every other
# read of this door uses), ingest_seq as the tiebreak -- under the shim's spool
# rung the two clocks diverge, and "the previous check-in" means the one that
# happened last, not the one that landed last. A truncated detail is NOT a
# JSON document (detail_truncated=1 IS the parse guard), so its fields read
# NULL and the row is still returned: the read door exists to show every
# decision, and dropping the over-cap ones would hide exactly the records that
# most need looking at. `json_type` guards the extract on well-formed rows.
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

- [ ] **Step 4: The shared since-parser**

Append to `claudlobby/commands/_helpers.py`:

```python


def parse_since(text: str) -> "datetime":
    """The `--since` grammar every read door shares: `24h`, `7d`, `30m`, or an
    ISO instant (a trailing `Z` accepted). The same rule `cmd_report_back`
    applies inline (core.py) -- lifted here for the next door so the CLI does
    not grow a third grammar. Raises ValueError with the usage text."""
    from datetime import datetime, timedelta, timezone

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
```

- [ ] **Step 5: The command**

```python
# claudlobby/commands/checkins.py
"""`claudlobby checkins` — the check-in's read door (manager check-in spec §11),
minimal form: the decision rows, newest first. `--summary` and the outcome join
land in chunk 3.

Two connections, on purpose: `brief.plane_session` is THE reachability door for
the package (no db / no fleet / a plane that has never seen the fleet all refuse
with a note -- unreachable is not empty), but its connection yields TUPLES
(plane-readers.py:53-58; status.py:218). The rows are read through
`plane.db.open_ro`, which sets sqlite3.Row -- the `commands/task.py` pattern."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from ..plane.db import open_ro
from ..plane.queries import CHECKIN_ROWS_SQL, fleet_range_params
from ._helpers import _resolve_paths, parse_since, refuse_unreachable


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
                     last: bool, limit: int) -> tuple[list[dict], int]:
    """(rows, total matching) -- newest first by occurred_at. `since` None means
    no window (--last). `limit` caps the returned rows, never the count."""
    out: list[dict] = []
    total = 0
    for r in conn.execute(CHECKIN_ROWS_SQL, fleet_range_params(fleet)):
        if bot and _bot_of(r["subject_alias"]) != bot:
            continue
        if since is not None and datetime.fromisoformat(r["occurred_at"]) < since:
            continue
        total += 1
        if len(out) < limit:
            out.append(_row(r))
        if last:
            break
    return out, total


def cmd_checkins(args) -> int:
    paths = _resolve_paths(args)
    from ..brief import plane_session, resolve_fleet_name

    fleet = getattr(args, "checkins_fleet", None) or resolve_fleet_name(paths)
    if not fleet:
        print("checkins: no fleet is named (--fleet <name>, or a fleet.yaml naming one)"
              " — the plane's rows are per fleet", file=sys.stderr)
        return 2
    try:
        since = None if args.last else parse_since(args.since)
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
        rows, total = collect_checkins(conn, fleet, since=since, bot=args.bot,
                                       last=args.last, limit=1 if args.last else args.limit)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"schema": 1, "fleet": fleet,
                          "since": since.isoformat() if since else None,
                          "total": total, "checkins": rows}, indent=2))
        return 0
    scope = f"fleet {fleet}" + (f", bot {args.bot}" if args.bot else "") + \
        (" (newest only)" if args.last else f", last {args.since}")
    if not rows:
        print(f"no check-ins — {scope}")
        return 0
    shown = f"{len(rows)} of {total}" if total > len(rows) else str(total)
    print(f"check-ins — {scope}: {shown}")
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

Register it in `claudlobby/commands/_parsers.py` directly after the `workstreams` block (ends `:197`), with a **standalone** import line placed after the `from .core import (…)` block (never inside it — `cmd_checkins` is not in `core`):

```python
from .checkins import cmd_checkins
```

```python
    pck = sub.add_parser("checkins", help="The manager check-in's decisions (plane read)")
    pck.add_argument("--fleet", dest="checkins_fleet", default=None,
                     help="fleet whose rows to read (default: the fleet.yaml this root names)")
    pck.add_argument("--bot", default=None, help="one manager's rows only")
    pck.add_argument("--since", default="7d", help="window: 24h, 7d, 30m, or an ISO instant (default 7d)")
    pck.add_argument("--last", action="store_true", help="only the newest row, ignoring --since")
    pck.add_argument("--limit", type=int, default=20, help="rows to print (text output; the count is always full)")
    pck.add_argument("--json", action="store_true", help="machine-facing envelope")
    pck.set_defaults(func=cmd_checkins)
```

(`dest="checkins_fleet"`, not `fleet`: a subparser copies its namespace over the parent's — `_parsers.py:223-225`.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkins_cli.py tests/test_main.py -q`
Expected: all pass. `test_a_truncated_record_is_listed_and_marked_never_dropped` depends on the 16 KiB DIAGNOSTIC cap truncating at ingest (`ingest.py:286-294`); if `emit_batch` rejects the oversize record instead, that is a contract change on main — stop and re-read `FIELD_POLICY[("system","data")]`.

- [ ] **Step 7: Commit**

```bash
git add claudlobby/plane/queries.py claudlobby/commands/_helpers.py claudlobby/commands/checkins.py claudlobby/commands/_parsers.py tests/test_checkins_cli.py
printf '%s\n' 'feat(cli): claudlobby checkins — the decision rows, newest first by occurred_at' '' 'Reachability through brief.plane_session (unreachable is not empty, rc 3 via' 'refuse_unreachable), rows through plane.db.open_ro (the session yields tuples).' '--last ignores the window so an idle month never reads as a first check-in;' 'truncated records are listed and marked, never dropped; parse_since is the' 'one --since grammar. Spec §11.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c3.txt"
git commit -q -F "$TMPDIR/c3.txt" && git log --oneline -1
```

---

### Task 4: The protocol and the skill

**Files:**
- Create: `library/protocols/checkin.md`, `library/skills/checkin/SKILL.md`
- Test: `tests/test_checkin_library.py`

**Interfaces:**
- The protocol is additive and declares no `requires:` (chunk 5). Two H2 sections, `## Manager` / `## Worker`, composed as `###` after the loader's demotion.
- The skill consumes, by name: `claudlobby checkins --bot $BOT_ID --last --json` (READ 0), `claudlobby brief --bot $BOT_ID --json` (keys `dispatches{open,overdue,orphaned,dispatched}`, `workstreams{active,stalled}`, `reports{unacked}`, `alerts[]`, `mission`, `degraded[]`), `claudron lookup --limit 5 <project>`, `PROJECT_TIER_<SLUG>` / `PROJECT_REPOS_<SLUG>`, `gh issue list`, `$CLAUDLOBBY_ROOT/lib/checkin-record.sh`, `$CLAUDLOBBY_ROOT/lib/dispatch-task.sh --project --checkin`, and the Telegram reply tool the manager already holds for `ask`.
- Grants are narrow (cycle-1 R3): no `Bash(bash *)`, no `Bash(cat *)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkin_library.py
"""The check-in protocol (spec §9) composes both audiences from one file; the
skill (§6) is coupled to its doors by name and holds only narrow grants. The
cadence rules the protocol will supersede are NOT edited in this chunk (fork F3)."""

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


def test_the_protocol_declares_no_requires_yet():
    fm, _ = parse_frontmatter((LIB / "protocols" / "checkin.md").read_text())
    assert fm["title"] == "Check-in" and "requires" not in fm     # equipment linking is chunk 5


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
    # fork F3: retiring them is the canary's dependent variable, not chunk 1's edit
    assert "Idle silence is a bug" in (LIB / "protocols" / "proactivity-discipline.md").read_text()
    assert re.search(r"2.3 min", (LIB / "protocols" / "worker-lifecycle.md").read_text())


DOORS = ["claudlobby checkins --bot $BOT_ID --last --json", "claudlobby brief --bot $BOT_ID --json",
         "claudron lookup", "gh issue list", "lib/checkin-record.sh",
         "lib/dispatch-task.sh", "--checkin", "--project", "PROJECT_TIER_", "PROJECT_REPOS_", "PROJECT_MISSION.md"]


def test_the_skill_is_coupled_to_its_doors():
    text = (LIB / "skills" / "checkin" / "SKILL.md").read_text()
    for d in DOORS:
        assert d in text, d
    for action in ("dispatch", "ask", "nothing"):
        assert f"**{action}**" in text, action
    assert "RECORD before ACT" in text and "unavailable" in text
    assert "propose" not in text.split("## Not in this chunk")[0]   # the enum the contract accepts


def test_the_skill_grants_are_narrow():
    fm, _ = parse_frontmatter((LIB / "skills" / "checkin" / "SKILL.md").read_text())
    grants = fm["tool_grants"]
    assert not any(g.startswith("Bash(bash") or g.startswith("Bash(cat") or g == "Bash" for g in grants), grants
    assert "Bash(claudlobby *)" in grants and "Bash(gh *)" in grants
    assert any("checkin-record.sh" in g for g in grants) and any("dispatch-task.sh" in g for g in grants)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_library.py -q`
Expected: all fail on the missing files, except `test_the_cadence_rules_are_untouched_in_this_chunk` (already true — it is the guard that F3 stays honoured).

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
carrier, is a recorded communication — the plane recreates every occurrence.

## Manager

**The check-in is yours, not the operator's.** When you reach a natural idle point
(and, once the `manager-checkin` job is armed, when it injects `/checkin`), run it:
read the SSOT, decide one project and one action, record the decision, then act.
The operator never sees a check-in; they see only what the surfacing judgment
decides they should.

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
project's rigor tier in the message; point to it. **Held asks are not nagged:** two
open, unanswered asks mean you proceed on your best tier-gated judgment or wait
quietly — never a third.

## Worker

**One thin line on start, done and blocked — to Telegram where you are configured
for it**, plane-only where you are not. Detail goes to your manager and the plane
through `$CLAUDLOBBY_ROOT/lib/report-back.sh`, never to the channel. Shape —
`<verb>: <what>`: `start: #123 price feed` · `done: #123 PR #130` · `blocked: #123
needs the API key`. Where this protocol composes beside an older cadence rule, this
section governs; the older rules retire when the check-in ships as a default.
```

- [ ] **Step 4: The skill**

```markdown
---
name: checkin
description: "The idle-manager check-in: read the SSOT (the plane through checkins and brief, Claudron, the mission with each project's tier and repos, the GitHub backlog), decide ONE project and ONE action, record the decision BEFORE acting, and let the surfacing judgment decide whether the operator hears anything at all. Silence is the default."
argument-hint: "[--dry-run]"
tool_grants:
  - "Bash(claudlobby *)"
  - "Bash(claudron *)"
  - "Bash(gh *)"
  - "Bash(*lib/checkin-record.sh *)"
  - "Bash(*lib/dispatch-task.sh *)"
  - "Read"
---

# Check-in

Your own re-engagement cycle. Nobody is watching it; what they may see is only what
the surfacing judgment (DECIDE, below) lets through. **Every read goes through a
named door and every write through a named door** — never a hand-rolled query,
never a hand-built plane envelope. That coupling is what makes your reasoning
inspectable (`claudlobby checkins`) and the edges deterministic.

`$BOT_ID`, `$FLEET_NAME`, `$CLAUDLOBBY_ROOT` and the `PROJECT_*` map come from your
`bot.conf`. `SLUG` below is a project key upper-cased with `-` → `_`.

## Arguments

Parse `$ARGUMENTS`:
- `--dry-run`: do every READ and the DECIDE, print the decision JSON, validate it
  with `checkin-record.sh --dry-run`, record nothing, act on nothing.

## READ — in order, all cheap, all SSOT

A step that fails is **recorded, never guessed around**: add its name to
`inputs_seen.unavailable` and continue. A count you could not measure is `null`,
never `0`.

0. **The previous check-in** — `claudlobby checkins --bot $BOT_ID --last --json`.
   Its `record.inputs_seen` is *the state at the last check-in*; its `action` and
   `record.raise` are what was done and what was held. Keep its `checkin_id` for
   `prev_checkin_id`. None → this is the first; `prev_checkin_id: null`. (rc 3 means
   the plane is unreachable: record `checkins` as unavailable — you will be limited
   to `ask | nothing` below.)
1. **The fleet's present** — `claudlobby brief --bot $BOT_ID --json`: `dispatches`
   (`open` / `overdue` / `orphaned` / `dispatched`, each row with `escalated`,
   `nudged`, `last_progress_at`), `workstreams` (`active`, `stalled`),
   `reports.unacked`, `alerts` (last 24h critical), `mission`. Honour `degraded[]`: a
   field named there is labeled or omitted — treat its section as unavailable, never
   as zero.
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
alignment and backlog depth weigh in; the `rationale` records the weighing.

| action | when | through |
|---|---|---|
| **dispatch** | an open or backlog item fits an idle worker; you choose the worker and the rationale says why | `bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --project <key> [--repo <owner/name>] [--ref <issue-url>] --checkin "$ck_id" <worker> "<task>"` — `--project` is the well-defined bar (a projects.yaml key); `--checkin` joins the dispatch to this decision |
| **ask** | the surfacing judgment (below) concludes the operator should hear something — a fork only they can resolve, or the backlog holds nothing worth starting ("ask for tasks") | one Telegram post, with the reply tool you already hold, shaped by the check-in protocol: one line, one ask with named options, one pointer. It is recorded as your communication by the outbound hook |
| **nothing** | all work in flight, nothing worthwhile — **recorded**, so "checked and chose nothing" is a fact, not silence | — |

**The surfacing judgment.** Its default answer is **no**. Weigh, at minimum: does
this genuinely need a human (a `requires-approval` boundary in the mission, a tier
that mandates sign-off, conflicting priorities)? · would the operator want to know (a
deliverable ready, a blocker that stalls the fleet, a failure with cost)? · what
changed since they were last told (the delta against step 0 and the last post's
`held`)? · has enough accumulated to be worth one message? · are two asks already
open and unanswered (then proceed on your best tier-gated judgment or wait quietly
— never a third)? · what Claudron says about how the operator wants to be engaged ·
the urgency floor (a `blocked` that stalls everything breaks through regardless).
Record the judgment in `raise`: `decided`, `reason` (always, in both directions),
and what you `held`.

**Degraded inputs narrow the actions to `ask | nothing`** — never dispatch from
partial information.

## RECORD before ACT

Build the decision as JSON (schema 1) and record it FIRST — the decision exists even
if the action then fails:

```bash
ck_id=$(cat <<'EOF' | bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh"
{"prev_checkin_id": <from step 0, or null>,
 "inputs_seen": {"open_tasks": N, "stalls": N, "unacked": N, "issues_considered": N,
                 "knowledge_hits": N, "unavailable": []},
 "delta": {"tasks_opened": N|null, "tasks_completed": N|null, "stalls_appeared": N|null,
           "stalls_cleared": N|null, "issues_new": N|null, "messages_new": N|null, "held_pending": N|null},
 "action": "dispatch|ask|nothing",
 "project_key": "<slug or null>",
 "rationale": "<your words, <= 600 chars: the weighing, the worker, the why>",
 "raise": {"decided": false, "reason": "<why it surfaced, or why not>", "held": []}}
EOF
)
```

The door prints the `checkin_id`. rc 2: the decision was refused — every reason is
on stderr; fix and re-record. rc 3: the plane did not record it — do **not** act on
an unrecorded `dispatch`; say so in your next justified post. Then ACT through the
door in the table, passing `--checkin "$ck_id"` to `dispatch-task.sh`.

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

Run: `./.venv/bin/pytest tests/test_checkin_library.py tests/test_skill_ref_resolution.py tests/test_dispatch_type.py -q`
Expected: all pass (`test_skill_ref_resolution.py` resolves backticked `/checkin` references library-wide; `test_dispatch_type.py` parses the protocol docs' `[BOTCOMMAND]` list — untouched). Then the grant shapes on a real validate, rc read first, never piped:

```bash
./.venv/bin/claudlobby --root "$(pwd)" validate > "$TMPDIR/validate.out" 2>&1; echo "rc=$?"
grep -i 'checkin' "$TMPDIR/validate.out" || echo "no checkin findings"
```
Expected: `rc=0` (or the repo's pre-existing rc on main — compare) and `no checkin findings`.

- [ ] **Step 6: Commit**

```bash
git add library/protocols/checkin.md library/skills/checkin/SKILL.md tests/test_checkin_library.py
printf '%s\n' 'feat(library): the check-in protocol (additive) and the /checkin skill' '' 'One file, two sections (Manager: the surfacing judgment; Worker: one thin line).' 'The skill is a thin reasoning wrapper coupled to its doors by name, actions' 'dispatch | ask | nothing, grants by path (no shell grant). The cadence rules it' 'will supersede are untouched here (fork F3). Spec §6, §9.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c4.txt"
git commit -q -F "$TMPDIR/c4.txt" && git log --oneline -1
```

---

### Task 5: The empirical gate — the record door and the read door on a real plane through the harness

**Files:**
- Modify: `lib/validate-bot-change.sh` (append a scenario block before the harness summary; helpers `val_plane_ready`, `val_sql` at `:163,171`, `harness_check` at `lib-common.sh:4634`, `$VAL_CLI`, `$VAL_REPO`, `$LIB_DIR`, `$ROOT`)

**Interfaces:** consumes Tasks 1 and 3. The dispatch join is proven by `test_dispatch_checkin_appends_the_join_row_to_the_same_batch` on a real plane through the real shim (Task 2), so this block does not re-prove it; it proves the two doors work **under the identity env a manager session carries and through the installed CLI**, and that the read door's negative case is a real negative (a positive control first — cycle-1 B12).

- [ ] **Step 1: Append the block**

```bash
# ===========================================================================
# manager check-in chunk 1 -- the record door and the read door, end to end
# on a real plane. Unit tests pin the contract and the envelopes; what only
# running the real doors proves is that a decision LANDS as a row the read
# door can join, through the real shim, under the identity env a manager
# session carries (BOT_ID, FLEET_NAME), and that "not listed" is a real
# negative -- the positive control runs FIRST, so an unreachable read door can
# never read as a clean answer.
# ===========================================================================
echo ""
echo "=== validate manager check-in: the decision lands and the read door joins it ==="
CK_FLEET="valckf"
CK_SOCKDIR=$(mktemp -d /tmp/vck.XXXXXX)        # a SHORT path: sun_path is 104 bytes on macOS
CK_SOCK="$CK_SOCKDIR/s"
val_plane_ready "$ROOT" "$CK_FLEET"
ck_decision='{"inputs_seen":{"open_tasks":0},"delta":{},"action":"nothing","project_key":null,"rationale":"harness: nothing worth starting","raise":{"decided":false,"reason":"no delta","held":[]}}'
ck_id=$(printf '%s' "$ck_decision" | env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET" BOT_ID="valckmgr" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$CK_SOCK" \
    bash "$VAL_REPO/lib/checkin-record.sh" 2> "$ROOT/ck-record.err" || true)
case "$ck_id" in ck_*) r=yes ;; *) r=no ;; esac
harness_check "checkin: the record door returns a checkin_id" "$r"
ck_row=$(val_sql "$ROOT" "SELECT json_extract(detail,'\$.action') || '|' || severity || '|' || subject_alias FROM events WHERE kind='system' AND event='checkin_decision' AND source_ref='checkin:$ck_id'")
[ "$ck_row" = "nothing|notice|bot:$CK_FLEET/valckmgr" ] && r=yes || r=no
harness_check "checkin: ...and the decision LANDED as one actor-anchored notice row (source_ref checkin:<id>, BOT_ID alias)" "$r"
ck_other=$(printf '%s' "$ck_decision" | env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET" BOT_ID="valckother" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$CK_SOCK" \
    bash "$VAL_REPO/lib/checkin-record.sh" 2>> "$ROOT/ck-record.err" || true)
# The CLI reaches the plane through <root>/lib/dispatch-overdue.py -- linked for
# THIS scenario and removed after it (the #1481 neighbour rule at :652/:682).
ln -sfn "$LIB_DIR" "$ROOT/lib"
CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$CK_FLEET" --json \
    > "$ROOT/ck-read.out" 2> "$ROOT/ck-read.err" || true
ck_seen=$(grep -c "$ck_id" "$ROOT/ck-read.out" || true)
[ "${ck_seen:-0}" -ge 1 ] && r=yes || r=no
harness_check "checkin: the read door LISTS the decision (positive control)" "$r"
CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$CK_FLEET" --bot valckmgr --last --json \
    > "$ROOT/ck-read2.out" 2> "$ROOT/ck-read2.err" || true
rm -f "$ROOT/lib"
{ grep -q "$ck_id" "$ROOT/ck-read2.out" && ! grep -q "$ck_other" "$ROOT/ck-read2.out"; } && r=yes || r=no
harness_check "checkin: ...--bot --last returns THIS manager's newest row and not the other manager's (a real negative)" "$r"
rm -rf "$CK_SOCKDIR"
```

- [ ] **Step 2: Run the harness unsandboxed and read the four lines**

Run: `bash lib/validate-bot-change.sh > "$TMPDIR/vbc.txt" 2>&1; echo "rc=$?"; grep -E 'checkin:' "$TMPDIR/vbc.txt"`
Expected: four `PASS` lines beginning `checkin:`; the harness's overall verdict unchanged from main's (run main's harness once for the baseline if unsure — a pre-existing failure in another block is out of scope and is named, not fixed). **Paste the four lines into the PR body** (Task 7).

- [ ] **Step 3: Commit**

```bash
git add lib/validate-bot-change.sh
printf '%s\n' 'test(harness): the check-in record lands and the read door joins it' '' 'validate-bot-change.sh gains the chunk-1 scenario: record -> row under a manager' 'identity env; read door lists it (positive control first), --bot --last excludes' 'the other manager (a real negative). Short socket path; $ROOT/lib linked around' 'the CLI calls like the #1481 block.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c5.txt"
git commit -q -F "$TMPDIR/c5.txt" && git log --oneline -1
```

---

### Task 6: `CLAUDE.md`, `CHANGELOG.md`

**Files:** `CLAUDE.md` (the `lib/` table; the `commands/` line in the package structure; `## Key Commands` → `# Operations`), `CHANGELOG.md` (`[Unreleased]`).

- [ ] **Step 1: The lib rows** — append to the `lib/` table, in the house style:

```markdown
| `checkin-record.sh` | THE write door for a manager check-in decision (manager check-in spec §7). Validates the schema-1 record through `checkin-contract.py`, mints the `ck_` id, lands ONE actor-anchored `checkin_decision` system event stamped `source_ref checkin:<id>` — the task-recheck stamp idiom, so a reader joins on the ref and the record needs no other key. **For this door the record IS the action**: rc 3 when the plane did not record (never a silent 0), rc 2 when the contract refuses (every reason named), rc 1 for usage including a missing identity — the `task-act.sh` ladder. Identity is `BOT_ID` + `FLEET_NAME`, never `BOT_NAME` (a display field). `PLANE_EMIT_DISABLED=1` is the one silencer and it says so at rc 3 |
| `checkin-contract.py` | The schema-1 decision record, stdlib (the `dispatch-overdue.py` precedent): `normalize()` lists EVERY defect rather than the first, mints `checkin_id`, keeps a `delta` count `null` when the manager could not measure it (never collapsed to 0 — an unchanged delta is the skill's argument for silence), requires `raise.reason` in both directions. Actions this chunk: `dispatch \| ask \| nothing`; `propose`/`sprint` widen the enum as schema 2 when their chunks land. Also the CLI filter the door pipes through |
```

and in the `dispatch-task.sh` row's text, one sentence: ``Since the check-in chunk: `--project <key>` stamps `project_key` on the work item and opens the envelope; `--checkin ck_<32hex>` appends a `checkin_dispatch` join row to the SAME batch as the assignment (a strict `Assignment` payload cannot carry the id).`` And in `lib-common.sh`'s row: ``Also `flag_val <flag> <value?> [<door>]` — the explicit missing-value guard every door with flags uses, because `${2:?…}` exits 0 through the EXIT trap (measured, bash 3.2.57).``

- [ ] **Step 2: The command** — in the package structure's `commands/` line add `checkins`; under `## Key Commands` → `# Operations`:

```bash
claudlobby checkins [--bot B] [--since 7d] [--last] [--json]   # the manager check-in's decisions, newest first (plane read)
```

- [ ] **Step 3: CHANGELOG** — under `[Unreleased]`, one bullet per landed piece (the record door + contract, `flag_val`, `dispatch-task --project/--checkin`, `checkins`, the protocol + skill).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md CHANGELOG.md
printf '%s\n' 'docs: CLAUDE.md rows and CHANGELOG for the check-in record and run (chunk 1)' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c6.txt"
git commit -q -F "$TMPDIR/c6.txt" && git log --oneline -1
```

---

### Task 7: The gauntlet, the real run, the PR, the merge, the deploy

The operator's standing loop for every chunk. Nothing merges without all of it, and the PR body cites each observation — claimed evidence is not evidence.

- [ ] **Step 1: Review lenses** — `/simplify`, then `/review-work`, then `/verify-completion` on the branch (the phase-finalization gate). Fold every finding as its own commit; re-run the touched test files.

- [ ] **Step 2: Committed-code mutants, in a detached worktree** — never against uncommitted code. For each: apply, run the named tests, expect ≥ 1 failure, restore with `git checkout -- <file>`. Every anchor must occur exactly once (`assert text.count(old) == 1`); a surviving mutant is a missing test — add the test, never a weaker mutant.

```python
# $TMPDIR/mut-ck1-defs.py
MUTANTS = [
    ("rationale-cap-off", "lib/checkin-contract.py",
     "elif len(rationale) > RATIONALE_MAX:", "elif False:", ["tests/test_checkin_contract.py"]),
    ("null-delta-collapsed", "lib/checkin-contract.py",
     "v = delta.get(k)\n        if v is not None and not _count(v):", "v = delta.get(k, 0) or 0\n        if not _count(v):",
     ["tests/test_checkin_contract.py"]),
    ("reason-optional", "lib/checkin-contract.py",
     "if not isinstance(reason, str) or not reason.strip():", "if reason is not None and not isinstance(reason, str):",
     ["tests/test_checkin_contract.py"]),
    ("record-swallows-failure", "lib/checkin-record.sh",
     "if [ \"${PLANE_EMIT_LAST_RC:-0}\" -ne 0 ]; then", "if false; then", ["tests/test_checkin_doors.py"]),
    ("record-uses-bot-name", "lib/checkin-record.sh",
     "BOT=\"${BOT_ID:-}\"", "BOT=\"${BOT_NAME:-${BOT_ID:-}}\"", ["tests/test_checkin_doors.py"]),
    ("flag-guard-exits-0", "lib/lib-common.sh",
     "        printf '%s: %s needs a value\\n' \"$door\" \"$flag\" >&2\n        exit 1", "        printf '%s: %s needs a value\\n' \"$door\" \"$flag\" >&2\n        exit 0",
     ["tests/test_checkin_doors.py"]),
    ("project-never-opens-gate", "lib/dispatch-task.sh",
     "|| [ -n \"$DISPATCH_WORKSTREAM\" ] || [ -n \"$DISPATCH_PROJECT\" ]; then", "|| [ -n \"$DISPATCH_WORKSTREAM\" ]; then",
     ["tests/test_checkin_doors.py"]),
    ("join-row-dropped", "lib/dispatch-task.sh",
     "\"${sup_ev:+,$sup_ev}\" \"${ck_ev:+,$ck_ev}\"", "\"${sup_ev:+,$sup_ev}\" \"\"", ["tests/test_checkin_doors.py"]),
    ("rows-by-ingest-order", "claudlobby/plane/queries.py",
     "ORDER BY {_epoch('e.occurred_at')} DESC, e.ingest_seq DESC", "ORDER BY e.ingest_seq DESC", ["tests/test_checkins_cli.py"]),
    ("last-bounded-by-since", "claudlobby/commands/checkins.py",
     "since = None if args.last else parse_since(args.since)", "since = parse_since(args.since)", ["tests/test_checkins_cli.py"]),
    ("truncated-rows-dropped", "claudlobby/plane/queries.py",
     "\" WHERE e.kind = 'system' AND e.event = 'checkin_decision'\"", "\" WHERE e.kind = 'system' AND e.event = 'checkin_decision' AND e.detail_truncated = 0\"",
     ["tests/test_checkins_cli.py"]),
    ("shell-grant-sneaks-in", "library/skills/checkin/SKILL.md",
     "  - \"Read\"\n", "  - \"Read\"\n  - \"Bash(bash *)\"\n", ["tests/test_checkin_library.py"]),
]
```

- [ ] **Step 3: The two-leg full-suite gate** — `before.txt` is Task 0's; the after leg runs on the FINAL committed tip:

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
The PR body carries, in order: what landed (one line per task); **the empirical observations** — the four `checkin:` harness lines from Task 5, the name of the real-plane dispatch-join test, and (after step 5) the `/checkin --dry-run` output; the two-leg gate result (`comm -13` empty; before/after count lines pasted); the mutant table (12 names, each with the test that killed it); the two baselines from Task 0; the spec link and the five forks (all locked). End with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Wait for CI green; a `test_boot_capture.sh` load flake is re-run, not waved through.

- [ ] **Step 5: The real run — the mandatory runtime gate for the skill and the protocol** (cycle-1 R1)

Unit tests prove the doors; only running the skill proves the READ chain (the `brief --json` key names, `claudron lookup`, `gh`, the `PROJECT_*` map) and the contract are producible by the thing that has to produce them. This needs one real manager session, so it happens on the canary manager, **before merge, from the PR branch's `lib/` and `library/`**, and it is read-only for the fleet (`--dry-run` records nothing and acts on nothing):

1. **Operator action (operator config is never edited by the executor):** the operator adds two lines to the canary leaf manager's entry in its `fleet.yaml` — `protocols: [checkin]` and `skills: [checkin]` (appended to any existing lists) — and runs `claudlobby --fleet <fleet> generate` from a checkout of the PR branch. The skill symlink is live instantly; the protocol lands at the manager's next session start (not needed for the dry run).
2. Inject the dry run into the manager's pane through the socket-aware helper, from the PR branch's checkout on the host: `"$CLAUDLOBBY_ROOT/lib/dispatch.sh" <manager> "/checkin --dry-run"` (the slash payload reaches the pane bare — `dispatch.sh:26-42`).
3. Capture from the pane (`tmux -L <socket> capture-pane -p -S -200`) and from the manager's transcript: (a) the decision JSON the model emitted, (b) the `checkin-record.sh --dry-run` rc and printed `ck_` id, (c) which of READ 0–5 answered and which landed in `unavailable`. **Paste all three into the PR body.** A refused decision (rc 2) is a finding about the skill text, fixed before merge; a READ door that the model could not drive is a finding about the skill's door names.

- [ ] **Step 6: Admin-merge with the explicit squash body** (the operator's standing authorization for gauntleted work) — `gh pr merge --squash --admin --body-file "$TMPDIR/pr-body.md"`; delete the branch.

- [ ] **Step 7: Deploy to the Mini and verify live**

Chunk 1 ships **no default, no trigger, no template change, no `bot.conf` change and no protocol edit beyond one additive file** — so `generate` composes nothing differently for any bot that does not declare `checkin`, and the deploy is a pull. `lib/` scripts are live on pull; the `checkins` subcommand lands with the editable install on pull.

```bash
ssh -o BatchMode=yes mini 'bash -s' <<'EOF'
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd ~/Projects/claudlobby
git status --porcelain | grep -q . && { echo "dirty checkout — stop"; exit 1; }
git fetch -q origin main
git pull --ff-only
for f in local/home/*/; do
  fleet=$(basename "$f")
  .venv/bin/claudlobby --fleet "$fleet" validate > "/tmp/ck-validate-$fleet.out" 2>&1; echo "validate $fleet rc=$?"
  grep -i 'checkin' "/tmp/ck-validate-$fleet.out" || echo "  no checkin findings"
  .venv/bin/claudlobby --fleet "$fleet" diff | head -5    # expected: no drift except the canary manager's new skill symlink/protocol section
done
.venv/bin/claudlobby checkins --fleet "$(basename "$(ls -d local/home/*/ | head -1)")"; echo "checkins rc=$?"
EOF
```
Expected: `validate` rc unchanged from before the pull on every fleet, no `checkin` findings, `diff` clean except the canary manager (which the operator already regenerated in step 5), and `claudlobby checkins` printing `no check-ins — fleet …` at rc 0 (an rc 3 here means the install's `lib/` predates the door — pull again). Record the result on the PR as a comment. The canary manager's first **real** `/checkin` (not a dry run) is chunk 2's first step, after the trigger exists to fire it.

---

## Self-review (run against the spec and the cycle-1 review after writing; findings folded above)

**Cycle-1 blockers → where each is resolved.** B1 (estate-wide cadence retirement) → §Scope deferred + fork F3; the deploy claim at Task 7 step 7 is now true. B2 (incomplete sweep) → F3 carries the grep-derived sweep; `test_the_cadence_rules_are_untouched_in_this_chunk` guards that chunk 1 does not half-do it. B3 (import) → Task 3 `from ._helpers import …`, standalone parser import. B4 (tuple rows) → Task 3 `open_ro` after `plane_session`. B5 (clock) → `occurred_at` order, ingest tiebreak, test rewritten. B6 (dedent fixture) → the leaf-manager test file no longer exists in this chunk (F2); the note travels to chunk 5 via the spec. B7 (exact-equality pin) → same. B8 (linked not granted) → deferred with F2; the grant-union requirement is written into spec §10 for chunk 5. B9 (degraded rule strands actions) → the skill reads no focus, so nothing is "unavailable by construction"; the enum is what the chunk ships. B10 (`${2:?}`) → `flag_val` lifted; a test passes a flag without a value. B11 (unjoinable dispatch/ask) → `--checkin` join row; `targets` deleted; asks join by alias + time (F4). B12 (vacuous gate) → positive control first, `$ROOT/lib` linked, stderr captured, deploy loop over `local/home/*/`.

**Cycle-1 risks.** R1 → Task 7 step 5 (the real dry run). R2 → F5 + spec §10 corrected. R3 → grants by path; a mutant guards it. R4 → truncated rows listed and marked; `json_type` guard. R5 → `BOT_ID`; a test and a mutant. R6 → task-act ladder. R7 → the envelope gate; a `--project`-alone test and a mutant. R8 → `--work-item` deferred (1d) with the lookup requirement noted in the spec. R9 → no template edit in this chunk. R10 → `planning.initiative` deferred (1d) with the doctor-surface requirement noted in the spec. R11, R12 → `propose` deferred (F1). R13 → `--last` ignores the window; test + mutant. R14 → the per-bot `requires` error deferred (chunk 5) with the once-per-protocol note in the spec. R15 → the missing test names removed; validate never piped. R16 → the deferral table. R17 → nullable delta; test + mutant. R18 → Task 0 step 4.

**Placeholder scan.** No TBD/TODO; every code step carries code. One step requires the executor to read a neighbouring line before editing (Task 6: the exact `dispatch-task.sh` row text to extend) — it names the file and the row, so it is a verification, not a placeholder.

**Type consistency.** `flag_val <flag> <value?> [<door>]` (Task 1) matches both doors' call sites and the stub rig. `checkin-record.sh` rc ladder 0/1/2/3 is identical in the header, the tests, the CLAUDE.md row and the skill. `CHECKIN_ROWS_SQL` binds `(fleet, fleet)` — matched in `collect_checkins`. `_Args.checkins_fleet` matches `dest="checkins_fleet"`; `_Args.limit` matches `--limit`. `parse_since` (Task 3) is the name the tests import. The two severity kinds are named identically in registries, the record door, the dispatch door and the tests. The skill's door strings match `test_the_skill_is_coupled_to_its_doors` verbatim.
