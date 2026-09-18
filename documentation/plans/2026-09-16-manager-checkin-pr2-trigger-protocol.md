---
title: "Manager Check-in — Chunk 2: The Trigger and the Protocol — Implementation Plan"
type: plan
status: draft
owner: fleet owner
created: 2026-09-16
---

# Manager Check-in — Chunk 2: The Trigger and the Protocol — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **The operator's ruling (2026-09-16).** The loop ships as four PRs with **mechanical gates only** — tests, mutants, `validate-bot-change.sh`. No evaluation apparatus: no baselines, no control fleet, no pre-registered bar, no burn-in verdict. **No review cycles.** Findings become targeted fixes or named forks. Chunk 1 (`documentation/plans/2026-09-14-manager-checkin-chunk1-contract.md`) merges and deploys first; this plan starts from a tree where the contract, `checkin-record.sh`, `claudlobby checkins`, `dispatch-task.sh --project/--checkin` and `library/skills/checkin/SKILL.md` are on `main`.

## Goal

After this PR merges and deploys, the canary leaf manager — the one an operator equipped by hand in chunk 1 — **stops needing to be asked.** A composed fleet timer wakes every 15 minutes, finds that manager idle and equipped (the `checkin` skill symlink is composed under its `.claude/skills/`), confirms through a plane read that it has not already checked in inside the last 45 minutes, and injects `/checkin` into its pane through the socket-aware `lib/dispatch.sh`. The manager then does what chunk 1 taught it to do: read, decide one action, record a `checkin_decision`, act. The same deploy puts `library/protocols/checkin.md` in that manager's composed context, so when the check-in decides the operator should hear something, the message has a fixed shape — one status line, one ask with named options, one pointer — and silence is the default everywhere else. The job ships **dormant**: no fleet on the estate behaves differently until someone writes `enroll: true` and runs `lib/setup-fleet`.

## Architecture

```
<prefix>.manager-checkin timer  (interval 900, enroll: false, composed-but-dormant)
  └─ lib/manager-checkin.sh <fleet>
       roster = declared_bots_strict           (the LOUD door; an empty roster injects nothing)
       per declared bot of THIS fleet:
         bot_is_manager(bot_dir)?              ── no ──▶ silent skip
         .claude/skills/checkin resolves?       ── no ──▶ silent skip   (the equip gate)
         check_tmux_session?                    ── no ──▶ checkin_skipped reason=session_down
         bot_is_busy?                           ── yes ─▶ checkin_skipped reason=busy
         plane: any checkin_triggered for this
           manager since (now - MIN_GAP_S)?
             rc 3 unreachable ────────────────────────▶ log only, DO NOT FIRE (fail closed)
             a row ───────────────────────────────────▶ log only (derivable; no plane row)
             nothing ─▶ dispatch.sh <bot> "/checkin"
                          ok   ──▶ checkin_triggered
                          fail ──▶ checkin_skipped reason=send_failed   (next beat retries)
```

The rate limit is **a plane read, not a state file** — `task-recheck`'s rule (`commands/task.py:452-467`: *"no timer state file to lose or to lie"*). The read goes through the shipped stdlib door `lib/plane-lookup.py --events --fleet F --bot B --type checkin_triggered --since <iso>`, whose `fleet_events` reader (`lib/plane-readers.py:916-928`) already filters by bot alias, event type and instant, and whose `_with_plane` ladder already answers **rc 3 for unreachable, empty for nothing** (`lib/plane-lookup.py:60-78`). No new read surface, no new SQL.

The events ride `emit_fleet_event` (`lib/lib-common.sh:1392-1430`), which anchors them actor-first on `bot:<fleet>/<bot_id>` — the same door `briefing-trigger.sh:45,105,109` uses for a trigger's own record, and the same door `fleet_events` reads back. That closure is the whole reason the rate limit needs nothing new.

## Scope

**In this chunk:** `lib/manager-checkin.sh`; the `manager-checkin` fleet job in `claudlobby/system.yaml` (`interval: 900`, `enroll: false`) and its `Switch` row; `library/protocols/checkin.md` (spec §9) and its tests; the `project:` row in `library/protocols/dispatch.md`'s envelope field table and its recipe flag; the `validate-bot-change.sh` scenario (throwaway manager → idle → `/checkin` injected → `checkin_decision` lands); two severity registrations; README counts, the CLAUDE.md lib row, the regenerated switch doc blocks, CHANGELOG; the gauntlet, the merge and the deploy (pull → generate → restart the canary manager once → arm the job → one observed beat).

**Not in this chunk:** anything evaluative (no baseline, no control fleet, no pre-registered bar, no burn-in verdict — the ruling). The default registry line, `requires:` frontmatter linking, the `leaf-manager` role and the cadence retirement (chunk 4 / spec §10, §12.4 — `proactivity-discipline.md` and `worker-lifecycle.md` are pinned **untouched** here). `checkins --summary` / `--limit` and the outcome join (chunk 3 / spec §12.3). `propose`, the intake store, sprint and focus (features, later). Backoff-on-`nothing` (spec §16 Q1 names it as cost hygiene; it needs a second plane read and no gate asks for it — named here so it is a deferral, not an omission). A `communication` + transmission row for the injection: `briefing-trigger.sh` records one, and this trigger deliberately does not — `checkin_triggered` already carries the only fact the rate limit and chunk 3 read, and a second record of one beat is two rows that can disagree.

## Decision Forks

**F1 — the `BOT_NAME` residue stays out, and the reason is measured.** Spec §12.2 parks the residue here *"if the trigger's timer env needs it"*. It does not. The trigger records through `emit_fleet_event`, which calls `plane_armed emit_fleet_event` with **no** `--require-bot` (`lib/lib-common.sh:1416`), and derives its subject from its own `bot_id` argument (`:1402,1420`). The only two callers of `--require-bot` on the tree are `lib/keepalive.sh:78` and `lib/tg-post.sh:69`, neither of which this chunk runs. Changing `keepalive.sh:102`'s heartbeat subject from `$BOT_NAME` to `${BOT_ID:-$BOT_NAME}` would move the presence-join alias for **every bot on the estate whose display name differs from its id**, on pull, with no consumer in this chunk asking for it — the "pull a mechanism forward four chunks" defect the re-cut ladder exists to kill (spec §12 preamble). So: the trigger is **built not to need it** (its header says why, and a test pins that its rows carry `BOT_ID`, never the directory name), and the residue is carried as an open fork for whoever touches presence next (tracked in #1563).

**F2 — one skip event with a `reason`, not four event kinds.** Spec §5 names `checkin_skipped_down`, `checkin_skipped_busy`, `checkin_skipped_ratelimit`, `checkin_skipped_unreachable`. Those read as four *reasons*, and the repo's own trigger precedent is one event carrying one (`briefing_deferred` + `briefing_data()`'s `reason`, `lib/briefing-trigger.sh:36,42`). One kind, one severity line, and chunk 3's "skip reasons" becomes a GROUP BY rather than a union. **Two of the four are not recorded to the plane at all**, each for its own reason: *ratelimit* is a pure function of the `checkin_triggered` rows the plane already holds and would otherwise write two rows per hour per manager forever (at `interval: 900` / gap 2700, exactly two ticks in three are rate-limited); *unreachable* cannot be recorded by definition — the plane is the thing that could not be reached. Both land in the log, and the header says so.

**F3 — the gap default is 2700 and it rides a FLAG, not the fleet `.env`.** Spec §5 says `CHECKIN_MIN_GAP_S` default **2700**; §16 Q1 says "60-min gap". §5 is the normative trigger section and names the variable and the number, so 2700 stands. A fleet timer's env is **closed** (`#1383`; `composer.py:4055-4060`), so a `.env` value could never reach the unit — the retune carrier is the job's script line, `data-sweep`'s documented pattern (`claudlobby/system.yaml:455-457`: *"A fleet changes retention by overriding this job's script line"*). Hence `manager-checkin.sh <fleet> [--min-gap-s N]`, with `CHECKIN_MIN_GAP_S` honoured for hand runs.

