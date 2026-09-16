---
title: "Manager Check-in — PR 4: The Default — Implementation Plan"
type: plan
status: draft
owner: fleet owner
created: 2026-09-16
---

# Manager Check-in — PR 4: The Default — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **The operator's ruling (2026-09-16):** build to the end with **mechanical gates only** — tests, committed-code mutants, the naked-bot gate, the two-leg full-suite gate, CI. No evaluation apparatus, no bar, no verdict, no burn-in metric, no review cycles. Findings are folded in a targeted way; a decision becomes a fork.

**Goal:** After PRs 1–3 have merged and one canary manager has run the loop for a day, this PR makes the check-in the estate's default: the `checkin` protocol declares `requires: {skills: [checkin]}`, the compositor unions that requirement into the bot's skill set **with its grants**, `config.py` derives a second detectable role `leaf-manager` (a manager at least one of whose *in-fleet* reports is not itself a manager — a cross-fleet `manages:` target never makes a manager leaf), and one registry line in `defaults.py` gives every leaf manager the protocol and, through `requires:`, the skill. The older cadence mandates the check-in protocol's precedence sentence overrides are retired estate-wide from a grep-derived sweep. The naked-bot observation gate gains the leaf-manager arm that makes the new default visible to `--baseline`, and the run ends with the estate deploy: pull, `generate` every fleet, `diff`/`validate` per fleet, a gated rolling restart of the managers, and the first automated check-ins read across fleets.

**Architecture.** Four seams, each an existing one widened rather than a new mechanism.

1. **`requires:` is a frontmatter reader plus one resolver.** `loader.library_requires(path)` reads the generic `requires.<entity_type>: [names]` block (the `_read_tool_grants` shape, `loader.py:315`). `composer.resolve_effective_skills(bot, fleet, paths, *, is_manager)` becomes the **fourth** "effective" resolver beside `resolve_effective_protocols` (`composer.py:1736`) and `resolve_effective_integrations` (`:1769`): declared skills, plus `requires.skills` across the bot's *effective* protocols. It feeds all four consumers spec §10 names — `_resolve_skill_permissions` (`:2233`), `_resolve_skill_grants` (`:2245`), `link_skills` (`:1429`), the validator's grant loop (`validator.py:764`) — plus `freshbox._sourced_grants` (`freshbox.py:74`) and the plane registry keyframe (`registry_emit.py:371`). **The grant union is the whole point of touching the permission side** (spec §10, cycle-1 B8): settings compose at `composer.py:2780` *before* `link_skills` at `:2788`, so a skill that is only symlinked is linked and never granted.

2. **The role is a predicate, not a key.** `defaults.py:380-384` states the bound: "TO ADD A SECOND ROLE you need a predicate that can DETECT it, not just a key here." So `FleetConfig.leaf_manager_bots()` lands first (beside `manager_bots():735`), then `ROLE_LEAF_MANAGER` joins `DETECTABLE_ROLES`, then the composer passes both roles at `composer.py:1763`.

3. **The default is one registry line**, `REGISTRY["protocols"].roles = {"leaf-manager": ("checkin",)}` — INSTRUCT tier, so it must clear the tier test and diff against the recorded naked-bot baseline. Because the arm that could see it does not exist (`naked-bot-observation-gate.md:294-296`: "no arm composes a manager"), the arm lands in the same PR, **before** the line.