**F4 — arming is ENROLL_FLEET, so there is no `switch_is_on` self-gate.** Spec §5's arming paragraph mixes two carriers: it names `carrier=ENROLL_FLEET` *and* an `Environment=` stamp read by `switch_is_on`. Those are different mechanisms — `FLEET_JOB_ARMING` stamps only switches that declare an `env` (`composer.py:3967`, `switches.jobs_with_env`), and an ENROLL_FLEET row declares none. Take the enroll carrier (`code-audit-sweep`, `weekly-worker-restart`): the unit is **composed but dormant**, its basename lands in the `DORMANT` manifest, and `lib/setup-fleet` skips it until a fleet writes `defaults.jobs.manager-checkin.enroll: true` (`composer.py:4013-4017, 4128-4139`). A `switch_is_on` gate would be a second flag the registry does not declare. The per-bot gate is the composed skill symlink; the per-fleet gate is enrollment; there is no third.

## Global Constraints

Every task's requirements include these. Carried from chunk 1 where they still bind.

- **The repo is PUBLIC.** No PII, real chat ids, handles, tokens, tailnet names or fleet-specific paths in any committed asset, test, fixture or commit message. Never `@`-mention a bot name in GitHub-bound text. The canary fleet, its manager and the host's install root live only in `$CK_FLEET` / `$CK_MGR` / `$MINI_ROOT`, set in `$OUT/env.sh` (Task 0) and never written into this plan, a commit or a PR body.
- **bash 3.2 target.** `set -euo pipefail`; source `lib-common.sh`; quote every variable; `printf '%s'` for values. **No apostrophes in comments inside a command substitution** (`tests/test_bash_parse.py` gates `lib/` and every `library/**/*.sh`). **Never guard a flag value with `${2:?…}`** — an expansion fault exits rc 0 through lib-common's EXIT trap; parse flags inline with an explicit empty check (this trigger's own form; `lib/dispatch-task.sh:94-101` documents the trap).
- **The ERR trap fires INSIDE a command substitution.** `install_error_trap` arms `set -E` plus an ERR trap that lands a `critical` `script_error` row, and bash fires it for a failing command inside `$( )` whatever surrounds it. **Every call that EXPECTS a nonzero child runs as a top-level `if` pipeline into a file** (form D: `if cmd … > "$tmp"; then`), never `if out=$(cmd)`. Two calls in this chunk are in that class: the rate-limit read (rc 3 is normal) and `declared_bots_strict` (rc 1 is normal).
- **New `system` event kinds need NO migration and NO contract change.** Register severity with one line per kind in `claudlobby/plane/registries.py:54-123`.
- **The plane is always on.** `plane_armed` is opt-OUT; `PLANE_EMIT_DISABLED=1` is the only silencer.
- **Events from bash go through `emit_fleet_event <type> <source> <data_json> <bot_dir> <bot_id>`** — actor-anchored `bot:<fleet>/<bot_id>`, **`BOT_ID`, never `BOT_NAME`** and never the directory name. `json_escape` every interpolated value.
- **Doors' rc ladder:** 0 acted · **2 usage** (this door's only refusal — it takes no other rc, because §14 says every operating path exits 0) · never a nonzero for a bot it chose not to inject into.
- **Read doors:** unreachable ≠ empty. `plane-lookup.py` rc 3 is *the question could not be answered*, and for a **spending** action that means DO NOT FIRE (spec §5 step 4, §14).
- **Line numbers** are as of `main` @ `9ac252f` **plus chunk 1's merge**. Re-anchor by the symbol named beside a line, never by the number; where chunk 1 touched a file (`registries.py`, `README.md`, `CLAUDE.md`, `validate-bot-change.sh`, `tests/test_checkin_library.py`) re-read it before editing.
- **Tests run unsandboxed; the baseline is red** (~46 failed / 2 errors on macOS, chunk 1's reading). Under a sandbox `mktemp -d` fails and ~250 bash-door tests fail spuriously. The gate is **names + counts** (Task 7), never `pytest | grep`; redirect, read `$?`, then read the file.
- **Test command form:** `./.venv/bin/pytest tests/<file>.py -q` from `$WT`, whose `.venv` Task 0 creates.
- **Operator config (`local/<fleet>/fleet.yaml`) is REPORTED, never edited by the executor**, and **no unmerged code ever runs on the live host.**
- **Commits:** message via `git commit -F <file>`; end every message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File structure

**Create**
| Path | Responsibility |
|---|---|
| `lib/manager-checkin.sh` | The beat. `manager-checkin.sh <fleet> [--min-gap-s N]`; roster → manager gate → equip gate → session gate → busy gate → plane rate limit → `dispatch.sh <bot> "/checkin"` → `checkin_triggered`. rc 2 usage only; every other path exits 0. |
| `library/protocols/checkin.md` | The protocol (spec §9): preamble with the precedence sentence; `## Manager` (silence is the default, the surfacing judgment, the fixed post shape, the two-ask rate limit through `claudlobby checkins --bot $BOT_ID --since 7d --raised`); `## Worker` (one thin line). No `requires:`, no self-fire clause. |
| `tests/test_manager_checkin.py` | The trigger's gates and throttles (stub-lib style, `tests/test_briefing_trigger.py`'s shape), plus the composed job and the switch row. |

**Modify**
| Path | Change |
|---|---|
| `claudlobby/system.yaml` (`defaults.jobs`, beside `task-recheck`) | `manager-checkin: { enroll: false, script: "$CLAUDLOBBY_ROOT/lib/manager-checkin.sh", interval: 900, type: oneshot }` + the comment block naming what it does and why it is dormant. |
| `claudlobby/switches.py` (`SWITCHES`) | one `Switch` row: `key="manager-checkin"`, `scope=FLEET_JOB`, `polarity=OPT_IN`, `carrier=ENROLL_FLEET`, `job="manager-checkin"`, `plane=True`, `why_opt_in=…`, `what=…`. |
| `claudlobby/plane/registries.py:54-123` | two severity lines: `"checkin_triggered": "notice"`, `"checkin_skipped": "notice"`. |
| `library/protocols/dispatch.md` (the key-value table, `:27-35`; the tracked-dispatch recipe, `:126-133`) | the `project:<key>` row and the `--project <key>` flag in the recipe. |
| `tests/test_switches.py:98-100` | add `"manager-checkin"` to the opt-in allowlist (a visible test edit is the point of that allowlist). |
| `tests/test_system_defaults.py:713-722` | add `"manager-checkin"` to `_ALL_JOB_NAMES`. |
| `tests/test_checkin_library.py` (append) | the five protocol tests and the two `dispatch.md` tests. |
| `lib/validate-bot-change.sh` (append a block before the summary) | the end-to-end beat scenario. |
| `README.md:145-146` | `39 protocols` → **40**; `92 bash lifecycle scripts` → **93** (skills unchanged at 54). Measured with `tests/test_readme_library_counts.py`'s own functions. |
| `documentation/system-yaml-schema.md`, `documentation/fleet-yaml-schema.md`, `documentation/architecture/observable-plane.md` | regenerate the switch block: `claudlobby doctor --switches --markdown`. |
| `CLAUDE.md` (lib table), `CHANGELOG.md` | the `manager-checkin.sh` row; one `[Unreleased]` bullet per landed piece. |

**Sizing:** Task 0 S · Task 1 L · Task 2 M · Task 3 M · Task 4 S · Task 5 M · Task 6 S · Task 7 L.

---

### Task 0: Worktree, venv, the before leg

**Files:** none in the repo. Produces `$OUT/env.sh`, `$OUT/before.txt`, `$OUT/run_before.txt`, `$OUT/collect-lib-before.txt`.

- [ ] **Step 1: Worktree, venv, env.sh** — copy chunk 1's Task 0 step 1 block **verbatim** with `ck1-out` → `ck2-out` and the branch `checkin/chunk2-trigger` (off `origin/main` **after** chunk 1 has merged), and keep its `no_names` identifier gate and its `CK_FLEET` / `CK_MGR` / `MINI_ROOT` guards unchanged — do not restate them here, and do not write any of those values into a file inside `$WT`.

Run: `git -C "$WT" log --oneline -1; ls "$WT/lib/checkin-record.sh" "$WT/library/skills/checkin/SKILL.md"`
Expected: chunk 1's squash commit, and both files present. **If either is missing, stop** — this plan starts after chunk 1.

- [ ] **Step 2: The before leg, names + counts (unsandboxed)**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck2-out/env.sh"
./.venv/bin/pytest --tb=no -ra > "$OUT/run_before.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$OUT/run_before.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$OUT/before.txt"
tail -1 "$OUT/run_before.txt"
./.venv/bin/pytest --collect-only -q tests/test_checkin_library.py > "$OUT/collect-lib-before.txt"; echo "rc=$?"; tail -1 "$OUT/collect-lib-before.txt"
```
Expected: `rc=1` (the red baseline), a count line, and the pre-edit collected count of `test_checkin_library.py` — Task 7's arithmetic needs it because this chunk **appends** to that file rather than creating it.

---

### Task 1: `lib/manager-checkin.sh` — the beat and its throttles

**Files:** Create `lib/manager-checkin.sh`, `tests/test_manager_checkin.py`. Modify `claudlobby/plane/registries.py`.

**Interfaces:** consumes `declared_bots_strict`, `bot_is_manager`, `bot_conf_get`, `tmux_socket_for_bot`, `check_tmux_session`, `bot_is_busy`, `emit_fleet_event`, `epoch_to_iso_utc`, `safe_mktemp`, `ts_iso`, `setup_log_dir`, `install_error_trap` (all `lib/lib-common.sh`); `lib/dispatch.sh` (slash-aware — the leading `/checkin` reaches the pane as its first characters, `dispatch.sh:36-40`); `lib/plane-lookup.py --events` (the rate-limit read).

- [ ] **Step 1: Write the failing tests**

`tests/test_manager_checkin.py`, in `tests/test_briefing_trigger.py`'s shape: copy the real script next to a stub `lib-common.sh`, a stub `dispatch.sh` that captures `<session>\t<message>`, and a stub `plane-lookup.py` steered by env. Helper `_run(tmp_path, *, bots, env_extra)` builds a roster file, the bot dirs and their `bot.conf`s, then runs the real script with `CLAUDLOBBY_ROOT` at `tmp_path`. Each test asserts ONE fact.

| test | asserts |
|---|---|
| `test_an_equipped_idle_manager_gets_slash_checkin_in_its_own_session` | the dispatch capture is exactly `<dir-name>\t/checkin`, and one `checkin_triggered` event was emitted |
| `test_the_plane_row_is_anchored_on_BOT_ID_not_the_directory_name` | dir `mgrdir`, `BOT_ID=lead`: the emit call's bot argument is `lead` while the tmux session is `mgrdir` (F1's pin) |
| `test_a_worker_is_never_injected_into` | `bot_is_manager` false → no dispatch **and no event** |
| `test_a_manager_without_the_composed_skill_symlink_is_skipped_silently` | no `.claude/skills/checkin` → no dispatch, no event, rc 0 (an un-equipped manager must not write a row every 15 minutes) |
| `test_a_dangling_skill_symlink_counts_as_unequipped` | the symlink exists but its target does not → treated as absent (`-e` follows; this IS the un-equip path: drop the protocol, regenerate, the injection stops) |
| `test_a_manager_whose_session_is_down_is_recorded_not_injected` | `checkin_skipped` with `reason` `session_down`; no dispatch |
| `test_a_busy_manager_is_never_injected_into_mid_turn` | `bot_is_busy` true → `checkin_skipped` `reason=busy`, no dispatch (the `briefing-trigger.sh:63-65` rule) |
| `test_a_recent_trigger_inside_the_min_gap_suppresses_the_beat` | the stub read prints a row → no dispatch, **and no plane row for the skip** (F2: derivable) |
| `test_the_rate_limit_read_names_the_manager_the_type_and_the_window` | the stub records its argv: `--events --fleet <f> --bot lead --type checkin_triggered --since <iso>`, and the iso is within 2s of `now - 2700` |
| `test_the_min_gap_is_overridable_by_flag_and_by_env` | `--min-gap-s 60` and `CHECKIN_MIN_GAP_S=60` each move the `--since` instant; the flag wins over the env |
| `test_a_min_gap_that_is_not_a_number_is_a_usage_refusal_at_rc_2` | `--min-gap-s later` → rc 2, nothing dispatched |
| `test_an_unreachable_plane_does_not_fire` | the stub read exits 3 → no dispatch, no `checkin_triggered`, no plane row; the log names `unreachable`; script rc 0 (fail closed for a spending action) |
| `test_a_failed_send_records_no_trigger_so_the_next_beat_retries` | stub `dispatch.sh` rc 1 → `checkin_skipped` `reason=send_failed`, **no** `checkin_triggered` (a trigger row for a beat that never landed would rate-limit the retry on a lie) |
| `test_a_bot_of_another_fleet_is_not_touched` | roster carries two fleets; only the named fleet's manager fires |
| `test_an_empty_roster_injects_nothing` | `declared_bots_strict` prints nothing → no dispatch, rc 0 (empty means *do nothing*; `bot_in_fleet` is **not** used — it inverts an empty roster into "every directory is declared", CLAUDE.md #1146) |
| `test_a_bad_sibling_manifest_is_disclosed_and_the_good_fleet_still_fires` | the roster door exits 1 with bad lines **and** good rows → the bad lines reach stderr, the good manager still fires, rc 0 |
| `test_no_fleet_named_is_a_usage_refusal_at_rc_2` | no argument and no `CLAUDLOBBY_FLEET` → rc 2, nothing dispatched |
| `test_the_trigger_reads_the_plane_through_the_shipped_door_not_sqlite` | source assertion: `plane-lookup.py` appears, `sqlite3` does not |
| `test_neither_nonzero_expecting_call_runs_in_a_command_substitution` | source assertion: the `plane-lookup.py` call and the `declared_bots_strict` call each appear as an `if …` head, and neither appears inside `$(` (the ERR-trap rule; a substitution here lands a `critical` `script_error` for a normal rc 3) |

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_manager_checkin.py -q > "$TMPDIR/t1.txt" 2>&1; echo "rc=$?"; tail -3 "$TMPDIR/t1.txt"`
Expected: rc 1, every test failing on the missing script.

- [ ] **Step 3: The trigger**

The gate ladder IS the specification, so it is written out; the header prose is the executor's, following `task-recheck.sh:1-42`'s house style (what it does, what it will not do, why each refusal is the shape it is).

```bash
#!/bin/bash
# manager-checkin.sh -- the check-in beat. The composed `<prefix>.manager-checkin`
# FLEET timer execs this; the fleet arrives as $1 the way `task-recheck.sh` takes it.
#
# Usage: manager-checkin.sh <fleet> [--min-gap-s N]
#   rc 0 always, on every operating path (spec section 14) -- a manager it chose
#   not to wake is not a failure. rc 2 is the ONE refusal: a malformed call.
#
# ARMING is enrollment, not a flag: `defaults.jobs.manager-checkin.enroll: true`
# in fleet.yaml, then generate + lib/setup-fleet. The PER-BOT gate is the
# composed `checkin` skill symlink -- that is how a worker, a coordinator and an
# opted-out manager are all excluded, and how an operator un-equips one.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
[ $# -gt 0 ] && shift
MIN_GAP_S="${CHECKIN_MIN_GAP_S:-2700}"
while [ $# -gt 0 ]; do
    case "$1" in
        --min-gap-s)
            if [ -z "${2:-}" ]; then
                printf 'manager-checkin: --min-gap-s needs a value\n' >&2; exit 2
            fi
            MIN_GAP_S="$2"; shift 2 ;;
        *) printf 'manager-checkin: unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done
if [ -z "$FLEET" ]; then
    printf 'manager-checkin: no fleet named (usage: manager-checkin.sh <fleet> [--min-gap-s N])\n' >&2
    exit 2
fi
case "$MIN_GAP_S" in ''|*[!0-9]*)
    printf 'manager-checkin: --min-gap-s takes whole seconds, got: %s\n' "$MIN_GAP_S" >&2; exit 2 ;;
esac

ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT="$ROOT"
LOG="${MANAGER_CHECKIN_LOG:-$ROOT/logs/manager-checkin.log}"
setup_log_dir "$LOG"
TS="$(ts_iso)"
SINCE="$(epoch_to_iso_utc "$(( $(date +%s) - MIN_GAP_S ))")"

ck_skip() {   # <bot_dir> <bot_id> <reason> <log note>
    echo "$TS SKIP $2 -- $4" >> "$LOG"
    emit_fleet_event checkin_skipped manager-checkin \
        "$(printf '{"bot":"%s","reason":"%s"}' "$(json_escape "$2")" "$3")" "$1" "$2"
}

ROSTER="$(safe_mktemp)"; BAD="$(safe_mktemp)"; HITS="$(safe_mktemp)"
# Form D, both calls: rc 1 here means SOME manifest was bad, and the good rows
# still printed -- a disclosure, never a reason to stop. A substitution would
# fire the ERR trap and record a critical row for a normal outcome.
if declared_bots_strict "$BAD" > "$ROSTER"; then :; else
    [ -s "$BAD" ] && sed 's/^/manager-checkin: roster: /' "$BAD" >&2 || true
fi

while IFS="$(printf '\t')" read -r bot bot_fleet bot_dir; do
    [ -n "$bot" ] || continue
    [ "$bot_fleet" = "$FLEET" ] || continue
    [ -d "$bot_dir" ] || continue
    bot_is_manager "$bot_dir" || continue                      # silent: a worker
    [ -e "$bot_dir/.claude/skills/checkin" ] || continue       # silent: not equipped
    bot_id="$(bot_conf_get "$bot_dir" BOT_ID "$bot")"
    socket="$(tmux_socket_for_bot "$bot_dir" 2>/dev/null || true)"
    if ! check_tmux_session "$bot" "$socket"; then
        ck_skip "$bot_dir" "$bot_id" session_down "session not alive"; continue
    fi
    if bot_is_busy "$socket" "$bot" "$bot_dir"; then
        ck_skip "$bot_dir" "$bot_id" busy "mid-turn"; continue
    fi
    # THE RATE LIMIT IS A PLANE READ (task-recheck rule: no timer state file to
    # lose or to lie). Unreachable is NOT empty: a money-spending action must
    # not run blind, so rc 3 skips without firing and without a row -- the plane
    # is the thing that could not be reached.
    if python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$ROOT" --events \
            --fleet "$FLEET" --bot "$bot_id" --type checkin_triggered \
            --since "$SINCE" > "$HITS" 2>> "$LOG"; then
        if [ -s "$HITS" ]; then
            echo "$TS SKIP $bot_id -- checked in within ${MIN_GAP_S}s" >> "$LOG"; continue
        fi
    else
        echo "$TS SKIP $bot_id -- plane unreachable, not firing" >> "$LOG"; continue
    fi
    if "$LIB_DIR/dispatch.sh" "$bot" "/checkin"; then
        echo "$TS DISPATCH $bot_id -- /checkin sent" >> "$LOG"
        emit_fleet_event checkin_triggered manager-checkin \
            "$(printf '{"bot":"%s","min_gap_s":%s}' "$(json_escape "$bot_id")" "$MIN_GAP_S")" \
            "$bot_dir" "$bot_id"
    else
        ck_skip "$bot_dir" "$bot_id" send_failed "dispatch failed"
    fi
done < "$ROSTER"
exit 0
```

Then the two severity lines in `claudlobby/plane/registries.py`, beside `briefing_deferred`:

```python
    # manager check-in (spec section 5): the beat fired, or it did not and why.
    # Ratelimit and unreachable are deliberately NOT rows -- the first is
    # derivable from checkin_triggered, the second cannot reach the plane.
    "checkin_triggered": "notice",
    "checkin_skipped": "notice",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run (unsandboxed): `./.venv/bin/pytest tests/test_manager_checkin.py tests/test_bash_parse.py -q > "$TMPDIR/t1b.txt" 2>&1; echo "rc=$?"; tail -3 "$TMPDIR/t1b.txt"`
Expected: rc 0. `test_bash_parse.py` is run here because it parametrizes over `lib/*.sh` and a stray apostrophe inside a `$( )` comment is a bash-3.2 parse failure nothing else catches.

- [ ] **Step 5: Commit** — `feat(checkin): the beat — manager-checkin.sh injects /checkin into an idle, equipped manager` with the gate ladder and the two fail-closed rules in the body.

---

### Task 2: The fleet job, the switch row, the composed unit

**Files:** Modify `claudlobby/system.yaml`, `claudlobby/switches.py`, `tests/test_switches.py`, `tests/test_system_defaults.py`; append to `tests/test_manager_checkin.py`.

**Interfaces:** `compose_fleet_timers` (`composer.py:4000-4139`) emits the unit and the `DORMANT` manifest; `switches.SWITCHES` is the one declaration every consumer derives from (doctor, status, the validator's dead-flag warning, the three doc blocks).

- [ ] **Step 1: Append the failing tests** to `tests/test_manager_checkin.py`

| test | asserts |
|---|---|
| `test_the_job_is_composed_but_dormant_on_a_fleet_that_did_not_ask` | after `generate`, `<prefix>.manager-checkin` units exist **and** the basename is listed in the `DORMANT` manifest |
| `test_arming_the_job_takes_it_out_of_the_dormant_manifest` | `defaults: { jobs: { manager-checkin: { enroll: true } } }` in fleet.yaml → composed and **not** listed |
| `test_the_unit_execs_the_trigger_with_the_fleet_as_its_argument` | the composed unit body names `manager-checkin.sh` followed by the fleet name |
| `test_the_beat_is_fifteen_minutes` | the composed interval is 900 |
| `test_the_switch_row_is_opt_in_and_states_why` | `sw.by_key("manager-checkin").polarity == OPT_IN` and `why_opt_in` is non-empty and names the spend |
| `test_the_arm_line_is_the_enroll_carrier` | `.arm` names `defaults.jobs.manager-checkin.enroll: true` and `lib/setup-fleet` (derived, not typed) |

Then the two edits that are *meant* to cost a visible line: `"manager-checkin"` into `tests/test_switches.py`'s opt-in allowlist (with a one-line reason in the comment beside `code-audit-sweep`'s class — model spend), and into `_ALL_JOB_NAMES` in `tests/test_system_defaults.py`.

- [ ] **Step 2: Run them to verify they fail** — `./.venv/bin/pytest tests/test_manager_checkin.py tests/test_switches.py tests/test_system_defaults.py -q`; expected rc 1.

- [ ] **Step 3: The job and the switch**

`claudlobby/system.yaml`, under `defaults.jobs`, after `task-recheck`, with a comment block in that file's house voice: what it does, that it **injects into a live manager session and spends a model turn per beat**, that the per-bot gate is the composed skill symlink, and that it is dormant because of the spend — not because the behaviour is doubted.

```yaml
    manager-checkin:
      enroll: false
      script: "$CLAUDLOBBY_ROOT/lib/manager-checkin.sh"
      interval: 900
      type: oneshot
```

`claudlobby/switches.py`, in `SWITCHES` beside `code-audit-sweep`:

```python
    Switch(
        key="manager-checkin",
        scope=FLEET_JOB,
        polarity=OPT_IN,
        carrier=ENROLL_FLEET,
        job="manager-checkin",
        plane=True,
        why_opt_in="model spend — one manager turn per idle beat — and it "
                   "injects into a live session",
        what="every 15 min, inject /checkin into an idle manager the compose "
             "equipped with the checkin skill",
    ),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_manager_checkin.py tests/test_switches.py tests/test_system_defaults.py tests/test_maintenance_jobs.py -q > "$TMPDIR/t2.txt" 2>&1; echo "rc=$?"; tail -5 "$TMPDIR/t2.txt"`
Expected: rc 0. If `test_the_doc_switch_tables_ARE_the_registrys_render` fails, that is Task 6's regeneration — note it and carry on.

- [ ] **Step 5: Commit** — `feat(checkin): the manager-checkin fleet job and its switch row (composed dormant)`.

---

### Task 3: `library/protocols/checkin.md`

**Files:** Create `library/protocols/checkin.md`. Append to `tests/test_checkin_library.py`.

**Interfaces:** composed through the protocols slot; `_demote_headings` turns the file's `## Manager` into `### Manager` in the composed `CLAUDE.md`, which is what the composition test asserts. The preamble's precedence sentence is what lets this protocol compose beside the older cadence rules without retiring them (that retirement is chunk 4's, and the two files are pinned untouched here).

- [ ] **Step 1: Append the failing tests** (the first two are chunk 1's cycle-8 originals, recovered from that plan's history at `91a23ea`)

```python
def test_the_protocol_declares_no_requires_and_no_self_fire():
    text = (LIB / "protocols" / "checkin.md").read_text()
    fm, body = parse_frontmatter(text)
    assert fm["title"] == "Check-in" and "requires" not in fm      # equipment linking is chunk 4
    assert "natural idle point" not in body                        # the trigger owns the beat, with its throttles
    assert "governs where it composes beside" in _flat(body.split("## Manager")[0])


def test_the_protocol_names_the_same_bounded_ask_read_as_the_skill():
    # without --raised every check-in counts as an ask and the manager falls silent
    _fm, body = parse_frontmatter((LIB / "protocols" / "checkin.md").read_text())
    assert "claudlobby checkins --bot $BOT_ID --since 7d --raised" in _flat(body)


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


def test_the_manager_section_fixes_the_post_shape_and_the_one_post_budget():
    _fm, body = parse_frontmatter((LIB / "protocols" / "checkin.md").read_text())
    m = _flat(body.split("## Manager")[1].split("## Worker")[0])
    for needle in ("one status line", "one ask", "one pointer",
                   "At most one post per check-in", "never one per project",
                   "point to it"):
        assert needle in m, needle


def test_the_cadence_rules_are_untouched_in_this_chunk():
    # chunk 4 retires them with a grep-derived sweep; this chunk composes beside them
    assert "Idle silence is a bug" in (LIB / "protocols" / "proactivity-discipline.md").read_text()
    assert re.search(r"2.3 min", (LIB / "protocols" / "worker-lifecycle.md").read_text())
```

- [ ] **Step 2: Run them to verify they fail** — `./.venv/bin/pytest tests/test_checkin_library.py -q`; expected rc 1.

- [ ] **Step 3: The protocol**

Frontmatter `title: Check-in`, `description: The idle-manager check-in and the thin human edge`, an H1 `# Check-in` the loader strips, then:

- **Preamble (both audiences).** What the check-in is: the manager's own re-engagement cycle, on a beat, recorded. Then the **precedence sentence**, which must contain the literal phrase the test pins — that this protocol **governs where it composes beside** an older cadence rule (milestone beacons, "idle silence is a bug", per-merge Telegram), because those describe a fleet with no recorded beat and this one has it. No self-fire clause anywhere: the trigger owns the beat and carries the throttles; a manager that fires its own check-in has none of them.
- **`## Manager`.** **Silence is the default** (that exact phrase) and a post is the exception the judgment must justify and record. A check-in that dispatches, proposes or chooses `nothing` posts nothing — it is in the plane. When the judgment does say post, the shape is fixed: **one status line, one ask** with named options, **one pointer** (a plane URL or a PR). **At most one post per check-in**; several qualifying items coalesce into one message **across all projects — one post covers the portfolio, never one per project**; held items wait for the next justified post. Never restate a project's rigor in the message — **point to it**. The rate limit on asking: read `claudlobby checkins --bot $BOT_ID --since 7d --raised` first; **two open, unanswered asks means the fleet proceeds on its best tier-gated judgment or waits quietly — it does not pile on a third**. An urgency floor breaks through regardless: a blocker that stalls the fleet, or a failure with real cost.
- **`## Worker`.** One thin line on start / done / blocked, to Telegram where the worker is configured for it and plane-only where it is not. No milestone cadence. Detail goes through `report-back.sh` to the manager and the plane; every line is a recorded communication regardless of carrier.

Bounds: ~40 lines total. It is read at every beat by a model that also carries the skill — anything the skill already says is context spent twice.

- [ ] **Step 4: Run the tests to verify they pass** — `./.venv/bin/pytest tests/test_checkin_library.py tests/test_skill_ref_resolution.py -q > "$TMPDIR/t3.txt" 2>&1; echo "rc=$?"; tail -3 "$TMPDIR/t3.txt"`
Expected: rc 0. `test_skill_ref_resolution.py` runs because the protocol names `` `/checkin` `` in backticks and that reference must resolve to the shipped skill (`claudlobby/skill_refs.py`).

- [ ] **Step 5: Commit** — `feat(checkin): the check-in protocol — silence is the default, the post has one shape`.

---

### Task 4: `dispatch.md` — the `project:` envelope row and the recipe flag

**Files:** Modify `library/protocols/dispatch.md`. Append to `tests/test_checkin_library.py`.

**Interfaces:** chunk 1 shipped `dispatch-task.sh --project KEY` (it opens the envelope gate and stamps `project_key` on the plane work item) but deliberately left the composed protocol alone, because composed text only reaches a session at its next start and chunk 1 performed no restart. This chunk restarts the canary manager, so the text rides with it.

- [ ] **Step 1: Append two failing tests**

- `test_the_dispatch_envelope_documents_the_project_field` — the key-value table holds a `` `project:<key>` `` row naming a `projects.yaml` slug and saying that it opens the envelope and stamps the plane work item.
- `test_the_tracked_dispatch_recipe_shows_the_project_flag` — the recipe block under "Tracked dispatch" contains `--project <key>`.

- [ ] **Step 2: Run them to verify they fail**; then add the row and the flag.

The table row, in the file's existing voice:

```markdown
| `project:<key>` | `projects.yaml` slug | The project this work belongs to — the well-defined bar. `dispatch-task.sh --project <key>` adds it to the envelope and stamps `project_key` on the plane work item, so a task can be read back per project. |
```

and `--project <key>` into the tracked-dispatch recipe beside `--repo` / `--workstream`.

- [ ] **Step 3: Run the tests to verify they pass** — `./.venv/bin/pytest tests/test_checkin_library.py tests/test_dispatch_type.py -q`; expected rc 0 (`test_dispatch_type.py` parses this doc and fails if the vocabulary drifts from `dispatch-task.sh`'s copy).

- [ ] **Step 4: Commit** — `docs(dispatch): the project: envelope field and its flag (composed text, rides this chunk restart)`.

---

### Task 5: The empirical gate — the whole beat on a real plane

**Files:** Modify `lib/validate-bot-change.sh` (append a block before the `=== $pass passed ===` summary; helpers `val_plane_ready`, `val_events`, `val_sql`, `val_iso` at `:154-179`, `harness_check` at `lib-common.sh:4634`, `$VAL_REPO`, `$VAL_CLI`, `$LIB_DIR`, `$ROOT`, `$PLANE_SOCKET`).

**Interfaces:** consumes Tasks 1–3 and chunk 1's `checkin-record.sh`. **The harness boots a stubbed `claude`** (`exec cat`, `:42`), which cannot run a skill — so the manager pane runs a **scripted responder** that does what the skill's RECORD step does: on a line beginning `/checkin`, it feeds one canned decision to the real `checkin-record.sh`. The responder stands in for the *reasoning* and for nothing else; every door in the chain (the timer script, the gates, `dispatch.sh`, `pane_send_verified`, the record door, the plane) is the real one. That is the only way to observe timer → pane → row end to end without a model, and it is stated in the block so it reads as a bound rather than a claim.

- [ ] **Step 1: Append the block**

Shape (the executor writes it out; every assertion below is one `harness_check`):

1. `val_plane_ready "$ROOT" valckbeat`; write `local/valckbeat/fleet.yaml` declaring one bot `valckmgr2`, and its bot dir with `bot.conf` (`BOT_ID=valckmgr2`, `FLEET_NAME=valckbeat`, `MANAGER_TMUX=valckmgr2`, `BOT_SERVICE=` empty so `tmux_socket_for_bot` yields the default socket — the `:710` convention).
2. `ln -sfn "$VAL_REPO/library/skills/checkin" "$CK2_DIR/.claude/skills/checkin"` — the equip gate, the real symlink shape the composer writes.
3. Write `decision.json` (chunk 1's harness decision, `action: "nothing"`) and `responder.sh`: an unquoted heredoc so `$ROOT`/`$VAL_REPO`/`$VAL_CLI` expand at write time and `\$line` does not; it exports the manager's identity env, prints `\n> \n` (an idle-looking prompt — `pane_is_busy` must not match it), and loops `while IFS= read -r line; do case "\$line" in /checkin*) bash "$VAL_REPO/lib/checkin-record.sh" < "$CK2_DIR/decision.json" >> "$CK2_DIR/logs/record.out" 2>&1 || true ;; esac; printf '\n> \n'; done`. Start it: `tmux new-session -d -s valckmgr2 "bash '$CK2_DIR/responder.sh'"`.
4. Run the real trigger: `CLAUDLOBBY_ROOT="$ROOT" CLAUDLOBBY_FLEET=valckbeat bash "$VAL_REPO/lib/manager-checkin.sh" valckbeat`, then settle ~3s.

| harness_check | how |
|---|---|
| `checkin: the beat injected /checkin into the equipped idle manager pane` | `tmux capture-pane -t valckmgr2 -p \| grep -c '/checkin'` is ≥ 1 |
| `checkin: ...and recorded ONE checkin_triggered anchored on the manager` | `val_events "$ROOT" valckbeat valckmgr2 checkin_triggered \| wc -l` is 1 |
| `checkin: ...and the session's answer landed a checkin_decision the read door lists` | `val_sql` counts one `checkin_decision`, and `$VAL_CLI --root "$ROOT" checkins --fleet valckbeat --json` names its `ck_` id (link `$ROOT/lib` around the CLI call and `rm -f` it after, the `#1481` neighbour rule at `:652/:682`) |
| `checkin: a second tick inside the min gap does NOT inject again (the plane read IS the rate limit)` | re-run the trigger; the pane's `/checkin` count is unchanged and `checkin_triggered` is still 1 |
| `checkin: a BUSY manager is never injected into mid-turn, and the skip is recorded` | `touch "$CK2_DIR/data/.last-tool-call"` (the marker-first busy path — rendering-immune), run with `--min-gap-s 0`; no new injection, and `val_events … checkin_skipped` holds `"reason":"busy"` |
| `checkin: an unreachable plane does NOT fire (fail closed for a spending action)` | a second scratch root carrying the same manifest and bot dir but **no** `state/plane/plane.db`; run the trigger against it; the pane count is unchanged and its log says `unreachable` |

- [ ] **Step 2: Run the harness unsandboxed twice — clean, then with the mutant applied — and prove the restore**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck2-out/env.sh"
bash lib/validate-bot-change.sh > "$OUT/vbc.txt" 2>&1; echo "rc=$?"; grep -E 'checkin:' "$OUT/vbc.txt"
```
Expected: six `PASS` lines beginning `checkin:`; the harness's overall verdict unchanged from chunk 1's tip (run `main`'s harness once for the baseline if unsure — a pre-existing failure in another block is named, not fixed).

Negative control — a guard that cannot fail certifies nothing:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck2-out/env.sh"
./.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("lib/manager-checkin.sh"); t = p.read_text()
old = '        if [ -s "$HITS" ]; then'
new = '        if false; then'
assert t.count(old) == 1; p.write_text(t.replace(old, new))     # ratelimit-off: the fourth check must flip to FAIL
PY
bash lib/validate-bot-change.sh > "$OUT/vbc-negctl.txt" 2>&1; echo "rc=$?"; grep -E 'checkin:' "$OUT/vbc-negctl.txt"
git checkout -- lib/manager-checkin.sh; git diff --quiet -- lib/manager-checkin.sh && echo "restored"
```
Expected: in `vbc-negctl.txt` exactly **one** `checkin:` line reads `FAIL` — the second-tick one — and the other five still PASS, because the mutant leaves every other gate intact; then `restored`. **Both files go into the PR body** (Task 7 step 3b).

- [ ] **Step 3: Commit** — `test(harness): the whole beat — timer injects /checkin, the session records a decision`.

---

### Task 6: README counts, the CLAUDE.md row, the switch doc blocks, CHANGELOG

**Files:** `README.md:145-146`; `CLAUDE.md` (the `lib/` table); the three `DOC_BLOCKS` docs (`switches.py:842-846`); `CHANGELOG.md` (`[Unreleased]`).

**Interfaces:** every number is measured in the same breath as it is written.

- [ ] **Step 1: The counts**

```bash
./.venv/bin/python -c "from tests.test_readme_library_counts import _bash_scripts, _md_members; print('lib', len(_bash_scripts()), 'protocols', len(_md_members('protocols')))"
```
Expected `lib 93 protocols 40`. Edit `README.md:145-146` to those two numbers (skills stay 54 — this chunk adds none), then `./.venv/bin/pytest tests/test_readme_library_counts.py -q` → pass.

- [ ] **Step 2: The lib row** — append to the `lib/` table in `CLAUDE.md`, in the house style, one paragraph covering: what it is (the check-in beat, the composed `<prefix>.manager-checkin` fleet timer's script); that the **per-bot gate is the composed skill symlink**, which is how a worker, a coordinator and an opted-out manager are all excluded and how an operator un-equips; that the **rate limit is a plane read** through `plane-lookup.py --events`, `task-recheck`'s rule — no timer state file to lose or to lie; that **unreachable is not empty and a spending action fails closed** (rc 3 → do not fire, log only, since the plane is the thing that could not answer); that a *ratelimit* skip is deliberately not a row (derivable from `checkin_triggered`, and at a 900s beat against a 2700s gap it would write two rows in three forever); that a **failed send records no trigger**, so the next beat retries rather than rate-limiting itself on a beat that never landed; that the roster is `declared_bots_strict`, never `bot_in_fleet` (which inverts an empty roster into every directory on the host); and that both nonzero-expecting calls run as top-level `if` pipelines because the ERR trap fires inside a substitution.

- [ ] **Step 3: The switch doc blocks** — `./.venv/bin/claudlobby doctor --switches --markdown` and paste each block between its `BEGIN`/`END` markers in the three docs; then `./.venv/bin/pytest tests/test_switches.py -q` → pass. (Hand-editing these is the drift the generated block exists to end.)

- [ ] **Step 4: CHANGELOG** — under `[Unreleased]`, one bullet per landed piece: the trigger and its gate ladder; the dormant fleet job and its switch row; the protocol; the `dispatch.md` field; the harness scenario. Name the arm line (`defaults.jobs.manager-checkin.enroll: true`, then generate + `lib/setup-fleet`) so an operator reading only the changelog can find it.

- [ ] **Step 5: Commit** — `docs: README counts, the CLAUDE.md row, the switch tables and CHANGELOG for the check-in beat`.

---

### Task 7: The gauntlet, the merge, the deploy, the first real beat

The operator's standing loop. Nothing merges without all of it, and the PR body cites each observation — claimed evidence is not evidence. **No review cycles** (the ruling): findings from step 1's lenses are folded as targeted commits or recorded as forks.

**Files:** none beyond step 1's fold commits.

**Interfaces:** consumes `$OUT/env.sh` (Task 0 — the host facts, the `no_names` gate; sourcing it enters the worktree), `$OUT/before.txt`, `run_before.txt`, `collect-lib-before.txt`, `vbc.txt`, `vbc-negctl.txt` and every commit of Tasks 1–6. Shell state does not survive between the executor's tool calls, so **every block below begins by sourcing `$OUT/env.sh`**.

```bash
. "$HOME/Projects/claudlobby-worktrees/ck2-out/env.sh"; cd "$WT"
ls "$OUT/before.txt" "$OUT/run_before.txt" "$OUT/collect-lib-before.txt" "$OUT/vbc.txt" "$OUT/vbc-negctl.txt" > /dev/null || { echo "missing evidence -- Task 0 or Task 5 did not complete"; exit 1; }
git status --porcelain | grep -q . && { echo "dirty tree -- commit first; the mutant driver restores with git checkout"; exit 1; }
git log --oneline -1
```

- [ ] **Step 1: Review lenses** — `/simplify`, then `/review-work`, then `/verify-completion` on the branch (the phase-finalization gate). Fold each finding as its own commit; re-run the touched test files. A finding that needs a decision becomes a fork in this plan, not a review round.

- [ ] **Step 2: Committed-code mutants, from `$WT`** — reuse chunk 1's driver **by reference**: write `$OUT/mut-ck2-defs.py` with the `MUTANTS` list below and the **identical** `if __name__ == "__main__":` driver block from chunk 1's Task 7 step 2 (dirty-tree refusal, the green precondition on the killing files, apply → run → `git checkout --` → assert restored, `killed ⇔ pytest rc 1`, anything else an INVALID RUN, the final assertion IS the gate). Do not re-derive it.

```python
# $OUT/mut-ck2-defs.py — (name, file, old, new, [killing test files]) + chunk 1's driver
MUTANTS = [
    ("busy-gate-off", "lib/manager-checkin.sh",
     '    if bot_is_busy "$socket" "$bot" "$bot_dir"; then', "    if false; then",
     ["tests/test_manager_checkin.py"]),
    ("session-gate-off", "lib/manager-checkin.sh",
     '    if ! check_tmux_session "$bot" "$socket"; then', "    if false; then",
     ["tests/test_manager_checkin.py"]),
    ("manager-gate-off", "lib/manager-checkin.sh",
     '    bot_is_manager "$bot_dir" || continue', "    true || continue",
     ["tests/test_manager_checkin.py"]),
    ("equip-gate-off", "lib/manager-checkin.sh",
     '    [ -e "$bot_dir/.claude/skills/checkin" ] || continue', "    true || continue",
     ["tests/test_manager_checkin.py"]),
    ("equip-gate-accepts-a-dangling-link", "lib/manager-checkin.sh",
     '    [ -e "$bot_dir/.claude/skills/checkin" ] || continue',
     '    [ -L "$bot_dir/.claude/skills/checkin" ] || continue',
     ["tests/test_manager_checkin.py"]),
    ("ratelimit-off", "lib/manager-checkin.sh",
     '        if [ -s "$HITS" ]; then', "        if false; then",
     ["tests/test_manager_checkin.py"]),
    ("gap-default-zero", "lib/manager-checkin.sh",
     'MIN_GAP_S="${CHECKIN_MIN_GAP_S:-2700}"', 'MIN_GAP_S="${CHECKIN_MIN_GAP_S:-0}"',
     ["tests/test_manager_checkin.py"]),
    ("unreachable-fires-anyway", "lib/manager-checkin.sh",
     '        echo "$TS SKIP $bot_id -- plane unreachable, not firing" >> "$LOG"; continue',
     '        echo "$TS SKIP $bot_id -- plane unreachable, firing anyway" >> "$LOG"',
     ["tests/test_manager_checkin.py"]),
    ("trigger-recorded-on-a-failed-send", "lib/manager-checkin.sh",
     '        ck_skip "$bot_dir" "$bot_id" send_failed "dispatch failed"',
     '        emit_fleet_event checkin_triggered manager-checkin "{}" "$bot_dir" "$bot_id"',
     ["tests/test_manager_checkin.py"]),
    ("alias-is-the-directory-name", "lib/manager-checkin.sh",
     '            "$bot_dir" "$bot_id"', '            "$bot_dir" "$bot"',
     ["tests/test_manager_checkin.py"]),
    ("fleet-filter-off", "lib/manager-checkin.sh",
     '    [ "$bot_fleet" = "$FLEET" ] || continue', "    true || continue",
     ["tests/test_manager_checkin.py"]),
    ("roster-from-a-directory-scan", "lib/manager-checkin.sh",
     'if declared_bots_strict "$BAD" > "$ROSTER"; then :; else',
     'if ls -d "$ROOT"/local/*/runtime/bots/*/ 2>/dev/null | sed "s#.*/bots/##;s#/##" > "$ROSTER"; then :; else',
     ["tests/test_manager_checkin.py"]),
    # A WELL-FORMED substitution, deliberately: a mutant that merely broke bash
    # syntax would "die" on a parse error and certify nothing about the rule.
    # This one runs, and dies on the source assertion. Anchor = the whole 3-line
    # head (verified to occur exactly once and to re-parse under /bin/bash).
    ("rate-read-in-a-substitution", "lib/manager-checkin.sh",
     '    if python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$ROOT" --events \\\n'
     '            --fleet "$FLEET" --bot "$bot_id" --type checkin_triggered \\\n'
     '            --since "$SINCE" > "$HITS" 2>> "$LOG"; then\n',
     '    if hits="$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$ROOT" --events \\\n'
     '            --fleet "$FLEET" --bot "$bot_id" --type checkin_triggered \\\n'
     '            --since "$SINCE" 2>> "$LOG")"; then\n'
     '        printf \'%s\' "$hits" > "$HITS"\n',
     ["tests/test_manager_checkin.py"]),
    ("job-enrolled-by-default", "claudlobby/system.yaml",
     "    manager-checkin:\n      enroll: false\n", "    manager-checkin:\n",
     ["tests/test_switches.py", "tests/test_manager_checkin.py"]),
    ("switch-ships-on", "claudlobby/switches.py",
     '        key="manager-checkin",\n        scope=FLEET_JOB,\n        polarity=OPT_IN,',
     '        key="manager-checkin",\n        scope=FLEET_JOB,\n        polarity=OPT_OUT,',
     ["tests/test_switches.py"]),
    ("protocol-self-fires", "library/protocols/checkin.md",
     "## Manager", "Run one when you reach a natural idle point.\n\n## Manager",
     ["tests/test_checkin_library.py"]),
    ("ask-read-unbounded", "library/protocols/checkin.md",
     "claudlobby checkins --bot $BOT_ID --since 7d --raised",
     "claudlobby checkins --bot $BOT_ID --since 7d",
     ["tests/test_checkin_library.py"]),
    ("project-row-dropped", "library/protocols/dispatch.md",
     "| `project:<key>` |", "| `projectx:<key>` |",
     ["tests/test_checkin_library.py"]),
]
```

```bash
. "$HOME/Projects/claudlobby-worktrees/ck2-out/env.sh"; cd "$WT"
./.venv/bin/python "$OUT/mut-ck2-defs.py" > "$OUT/mutants.md" 2> "$OUT/mut-progress.txt"; echo "rc=$?"; head -1 "$OUT/mut-progress.txt"; cat "$OUT/mutants.md"
```
Expected (unsandboxed): `rc=0`, the progress file's first line `green: the N killing files pass on the committed tip`, and an **18-row** table with every result `killed`. `$OUT/mutants.md` IS the PR body's mutant table. Anchors that do not occur exactly once are the driver's own refusal — re-anchor on the shipped text, never weaken the mutant; a survivor is a missing test.

- [ ] **Step 3: The two-leg full-suite gate** — `before.txt` is Task 0's; the after leg runs on the FINAL committed tip, unsandboxed:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck2-out/env.sh"
./.venv/bin/pytest --tb=no -ra > "$OUT/run_after.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$OUT/run_after.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$OUT/after.txt"
comm -13 "$OUT/before.txt" "$OUT/after.txt"     # failures YOU introduced — must be empty
tail -1 "$OUT/run_before.txt"; tail -1 "$OUT/run_after.txt"
./.venv/bin/pytest --collect-only -q tests/test_manager_checkin.py > "$OUT/collect-new.txt"; echo "rc=$?"; tail -1 "$OUT/collect-new.txt"
./.venv/bin/pytest --collect-only -q tests/test_checkin_library.py > "$OUT/collect-lib-after.txt"; echo "rc=$?"; tail -1 "$OUT/collect-lib-after.txt"
./.venv/bin/pytest --collect-only -q tests/test_bash_parse.py -k manager-checkin > "$OUT/collect-parse.txt"; echo "rc=$?"; tail -1 "$OUT/collect-parse.txt"
./.venv/bin/pytest --collect-only -q tests/test_no_dead_session_command.py -k manager-checkin > "$OUT/collect-dead.txt"; echo "rc=$?"; tail -1 "$OUT/collect-dead.txt"
```
Both suite rc must be 1 (the red baseline). The after leg's `passed` must equal before's **plus** `collect-new`, **plus** (`collect-lib-after` − `collect-lib-before`), **plus** the two `-k` numerators (those lines print `1/N tests collected (N-1 deselected)` — add the **1**, never the N). `tests/test_bash_parse.py` builds `LIB_SCRIPTS` from `lib/*.sh` and `tests/test_no_dead_session_command.py` parametrizes over the same list, so the new `.sh` lands one case in each; nothing parametrizes over `library/protocols/*`. A count change with an empty name diff is evidence the names mechanism broke, not a clean run.