4. **The retirement is a sweep, and the test asserts over the grep** (spec §12 item 5), never over a hand list. The precedence sentence lives in `library/protocols/checkin.md` (PR 2's file) and is what makes the older text retirable rather than merely contradicted.

**Nothing composes differently on a fleet without a leaf manager.** A one-bot fleet, a worker-only fleet and a coordinator-only fleet all compose byte-identically before and after. That is a pinned property, not a claim — Task 3 step 4 diffs a composed tree.

**Tech Stack:** Python 3.11 (stdlib + the package's pydantic/yaml), bash 3.2-compatible `lib/` scripts sourcing `lib-common.sh`, pytest.

**Spec:** `documentation/plans/2026-09-13-manager-checkin-design.md` §2 (rulings, esp. 13), §9, §10, §12 item 5, §16. **Chunk-1 plan** (conventions, the identifier gate, the evidence-file discipline): `documentation/plans/2026-09-14-manager-checkin-chunk1-contract.md`. Executors read all three.

## Scope

**In PR 4:** `requires:` frontmatter + `library_requires` + `resolve_effective_skills` with the grant union; `leaf_manager_bots()` + `ROLE_LEAF_MANAGER` + the two validator messages; the `defaults.py` registry line + the compose-time job gate + the opt-out surface; the naked-bot gate's leaf-manager arm, `SCHEMA` bump and new dated baseline; the cadence-retirement edits over the grep-derived sweep with a grep-derived test; `rolling-restart.sh --managers-only`; README counts, CLAUDE.md rows, CHANGELOG, getting-started + fleet-yaml-schema; the gauntlet; the estate deploy.

**Not in PR 4:** anything evaluative (no bar, no verdict, no burn-in metric, no A/B); `propose` / the intake store / `planning.initiative` (chunk 1d); the `sprint` action (1c); `focus_*` (1b); the operator-plane check-in card; `checkins --summary` / `--proposals` (chunk 3's, already landed or still deferred there); any new library file; any change to the decision contract or the record door.

## Decision Forks

- **F8 — The trigger job's polarity.** *Context:* the brief asks PR 4 to arm the trigger by default for fleets with a leaf manager. The repo's **ruled** defaults rule says a job is ON by default unless it *deletes data, **spends money**, mutates operator source, sends outbound at scale, or has no deployment gate* (`claudlobby/system.yaml:22-24`; `switches.py:3-8`), and `tests/test_switches.py:96-105` enforces that an `OPT_IN` row states its category while a default-on row states none. The check-in spends money by construction — the Switch's own `what` string says "a manager turn per idle interval" (spec §5); spec §12 item 5 also says the polarity "stays `OPT_IN` unless the burn-in argues otherwise", and PR 4's premise is one canary manager for one day. *Options:* (a) keep `OPT_IN`, make the EQUIPMENT the default, compose the units only for a fleet with a leaf manager, and have the deploy print the one arming line per such fleet for the operator to apply; (b) set `defaults.jobs.manager-checkin.enroll: true` in `system.yaml`, flip the row to `OPT_OUT`, drop `why_opt_in` — three edits, named here so the reversal is one change. *Lean:* **(a)** — it delivers "every leaf manager is equipped and `/checkin` runs" estate-wide without overruling a tested invariant, and surfaces the arming line at the one moment it is actionable (`doctor`, `setup-fleet`'s closing table), which is what *no silent switches* asks for. *Ratifier:* operator. **The executor proceeds on (a)**; Task 3 step 5 carries (b).
- **F9 — Which way `requires:` points (RESOLVED, not open).** The brief says the `checkin` **skill** requires the **protocol**; spec §10 says the **protocol** requires the **skill**, and the spec's direction is the measured one, so it is what ships: `defaults.resolve("protocols", roles)` is *already wired* at `composer.py:1763`, while a skill in `REGISTRY["skills"]` composes **no symlink at all** (`naked-bot-observation-gate.md:234-251`: "`doctor` placed in `REGISTRY["skills"].entries` → no symlink composed"). The brief's direction would additionally need role-scoped `link_skills`, a `SystemDefaultsConfig.skills` opt-out key and an INSTRUCT-evidence line — the three pieces spec §10 says are "not needed". The *effect* is identical: one registry line gives a leaf manager both the protocol and the skill. The reader is generic (`requires.<entity_type>: [names]`), so the reverse stays expressible without a format change.

## Global Constraints

Carried from the chunk-1 plan where they still bind, plus three that are new here.

- **The repo is PUBLIC.** No PII, real chat ids, handles, tokens, tailnet names or fleet-specific paths in any committed asset, test, fixture, commit message or PR body. Never `@`-mention a bot name in GitHub-bound text. Operator values are `$CK_FLEET` / `$CK_MGR` / `$MINI_ROOT` (and `$CK_CONTROL`, `$CK_CONTROL_MGR`) sourced from `$OUT/env.sh`, never written into this plan, a commit or a comment. The `no_names <file>` gate from `$OUT/env.sh` runs **in the same block as** every publishing command.
- **NEW — nothing composes differently on a fleet without a leaf manager.** Pinned by a composed-tree diff (Task 3 step 4) over three shapes: one bot, manager+worker where the manager is excluded by opt-out, and a coordinator whose every in-fleet report is a manager.
- **NEW — the opt-out surface.** The default is switchable off with `fleet.system_defaults.protocols: false` (existing key, `config.py:113`), which by spec §10's opt-out semantics drops the protocol's requirements too unless the skill is declared directly. That key is currently **undocumented** in `documentation/fleet-yaml-schema.md:179-196`; Task 6 documents it. A default nobody can find is a silent switch.
- **NEW — composed text is read ONCE at session start** (`documentation/fleet-update-lifecycle.md`; CLAUDE.md's lifecycle note). The registry line changes every leaf manager's composed `CLAUDE.md`, so it reaches a *running* manager only at its next restart; the skill **symlink** and its grants are live on `generate`. That is why the deploy's last legs are a gated rolling restart and then a read of the first automated check-ins — the restart is the carrier, not a tidy-up.
- **bash 3.2 target** for every `lib/` edit: `set -euo pipefail`, source `lib-common.sh`, quote every variable, `printf '%s'` for values, **no apostrophes in comments inside a command substitution** (`tests/test_bash_parse.py`).
- **INSTRUCT-tier admission.** A new `REGISTRY` entry that is not `grandfathered` must clear `TIER_TESTS[Tier.INSTRUCT]` (`defaults.py:88-96`), gated by `tests/test_defaults_registry.py::TestScopeBoundary::test_every_instruct_default_was_already_composing_when_registered` and `::test_every_new_instruct_allowance_names_a_live_entry_and_its_evidence` — read both before writing the `reason`. A name in `Disposition.roles` that nothing can DETECT is silently inert (`defaults.py:360-378`); the predicate lands first, and `::TestRoleOverlay::test_only_detectable_roles_are_declared` is the gate.
- **Operator config (`local/<fleet>/fleet.yaml`) is REPORTED, never edited by the executor**, and **no unmerged code ever runs on the live host**.
- **Tests run unsandboxed; the baseline is red.** The gate is *names + counts* (three checks, per CLAUDE.md), never `pytest | grep`. Redirect, read `$?`, then read the file.
- **Test command form:** `./.venv/bin/pytest tests/<file>.py -q` from `$WT`. Pre-existing test files this plan names, all present on main: `tests/test_defaults_registry.py`, `tests/test_config.py`, `tests/test_validator.py`, `tests/test_composer.py`, `tests/test_composer_gaps.py`, `tests/test_system_defaults.py`, `tests/test_switches.py`, `tests/test_naked_bot_observe.py`, `tests/test_readme_library_counts.py`, `tests/test_dispatch_type.py`, `tests/test_plane_registry_read.py`, `tests/test_freshbox.py`, `tests/test_bash_parse.py`, `tests/test_seed_fleet.py`.
- **Line numbers** are as of the PR-4 branch point (PRs 1–3 merged). Re-anchor by the symbol named beside a line, never by the number.
- **Commits:** one per task, message via `git commit -F <file>`, ending `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File structure

**Create**

| Path | Responsibility |
|---|---|
| `tests/test_requires_linking.py` | `library_requires` parsing; `resolve_effective_skills` union; the **grant** union (the linked-not-granted regression); opt-out drops the requirement; the validator error is reported once library-wide. |
| `tests/test_leaf_manager_role.py` | `leaf_manager_bots()` over eight fleet shapes incl. F5; `DETECTABLE_ROLES`; the composer passing both roles; the two validator messages. |
| `tests/test_cadence_retirement.py` | The sweep as a grep, not a list: every hit is either edited or on a KEEP list carrying a reason; `checkin.md` carries the precedence sentence. |
| `tests/fixtures/compose_shape.py` | ~15-line harness for Task 3 step 4: compose one named fleet shape (`solo` / `worker-only` / `coordinator-only`) inside an exported tree, with and without the registry line, into two directories for `diff -r`. |
| `documentation/baselines/naked-bot-2026-09-16.json` | The recorded post-change inventory (schema 3, with the leaf-manager arm). Recorded output, never hand-authored. |

**Modify**

| Path | Change |
|---|---|
| `claudlobby/loader.py` (append after `_read_tool_grants:315`) | `library_requires(md_path) -> dict[str, list[str]]`; `iter_library_requires(paths, kind, names)`. |
| `claudlobby/composer.py:1429, 2233, 2245, 2780, 2788` (+ new resolver beside `:1736`) | `resolve_effective_skills(...)`; `link_skills`, `_resolve_skill_permissions`, `_resolve_skill_grants` take the resolved list. |
| `claudlobby/config.py` (after `manager_bots:735-757`) | `leaf_manager_bots()`. |
| `claudlobby/defaults.py:380-384, 142-...` | `ROLE_LEAF_MANAGER`, `DETECTABLE_ROLES`, the `protocols` role overlay + its `reason`. |
| `claudlobby/validator.py:568, 764, 1203` | the unresolvable-`requires:` error (once per protocol, library-wide) and the wrong-role warning. |
| `claudlobby/freshbox.py:59, 74-75, 599` | `_sourced_grants(bot, fleet, paths)`. |
| `claudlobby/plane/registry_emit.py:371` | `equipment.skills` = the effective set (the `#1405` rule already applied to protocols/integrations two lines down). |
| `claudlobby/composer.py` (fleet-job compose) | the `manager-checkin` unit composes only for a fleet with a leaf manager. |
| `claudlobby/switches.py` (`manager-checkin` row) | `what` + `config_extra` name the leaf-manager condition; `why_opt_in` states the money category (F8 (a)). |
| `library/protocols/checkin.md` (PR 2's file) | `requires: {skills: [checkin]}` frontmatter. |
| 9 `library/` files (Task 5) | the cadence retirement. |
| `lib/naked-bot-observe.py:76, 325, 341, 463, 467, 490` | `SCHEMA = 3`; the `shape:leaf-manager` arm. |
| `lib/rolling-restart.sh:40, 49, 88, 94` | `--managers-only`, the mirror of `--workers-only`. |
| `documentation/naked-bot-observation-gate.md:293-296, 340` | the role-overlay bound closed; the new dated baseline named. |
| `README.md:145-146`, `CLAUDE.md:475` + lib table, `CHANGELOG.md`, `documentation/getting-started.md`, `documentation/fleet-yaml-schema.md:179-196, 253-256` | counts, rows, the role and the knob. |

**Sizing:** Task 0 S · Task 1 L · Task 2 M · Task 3 M · Task 4 M · Task 5 M · Task 6 S · Task 7 L · Task 8 M.

---

### Task 0: Worktree, venv, the before leg, and the two pre-change inventories

**Files:** none changed. Produces the branch, the venv, the *before* leg every later gate diffs against, the pre-change naked-bot inventory, and the sweep's own pre-change hit list.

**Interfaces:** produces `$WT` and `$OUT` (evidence files, outside any session `$TMPDIR`), `$OUT/env.sh` (host facts + the `no_names` gate, **sourced first by every later block** — shell state does not survive between the executor's tool calls), `$OUT/before.txt`, `$OUT/run_before.txt`, `$OUT/naked-before.json`, `$OUT/sweep-before.txt`.

- [ ] **Step 1: Worktree on a fresh branch off main — after PRs 1–3 are ON main**

```bash
git -C /Users/chris/Projects/Claudlobby fetch -q origin main
for f in library/protocols/checkin.md library/skills/checkin/SKILL.md lib/manager-checkin.sh; do
  git -C /Users/chris/Projects/Claudlobby show "origin/main:$f" > /dev/null || { echo "STOP: $f is not on origin/main -- PRs 1-3 must merge first"; exit 1; }
done
WT="$HOME/Projects/claudlobby-worktrees/ck5"
OUT="$WT-out"
mkdir -p "$(dirname "$WT")" "$OUT"
: "${CK_FLEET:?the engineering fleet name (ruling 13), as the operator spells it}"
: "${CK_MGR:?its leaf manager id}"
: "${MINI_ROOT:?the claudlobby install root on the host}"
{
  printf "export WT='%s' OUT='%s'\n" "$WT" "$OUT"
  printf "export CK_FLEET='%s' CK_MGR='%s' MINI_ROOT='%s'\n" "$CK_FLEET" "$CK_MGR" "$MINI_ROOT"
  cat <<'FN'
no_names() {   # <file>: refuse when any host identifier reached a body bound for the public PR
  local f="$1" hits=0 v pat out
  for v in "$CK_FLEET" "$CK_MGR"; do
    [ -n "$v" ] || continue; pat=$(printf '%s' "$v" | sed 's/[][\.*^$/]/\\&/g')
    out=$(grep -n -E "(^|[^A-Za-z0-9_-])${pat}([^A-Za-z0-9_-]|$)" "$f" | head -3); [ -z "$out" ] || { printf '%s\n' "$out"; hits=$((hits + 1)); }
  done
  for v in "$MINI_ROOT" "$(dirname "$MINI_ROOT")"; do
    out=$(grep -n -F -- "$v" "$f" | head -3); [ -z "$out" ] || { printf '%s\n' "$out"; hits=$((hits + 1)); }
  done
  if [ "$hits" = 0 ]; then echo "identifiers in $(basename "$f"): clean"; else echo "STOP: $hits identifier(s) reached $f -- fix the PRODUCING step, never the body"; exit 1; fi
}
FN
  printf 'cd "%s" || { echo "STOP: worktree %s missing"; exit 1; }\n' "$WT" "$WT"
} > "$OUT/env.sh"
cd /Users/chris/Projects/Claudlobby
git worktree add -B checkin/chunk5-default "$WT" origin/main
. "$OUT/env.sh"; git log --oneline -1; type no_names | head -1
```
Expected: the tip of `origin/main`, and `no_names is a function`.

- [ ] **Step 2: A venv IN the worktree**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
python3 -m venv .venv && ./.venv/bin/python -m pip install -q -e '.[dev]'
./.venv/bin/python -c "import claudlobby, pathlib; print(pathlib.Path(claudlobby.__file__).resolve())"
```
Expected: a path under `$WT/claudlobby/`. Any other path: stop — the editable finder is shadowing the worktree.

- [ ] **Step 3: The before leg, names + counts (unsandboxed)**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest --tb=no -ra > "$OUT/run_before.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$OUT/run_before.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$OUT/before.txt"
wc -l < "$OUT/before.txt"; tail -1 "$OUT/run_before.txt"
```
Expected: `rc=1` (the baseline is red) and an `N failed, M passed` line. rc 2/4/5/127 means the run did not complete and the diff is not evidence.

- [ ] **Step 4: The two pre-change inventories**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
git status --porcelain | grep -q . && { echo "STOP: the worktree is dirty -- both readings below must describe a named ref"; exit 1; }
./.venv/bin/python lib/naked-bot-observe.py --ref HEAD --json > "$OUT/naked-before.json" 2>"$OUT/naked-before.err"; echo "naked rc=$?"
LC_ALL=en_US.UTF-8 grep -r -n -i -E 'milestone|beacon|2.3 min|10.15 min|idle silence|never go silent|never licenses silence|cadence and frequency' library/ | sort > "$OUT/sweep-before.txt"
cut -d: -f1 "$OUT/sweep-before.txt" | sort -u | tee "$OUT/sweep-files.txt" | wc -l
wc -l < "$OUT/sweep-before.txt"
```
Expected: `naked rc=0`; **11 files, 19 hit lines** (measured on `main` @ `9ac252f`). A different count means the library moved — re-derive the KEEP list in Task 5 rather than editing the number. The dirty-tree refusal is not ceremony: `naked-bot-observe.py` exports a named ref, so an uncommitted edit would be invisible to the *before* reading and visible to the *after* one.

---

### Task 1: `requires:` frontmatter, the effective-skill resolver, and the grant union

**Files:** `claudlobby/loader.py`, `claudlobby/composer.py`, `claudlobby/freshbox.py`, `claudlobby/validator.py`, `claudlobby/plane/registry_emit.py`, `library/protocols/checkin.md`, `tests/test_requires_linking.py`.

**Interfaces:** `loader.library_requires(md_path: Path) -> dict[str, list[str]]`; `loader.iter_library_requires(paths, kind: str, names: list[str]) -> list[tuple[str, dict[str, list[str]]]]`; `composer.resolve_effective_skills(bot: BotConfig, fleet: FleetConfig, paths: Paths, *, is_manager: bool) -> list[str]`; `composer.link_skills(bot, paths, log, *, skills: list[str])`; `composer._resolve_skill_permissions(skills: list[str])`; `composer._resolve_skill_grants(skills: list[str], paths)`; `freshbox._sourced_grants(bot, fleet, paths)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_requires_linking.py`:
  - `test_requires_block_parses_generically` — `requires: {skills: [a, b], guardrails: [c]}` returns all three keys; absent/malformed/non-list → `{}` or the key omitted, never a raise (the `_read_tool_grants` posture).
  - `test_a_required_skill_joins_the_effective_set`; `test_a_required_skill_is_symlinked` (`.claude/skills/checkin` after `compose_bot`); `test_a_declared_skill_is_not_duplicated` (declared first, order preserved).
  - `test_a_required_skill_is_GRANTED_not_only_linked` — the composed `settings.local.json` `permissions.allow` carries `Skill(checkin)` **and** every `tool_grants` entry of `checkin/SKILL.md`. *This is the cycle-1 B8 regression; settings compose before `link_skills`, so linking alone passes the previous test and fails this one.*
  - `test_opting_out_of_the_protocol_drops_its_requirement` (`system_defaults: {protocols: false}` composes neither) and `test_declaring_the_skill_directly_survives_the_protocol_opt_out` (spec §10 opt-out semantics).
  - `test_an_unresolvable_requirement_is_one_library_wide_error` — a protocol requiring an absent skill produces **exactly one** error naming the protocol FILE, on a fleet where three bots equip it (`validator.py:383-396`'s "one defect, one error" rule).
  - `test_the_registry_keyframe_records_the_effective_skills` (the `#1405` rule) and `test_freshbox_traces_a_required_skills_grants` (no `orphan_grant`).

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest tests/test_requires_linking.py -q > "$OUT/t1-red.txt" 2>&1; echo "rc=$?"; tail -3 "$OUT/t1-red.txt"
```
Expected: rc 1, every test failing (import error is acceptable only for the first run).

- [ ] **Step 2: The reader** — `claudlobby/loader.py`, appended beside `_read_tool_grants`:

```python
def library_requires(md_path: Path) -> dict[str, list[str]]:
    """Read the generic ``requires.<entity_type>: [names]`` block (spec §10).

    v1 CONSUMER is protocols -> skills, but the schema is deliberately generic
    so protocols -> guardrails or skills -> mcp follow without a format change.
    Returns ``{}`` when the file, its frontmatter, or the block is absent, and
    drops a non-list value rather than raising: a malformed block is
    ``validate``'s to report (the ``_read_tool_grants`` posture, one door over).
    """
```
`iter_library_requires(paths, kind, names)` mirrors `iter_skill_grants` (`loader.py:361`) — folder entries (`dir/`) expanded, a missing file yielding `{}`.

- [ ] **Step 3: The resolver** — `claudlobby/composer.py`, beside `resolve_effective_protocols`:

```python
def resolve_effective_skills(bot, fleet, paths, *, is_manager) -> list[str]:
    """The skills a bot is ACTUALLY composed with: declared, plus every
    ``requires.skills`` entry of its EFFECTIVE protocols (spec §10).

    ONE definition, for the reason ``resolve_effective_protocols`` states two
    functions up: the compose path, the validator, freshbox and the plane's
    registry keyframe all call this, so a required skill is linked, GRANTED and
    recorded as equipment rather than reading "unused" in the inventory (#1405).
    Declared entries keep their order and come first; a requirement already
    declared is not duplicated.
    """
```
Then rewire, in this order (each is a signature change with its call sites updated in the same commit):
- `_resolve_skill_permissions(skills)` and `_resolve_skill_grants(skills, paths)` — `compose_settings_local` (`:2780`) computes the effective list once and passes it to both.
- `link_skills(bot, paths, log, *, skills)` — `compose_bot` (`:2788`) passes the same list. **Keyword-only and required**, never defaulted to `bot.skills`: a default is how the two lists silently diverge, which is the failure this resolver exists to prevent.
- `validator.py:764` — `iter_skill_grants(paths, resolve_effective_skills(...))`; `validator.py:568`'s existence check likewise, so a required-but-missing skill is reported.
- `freshbox._sourced_grants(bot, fleet, paths)` — the caller at `:599` is `audit_bot`, which already has `fleet`.
- `registry_emit.py:371` — `"skills": sorted(_composer().resolve_effective_skills(bot, fleet, paths, is_manager=bot.bot_id in fleet.manager_bots()))`, with the comment two lines below extended to name skills as the third effective set.

- [ ] **Step 4: The validator's unresolvable-requirement error** — a library-wide scan beside `_validate_library_frontmatter` (`validator.py`'s `for root in (paths.base_library, paths.overlay_library)` idiom), so an unequipped protocol with a broken `requires:` is still caught and the message names the **file**, once.

- [ ] **Step 5: The frontmatter on the protocol** — add to `library/protocols/checkin.md`:

```yaml
requires:
  skills: [checkin]
```
The skill's own doors stay where PR 1 put them: `tool_grants` in `library/skills/checkin/SKILL.md`. The grant union in step 3 is what carries them to a bot that never declared the skill.

- [ ] **Step 6: Run the tests**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest tests/test_requires_linking.py tests/test_composer.py tests/test_composer_gaps.py tests/test_validator.py tests/test_freshbox.py tests/test_plane_registry_read.py -q > "$OUT/t1-green.txt" 2>&1; echo "rc=$?"; tail -3 "$OUT/t1-green.txt"
```
Expected: rc 0.

- [ ] **Step 7: Commit** — `feat(compose): requires: linking with the grant union — a protocol brings its skill`.

---

### Task 2: The `leaf-manager` role

**Files:** `claudlobby/config.py`, `claudlobby/defaults.py`, `claudlobby/composer.py:1763`, `claudlobby/validator.py`, `lib/rolling-restart.sh`, `tests/test_leaf_manager_role.py`.

**Interfaces:** `FleetConfig.leaf_manager_bots() -> set[str]`; `defaults.ROLE_LEAF_MANAGER = "leaf-manager"`; `DETECTABLE_ROLES` gains it; `rolling-restart.sh --managers-only`.

- [ ] **Step 1: Write the failing tests** — `tests/test_leaf_manager_role.py`, one case per shape:
  - `test_a_team_manager_with_a_worker_is_leaf`; `test_a_coordinator_whose_every_in_fleet_report_is_a_manager_is_not_leaf`; `test_a_manager_with_an_empty_team_is_not_leaf`; `test_a_bot_that_manages_only_itself_is_not_leaf`; `test_a_worker_is_never_leaf`.
  - `test_a_cross_fleet_manages_target_does_NOT_make_a_manager_leaf` — **F5**, the ruled direction: `manages: [someone-not-in-fleet]` and no in-fleet non-manager report → not leaf. *Evidence: `config.py:736-748`'s docstring ("a top-level coordinator whose reports are themselves managers of other fleets") and `validator.py:1211-1216` warning rather than erroring on such a target.* Its pair: `test_a_manager_with_a_cross_fleet_target_AND_an_in_fleet_worker_is_leaf`.
  - `test_the_single_team_fleet_shape_names_the_same_bot_as_manager_bots` — spec §10's "where a fleet has one team and no `manages:` chain the two roles name the same bot"; `test_leaf_manager_is_a_subset_of_manager_bots`, asserted over every shape above.
  - `test_the_composer_passes_both_roles` — `resolve_effective_protocols` sees `("manager", "leaf-manager")` for a leaf and `("manager",)` for a coordinator.
  - `test_declaring_checkin_on_a_non_leaf_warns_and_says_why` / `..._on_a_worker_...` — spec §10's validator surface.
  - `test_rolling_restart_managers_only_skips_workers` — a `tests/test_*.sh`-style bash test; the flag mirrors `--workers-only` (`lib/rolling-restart.sh:49,94`).

- [ ] **Step 2: Run them to verify they fail**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest tests/test_leaf_manager_role.py -q > "$OUT/t2-red.txt" 2>&1; echo "rc=$?"; tail -3 "$OUT/t2-red.txt"
```
Expected: rc 1.

- [ ] **Step 3: The predicate** — `claudlobby/config.py`, after `manager_bots()`:

```python
def leaf_manager_bots(self) -> set[str]:
    """Managers at least one of whose IN-FLEET reports is not itself a manager.

    The second detectable role (spec §10). ``manager_bots()`` is true for a
    coordinator too, and the composed ``MANAGER_TMUX`` self-pointer follows the
    same set, so ``bot_is_manager`` cannot tell the two apart at runtime either
    — the distinction has to be made at compose time.

    A CROSS-FLEET ``manages:`` target does NOT make a manager leaf (F5, ruled):
    ``manages:`` exists precisely to express a coordinator whose reports are
    managers of other fleets, so an unresolvable target is evidence of a
    coordinator rather than of a worker. Out-of-fleet names are dropped BEFORE
    the test, never counted as non-managers; for a money-spending default the
    conservative direction is not to equip. A manager with no in-fleet report
    at all is likewise not leaf.
    """
    managers = self.manager_bots()
    leaf: set[str] = set()
    for name in managers:
        reports = {w for t in self.teams.values() if t.manager == name for w in t.workers}
        reports |= set((self.bots[name].manages or []) if name in self.bots else [])
        in_fleet = {r for r in reports if r in self.bots and r != name}
        if in_fleet - managers:
            leaf.add(name)
    return leaf
```

- [ ] **Step 4: The role key** — `defaults.py:380-384`:

```python
ROLE_LEAF_MANAGER = "leaf-manager"
DETECTABLE_ROLES: frozenset[str] = frozenset({ROLE_MANAGER, ROLE_LEAF_MANAGER})
```
Extend the note above it: the seam it describes is now used once, and the predicate is `FleetConfig.leaf_manager_bots()`. `composer.py:1763` becomes:

```python
roles = ((defaults.ROLE_MANAGER,) if is_manager else ()) + (
    (defaults.ROLE_LEAF_MANAGER,) if bot.bot_id in fleet.leaf_manager_bots() else ())
```

- [ ] **Step 5: The validator messages** — in the per-bot loop (`validator.py`, beside the skills check at `:568`): when `checkin` is in `bot.protocols` and the bot is not in `fleet.leaf_manager_bots()`, warn, naming the reason (worker / coordinator) and that the skill still links so `/checkin` runs by hand — only the injection is withheld (spec §10, §5 step 1).

- [ ] **Step 6: `--managers-only`** — `lib/rolling-restart.sh`: `MANAGERS_ONLY=0` beside `:40`, the flag at `:49`, the skip at `:94` (`[ "$MANAGERS_ONLY" -eq 1 ] && ! bot_is_manager "$bot_dir" && continue`), the log line at `:88`, and the usage block. It is the mirror of `--workers-only` and reuses `bot_is_manager`; a coordinator is therefore restarted too, which is one inert extra bot and is stated in the usage text rather than worked around.

- [ ] **Step 7: Run the tests**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest tests/test_leaf_manager_role.py tests/test_config.py tests/test_defaults_registry.py tests/test_validator.py -q > "$OUT/t2-green.txt" 2>&1; echo "rc=$?"; tail -3 "$OUT/t2-green.txt"
bash -n lib/rolling-restart.sh && echo "parse ok"
```
Expected: rc 0 and `parse ok`.

- [ ] **Step 8: Commit** — `feat(config): the leaf-manager role — a manager whose in-fleet reports are not all managers`.

---

### Task 3: The registry line, the job gate, and the opt-out surface

**Files:** `claudlobby/defaults.py`, `claudlobby/composer.py` (fleet-job compose), `claudlobby/switches.py`, `tests/test_defaults_registry.py`, `tests/test_system_defaults.py`, `tests/test_switches.py`.

**Interfaces:** `REGISTRY["protocols"].roles["leaf-manager"] = ("checkin",)`; the composer's fleet-job emitter skips `manager-checkin` for a fleet with no leaf manager.

- [ ] **Step 1: Write the failing tests** (extend the existing files rather than adding a fourth):
  - `tests/test_defaults_registry.py::test_the_leaf_manager_overlay_composes_the_checkin_protocol` — a leaf manager's `CLAUDE.md` carries the `checkin` section; a coordinator's and a worker's do not.
  - `::test_the_overlay_brings_the_skill_and_its_grants` — the union of Task 1 reaching through the registry, end to end.
  - `::test_the_checkin_entry_is_not_grandfathered` — it is a NEW instruction, so `grandfathered` must not name it and the two `TestScopeBoundary` gates must pass on its `reason`.
  - `::test_the_entry_resolves_to_a_library_file` — the existing `test_every_registered_entry_resolves_to_a_library_file` already covers it; assert it is exercised.
  - `tests/test_system_defaults.py::test_protocols_false_removes_the_leaf_manager_default` — the opt-out.
  - `tests/test_switches.py::test_manager_checkin_states_its_money_category` — `why_opt_in` non-empty while polarity is `OPT_IN` (F8 (a)); the existing `test_exactly_the_categories_that_ship_off` is the real gate.
  - **`::test_a_fleet_with_no_leaf_manager_composes_no_manager_checkin_unit`** — and its positive control, that a leaf-manager fleet does.

- [ ] **Step 2: Run them to verify they fail** — same shape as Task 2 step 2, output to `$OUT/t3-red.txt`. Expected: rc 1.

- [ ] **Step 3: The registry line** — `claudlobby/defaults.py`, in `REGISTRY["protocols"]`:

```python
roles={ROLE_LEAF_MANAGER: ("checkin",)},
```
and extend `reason` with the INSTRUCT argument, written against `TIER_TESTS[Tier.INSTRUCT]` verbatim: *every leaf manager would be worse at its job without it* — without the protocol a manager has no ruled beat and the older cadence text tells it to post continuously — *and no bot is made to do something surprising by having it*: the role overlay reaches only a manager that already has non-manager reports, the protocol's default answer is silence, and the injection that spends money is a separate, opt-in switch. State explicitly that it is **not grandfathered**: it is a new instruction and it changes what leaf managers are told, which is the point.

- [ ] **Step 4: The compose-time job gate + the byte-identity proof** — the fleet-job emitter skips `manager-checkin` when `fleet.leaf_manager_bots()` is empty. Then prove the Global Constraint:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
git archive HEAD | tar -x -C "$tmp"
for shape in solo worker-only coordinator-only; do
  echo "== $shape =="
  ./.venv/bin/python tests/fixtures/compose_shape.py "$tmp" "$shape" --before > "$tmp/$shape.before" 2>&1
  ./.venv/bin/python tests/fixtures/compose_shape.py "$tmp" "$shape" --after  > "$tmp/$shape.after"  2>&1
  diff -r "$tmp/$shape.before" "$tmp/$shape.after" > "$OUT/shape-$shape.diff" 2>&1; echo "rc=$?"
done
wc -l "$OUT"/shape-*.diff
```
Expected: `rc=0` and `0` lines for all three. (`tests/fixtures/compose_shape.py` is a ten-line harness the task writes: compose the named fleet shape at the exported ref with and without the registry line, into two trees.) A non-empty diff on any shape is a **blocker**, not a note.

- [ ] **Step 5: The switch row** — `claudlobby/switches.py`, the `manager-checkin` row PR 2 added: `what` names the leaf-manager condition; `why_opt_in` states the money category (`system.yaml:22-24`). **If the operator rules F8 (b):** set `polarity=OPT_OUT`, clear `why_opt_in`, and add `manager-checkin: { enroll: true }` under `defaults.jobs` in `claudlobby/system.yaml`. Those are the only three edits; no other code changes.

- [ ] **Step 6: Run the tests**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest tests/test_defaults_registry.py tests/test_system_defaults.py tests/test_switches.py tests/test_composer.py -q > "$OUT/t3-green.txt" 2>&1; echo "rc=$?"; tail -3 "$OUT/t3-green.txt"
```
Expected: rc 0.

- [ ] **Step 7: Commit** — `feat(defaults): the check-in is a leaf-manager default — one registry line`.

---

### Task 4: The naked-bot gate's leaf-manager arm and the recorded baseline delta

**Files:** `lib/naked-bot-observe.py`, `tests/test_naked_bot_observe.py`, `documentation/naked-bot-observation-gate.md`, `documentation/baselines/naked-bot-2026-09-16.json`.

**Interfaces:** `Arm.teams: bool` (or `Arm.extra_bot: str | None`) and `Arm.observed_bot: str`; `SCHEMA = 3`; a new arm `shape:leaf-manager`.

**Why the arm must land BEFORE the line is judged:** the gate composes no manager today (`naked-bot-observation-gate.md:294-296` — "Role overlays (`Disposition.roles`, `manager`) are unexercised — no arm composes a manager. When Phase 2 populates a role overlay, this gate needs a manager arm or it will not see it"). A role-scoped default is invisible to `--baseline` until the arm exists, so recording a baseline first and adding the arm later would certify a change nothing observed.

- [ ] **Step 1: Write the failing tests** — `tests/test_naked_bot_observe.py`:
  - `test_the_leaf_manager_arm_composes_a_manager` — the arm's fleet has two bots and a `teams:` block; the observed bot is the manager.
  - `test_the_leaf_manager_arm_sees_the_role_overlay` — its `protocols` observation carries `checkin`; the baseline arm's does not.
  - `test_the_arm_records_the_skill_symlink_and_the_grant` — `.claude/skills/*` artifacts carry `checkin` and `permissions.allow` carries its grants. *(Both surfaces already exist: `SURFACES["skills"].artifacts` and `SURFACES["permissions"].content_keys` — no new Surface.)*
  - `test_schema_bumped_so_an_old_baseline_refuses_to_compare` — `SCHEMA == 3`; `diff_reports` against a schema-2 record refuses rather than half-comparing.
  - `test_the_other_arms_are_unchanged` — the existing arms still observe `nakedbot`.

- [ ] **Step 2: The arm** — `lib/naked-bot-observe.py`:
  - `SCHEMA = 3` (`:76`).
  - `Arm` gains `observed_bot: str = "nakedbot"` and `teams: bool = False` — a **fleet-SHAPE axis**, the third after `declared` and `vault_wired`, and the doc's own "generalisation is still open — `teams:`, multi-bot fleets … are all shapes" is the precedent to cite in the comment.
  - `FLEET_TEMPLATE` (`:325`) gains a `{teams}` slot and a second bot; `write_probe` (`:341`) fills it with `teams: {core: {manager: nakedmgr, workers: [nakedbot]}}` and a `nakedmgr` bot carrying the same `probe-minimal` expertise.
  - `observe_arm` (`:467`) resolves `bot_dir` from `arm.observed_bot` instead of the literal `nakedbot`.
  - `build_arms` (`:490`) appends `Arm(label="shape:leaf-manager", system_defaults=None, teams=True, observed_bot="nakedmgr")`, with the comment stating what it exists for: a role overlay is the one thing no other arm can see, and the gate certified a fleet nobody runs until it did.
- [ ] **Step 3: Record the new dated baseline** — never overwrite the old one (`naked-bot-observation-gate.md:340-345`).

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
git add -A && git commit -q -F - <<'M'
feat(gate): the naked-bot observation gate gets its leaf-manager arm

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
M
./.venv/bin/python lib/naked-bot-observe.py --ref HEAD --json > documentation/baselines/naked-bot-2026-09-16.json; echo "rc=$?"
./.venv/bin/python lib/naked-bot-observe.py --ref HEAD --baseline documentation/baselines/naked-bot-2026-09-16.json > "$OUT/naked-selfcheck.txt" 2>&1; echo "rc=$?"; cat "$OUT/naked-selfcheck.txt"
./.venv/bin/python lib/naked-bot-observe.py --ref HEAD --baseline documentation/baselines/naked-bot-2026-08-12.json > "$OUT/naked-delta.txt" 2>&1; echo "rc=$?"; cat "$OUT/naked-delta.txt"
```
Expected: the record rc 0; the self-check `No drift`, rc 0; the delta against the August baseline **rc 1** naming `[shape:leaf-manager] NEW ARM` and the `protocols` / `skills` / `permissions` changes on it — that non-zero exit **is** the recorded delta this task delivers, and `$OUT/naked-delta.txt` is pasted into the PR body. A rc 0 there means the arm saw nothing and the gate is not measuring the default.

- [ ] **Step 4: The doc** — `documentation/naked-bot-observation-gate.md`: change the "One bot, one expertise" bound (`:293-296`) to record that a leaf-manager arm now exists and what it covers; add the new baseline path at `:340`; note that the *manager* role overlay is still unpopulated so `shape:leaf-manager` exercises `leaf-manager` only.

- [ ] **Step 5: Run the tests and commit**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest tests/test_naked_bot_observe.py -q > "$OUT/t4-green.txt" 2>&1; echo "rc=$?"; tail -3 "$OUT/t4-green.txt"
```
Expected: rc 0. Commit: `feat(gate): record the 2026-09-16 naked-bot baseline with the leaf-manager arm`.

---

### Task 5: The cadence retirement — the sweep, the edits, the grep-derived pin

**Files:** 9 of the 11 sweep files, `tests/test_cadence_retirement.py`, `tests/test_dispatch_type.py`.

**The sweep, run on the worktree at `main` @ `9ac252f`** (spec §12 item 5's exact grep, case-insensitive under a UTF-8 locale): **11 files, 19 hit lines.** Two files are KEEP-whole false positives, and naming them is part of the deliverable — a sweep that edits a false positive is worse than one that misses a hit.

| File:line | Text | Disposition |
|---|---|---|
| `protocols/worker-lifecycle.md:119, 197` | the `**Telegram milestones every 2–3 minutes of active work:**` block (four bullets + format line) and the quick-reference row `\| Every 2-3 min during work \| One-line milestone \|` | **RETIRE** both. The *moments* (sub-step done, phase switch, surprise) survive as the protocol's start/done/blocked line (spec §9 `## Worker`); the table keeps start / completion / blocked. |
| `protocols/worker-lifecycle.md:52, 87` | `5. IMPLEMENT ─── … Telegram milestones`; `The group sees your milestone and outcome posts` | **EDIT** the nouns → `one thin line on start/done/blocked` and `your start and outcome posts`. Both rules still hold. |
| `protocols/telegram-routing.md:29` | `progress milestone (~2-3 min during active work)` in the mandatory-post list | **RETIRE** the cadence clause; keep completion / blocked / scope change. |
| `protocols/proactivity-discipline.md:7` | `Idle silence is a bug.` | **EDIT** → *idle silence is recorded, not posted* (spec §9's exact supersession). Keep the wait-point list — a wait-point post is a real fact — and reframe `:17` to point at the check-in record. **The file stays on the KEEP list**: spec §9's replacement sentence contains the phrase `idle silence` verbatim, so the hit survives the retirement by design. |
| `protocols/continuous-autonomous-mode.md:24, 25` | `Never go silent. Silence reads as "stuck"…`; `A "still waiting" beacon every 10–15 min…` | **RETIRE** both — spec §9: the check-in is the beat. |
| `protocols/inbound-acknowledgment.md:39, 42, 58` | `**At each major milestone**`; `every 60s gets a heartbeat. Never go silent…`; the `proactivity-discipline (manager wait-point beacons)` cross-reference | **KEEP** the first two, scoped — this is an *inbound-reply* surface, a human is waiting on this turn, and the check-in governs the idle beat, never a live conversation. Reword to `at each substantive step` and add *"while a human is waiting on this turn"*; **EDIT** the cross-reference to match the retired text. |
| `protocols/comms-topology.md:74` | `**Cadence and frequency** … This protocol never licenses silence.` | **EDIT** — the pointer names `checkin` beside `proactivity-discipline`, and the trailing sentence goes: after the retirement it is the one line that would re-assert the retired mandate from a file nobody thinks to grep. |
| `protocols/token-efficiency.md:39` | `Acks, heartbeats, milestone cadence, wait-point beacons … stand unchanged` | **EDIT** — a cross-reference asserting the retired rules still stand. Replace the list with *"cadence is governed elsewhere (`checkin`, `inbound-acknowledgment`); this protocol still governs density only"*. That wording matches no sweep term, so this file leaves the hit set entirely — it is **not** on the KEEP list. |
| `expertise/orchestration.md:108` | `**Never go silent.** If you're processing, waiting on a worker, or blocked, say so in Telegram.` | **RETIRE** the mandate; replace with the check-in's precedence pointer. |
| `skills/lifecycle/SKILL.md:37` | `Never go silent — report what's happening` | **RETIRE**; the surrounding *"Every phase transition gets a Telegram message"* narrows to the start/done/blocked line. |
| `skills/autonomous-runner/SKILL.md:113` | `beacon to Telegram "No eligible work for this cadence tick" and EXIT` | **RETIRE** — the exact "nothing to do, said out loud every tick" the check-in records instead. Exit silently; the plane holds the fact. |
| `skills/autonomous-runner/SKILL.md:90` | `Beacon to Telegram: "Quota near limit; pausing…"` | **KEEP, unedited** — an urgency-floor event (spec §6's "a failure with real cost breaks through regardless"), not a cadence. Do **not** reword `Beacon` → `Post`: with `:113` retired it is this file's only surviving hit, and rewording it would leave the file's KEEP entry dead, which `test_no_keep_entry_is_dead` fails on. |
| `skills/cross-fleet-initiative/SKILL.md:39, 115` | `GATE.md (milestone definitions)` | **KEEP whole** — false positives: a project milestone in a gate document, nothing to do with a posting cadence. |

- [ ] **Step 1: Write the failing test** — `tests/test_cadence_retirement.py`. It asserts **over the grep, never over a hand list** (spec §12 item 5):

```python
SWEEP = re.compile(r"milestone|beacon|2.3 min|10.15 min|idle silence|never go silent"
                   r"|never licenses silence|cadence and frequency", re.I)
#: library-relative path -> why a hit SURVIVES the retirement. Every other hit must be gone.
KEEP = {
    "protocols/proactivity-discipline.md": "spec section 9's replacement sentence carries the phrase verbatim",
    "protocols/inbound-acknowledgment.md": "inbound reply loop -- a human is waiting on this turn",
    "protocols/comms-topology.md": "the bullet LABEL stays; the mandate sentence after it is gone",
    "skills/autonomous-runner/SKILL.md": "quota beacon is an urgency-floor event, not a cadence",
    "skills/cross-fleet-initiative/SKILL.md": "GATE.md project milestones -- not a posting cadence",
}
```
- `test_every_surviving_hit_is_on_the_keep_list_with_a_reason` — walks `library/**/*.md`, applies `SWEEP`, and fails naming any file not in `KEEP`. **A new library file that reintroduces the cadence fails this test**, which is the point of deriving from the grep.
- `test_no_keep_entry_is_dead` — every `KEEP` key still produces at least one hit, so the list cannot rot into permission for a file that no longer needs it.
- `test_the_retired_phrases_are_gone_from_the_nine_edited_files` — the specific strings above.
- `test_checkin_carries_the_precedence_sentence` — `library/protocols/checkin.md` states that it governs where it composes beside an older cadence rule (PR 2's preamble), so the retirement and the precedence agree.

- [ ] **Step 2: Run it to verify it fails** — `$OUT/t5-red.txt`, rc 1.

- [ ] **Step 3: The edits** — nine files, per the table. Heading levels are untouched; every file keeps its frontmatter. Do not delete a file and do not renumber a list.

- [ ] **Step 4: Update the existing pins** — `tests/test_dispatch_type.py:252-265` parses `worker-lifecycle.md`'s `**Types:**` line and `dispatch.md`'s table; neither anchor is edited by this sweep, but run both to prove it:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest tests/test_cadence_retirement.py tests/test_dispatch_type.py tests/test_seed_fleet.py -q > "$OUT/t5-green.txt" 2>&1; echo "rc=$?"; tail -3 "$OUT/t5-green.txt"
LC_ALL=en_US.UTF-8 grep -r -n -i -E 'milestone|beacon|2.3 min|10.15 min|idle silence|never go silent|never licenses silence|cadence and frequency' library/ | sort > "$OUT/sweep-after.txt"
diff "$OUT/sweep-before.txt" "$OUT/sweep-after.txt" > "$OUT/sweep.diff"; echo "diff rc=$?"; wc -l < "$OUT/sweep-after.txt"
```
Expected: pytest rc 0 and `diff rc=1`. **Do not paste a predicted after-count** — the surviving hits are whatever the five KEEP files produce once the edits are written, and they are what `sweep-after.txt` measures. Record the measured number in the PR body in the same breath as measuring it. If a hit survives outside `KEEP`, the test already said so — fix the file, never the list; if a KEEP file loses its last hit, the edit went too far — the `autonomous-runner:90` row above is the worked example of exactly that.

- [ ] **Step 5: Commit** — `refactor(library): retire the cadence mandates the check-in protocol supersedes`.

---

### Task 6: README counts, CLAUDE.md rows, CHANGELOG, the two schema docs

**Files:** `README.md`, `CLAUDE.md`, `CHANGELOG.md`, `documentation/getting-started.md`, `documentation/fleet-yaml-schema.md`.

- [ ] **Step 1: README counts** — PR 4 adds **no** library file and **no** `lib/` script, so every count in `README.md:145-146` is expected **unchanged**. Prove it rather than assume it; `tests/test_readme_library_counts.py` names the number to write if one moved.

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest tests/test_readme_library_counts.py -q > "$OUT/t6-counts.txt" 2>&1; echo "rc=$?"; tail -3 "$OUT/t6-counts.txt"
```
Expected: rc 0 with no edit. A failure means PR 2's protocol count was never updated — fix it here and say so in the PR body.

- [ ] **Step 2: CLAUDE.md** — extend the `defaults.py` row (`:475`) to name the `leaf-manager` role overlay and the `checkin` entry; extend the `config.py` line with `leaf_manager_bots`; add `--managers-only` to the `rolling-restart.sh` row in the `lib/` table; add one sentence to the **Runtime model** paragraph noting that the check-in default is composed `CLAUDE.md` text and so reaches a running manager only at its next restart.
- [ ] **Step 3: `documentation/fleet-yaml-schema.md`** — under `fleet.system_defaults` (`:179-196`) add the two undocumented keys `guardrails: true` and `protocols: true` to the per-category block, with one line naming `protocols: false` as the opt-out for the leaf-manager check-in default and noting that opting out of the protocol drops its `requires:` skill unless the skill is declared directly. Under `fleet.teams` (`:253-256`) and `bots.<name>.manages` (`:342-344`) add the leaf-manager rule in two sentences, including F5's direction.
- [ ] **Step 4: `documentation/getting-started.md`** — in §3 (Write fleet.yaml, the `teams:` discussion near `:158`), one short paragraph: a manager with at least one non-manager report in its own fleet is a *leaf manager* and is equipped with `/checkin` by default; the automatic beat is one line (`defaults.jobs.manager-checkin: { enroll: true }`) that `claudlobby doctor` names.
- [ ] **Step 5: CHANGELOG** — under `[Unreleased]`, one bullet per landed piece: `requires:` linking with the grant union; the `leaf-manager` role (with F5's direction); the registry line; the naked-bot arm + the new baseline; the cadence retirement with its KEEP list; `--managers-only`.
- [ ] **Step 6: Commit** — `docs: the leaf-manager role, the check-in default and its opt-out`.

---

### Task 7: The gauntlet — mutants, the naked-bot gate, the two-leg gate, the PR

The mechanical gauntlet: nothing merges without all of it, and the PR body cites each observation — claimed evidence is not evidence.

**Files:** none beyond step 1's fold commits. **Interfaces:** consumes `$OUT/env.sh`, `$OUT/before.txt`, `$OUT/run_before.txt`, `$OUT/naked-delta.txt`, `$OUT/sweep.diff`, `$OUT/shape-*.diff`; produces `$OUT/mut-ck5-defs.py`, `mutants.md`, `run_after.txt`, `after.txt`, `pr-body.md`, `squash-body.md`, `pr-url.txt`.

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
ls "$OUT/before.txt" "$OUT/run_before.txt" "$OUT/naked-delta.txt" "$OUT/sweep.diff" > /dev/null || { echo "missing evidence -- Tasks 0/4/5 incomplete"; exit 1; }
git status --porcelain | grep -q . && { echo "dirty tree -- commit first; the mutant driver restores with git checkout"; exit 1; }
git log --oneline -1
```

- [ ] **Step 1: Review lenses** — `/simplify`, then `/review-work`, then `/verify-completion` on the branch (the phase-finalization gate). Fold every finding as its own commit; re-run the touched test files. *(These are the standing mechanical lenses, not a review cycle.)*

- [ ] **Step 2: Committed-code mutants, from `$WT`.** For each: apply, run the named tests, expect pytest **rc 1** (rc 2/4/5/127 are INVALID RUNS, never kills), restore. Every anchor must occur exactly once; a surviving mutant is a missing test — add the test, never a weaker mutant. Bash-door tests unsandboxed. The driver writes `$OUT/mutants.md`.

```python
# $OUT/mut-ck5-defs.py — (name, file, old, new, [killing test files])
MUTANTS = [
    # --- the role derivation (F5 is the load-bearing direction) ---
    ("leaf-counts-cross-fleet", "claudlobby/config.py",
     "in_fleet = {r for r in reports if r in self.bots and r != name}",
     "in_fleet = {r for r in reports if r != name}",
     ["tests/test_leaf_manager_role.py"]),
    ("leaf-is-any-manager", "claudlobby/config.py",
     "if in_fleet - managers:", "if in_fleet or True:",
     ["tests/test_leaf_manager_role.py"]),
    ("leaf-ignores-manages", "claudlobby/config.py",
     "reports |= set((self.bots[name].manages or []) if name in self.bots else [])",
     "reports |= set()",
     ["tests/test_leaf_manager_role.py"]),
    ("leaf-counts-self", "claudlobby/config.py",
     "if r in self.bots and r != name}", "if r in self.bots}",
     ["tests/test_leaf_manager_role.py"]),
    # --- the registry gate ---
    ("overlay-applies-to-every-manager", "claudlobby/composer.py",
     "(defaults.ROLE_LEAF_MANAGER,) if bot.bot_id in fleet.leaf_manager_bots() else ()",
     "(defaults.ROLE_LEAF_MANAGER,) if is_manager else ()",
     ["tests/test_leaf_manager_role.py", "tests/test_defaults_registry.py"]),
    ("overlay-ignores-the-opt-out", "claudlobby/composer.py",
     "if sd.enabled and sd.protocols:", "if True:",
     ["tests/test_system_defaults.py"]),
    ("role-not-detectable", "claudlobby/defaults.py",
     "frozenset({ROLE_MANAGER, ROLE_LEAF_MANAGER})", "frozenset({ROLE_MANAGER})",
     ["tests/test_defaults_registry.py"]),
    # --- the grant union (the linked-not-granted regression) ---
    ("required-skill-linked-not-granted", "claudlobby/composer.py",
     "_append_unique(allow_patterns, _resolve_skill_grants(skills, paths))",
     "_append_unique(allow_patterns, _resolve_skill_grants(bot.skills, paths))",
     ["tests/test_requires_linking.py"]),
    ("requires-block-ignored", "claudlobby/loader.py",
     "req = fm.get(\"requires\")", "req = None",
     ["tests/test_requires_linking.py"]),
    # --- the sweep pin ---
    ("keep-list-swallows-everything", "tests/test_cadence_retirement.py",
     "SWEEP = re.compile(", "SWEEP = re.compile(r\"zzz-never-matches\") or re.compile(",
     ["tests/test_cadence_retirement.py"]),
]
```
Expected: `$OUT/mutants.md` — ten rows, every one `KILLED`, driver rc 0.

- [ ] **Step 3: The naked-bot gate, on the final tip**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/python lib/naked-bot-observe.py --ref HEAD --baseline documentation/baselines/naked-bot-2026-09-16.json > "$OUT/naked-final.txt" 2>&1; echo "rc=$?"; cat "$OUT/naked-final.txt"
```
Expected: rc 0, `No drift`. Any drift here means a later task changed composition after the baseline was recorded — re-record and re-read the August delta, never suppress.

- [ ] **Step 4: The two-leg full-suite gate** — `before.txt` is Task 0's; the after leg runs on the FINAL committed tip, unsandboxed:

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
./.venv/bin/pytest --tb=no -ra > "$OUT/run_after.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$OUT/run_after.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$OUT/after.txt"
comm -13 "$OUT/before.txt" "$OUT/after.txt"
tail -1 "$OUT/run_before.txt"; tail -1 "$OUT/run_after.txt"
./.venv/bin/pytest --collect-only -q tests/test_requires_linking.py tests/test_leaf_manager_role.py tests/test_cadence_retirement.py > "$OUT/collect-new.txt"; echo "rc=$?"; tail -1 "$OUT/collect-new.txt"
```
Both pytest rc must be **1** (the red baseline). `comm -13` must be **empty** — those are failures this branch introduced. The after leg's `passed` must equal before's plus the three new files' collected count plus the cases added to `test_defaults_registry.py` / `test_system_defaults.py` / `test_switches.py` / `test_naked_bot_observe.py`. **A count change with an empty name diff is evidence the names mechanism is broken, not a clean run.**

- [ ] **Step 5: Assemble the PR body and the squash body from the evidence directory, behind the identifier gate** — the same shape as the chunk-1 plan's Task 7 step 3b: a heredoc for the prose, `cat`/`grep` for every number, then `no_names` on both files **in this block**. Sections: *What landed* (one line per task); *The composition gates* — the naked-bot delta (`$OUT/naked-delta.txt`, the August baseline, rc 1 naming the new arm) and the self-check (rc 0); *Nothing composes differently without a leaf manager* (`wc -l "$OUT"/shape-*.diff`, three zeros); *The sweep* (`$OUT/sweep.diff` summarised as before/after counts plus the KEEP list, never the fleet-specific text); *Two-leg gate* (`comm -13` empty, both count lines); *Mutants* (`cat "$OUT/mutants.md"`); *Rollout posture* — the registry line is composed `CLAUDE.md` text, so a running manager takes it at its next restart, which the deploy performs one bot at a time gated on `BRIDGE_READY`; the trigger job stays `OPT_IN` per F8 (a) and the deploy prints the arming line per leaf-manager fleet. End with the attribution line.

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
no_names "$OUT/pr-body.md"; no_names "$OUT/squash-body.md"; wc -l "$OUT/pr-body.md"
```
Expected: `identifiers in pr-body.md: clean`, `identifiers in squash-body.md: clean`, then the line count.

- [ ] **Step 6: Push, open the PR, CI on Linux**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
no_names "$OUT/pr-body.md"
git push -u origin checkin/chunk5-default
gh pr create --head checkin/chunk5-default --title "feat(checkin): PR 4 — the default (requires: linking, the leaf-manager role, the registry line, the cadence retirement)" --body-file "$OUT/pr-body.md" > "$OUT/pr-url.txt"; echo "rc=$?"; cat "$OUT/pr-url.txt"
```
Wait for CI green; a known load flake is re-run, never waved through.

- [ ] **Step 7: Admin-merge with the explicit squash body** (the operator's standing authorization for gauntleted work)

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
no_names "$OUT/squash-body.md" && gh pr merge --squash --admin --body-file "$OUT/squash-body.md" "$(cat "$OUT/pr-url.txt")"
```
Then delete the branch.

---

### Task 8: The estate deploy — pull, generate, verify, restart the managers, read the first check-ins

**This is the carrier.** The registry line is composed `CLAUDE.md` text, read once at session start; the skill symlink and its grants are live the instant `generate` writes them. So `generate` alone equips every leaf manager's *tools* and only the restart puts the *protocol* in front of a running manager.

**Files:** none. Fleets are enumerated the way `lib/setup-fleets:18` does (flat `local/*/fleet.yaml` and nested `local/*/*/fleet.yaml`). Nothing is piped into `head`/`grep`: redirect, read rc, then read the file. Operator config is REPORTED, never edited.

- [ ] **Step 1: Pull and the pre-pull reading**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
ssh -o BatchMode=yes mini "MINI_ROOT=$MINI_ROOT bash -s" <<'EOF' > "$OUT/deploy-pre.md"
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd "$MINI_ROOT" || exit 1
git status --porcelain | grep -q . && { echo "dirty checkout -- stop"; exit 1; }
for fy in local/*/fleet.yaml local/*/*/fleet.yaml; do
  [ -f "$fy" ] || continue
  f="$(basename "$(dirname "$fy")")"
  .venv/bin/claudlobby --fleet "$f" diff > "/tmp/ck5-pre-$f.txt" 2>&1
  echo "fleet $(printf '%s' "$f" | cksum | cut -d' ' -f1): diff rc=$? lines=$(wc -l < "/tmp/ck5-pre-$f.txt")"
done
EOF
echo "rc=$?"; cat "$OUT/deploy-pre.md"
```
The live host already carries composed drift, so the expectation is *unchanged*, not *empty*: this reading is what the post-pull one is compared against. Fleets are reported by a checksum of the name, never the name — the file is pasted into a public comment.

- [ ] **Step 2: Pull, generate every fleet, verify**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
ssh -o BatchMode=yes mini "MINI_ROOT=$MINI_ROOT bash -s" <<'EOF' > "$OUT/deploy.md"
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd "$MINI_ROOT" || exit 1
git pull --ff-only || { echo "pull refused -- stop"; exit 1; }
echo "tip: $(git rev-parse --short HEAD)"
for fy in local/*/fleet.yaml local/*/*/fleet.yaml; do
  [ -f "$fy" ] || continue
  f="$(basename "$(dirname "$fy")")"; k="$(printf '%s' "$f" | cksum | cut -d' ' -f1)"
  .venv/bin/claudlobby --fleet "$f" validate > "/tmp/ck5-val-$f.txt" 2>&1; vrc=$?
  .venv/bin/claudlobby --fleet "$f" generate > "/tmp/ck5-gen-$f.txt" 2>&1; grc=$?
  .venv/bin/claudlobby --fleet "$f" diff     > "/tmp/ck5-post-$f.txt" 2>&1; drc=$?
  eq=0; for d in local/*/"$f"/runtime/bots/*/.claude/skills/checkin local/"$f"/runtime/bots/*/.claude/skills/checkin; do [ -e "$d" ] && eq=$((eq + 1)); done
  echo "fleet $k: validate rc=$vrc generate rc=$grc diff rc=$drc pre=$(wc -l < "/tmp/ck5-pre-$f.txt") post=$(wc -l < "/tmp/ck5-post-$f.txt") equipped=$eq"
  grep -i "checkin" "/tmp/ck5-val-$f.txt" > "/tmp/ck5-vw-$f.txt" 2>/dev/null
  head -5 "/tmp/ck5-vw-$f.txt" | sed 's/^/    validate: /'
done
EOF
echo "rc=$?"; cat "$OUT/deploy.md"
```
Expected per fleet: `validate rc=0`, `generate rc=0`, `post` within a few lines of `pre` (the pre-existing drift, plus the newly composed protocol section on each leaf manager), and `equipped` equal to that fleet's leaf-manager count — **0 on a fleet with no leaf manager**, which is the Global Constraint observed on real config. A validate line naming `checkin` on a worker or a coordinator is Task 2's warning firing correctly; read it, do not suppress it.

- [ ] **Step 3: Report the arming line, do not write it** (F8 (a)) — for each fleet whose `equipped` is non-zero, print the one line the operator adds to its `fleet.yaml`:

```
defaults:
  jobs:
    manager-checkin: { enroll: true }
```
followed by `.venv/bin/claudlobby --fleet <f> generate && lib/setup-fleet <f>`. `claudlobby doctor`'s `switches` rung and `lib/setup-fleet`'s closing table already name it; this step is the moment it is actionable.

- [ ] **Step 4: The gated rolling restart of the managers, one bot at a time**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
ssh -o BatchMode=yes mini "MINI_ROOT=$MINI_ROOT bash -s" <<'EOF' > "$OUT/restart.md"
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd "$MINI_ROOT" || exit 1
for fy in local/*/fleet.yaml local/*/*/fleet.yaml; do
  [ -f "$fy" ] || continue
  f="$(basename "$(dirname "$fy")")"; k="$(printf '%s' "$f" | cksum | cut -d' ' -f1)"
  lib/rolling-restart.sh "$f" --managers-only --skip-healthy > "/tmp/ck5-rr-$f.txt" 2>&1
  echo "fleet $k: rolling-restart rc=$? (managers only, BRIDGE_READY-gated) $(grep -c BRIDGE_READY "/tmp/ck5-rr-$f.txt") ready markers"
done
lib/reconcile-fleet.sh --all > /tmp/ck5-reconcile.txt 2>&1; echo "reconcile rc=$?"; grep -c -E 'orphan|missing|unsupervised-down|unbound' /tmp/ck5-reconcile.txt
EOF
echo "rc=$?"; cat "$OUT/restart.md"
```
Expected: rc 0 per fleet, one `BRIDGE_READY` per restarted manager, and reconcile clean. `--managers-only` restarts a coordinator too where one exists — one inert extra bot, disclosed rather than worked around. A bot that never comes ready hard-stops the fleet's loop by design: proceed-anyway across a fleet is the 2026-07-23 outage itself.

- [ ] **Step 5: Read the first automated check-ins across fleets**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
ssh -o BatchMode=yes mini "MINI_ROOT=$MINI_ROOT bash -s" <<'EOF' > "$OUT/checkins.md"
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd "$MINI_ROOT" || exit 1
for fy in local/*/fleet.yaml local/*/*/fleet.yaml; do
  [ -f "$fy" ] || continue
  f="$(basename "$(dirname "$fy")")"; k="$(printf '%s' "$f" | cksum | cut -d' ' -f1)"
  .venv/bin/claudlobby --fleet "$f" checkins --since 24h --json > "/tmp/ck5-ck-$f.json" 2>&1; rc=$?
  n=$(grep -c '"checkin_id"' "/tmp/ck5-ck-$f.json" 2>/dev/null || echo 0)
  echo "fleet $k: checkins rc=$rc rows=$n actions=$(grep -o '"action": *"[a-z]*"' "/tmp/ck5-ck-$f.json" 2>/dev/null | sort | uniq -c | tr '\n' ' ')"
done
EOF
echo "rc=$?"; cat "$OUT/checkins.md"
```
Expected: `rc=0` per fleet (rc 3 would mean the plane could not answer — a refusal, never a clean empty). On a fleet whose job the operator armed in step 3, `rows` climbs within one `CHECKIN_MIN_GAP_S` window; on an unarmed fleet `rows=0` is correct and is not a failure. **Nothing here is graded** — the operator ruled no evaluation apparatus; this is the deploy's observation, recorded and pasted.

- [ ] **Step 6: Post the deploy evidence as a PR comment, behind the identifier gate**

```bash
. "$HOME/Projects/claudlobby-worktrees/ck5-out/env.sh"
cat "$OUT/deploy-pre.md" "$OUT/deploy.md" "$OUT/restart.md" "$OUT/checkins.md" > "$OUT/deploy-comment.md"
no_names "$OUT/deploy-comment.md" && gh pr comment "$(cat "$OUT/pr-url.txt")" --body-file "$OUT/deploy-comment.md"
```
Expected: `identifiers in deploy-comment.md: clean`, then the comment URL. Every fleet is a checksum, every bot unnamed, no host path — the four source files were written that way so the gate is a check rather than a scrub.

---

## Self-review

The riskiest thing in this plan is the registry line, and it is risky in a direction the repo has already been bitten in: an INSTRUCT default changes what every matching bot is told, silently, and the instrument that would have seen it did not exist — `naked-bot-observation-gate.md:294-296` says in as many words that no arm composes a manager, so a role overlay would have landed green against a gate blind to it. That is why Task 4 builds the arm and records a new dated baseline **before** Task 7 judges anything, and why the August-baseline delta (rc 1, naming `[shape:leaf-manager] NEW ARM`) is the evidence pasted rather than the self-check (rc 0), which proves only that the recorder is deterministic. The second risk is the grant union: settings compose at `composer.py:2780` and `link_skills` runs at `:2788`, so a required skill that is merely symlinked passes a "is it linked" test and is un-runnable in practice — the `required-skill-linked-not-granted` mutant exists because that is the exact regression cycle-1 B8 found, and `test_a_required_skill_is_GRANTED_not_only_linked` is its only killer. Two places I resolved rather than deferred: the `requires:` direction (spec §10's protocol→skill, because a skill in `REGISTRY["skills"]` composes no symlink at all — measured, not argued — and the brief's effect is unchanged either way), and the trigger's polarity, where the brief's "arm by default" collides with a *ruled and tested* invariant that a money-spending door ships opt-in; F8 takes the honest maximum — equipment by default, arming surfaced at the one actionable moment — and names the three-line reversal so the operator can overrule with one edit. The weakest part is Task 5's KEEP list: it is judgment over a mechanical sweep, and `test_no_keep_entry_is_dead` is the only thing stopping it from rotting into blanket permission for five files. If one assumption here is wrong it is probably the `equipped` count in Task 8 step 2 — the glob covers flat and nested layouts, but it assumes the estate's leaf managers are the bots the operator expects, and if that number surprises anyone the right response is to read `leaf_manager_bots()` against the real manifests before restarting anything.