- [ ] **Step 3b: Assemble the PR body and the squash body from the evidence directory, behind the identifier gate** — the same assembler shape as chunk 1's Task 7 step 3b: one `{ … } > "$OUT/pr-body.md"` block of `cat <<'HDR'` sections interleaved with `grep`/`comm`/`cat` of the evidence files, then a `{ … } > "$OUT/squash-body.md"` summary, then `no_names` on **both**. Sections, in order:

1. **What landed** — one line per task (the trigger and its gate ladder; the dormant job + switch row; the protocol; the `dispatch.md` field; the harness scenario; the docs).
2. **Empirical observation** — `grep -E 'checkin:' "$OUT/vbc.txt"`, then a `--- negative control (ratelimit-off mutant applied): ---` marker, then the same grep of `$OUT/vbc-negctl.txt`, both fenced.
3. **Two-leg gate** — the `comm -13` output (must be empty), and both `tail -1` count lines.
4. **Mutants** — `cat "$OUT/mutants.md"` (18 rows, rc 0).
5. **Rollout posture** — `lib/manager-checkin.sh` reaches every fleet on pull and **does nothing**: the job composes dormant, so no timer runs it until a fleet writes `enroll: true`; the per-bot gate is the composed skill symlink, which only the hand-equipped canary manager has. `library/protocols/checkin.md` composes only where a bot declares it. The `dispatch.md` edit is composed text and reaches a session at its next start — the canary manager's single restart at deploy. Name the two shipped protocols this engages: `canary-rollout` (one production manager before any fleet) and, by its own terms, nothing evaluative (the operator's ruling).
6. Spec and plan paths; the four forks F1–F4; the attribution line.

Gate the body before it is published:

```bash
[ "$(grep -c 'checkin:' "$OUT/pr-body.md")" = 12 ] && [ "$(grep -c 'FAIL' "$OUT/pr-body.md")" = 1 ] || { echo "STOP: the body does not carry six PASS lines twice and the one negative-control FAIL"; exit 1; }
no_names "$OUT/pr-body.md"; no_names "$OUT/squash-body.md"; wc -l "$OUT/pr-body.md"
```
Expected: both `clean`, then the line count.

- [ ] **Step 4: Push, open the PR, CI on Linux**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck2-out/env.sh"
no_names "$OUT/pr-body.md"      # the gate runs in THIS block, beside the door that publishes
git push -u origin checkin/chunk2-trigger
gh pr create --head checkin/chunk2-trigger --title "feat(checkin): chunk 2 — the trigger and the protocol" --body-file "$OUT/pr-body.md" > "$OUT/pr-url.txt"; echo "rc=$?"; cat "$OUT/pr-url.txt"
```
Wait for CI green; a `test_boot_capture.sh` load flake is re-run, not waved through.

- [ ] **Step 5: Admin-merge with the explicit squash body** (the operator's standing authorization for gauntleted work) — `. "$HOME/Projects/claudlobby-worktrees/ck2-out/env.sh"; no_names "$OUT/squash-body.md" && gh pr merge --squash --admin --body-file "$OUT/squash-body.md" "$(cat "$OUT/pr-url.txt")"`; delete the branch.

- [ ] **Step 6: Deploy — pull, generate, restart the canary manager once**

One ssh block, the host facts riding the command string as operator variables (`$MINI_ROOT`, `$CK_FLEET`, `$CK_MGR`), output to `$OUT/deploy.md`, nothing piped:

1. `git -C "$MINI_ROOT" pull --ff-only` and print the resulting `git log --oneline -1`.
2. `claudlobby --fleet "$CK_FLEET" generate` redirected to a file; read rc, then the file. Expect the new `<prefix>.manager-checkin` units in the fleet's `runtime/fleet/timers/` **and** the basename listed in that directory's `DORMANT` manifest — the job is composed and not enrolled.
3. Restart the canary manager **alone** (`lib/rolling-restart.sh`'s posture for one bot, gated on a fresh `BRIDGE_READY`): this is the composed-carrier step chunk 1 deliberately did not perform, and it is what puts `checkin.md` and the `dispatch.md` field in that manager's context.
4. Verify the composed context: the manager's `CLAUDE.md` holds `### Manager` and `### Worker` from the protocol, and its `.claude/skills/checkin` still resolves.

- [ ] **Step 6b: The skill's dry run — the runtime gate for the skill text, before any unattended beat**

The trigger will inject `/checkin` with nobody watching, so the skill text is rehearsed once first, on the restarted manager, through the same carrier: inject `/checkin --dry-run` with `lib/dispatch.sh` (the slash payload reaches the pane bare), wait for the turn (`sleep 240`; tool `timeout` ≥ 400000 ms), and capture the pane through the manager's own tmux server (`BOT_SERVICE` in its `bot.conf` is the `-L` name). The remote block reads the newest `checkin_id` before and after (`claudlobby checkins --bot "$CK_MGR" --last --json`) and prints `dryrun_recorded=<0|1>` — a dry run that landed a row ran the real door. From the capture, write `$OUT/dry-run-facts.md` as machine lines first, then the facts in words with no pane text: `dryrun_rc=<0|2|3>` (the door's `DRY-RUN ck_…` line at rc 0, or a refusal), `dryrun_refused=<0|1>`, `composed_prompted=<0|1>` (the skill's dry run is `ck=$(… --dry-run …) && claudlobby checkins …`, so the pane shows the read's JSON after the `DRY-RUN` line or a permission box), `read_prompted=<0|1>` (did any of READ 0–5 prompt), `turn_seconds=<n>`, `tool_calls=<n>`. **Step 7 refuses to arm unless all four gate lines read clean and `dryrun_recorded=0` stands in `$OUT/dry-run.md`**: a refusal or a prompt is a skill-text finding, fixed in `library/` (live on the next `generate`, no restart) before the beat is armed. The pane stays in `$OUT/dry-run.md`; it carries names and the manager's transcript and never leaves `$OUT`.

- [ ] **Step 7: Arm the job — an operator action, reported by the executor** (gated: `for k in dryrun_rc=0 dryrun_refused=0 composed_prompted=0 read_prompted=0; do grep -qx "$k" "$OUT/dry-run-facts.md" || exit 1; done; grep -qx dryrun_recorded=0 "$OUT/dry-run.md" || exit 1` runs first, in the same block that prints the stanza)

The fleet manifest is the operator's file. Print the exact stanza for them (`defaults: jobs: manager-checkin: enroll: true`), and, once they have written it: `claudlobby --fleet "$CK_FLEET" generate` (the basename leaves the `DORMANT` manifest) then `lib/setup-fleet "$CK_FLEET"` (the unit is enrolled). Record the arming instant in `$OUT/armed.md`.

- [ ] **Step 8: One observed beat — the deploy's positive control**

Wait for one interval past the arming instant with the manager idle (tool `timeout` ≥ 1200000 ms), then, over ssh, print **machine lines only** above a `--- row ---` marker (the decision's own text is the manager's and never leaves `$OUT`; the record may name projects and people):

- `triggered=<N>` — `claudlobby --fleet "$CK_FLEET" events --type checkin_triggered --since 1h` row count for that manager.
- `decided=<N>` and `action=<one of dispatch|ask|nothing|->` — from `claudlobby checkins --bot "$CK_MGR" --last --json`, the id and the action only.
- `injected=<yes|no>` — the manager's `logs/manager-checkin.log` tail holding a `DISPATCH` line in the window.

Derive the verdict into `$OUT/verdict.txt` **below** the ssh, from those lines, never typed at posting time: `triggered=1` **and** `decided=1` is the control passing. `triggered=1, decided=0` is a real and expected shape — the beat landed and the manager was mid-something — and means re-read after the next interval, not a failed deploy. `triggered=0` with the job enrolled and the manager idle is a defect: read the log's skip reason first (`unreachable` and `ratelimit` are the two that look identical from outside, and the log is the only place they differ).

- [ ] **Step 9: The deploy comment, behind the identifier gate** — assemble `$OUT/deploy-comment.md` from `deploy.md`, `dry-run-facts.md` (machine lines and the facts in words — never the pane), `armed.md`, the machine lines and `verdict.txt`; run `no_names` on it in the same block as the `gh pr comment` that posts it.

---

## Self-review

The thing most likely to be wrong here is the harness responder: it is the one place this plan puts a stand-in where production has a model, and if the pane's `read` loop does not receive what `pane_send_verified` types — a tty line-discipline question, not a tmux one — the end-to-end check degrades to "the trigger emitted a row", which the unit tests already prove and which would make the whole block look like evidence it is not. The block is written so that failure is loud rather than quiet (the decision assertion reads the plane, not the pane, so a responder that never fired shows up as a missing `checkin_decision` row), and the negative control proves the second-tick assertion can fail; but the executor should run the responder once by hand before wiring the assertions, and if the loop cannot be made to receive, say so and fall back to asserting the injection and the record separately rather than papering the gap. Second, F2's choice not to record ratelimit skips is a real trade: it keeps a 96-row-a-day-per-manager lane out of an unpruned table, and it costs chunk 3 the ability to count suppressions by reading rows — that number has to be derived from the beat and the gap instead, which is correct but is a thing chunk 3 must be told. Third, F4 resolves a genuine ambiguity in spec §5 (the arming paragraph names two carriers whose mechanisms exclude each other), and the resolution deliberately ships the trigger with **no self-gate at all** — if the operator wants a fleet-level off switch that does not require re-running `setup-fleet`, that is an `env` on the switch row and a `switch_is_on` line, and it is a five-line addition, not a redesign. Everything else here is mechanically checkable, which is what the ruling asked for: every gate has a mutant, every mutant names the test that kills it, and the one behaviour no unit test can reach has a harness block with a control that can fail.
