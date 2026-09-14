---
title: "Manager Check-in — Chunk 1: The Contract — Implementation Plan"
type: plan
status: proposed
owner: fleet owner
created: 2026-09-14
---

# Manager Check-in — Chunk 1: The Contract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the check-in's *contract* — the `planning.initiative` construct, `requires:` equipment linking, the `leaf-manager` role, the decision-record and intake doors, the `/checkin` skill and protocol, and the minimal read door — so a manager equipped by hand can run `/checkin` end to end and every decision lands in the plane as a row.

**Architecture:** Every new fact is a plane event (system kinds need no migration; severities are one registry line each). Config grows one mirrored block (`planning:` beside `validation:`) that flattens to `PROJECT_INITIATIVE_<SLUG>` beside the tier. The compositor gains one seam — a protocol's `requires:` links its skills through the existing `link_skills` loop — and one detectable role. Bash doors validate through a stdlib contract module and write through `plane_emit_events`; the skill is a thin reasoning wrapper that never freelances a read or a write.

**Tech Stack:** Python 3.11 (stdlib + the package's existing pydantic), bash 3.2-compatible `lib/` scripts sourcing `lib-common.sh`, SQLite plane (schema 11), pytest.

**Spec:** `documentation/plans/2026-09-13-manager-checkin-design.md` — this plan implements §12 chunk 1 and argues from §5–§11, §13, §14. Executors read both.

## Scope — what this plan is, and the plans it deliberately leaves

The spec's §12 cuts the work into gauntlet-sized chunks. **This plan is chunk 1 only.** Each of the following gets its own plan, written after its predecessor merges so it argues from landed code: **1b** focus (`focus_declared`, `lib/focus-declare.sh`, `claudlobby focus`), **1c** the sprint scalpel (§6b), **2** the trigger (`lib/manager-checkin.sh`, the fleet job, the `Switch` row, the `validate-bot-change.sh` trigger extension), **3** the full read door (`checkins --summary`, outcomes, the panel seam), **4** the canary on the engineering fleet, **5** the default (the naked-bot leaf-manager arm, then the registry line).

Three things the plan pulls *into* chunk 1 from later spec sections, because the skill cannot honour its contract without them: the `leaf-manager` role and the `requires.role` warning (§10), the intake door `lib/checkin-propose.sh` with `dispatch-task.sh --project` / `--work-item` (§8 — the well-defined bar is enforced mechanically, not by prose), and a *minimal* `claudlobby checkins` (rows, `--last`, `--proposals`; §11's summary and outcome join stay in chunk 3).

## Global Constraints

Every task's requirements include these. Exact values are copied from the spec and from `CLAUDE.md`.

- **The repo is PUBLIC.** No PII, real chat ids, user ids, handles, tokens, tailnet names or fleet-specific paths in any committed asset, test, fixture or commit message. Fixtures are shape-verbatim with faked identifiers. Never `@`-mention a bot name in GitHub-bound text.
- **bash 3.2 target.** `set -euo pipefail`; source `lib-common.sh`; quote every variable; `printf '%s'` for values; **no apostrophes in comments inside `$( )`** (`tests/test_bash_parse.py` gates `lib/` and every `library/**/*.sh`).
- **New `system` event kinds need NO migration and NO contract change** (`contracts.py:389-418` — token shape `^[a-z][a-z0-9_]{0,63}$`; the DDL's `kind='system'` branch lists no vocabulary). Register severity with **one line per kind** in `claudlobby/plane/registries.py:54` (`SYSTEM_EVENT_SEVERITY`); an unregistered kind ingests with `severity NULL`.
- **The plane is always on.** `plane_armed` (`lib/lib-common.sh:495-524`) is opt-OUT: `PLANE_EMIT_DISABLED=1` is the only silencer. `PLANE_EMIT_ENABLED` is not read by any door.
- **Emit from bash through `plane_emit_events <door> <<<"$batch"`** (a here-string, never a pipeline — `PLANE_EMIT_LAST_RC` must come back to the caller). Envelope: `{"events":[{"event_type","emitter","source_ref","fleet","occurred_at","payload":{...}}]}`.
- **Every emitted `system` event is actor-anchored:** `"subject_kind":"actor","subject":"bot:<fleet>/<bot>"` (the alias form ingest resolves; `contracts.py:413-417`). `severity` is never on the wire.
- **The decision record's `data` cap is 16,384 bytes** (`registries.py` `("system","data")`, DIAGNOSTIC — over-cap truncates, never rejects). Keep the schema-1 record well under it (`rationale` ≤ 600 chars).
- **Validator mechanics:** `report.errors.append(str)` / `report.warnings.append(str)` (`validator.py:139-150`); did-you-mean via `hint(value, candidates)` from `known_values`. **An unresolvable `requires:` is an ERROR** (spec §10); a role mismatch is a WARNING.
- **`generate` runs the validator** (`commands/core.py:228-240`): errors refuse to generate, warnings print — so a validator warning *is* the "generate warning" the spec asks for.
- **Line numbers** below are as of `main` @ `a96b47f` (2026-09-14). If a file has moved, re-anchor by the symbol named beside the line, never by the number.
- **Tests run unsandboxed; the baseline is red** (~46 failed / 2 errors on macOS). The gate is *names + counts* (Task 13), never `pytest | grep`.
- **Test command form:** `./.venv/bin/pytest tests/<file>.py -q` from the worktree, whose `.venv` Task 0 creates. **Never reuse the main checkout's `.venv`** — its editable install points at whatever branch that checkout is on.
- **Never hand-edit `runtime/`**; library content carries frontmatter `title:` + an H1 (skills use native `name:`/`description:`).
- **Commits:** message via `git commit -F <file>` (backticks in `-m` run command substitution); end every message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File structure

**Create**
| Path | Responsibility |
|---|---|
| `claudlobby/requires.py` | Equipment linking: read `requires:` frontmatter, resolve a protocol list to the skills it requires, read `requires.role`. Pure functions over `Paths`. |
| `claudlobby/commands/checkins.py` | `claudlobby checkins` — the minimal read door (rows, `--last`, `--proposals`, `--json`; rc 3 on an unreachable plane). |
| `lib/checkin-contract.py` | Stdlib contract for the schema-1 decision record: `normalize()`; CLI filter (stdin JSON → normalized JSON, rc 2 with reasons). |
| `lib/checkin-record.sh` | The decision door: validate → mint `ck_` id → ONE `checkin_decision` system event, `source_ref checkin:<id>`. |
| `lib/checkin-propose.sh` | The intake door: `propose` (work_item + `task_proposed`, refuses below the well-defined bar or without a grant) and `reject` (`task_rejected`). |
| `library/protocols/checkin.md` | The check-in protocol: `## Manager` (surfacing judgment; one line, one ask, one pointer) and `## Worker` (one thin line on start/done/blocked); `requires: {skills: [checkin], role: leaf-manager}`. |
| `library/skills/checkin/SKILL.md` | The `/checkin` reasoning contract: READ 0–6 through named doors, DECIDE one project then one action, RECORD before ACT, the surfacing judgment, failure posture. |
| `tests/test_projects_planning.py` | `planning.initiative`: parse, default, validator, composition, reserved env namespace. |
| `tests/test_requires_linking.py` | `requires:` reader, compositor linking, validator errors/warnings, `list-library` annotation. |
| `tests/test_leaf_manager_role.py` | `leaf_manager_bots()`, the detectable role, the composer's role tuple. |
| `tests/test_checkin_contract.py` | The stdlib contract + the severity registration. |
| `tests/test_checkin_doors.py` | `checkin-record.sh`, `checkin-propose.sh`, `dispatch-task.sh --project/--work-item` — stubbed transport, real plane. |
| `tests/test_checkins_cli.py` | The read door over a seeded plane. |
| `tests/test_checkin_protocol.py` | The protocol composes both sections; the superseded phrases are gone; the skill's doors are named. |

**Modify**
| Path | Change |
|---|---|
| `claudlobby/known_values.py:119-124, 186-188` | `VALID_INITIATIVES`; `PROJECT_KEYS` gains `planning`. |
| `claudlobby/config.py:244-260, 264-276, 310-365, 735-757` | `ProjectPlanningConfig`; `ProjectConfig.planning`; `_coerce_project` parses `planning:`; `FleetConfig.leaf_manager_bots()`. |
| `claudlobby/validator.py:797-816, 1387-1395, 1416-1421, 1467-1471, 1569-1612` | initiative membership error + unknown planning-key warning; reserved `PROJECT_INITIATIVE_` namespace; `requires:` errors and the role warning; malformed `requires:` library-wide. |
| `claudlobby/composer.py:1056-1083, 1429-1478, 1737-1766, 2788` | `PROJECT_INITIATIVE_<SLUG>`; `link_skills(..., required=)`; role tuple via `defaults.roles_for`; compose_bot wires required skills. |
| `templates/claude.md.j2:193-210` | `Initiative` column in the manager's `## Projects` table. |
| `claudlobby/defaults.py:360-384` | `ROLE_LEAF_MANAGER`, `DETECTABLE_ROLES`, `roles_for()`. |
| `claudlobby/commands/core.py:320-379` | `list-library` annotates protocols with their requirements. |
| `claudlobby/commands/_parsers.py:189-197` | registers `checkins` beside `workstreams`. |
| `claudlobby/plane/registries.py:54-123` | three severity lines. |
| `claudlobby/plane/queries.py` (append) | `CHECKIN_ROWS_SQL`, `PROPOSALS_SQL`. |
| `lib/dispatch-task.sh:5-24, 104-117, 395-405, 474-494, 681-689` | `--project KEY`, `--work-item ID`. |
| `lib/validate-bot-change.sh` (append a block) | the empirical gate for the two doors. |
| `library/protocols/{worker-lifecycle,proactivity-discipline,continuous-autonomous-mode,token-efficiency}.md`, `library/protocols/README.md` | the supersede edits; `requires:` documented. |
| `documentation/projects-yaml-schema.md:12-35`, `projects.yaml.example`, `documentation/fleet-yaml-schema.md:508-511` | `planning.initiative` documented and exampled; `requires:` sentence. |
| `CLAUDE.md` (lib table + package list), `CHANGELOG.md` (`[Unreleased]`) | rows for the three lib scripts, `requires.py`, `commands/checkins.py`. |

---

### Task 0: Worktree, venv, baseline

**Files:** none changed. Produces the branch, the venv, and the *before* leg every later gate diffs against.

- [ ] **Step 1: Worktree on a fresh branch off main** (the `superpowers:using-git-worktrees` skill; the branch name is `checkin/chunk1-contract`)

```bash
cd /Users/chris/Projects/Claudlobby
git fetch -q origin main
git worktree add -b checkin/chunk1-contract "$SCRATCH/ck1-wt" origin/main
cd "$SCRATCH/ck1-wt" && git log --oneline -1
```
Expected: one line, the tip of `origin/main`.

- [ ] **Step 2: A venv IN the worktree** (never the main checkout's)

```bash
python3 -m venv .venv && ./.venv/bin/python -m pip install -q -e '.[dev]'
./.venv/bin/python -c "import claudlobby, pathlib; print(pathlib.Path(claudlobby.__file__).resolve())"
```
Expected: the printed path is under `$SCRATCH/ck1-wt/claudlobby/`. If it is under `/Users/chris/Projects/Claudlobby/`, stop — the editable finder is shadowing the worktree.

- [ ] **Step 3: The before leg, names + counts**

```bash
./.venv/bin/pytest --tb=no -ra > "$TMPDIR/run_before.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$TMPDIR/run_before.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$TMPDIR/before.txt"
wc -l < "$TMPDIR/before.txt"; tail -1 "$TMPDIR/run_before.txt"
```
Expected: `rc=1` (the baseline is red); a count line of the shape `N failed, M passed`. rc 2/4/5/127 means the run did not complete — fix that before anything else.

---

### Task 1: `planning.initiative` — config, known values, validator

**Files:**
- Modify: `claudlobby/known_values.py:119-124` (after `VALID_TIERS`), `:186-188` (`PROJECT_KEYS`)
- Modify: `claudlobby/config.py:244-260` (after `ProjectValidationConfig` / `_PROJECT_VALIDATION_KEYS`), `:264-276` (`ProjectConfig`), `:310-365` (`_coerce_project`)
- Modify: `claudlobby/validator.py:39-42` (imports), `:1416-1421` (after the tier check), `:1467-1471` (after the validation-key loop)
- Test: `tests/test_projects_planning.py`

**Interfaces:**
- Produces: `known_values.VALID_INITIATIVES: tuple[str, ...] = ("autonomous", "propose", "none")`; `config.ProjectPlanningConfig(initiative: str = "none", raw: dict)`; `config._PROJECT_PLANNING_KEYS = {"initiative"}`; `ProjectConfig.planning: ProjectPlanningConfig`.
- Consumed by Task 2 (composition) and by the skill (Task 10) via `PROJECT_INITIATIVE_<SLUG>`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_projects_planning.py
"""projects.yaml `planning.initiative` — a project's ideation autonomy (manager
check-in spec §8b). Beside `validation.tier` and independent of it: planning
says how work is ORIGINATED, validation how it is CLOSED. Load -> validate ->
compose, mirroring tests/test_projects_tier.py."""

from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.validator import validate

PROJECTS_YAML = dedent("""\
    projects:
      acme-shop:
        title: Acme Shop storefront
        repos: [acme/storefront]
        planning:
          initiative: propose
        validation:
          tier: review
      post-scheduler:
        title: Post Scheduler SaaS
        repos: [acme/post-scheduler]
        planning:
          initiative: autonomous
      internal-tools:
        title: Internal CLI tools
        repos: [acme/dev-tools]
""")


def _write(fleet_dir: Path, text: str = PROJECTS_YAML) -> None:
    (fleet_dir / "projects.yaml").write_text(text)


def _load(fleet_dir: Path):
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return fleet


def _paths(fleet_dir: Path) -> Paths:
    return Paths(root=fleet_dir, fleet_dir=fleet_dir)


def _validated(fleet_dir: Path):
    return validate(_load(fleet_dir), _paths(fleet_dir))


def test_initiative_parses_and_defaults_to_none(fleet_dir):
    _write(fleet_dir)
    p = _load(fleet_dir).projects
    assert p["acme-shop"].planning.initiative == "propose"
    assert p["post-scheduler"].planning.initiative == "autonomous"
    # the asymmetric default: a project grants initiative explicitly, never via a root pull
    assert p["internal-tools"].planning.initiative == "none"


def test_planning_is_a_known_key(fleet_dir):
    _write(fleet_dir)
    report = _validated(fleet_dir)
    assert not [w for w in report.warnings if "planning" in w], report.warnings
    assert not [e for e in report.errors if "project" in e.lower()], report.errors


def test_bad_initiative_errors_with_did_you_mean(fleet_dir):
    _write(fleet_dir, "projects:\n  p:\n    title: P\n    repos: [a/b]\n"
                      "    planning: {initiative: proposed}\n")
    report = _validated(fleet_dir)
    hits = [e for e in report.errors if "proposed" in e]
    assert hits, report.errors
    assert "propose" in hits[0], "should suggest the closest valid initiative"


def test_unknown_planning_key_warns_and_never_grants(fleet_dir):
    _write(fleet_dir, "projects:\n  p:\n    title: P\n    repos: [a/b]\n"
                      "    planning: {initiatve: autonomous}\n")
    report = _validated(fleet_dir)
    hits = [w for w in report.warnings if "initiatve" in w]
    assert hits, f"nested unknown key must warn; got {report.warnings}"
    assert "initiative" in hits[0], "should suggest the closest planning key"
    assert _load(fleet_dir).projects["p"].planning.initiative == "none"


def test_planning_block_of_the_wrong_shape_is_a_load_error(fleet_dir):
    _write(fleet_dir, "projects:\n  p:\n    title: P\n    repos: [a/b]\n"
                      "    planning: autonomous\n")
    with pytest.raises(ValueError, match="planning"):
        _load(fleet_dir)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_projects_planning.py -q`
Expected: 5 failed — `AttributeError: 'ProjectConfig' object has no attribute 'planning'`, and the validator tests find no `initiative`/`initiatve` messages.

- [ ] **Step 3: The known values**

In `claudlobby/known_values.py`, directly after `VALID_TIERS` (line 124):

```python

# ── Projects planning initiative (projects.yaml) ─────────────────
# How a project's work is ORIGINATED — beside, and independent of, how it is
# CLOSED (VALID_TIERS). Tuple: error messages join them in this stable order,
# which is not a ranking. The default is `none`: a project grants initiative
# explicitly, and a root pull never makes a manager start originating work
# nobody opted into (manager check-in spec §8b; no silent switches).
VALID_INITIATIVES: tuple[str, ...] = ("autonomous", "propose", "none")
```

And at lines 186-188 add `"planning"` to the set:

```python
PROJECT_KEYS: frozenset[str] = frozenset(
    {"title", "repos", "mission_file", "validation", "planning"}
)
```

- [ ] **Step 4: The config dataclass and the parse**

In `claudlobby/config.py`, directly after `_PROJECT_VALIDATION_KEYS = {"tier", "preview", "notes"}` (line 260):

```python


@dataclass
class ProjectPlanningConfig:
    """Per-project ideation autonomy — how work is ORIGINATED (projects.yaml).

    `initiative` gates origination only: `autonomous` (the manager dispatches
    its own proposals; the operator holds a standing veto), `propose`
    (proposals wait in the intake store for the operator), `none` (the manager
    originates nothing here — the default). Closure rigor is `validation.tier`
    and the two never blend (manager check-in spec §8b). Values:
    VALID_INITIATIVES in known_values.py.
    """

    initiative: str = "none"
    # Unrecognized planning-block keys — the same .raw treatment as
    # validation, so a typo ('initiatve') warns instead of silently granting
    # or withholding initiative.
    raw: dict[str, Any] = field(default_factory=dict)


_PROJECT_PLANNING_KEYS = {"initiative"}
```

In `ProjectConfig` (line 273), directly after the `validation:` field:

```python
    planning: ProjectPlanningConfig = field(default_factory=ProjectPlanningConfig)
```

In `_coerce_project` (line 310+): after the `v = _shaped(... "validation" ...)` block (lines 314-319) add the sibling read, and after the `validation = ProjectValidationConfig(...)` construction (ends line 348) add the planning construction; then pass it into `ProjectConfig(...)` beside `validation=validation,`:

```python
    pl = _shaped(
        f"project '{key}': planning",
        d.get("planning"),
        dict,
        "planning: {initiative: none}",
    )
```

```python
    planning = ProjectPlanningConfig(
        initiative=_shaped(
            f"project '{key}': planning.initiative",
            pl.get("initiative"),
            str,
            "initiative: none",
        )
        or "none",
        raw={k: val for k, val in pl.items() if k not in _PROJECT_PLANNING_KEYS},
    )
```

```python
        validation=validation,
        planning=planning,
```

- [ ] **Step 5: The validator**

In `claudlobby/validator.py` add `VALID_INITIATIVES` to the `known_values` import (lines 39-42) and `_PROJECT_PLANNING_KEYS` beside the existing `_PROJECT_VALIDATION_KEYS` import from `.config`. Then, directly after the tier membership block (lines 1416-1421):

```python
        if project.planning.initiative not in VALID_INITIATIVES:
            report.errors.append(
                f"{label}: planning.initiative '{project.planning.initiative}' "
                f"is not one of {'/'.join(VALID_INITIATIVES)}"
                f"{hint(project.planning.initiative, VALID_INITIATIVES)}"
            )
```

and directly after the `for unknown in sorted(project.validation.raw):` loop (lines 1467-1471):

```python
        for unknown in sorted(project.planning.raw):
            report.warnings.append(
                f"{label}: unknown planning key "
                f"'{unknown}'{hint(unknown, _PROJECT_PLANNING_KEYS)}"
            )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_projects_planning.py tests/test_projects_tier.py tests/test_known_values.py -q`
Expected: all pass (the tier suite is the regression guard for `_coerce_project`; `test_known_values.py` pins the module's shape).

- [ ] **Step 7: Commit**

```bash
git add claudlobby/known_values.py claudlobby/config.py claudlobby/validator.py tests/test_projects_planning.py
printf '%s\n' 'feat(projects): planning.initiative — a project ideation grant beside validation.tier' '' 'autonomous | propose | none, default none (asymmetric with the tier on purpose:' 'a root pull must never make a manager originate work nobody opted into).' 'Parsed, did-you-mean validated, unknown planning keys warn. Spec §8b.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c1.txt"
git commit -q -F "$TMPDIR/c1.txt" && git log --oneline -1
```

---

### Task 2: `planning.initiative` — composition, the manager table, the reserved namespace, docs

**Files:**
- Modify: `claudlobby/composer.py:1078-1082` (in `compose_bot_conf`'s projects block)
- Modify: `templates/claude.md.j2:193-210`
- Modify: `claudlobby/validator.py:1387-1395` (the reserved-namespace tuple)
- Modify: `documentation/projects-yaml-schema.md:12-35` (+ a new section), `projects.yaml.example`
- Test: `tests/test_projects_planning.py` (append)

**Interfaces:**
- Produces: `export PROJECT_INITIATIVE_<SLUG>=<value>` in EVERY bot's `bot.conf`, beside `PROJECT_TIER_<SLUG>`; an `Initiative` column in the manager's `## Projects` table. The skill (Task 10) and the intake door (Task 7) read the env var.

- [ ] **Step 1: Append the failing tests**

```python
# append to tests/test_projects_planning.py
from claudlobby.composer import compose_bot_conf, compose_claude_md
from tests.conftest import install_real_template


def test_every_bot_conf_carries_the_initiative_map(fleet_dir):
    _write(fleet_dir)
    fleet = _load(fleet_dir)
    for bot_id in ("lead", "worker-1"):  # manager AND worker, like the tier map
        conf = compose_bot_conf(fleet.bots[bot_id], fleet, _paths(fleet_dir))
        assert "export PROJECT_INITIATIVE_ACME_SHOP=propose" in conf
        assert "export PROJECT_INITIATIVE_POST_SCHEDULER=autonomous" in conf
        assert "export PROJECT_INITIATIVE_INTERNAL_TOOLS=none" in conf
        assert "export PROJECT_TIER_ACME_SHOP=review" in conf  # beside the tier, never instead


def test_manager_projects_table_shows_the_initiative(fleet_dir):
    install_real_template(fleet_dir)
    _write(fleet_dir)
    fleet = _load(fleet_dir)
    md = compose_claude_md(fleet.bots["lead"], fleet, _paths(fleet_dir))
    assert "| Initiative |" in md
    assert "| acme-shop |" in md and "| propose |" in md


def test_initiative_env_namespace_is_reserved(fleet_dir):
    _write(fleet_dir)
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    worker-1:\n      expertise: [software-engineering]\n",
        "    worker-1:\n      expertise: [software-engineering]\n"
        "      env:\n        PROJECT_INITIATIVE_ACME_SHOP: autonomous\n",
    )
    (fleet_dir / "fleet.yaml").write_text(text)
    report = _validated(fleet_dir)
    assert any("PROJECT_INITIATIVE_ACME_SHOP" in e and "reserved" in e
               for e in report.errors), report.errors
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_projects_planning.py -q -k "initiative_map or shows_the_initiative or namespace"`
Expected: 3 failed (no `PROJECT_INITIATIVE_` line; no `| Initiative |`; no reserved error).

- [ ] **Step 3: Compose the env line**

In `claudlobby/composer.py`, directly after the `PROJECT_REPOS_` append (lines 1079-1082, inside `compose_bot_conf`):

```python
            # The ideation grant beside the closure bar (spec §8b): a sprint
            # or check-in resolves a project's `initiative` the same way it
            # resolves its tier — locally, from env, never from prose.
            lines.append(
                f"export PROJECT_INITIATIVE_{project.env_slug}="
                f"{_shq(project.planning.initiative)}"
            )
```

- [ ] **Step 4: The manager table**

In `templates/claude.md.j2` replace lines 197-206 (the paragraph + table header + row) with:

```jinja
The fleet serves these projects (projects.yaml). A project's validation tier
is what closing work in its repos requires — the tiers are alternatives, not
levels: auto: green CI · human: explicit operator approval · preview: preview
link + operator ack · review: reviewer verdict. Never close work without
meeting its project's declared tier. Its initiative is what you may ORIGINATE
there — autonomous: propose and dispatch your own work · propose: propose, and
wait for the operator · none: originate nothing (open work is still managed).

| Project | Title | Repos | Tier | Initiative | Mission |
|---------|-------|-------|------|------------|---------|
{% for p in projects -%}
| {{ p.key }} | {{ p.title }} | {{ p.repos | join(', ') }} | {{ p.validation.tier }} | {{ p.planning.initiative }} | {{ p.mission_file or '—' }} |
{% endfor %}
```

- [ ] **Step 5: The reserved namespace**

In `claudlobby/validator.py:1389` change the prefix tuple:

```python
                if env_key.startswith(("PROJECT_TIER_", "PROJECT_REPOS_", "PROJECT_INITIATIVE_")):
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_projects_planning.py tests/test_projects_tier.py tests/test_composer.py -q`
Expected: all pass (`test_composer.py` is the template-shape guard; if a test there pins the exact `## Projects` header row, update that pin to the six-column header).

- [ ] **Step 7: Documentation and the example**

In `documentation/projects-yaml-schema.md`: (a) in `## What composition emits` (line 14) extend the first bullet: ``…`PROJECT_TIER_<SLUG>`, `PROJECT_REPOS_<SLUG>` and `PROJECT_INITIATIVE_<SLUG>` per project…``, and the manager-table bullet: ``(key, title, repos, tier, initiative, mission pointer)``; (b) in the `## Fields` fence add, directly before the `validation:` line (29):

```yaml
    planning:               # optional; how work is ORIGINATED here (default initiative: none)
      initiative: autonomous | propose | none   # a grant, not a priority
```

(c) add this section directly before `## Reserved`:

```markdown
## Planning initiative

`planning.initiative` is how a project's work is **originated**; `validation.tier`
is how it is **closed**. The two blocks mirror each other phase to phase and never
blend: granting initiative changes nothing about what closing requires.

| Value | What the manager may originate here |
|---|---|
| `none` | nothing — the default. Open work is still dispatched, chased and re-routed. |
| `propose` | proposals into the intake store; each waits for the operator before it starts. |
| `autonomous` | proposals the manager dispatches itself; the operator holds a standing veto. |

`none` is the default deliberately — asymmetric with `validation.tier`'s `review` —
because a root pull must never make a manager start originating work nobody opted
into. There is no `priority:` field: a static `high` would monopolize the manager
and starve every other project; where attention goes is a soft, time-scoped
*focus* (manager check-in spec §8c). Composition flattens the grant to
`PROJECT_INITIATIVE_<SLUG>` beside the tier in every bot's `bot.conf`, and the
manager's `## Projects` table carries it as a column. Consumed by the `/checkin`
skill (`library/skills/checkin/SKILL.md`) and by `lib/checkin-propose.sh`, which
refuses to propose on a `none` project.
```

In `projects.yaml.example`, inside the `acme-shop` entry directly before its `validation:` block:

```yaml
    # How work is ORIGINATED here (independent of how it is closed, below).
    # none (default) — the manager originates nothing; propose — it proposes
    # and waits for you; autonomous — it dispatches its own proposals and you
    # hold a standing veto. See documentation/projects-yaml-schema.md.
    planning:
      initiative: propose
```

- [ ] **Step 8: Commit**

```bash
git add claudlobby/composer.py templates/claude.md.j2 claudlobby/validator.py documentation/projects-yaml-schema.md projects.yaml.example tests/test_projects_planning.py tests/test_composer.py
printf '%s\n' 'feat(projects): compose PROJECT_INITIATIVE_<SLUG> beside the tier; initiative column; reserved namespace' '' 'Every bot.conf carries the grant next to the closure bar; the manager table' 'shows it; an env: key in the namespace is a hard error like the tier keys.' 'Schema doc + example updated (no silent switches). Spec §8b.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c2.txt"
git commit -q -F "$TMPDIR/c2.txt" && git log --oneline -1
```

---

### Task 3: `requires:` — the reader and the compositor link

**Files:**
- Create: `claudlobby/requires.py`
- Modify: `claudlobby/composer.py:1429-1478` (`link_skills`), `:2788` (in `compose_bot`), the module's imports
- Test: `tests/test_requires_linking.py`

**Interfaces:**
- Produces: `requires.RequiresError(ValueError)`; `requires.read_requires(md_path: Path) -> dict[str, list[str]]`; `requires.read_required_role(md_path: Path) -> str | None`; `requires.protocol_files(paths, protocol_names) -> list[tuple[str, Path]]`; `requires.required_skills(paths, protocol_names, log=None) -> list[str]`; `link_skills(bot, paths, log, *, required: Sequence[str] = ())`.
- Consumes: `composer.resolve_effective_protocols(bot, fleet, paths, *, is_manager)` (`composer.py:1737`) — the ONE definition of a bot's protocols; `paths.find_library_file("protocols", name, ".md")`, `paths.expand_library_folder("protocols", dir)` → `dict[str, Path]`, `paths.find_library_dir("skills", name)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_requires_linking.py
"""Equipment linking — `requires:` frontmatter (manager check-in spec §10).

A protocol may declare the skills it needs; the compositor links them wherever
the protocol composes. Read by re-parsing the file (the loader._read_tool_grants
pattern): LibraryItem carries title/description/body only."""

from pathlib import Path

import pytest

from claudlobby.composer import compose_bot
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.requires import (
    RequiresError,
    read_required_role,
    read_requires,
    required_skills,
)

PROTOCOL = "---\ntitle: Check-in\nrequires:\n  skills: [ck]\n---\n\n# Check-in\n\nBody.\n"


def _paths(fleet_dir: Path) -> Paths:
    return Paths(root=fleet_dir, fleet_dir=fleet_dir)


def _write_skill(fleet_dir: Path, name: str) -> None:
    d = fleet_dir / "library" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: t\n---\n\n# {name}\n")


def _write_protocol(fleet_dir: Path, name: str, text: str = PROTOCOL) -> None:
    (fleet_dir / "library" / "protocols" / f"{name}.md").write_text(text)


def _equip(fleet_dir: Path, bot: str, protocol: str) -> None:
    text = (fleet_dir / "fleet.yaml").read_text()
    anchor = f"    {bot}:\n"
    assert anchor in text
    (fleet_dir / "fleet.yaml").write_text(
        text.replace(anchor, anchor + f"      protocols: [{protocol}]\n", 1))


def _compose(fleet_dir: Path, bot: str) -> Path:
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return compose_bot(fleet.bots[bot], fleet, _paths(fleet_dir), log=lambda _m: None)


def test_read_requires_returns_the_mapping(tmp_path):
    p = tmp_path / "x.md"
    p.write_text(PROTOCOL)
    assert read_requires(p) == {"skills": ["ck"]}


def test_read_requires_is_empty_when_absent(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\ntitle: T\n---\n\n# T\n")
    assert read_requires(p) == {}
    assert read_required_role(p) is None


def test_read_required_role(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\ntitle: T\nrequires:\n  skills: [ck]\n  role: leaf-manager\n---\n\n# T\n")
    assert read_requires(p) == {"skills": ["ck"]}     # role is not an entity type
    assert read_required_role(p) == "leaf-manager"


@pytest.mark.parametrize("bad", [
    "requires: ck\n",                 # not a mapping
    "requires:\n  skills: ck\n",      # not a list
    "requires:\n  skills: [1]\n",     # not strings
    "requires:\n  mcp: [x]\n",        # not linkable in v1 (skills only)
    "requires:\n  role: [x]\n",       # role is one name
])
def test_read_requires_refuses_malformed(tmp_path, bad):
    p = tmp_path / "x.md"
    p.write_text(f"---\ntitle: T\n{bad}---\n\n# T\n")
    with pytest.raises(RequiresError):
        read_requires(p)


def test_a_protocol_requirement_links_the_skill(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test")
    _write_skill(fleet_dir, "ck")
    _equip(fleet_dir, "lead", "checkin-test")
    link = _compose(fleet_dir, "lead") / ".claude" / "skills" / "ck"
    assert link.is_symlink()
    assert link.resolve() == (fleet_dir / "library" / "skills" / "ck").resolve()


def test_a_bot_without_the_protocol_gets_no_link(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test")
    _write_skill(fleet_dir, "ck")
    _equip(fleet_dir, "lead", "checkin-test")
    assert not (_compose(fleet_dir, "worker-1") / ".claude" / "skills" / "ck").exists()


def test_dropping_the_protocol_drops_the_requirement(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test")
    _write_skill(fleet_dir, "ck")
    _equip(fleet_dir, "lead", "checkin-test")
    assert (_compose(fleet_dir, "lead") / ".claude" / "skills" / "ck").is_symlink()
    text = (fleet_dir / "fleet.yaml").read_text().replace("      protocols: [checkin-test]\n", "")
    (fleet_dir / "fleet.yaml").write_text(text)
    assert not (_compose(fleet_dir, "lead") / ".claude" / "skills" / "ck").exists()


def test_a_directly_declared_skill_is_linked_once_and_survives(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test")
    _write_skill(fleet_dir, "ck")
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n", "    lead:\n      skills: [ck]\n      protocols: [checkin-test]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    skills = _compose(fleet_dir, "lead") / ".claude" / "skills"
    assert (skills / "ck").is_symlink()
    assert [p.name for p in skills.iterdir()] == ["ck"]


def test_a_missing_required_skill_is_skipped_at_compose(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test")     # requires ck, which is never written
    _equip(fleet_dir, "lead", "checkin-test")
    seen: list[str] = []
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    compose_bot(fleet.bots["lead"], fleet, _paths(fleet_dir), log=seen.append)
    assert any("required skill 'ck' missing" in m for m in seen), seen


def test_required_skills_is_deduped_and_ordered(fleet_dir):
    for name in ("p1", "p2"):
        _write_protocol(fleet_dir, name, "---\ntitle: P\nrequires:\n  skills: [ck, other]\n---\n\n# P\n")
    assert required_skills(_paths(fleet_dir), ["p1", "p2", "no-such-protocol"]) == ["ck", "other"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_requires_linking.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'claudlobby.requires'`.

- [ ] **Step 3: The reader module**

```python
# claudlobby/requires.py
"""Equipment linking — the `requires:` frontmatter (manager check-in spec §10).

A library item may declare what it needs to ship with:

    ---
    title: Check-in
    requires:
      skills: [checkin]
      role: leaf-manager      # optional: the role this item is written for
    ---

v1 scope is protocols -> skills. The schema is generic on purpose
(`requires.<entity_type>: [names]`) so protocols -> guardrails or skills -> mcp
can follow without a format change; any other entity type is REFUSED today,
because a requirement nobody links is a promise the compositor cannot keep.

Read by re-parsing the file (the `loader._read_tool_grants` pattern): a
LibraryItem carries title/description/body only, and widening it would also
mean widening `composer._expand_item`, which rebuilds the item field by field.
`LibraryItem.source_path` survives that rebuild, which is why a targeted
re-read is the cheaper seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from .loader import parse_frontmatter

REQUIRES_KEY = "requires"
ROLE_KEY = "role"
#: Entity types the compositor can LINK today. A requirement on any other
#: type is malformed rather than ignored — see the module docstring.
SUPPORTED_ENTITY_TYPES: tuple[str, ...] = ("skills",)


class RequiresError(ValueError):
    """A malformed `requires:` block. The validator reports it; compose skips it."""


def _block(md_path: Path) -> dict | None:
    if not md_path.is_file():
        return None
    try:
        fm, _ = parse_frontmatter(md_path.read_text())
    except OSError:
        return None
    block = fm.get(REQUIRES_KEY)
    if block is None:
        return None
    if not isinstance(block, dict):
        raise RequiresError(
            f"{md_path.name}: requires: must be a mapping of entity type to names")
    return block


def read_requires(md_path: Path) -> dict[str, list[str]]:
    """The entity requirements declared in ``md_path``'s frontmatter.

    ``{}`` when the file or the key is absent. Raises RequiresError when the
    block is present but is not ``{<entity_type>: [str, ...]}`` over
    SUPPORTED_ENTITY_TYPES. ``role`` is checked for shape here but returned by
    `read_required_role`, because it is a scope, not an entity to link.
    """
    block = _block(md_path)
    if block is None:
        return {}
    out: dict[str, list[str]] = {}
    for key, names in block.items():
        if key == ROLE_KEY:
            if not isinstance(names, str) or not names:
                raise RequiresError(f"{md_path.name}: requires.role must be one role name")
            continue
        if key not in SUPPORTED_ENTITY_TYPES:
            raise RequiresError(
                f"{md_path.name}: requires.{key} is not linkable "
                f"(supported: {', '.join(SUPPORTED_ENTITY_TYPES)})")
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise RequiresError(f"{md_path.name}: requires.{key} must be a list of names")
        out[key] = list(names)
    return out


def read_required_role(md_path: Path) -> str | None:
    """The ``requires.role`` of ``md_path`` — the role the item is written for —
    or None. Malformed -> RequiresError."""
    block = _block(md_path)
    if block is None or ROLE_KEY not in block:
        return None
    role = block[ROLE_KEY]
    if not isinstance(role, str) or not role:
        raise RequiresError(f"{md_path.name}: requires.role must be one role name")
    return role


def protocol_files(paths, protocol_names: Iterable[str]) -> list[tuple[str, Path]]:
    """``(name, path)`` for each resolvable protocol, folder entries expanded —
    the resolution `loader.load_library_items_overlay` performs, minus the load.
    An unresolvable name is skipped: the validator already warns about it."""
    out: list[tuple[str, Path]] = []
    for name in protocol_names:
        if name.endswith("/"):
            folder = paths.expand_library_folder("protocols", name.rstrip("/"))
            for leaf, path in sorted(folder.items()):
                out.append((f"{name}{leaf}", path))
            continue
        path = paths.find_library_file("protocols", name, ".md")
        if path is not None:
            out.append((name, path))
    return out


def required_skills(paths, protocol_names: Iterable[str],
                    log: Callable[[str], None] | None = None) -> list[str]:
    """Skills the given protocols require — deduped, first-seen order.

    A malformed block is skipped with a log line: compose never crashes on
    library content (the loader's own posture); `claudlobby validate` is where
    it fails.
    """
    out: list[str] = []
    for name, path in protocol_files(paths, protocol_names):
        try:
            req = read_requires(path)
        except RequiresError as exc:
            if log:
                log(f"  protocol '{name}': {exc} — requirement skipped (run claudlobby validate)")
            continue
        for skill in req.get("skills", ()):
            if skill not in out:
                out.append(skill)
    return out
```

- [ ] **Step 4: `link_skills` grows a second source**

In `claudlobby/composer.py` change the signature at line 1429 and append the loop after the `for skill in bot.skills:` loop (ends line 1478):

```python
def link_skills(bot: BotConfig, paths: Paths, log, *,
                required: "Sequence[str]" = ()) -> None:
```

```python
    # Equipment linking (requires: frontmatter — claudlobby/requires.py): a
    # skill a resolved protocol requires is linked exactly like a declared
    # one, AFTER the declared list so a declared skill wins a leaf collision
    # and a skill that is both declared and required links once.
    for skill in required:
        src = paths.find_library_dir("skills", skill)
        if src is None:
            log(f"  required skill '{skill}' missing — skipped (run claudlobby validate)")
            continue
        if src.name in linked:
            continue
        _add(src.name, src)
        log(f"  skill '{src.name}' linked by protocol requirement")
```

Add `from typing import Sequence` to the module's typing imports if it is not already there, and `from . import requires as _requires` beside the other package imports.

- [ ] **Step 5: `compose_bot` computes the requirements from the ONE protocol definition**

Replace line 2788 (`link_skills(bot, paths, _emit)`) with:

```python
    # The protocols this bot actually composes — declared plus gated defaults,
    # the ONE definition (resolve_effective_protocols) — decide which skills
    # arrive by requirement. Computed here rather than inside compose_claude_md
    # because that function returns rendered text, not names.
    _effective = resolve_effective_protocols(
        bot, fleet, paths, is_manager=bot.bot_id in fleet.manager_bots())
    link_skills(bot, paths, _emit,
                required=_requires.required_skills(paths, _effective, log=_emit))
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_requires_linking.py tests/test_composer.py tests/test_shared_docs_default.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add claudlobby/requires.py claudlobby/composer.py tests/test_requires_linking.py
printf '%s\n' 'feat(compositor): requires: frontmatter — a protocol brings the skills it needs' '' 'claudlobby/requires.py reads the block (re-parse, the tool_grants pattern);' 'compose_bot resolves the effective protocols once and link_skills links their' 'requirements after the declared list. Malformed blocks skip at compose with a' 'log line (the validator is where they fail, next task). Spec §10.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c3.txt"
git commit -q -F "$TMPDIR/c3.txt" && git log --oneline -1
```

---
### Task 4: `requires:` — validation, `list-library`, documentation

**Files:**
- Modify: `claudlobby/validator.py:797-816` (after the protocol-reference loop in `_validate_bots`), `:1569-1612` (`_validate_library_frontmatter`)
- Modify: `claudlobby/commands/core.py:320-379` (`cmd_list_library`)
- Modify: `library/protocols/README.md`, `documentation/fleet-yaml-schema.md:508-511`
- Test: `tests/test_requires_linking.py` (append)

**Interfaces:**
- Consumes: `requires.protocol_files`, `requires.read_requires`, `composer.resolve_effective_protocols` (imported locally inside the validator function — the validator already imports the composer locally at `validator.py:102,126` because the composer imports `config`, not the validator).
- Produces: validator error text containing `requires skill '<name>'`; `list-library` lines of the shape `  <protocol>  (requires skills: a, b)`.

- [ ] **Step 1: Append the failing tests**

```python
# append to tests/test_requires_linking.py
import logging
from types import SimpleNamespace

from claudlobby.commands.core import cmd_list_library
from claudlobby.validator import validate


def _validated(fleet_dir: Path):
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return validate(fleet, _paths(fleet_dir))


def test_validator_errors_on_a_requirement_that_does_not_resolve(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test")      # requires ck, never written
    _equip(fleet_dir, "lead", "checkin-test")
    report = _validated(fleet_dir)
    hits = [e for e in report.errors if "checkin-test" in e and "requires skill 'ck'" in e]
    assert hits, report.errors
    assert "lead" in hits[0]


def test_validator_errors_on_malformed_requires_anywhere_in_the_library(fleet_dir):
    _write_protocol(fleet_dir, "bad", "---\ntitle: B\nrequires: ck\n---\n\n# B\n")
    report = _validated(fleet_dir)                  # nobody declares bad.md
    hits = [e for e in report.errors if "bad.md" in e and "requires" in e]
    assert hits, report.errors


def test_validator_is_quiet_when_the_requirement_resolves(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test")
    _write_skill(fleet_dir, "ck")
    _equip(fleet_dir, "lead", "checkin-test")
    report = _validated(fleet_dir)
    assert not [e for e in report.errors if "requires" in e], report.errors


def test_list_library_annotates_requirements(fleet_dir, caplog):
    _write_protocol(fleet_dir, "checkin-test")
    args = SimpleNamespace(root=str(fleet_dir), fleet=None, seed=False,
                           bot=None, strict=False, verbose=False)
    with caplog.at_level(logging.INFO):
        assert cmd_list_library(args) == 0
    assert "checkin-test  (requires skills: ck)" in caplog.text
```

(If the CLI's logger does not propagate to `caplog` — check how `tests/test_main.py` captures `list-library` output — assert on that capture instead; the string is the same.)

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_requires_linking.py -q -k "validator or list_library"`
Expected: 3 failed (no such errors; no annotation) and 1 passed (the quiet case is quiet already).

- [ ] **Step 3: The per-bot requirement check**

In `claudlobby/validator.py`, directly after the protocol/guardrail reference loop (ends line 816, still inside `for bot_name, bot in fleet.bots.items():` of `_validate_bots`):

```python
        # Equipment linking (requires: — claudlobby/requires.py). Checked over
        # the protocols this bot will ACTUALLY compose, declared plus gated
        # defaults, through the composer's one definition — the reference
        # warnings above walk the declared list only. An unresolvable
        # requirement is an ERROR, not a warning: the protocol's body will tell
        # the bot to run a skill it does not have. A malformed block is
        # reported once, library-wide, by _validate_library_frontmatter.
        from . import requires as _requires  # local: keeps validator import-light
        from .composer import resolve_effective_protocols  # local: composer imports config, not us
        _effective = resolve_effective_protocols(
            bot, fleet, paths, is_manager=bot_name in fleet.manager_bots())
        for pname, ppath in _requires.protocol_files(paths, _effective):
            try:
                req = _requires.read_requires(ppath)
            except _requires.RequiresError:
                continue
            for skill in req.get("skills", ()):
                if paths.find_library_dir("skills", skill) is None:
                    report.errors.append(
                        f"bot '{bot_name}': protocol '{pname}' requires skill "
                        f"'{skill}', which is not in any library/skills/ — the "
                        f"protocol would tell the bot to run a skill it does not have"
                    )
```

- [ ] **Step 4: The library-wide shape check**

In `_validate_library_frontmatter` (line 1569+), after the `err = frontmatter_error(text)` block inside the loop, add:

```python
            # requires: is validated for SHAPE here, once per file, because
            # the per-bot check above only sees equipped protocols and a
            # malformed block on an unequipped one would otherwise wait for
            # its first equipper to surface.
            if md.parent.name == "protocols" or "protocols" in md.parts:
                from . import requires as _requires
                try:
                    _requires.read_requires(md)
                    _requires.read_required_role(md)
                except _requires.RequiresError as exc:
                    try:
                        rel = md.relative_to(root)
                    except ValueError:
                        rel = md
                    report.errors.append(f"malformed requires in library file '{rel}': {exc}")
```

- [ ] **Step 5: `list-library` shows requirements**

In `claudlobby/commands/core.py` change `_list_md` (lines 323-344): give it an optional annotator and keep the path beside the tag.

```python
    def _list_md(label: str, kind: str, annotate=None):
        """Walk overlay → base recursively. Display nested files as `dir/name`."""
        log.info("%s:", label)
        seen: dict[str, tuple[str, Path]] = {}  # rel_key (no .md) → (tag, path)
        for d in paths.library_search_dirs(kind):
            if not d.is_dir():
                continue
            tag = (
                "[overlay]"
                if (paths.overlay_library and d == paths.overlay_library / kind)
                else "[base]"
            )
            for p in sorted(d.rglob("*.md")):
                if p.stem.lower().startswith("readme"):
                    continue
                rel_key = str(p.relative_to(d).with_suffix(""))
                if rel_key not in seen:
                    seen[rel_key] = (tag, p)
        for rel_key, (tag, p) in sorted(seen.items()):
            marker = " (override)" if tag == "[overlay]" else ""
            note = annotate(p) if annotate else ""
            log.info("  %s%s%s", rel_key, marker, note)
```

and directly above the `_list_md("Protocols", "protocols")` call (line 353):

```python
    from .. import requires as _requires

    def _requires_note(p: Path) -> str:
        try:
            req = _requires.read_requires(p)
            role = _requires.read_required_role(p)
        except _requires.RequiresError:
            return "  (requires: MALFORMED — run claudlobby validate)"
        parts = [f"{k}: {', '.join(v)}" for k, v in req.items()]
        if role:
            parts.append(f"role: {role}")
        return f"  (requires {'; '.join(parts)})" if parts else ""

    _list_md("Protocols", "protocols", annotate=_requires_note)
```

(Confirm `Path` is imported at the top of `core.py`; add `from pathlib import Path` if not.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_requires_linking.py tests/test_validator.py tests/test_main.py -q`
Expected: all pass.

- [ ] **Step 7: Documentation**

`library/protocols/README.md` — replace the sentence `Protocols should be composable — avoid hard dependencies between them.` with:

```markdown
Protocols should be composable. A protocol that depends on a skill **declares** it — `requires: {skills: [name]}` in the frontmatter — and the compositor links the skill wherever the protocol composes (`claudlobby/requires.py`); an *undeclared* dependency is the hazard, not a declared one.
```

and add, after the `## Composition` section:

```markdown
## Requirements (`requires:`)

```yaml
---
title: Check-in
requires:
  skills: [checkin]        # linked into every equipping bot's .claude/skills/
  role: leaf-manager       # optional: the role this protocol is written for
---
```

`skills` is the only linkable entity type today (the key is generic so guardrails or MCP fragments can follow without a format change). A requirement the library cannot resolve is a `validate`/`generate` **error**; a `role` the equipping bot does not hold is a **warning** — the skill still links and runs by hand, only a role-gated trigger stays quiet. `list-library` shows each protocol's requirements.
```

`documentation/fleet-yaml-schema.md` — in the prose under the `### bots.<name>.guardrails / protocols / …` heading (line 510) add one sentence: ``A protocol may carry `requires: {skills: [...]}`; those skills are linked wherever the protocol composes, and dropped when it is (see `library/protocols/README.md`).``

- [ ] **Step 8: Commit**

```bash
git add claudlobby/validator.py claudlobby/commands/core.py library/protocols/README.md documentation/fleet-yaml-schema.md tests/test_requires_linking.py
printf '%s\n' 'feat(validator): requires: is an error when unresolvable, malformed anywhere; list-library shows it' '' 'Per-bot over the EFFECTIVE protocols (the composer one definition), library-wide' 'for shape. Docs: protocols README + fleet schema. Spec §10.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c4.txt"
git commit -q -F "$TMPDIR/c4.txt" && git log --oneline -1
```

---

### Task 5: The `leaf-manager` role and the `requires.role` warning

**Files:**
- Modify: `claudlobby/config.py:735-757` (after `manager_bots`)
- Modify: `claudlobby/defaults.py:360-384`
- Modify: `claudlobby/composer.py:1737-1766` (`resolve_effective_protocols`)
- Modify: `claudlobby/validator.py` (the requirement loop from Task 4)
- Test: `tests/test_leaf_manager_role.py`; `tests/test_requires_linking.py` (append)

**Interfaces:**
- Produces: `FleetConfig.leaf_manager_bots() -> set[str]`; `defaults.ROLE_LEAF_MANAGER = "leaf-manager"`; `defaults.DETECTABLE_ROLES = frozenset({ROLE_MANAGER, ROLE_LEAF_MANAGER})`; `defaults.roles_for(bot_id: str, fleet) -> tuple[str, ...]` — THE one place the role predicates are applied.
- Consumed by chunk 5 (`REGISTRY["protocols"].roles["leaf-manager"]`) and by the validator's role warning.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_leaf_manager_role.py
"""The `leaf-manager` role (manager check-in spec §10): a manager at least one
of whose in-fleet reports is not itself a manager — the bot that dispatches to
WORKERS. `manager_bots` is deliberately wide (a coordinator whose reports are
managers counts); a default that makes a manager reason over its workers'
portfolio must not also land on the coordinator above it."""

from dataclasses import replace
from pathlib import Path
from textwrap import dedent

from claudlobby import defaults
from claudlobby.composer import resolve_effective_protocols
from claudlobby.config import load_fleet
from claudlobby.paths import Paths

HEAD = dedent("""\
    fleet:
      name: test-fleet
      service_prefix: com.test
      telegram_group_chat_id: "-100999"
      accounts:
        default: ~/.claude
      defaults:
        model: opus
""")


def _load(fleet_dir: Path, body: str):
    (fleet_dir / "fleet.yaml").write_text(HEAD + dedent(body))
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return fleet


def test_a_team_manager_with_workers_is_leaf(fleet_dir):
    fleet = _load(fleet_dir, """\
      teams:
        eng: {manager: lead, workers: [w1]}
      bots:
        lead: {expertise: [orchestration]}
        w1: {expertise: [software-engineering]}
    """)
    assert fleet.leaf_manager_bots() == {"lead"}


def test_a_coordinator_of_managers_is_not_leaf(fleet_dir):
    fleet = _load(fleet_dir, """\
      teams:
        eng: {manager: lead, workers: [w1]}
      bots:
        boss: {expertise: [orchestration], manages: [lead]}
        lead: {expertise: [orchestration]}
        w1: {expertise: [software-engineering]}
    """)
    assert fleet.manager_bots() == {"boss", "lead"}
    assert fleet.leaf_manager_bots() == {"lead"}


def test_a_cross_fleet_report_counts_as_a_report(fleet_dir):
    fleet = _load(fleet_dir, """\
      bots:
        boss: {expertise: [orchestration], manages: [elsewhere-bot]}
    """)
    assert fleet.leaf_manager_bots() == {"boss"}   # unresolvable here: the conservative direction is to equip


def test_a_manager_naming_no_reports_is_not_leaf(fleet_dir):
    fleet = _load(fleet_dir, """\
      teams:
        eng: {manager: lead, workers: []}
      bots:
        lead: {expertise: [orchestration]}
    """)
    assert fleet.leaf_manager_bots() == set()


def test_roles_for_applies_both_predicates(fleet_dir):
    fleet = _load(fleet_dir, """\
      teams:
        eng: {manager: lead, workers: [w1]}
      bots:
        boss: {expertise: [orchestration], manages: [lead]}
        lead: {expertise: [orchestration]}
        w1: {expertise: [software-engineering]}
    """)
    assert defaults.roles_for("lead", fleet) == (defaults.ROLE_MANAGER, defaults.ROLE_LEAF_MANAGER)
    assert defaults.roles_for("boss", fleet) == (defaults.ROLE_MANAGER,)
    assert defaults.roles_for("w1", fleet) == ()
    assert defaults.ROLE_LEAF_MANAGER in defaults.DETECTABLE_ROLES


def test_the_composer_passes_the_leaf_role_to_the_overlay(fleet_dir, monkeypatch):
    fleet = _load(fleet_dir, """\
      teams:
        eng: {manager: lead, workers: [w1]}
      bots:
        boss: {expertise: [orchestration], manages: [lead]}
        lead: {expertise: [orchestration]}
        w1: {expertise: [software-engineering]}
    """)
    (fleet_dir / "library" / "protocols" / "probe-proto.md").write_text("---\ntitle: P\n---\n\n# P\n")
    d = defaults.REGISTRY["protocols"]
    monkeypatch.setitem(defaults.REGISTRY, "protocols",
                        replace(d, roles={**d.roles, defaults.ROLE_LEAF_MANAGER: ("probe-proto",)}))
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
    assert "probe-proto" in resolve_effective_protocols(fleet.bots["lead"], fleet, paths, is_manager=True)
    assert "probe-proto" not in resolve_effective_protocols(fleet.bots["boss"], fleet, paths, is_manager=True)
    assert "probe-proto" not in resolve_effective_protocols(fleet.bots["w1"], fleet, paths, is_manager=False)
```

And append to `tests/test_requires_linking.py`:

```python
ROLE_PROTOCOL = "---\ntitle: C\nrequires:\n  skills: [ck]\n  role: leaf-manager\n---\n\n# C\n"


def test_validator_warns_when_a_bot_outside_the_role_declares_the_protocol(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test", ROLE_PROTOCOL)
    _write_skill(fleet_dir, "ck")
    _equip(fleet_dir, "worker-1", "checkin-test")
    report = _validated(fleet_dir)
    hits = [w for w in report.warnings if "worker-1" in w and "leaf-manager" in w]
    assert hits, report.warnings
    assert "still link" in hits[0]
    assert not [e for e in report.errors if "requires" in e]   # a warning, never an error


def test_validator_is_quiet_when_the_bot_holds_the_role(fleet_dir):
    _write_protocol(fleet_dir, "checkin-test", ROLE_PROTOCOL)
    _write_skill(fleet_dir, "ck")
    _equip(fleet_dir, "lead", "checkin-test")          # lead manages worker-1: a leaf manager
    report = _validated(fleet_dir)
    assert not [w for w in report.warnings if "leaf-manager" in w], report.warnings
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_leaf_manager_role.py tests/test_requires_linking.py -q -k "leaf or role"`
Expected: failures — `AttributeError: 'FleetConfig' object has no attribute 'leaf_manager_bots'`, `module 'claudlobby.defaults' has no attribute 'ROLE_LEAF_MANAGER'`, no role warning.

- [ ] **Step 3: The predicate**

In `claudlobby/config.py`, directly after `manager_bots` (ends line 757):

```python
    def leaf_manager_bots(self) -> set[str]:
        """Managers at least one of whose in-fleet reports is not itself a
        manager — the bots that dispatch to WORKERS.

        `manager_bots` is deliberately wide: a coordinator whose reports are
        managers counts, and the composed MANAGER_TMUX self-pointer follows
        the same set, so neither can tell the two apart. A default that makes
        a manager reason over its workers' portfolio must not also land on the
        coordinator above it — two managers reasoning over one portfolio is
        the pile-on the check-in's allocation rule forbids. A report this
        manifest cannot resolve (a cross-fleet `manages:` target) counts as a
        report: the conservative direction is to equip. A manager naming no
        reports at all has nobody to dispatch to and is not a leaf.
        """
        managers = self.manager_bots()
        leaves: set[str] = set()
        for name in managers:
            reports: set[str] = set()
            for team in self.teams.values():
                if team.manager == name:
                    reports.update(team.workers)
            bot = self.bots.get(name)
            if bot is not None and bot.manages:
                reports.update(bot.manages)
            if any(r not in managers for r in reports):
                leaves.add(name)
        return leaves
```

- [ ] **Step 4: The role**

In `claudlobby/defaults.py` replace lines 380-384 with:

```python
ROLE_MANAGER = "manager"
#: A manager at least one of whose in-fleet reports is not itself a manager —
#: `FleetConfig.leaf_manager_bots`. The check-in default (manager check-in
#: spec §10) registers here, not under ROLE_MANAGER, so a coordinator whose
#: reports are managers is not equipped by default.
ROLE_LEAF_MANAGER = "leaf-manager"

#: Roles the composer can currently DETECT. Adding a name here without a
#: predicate that resolves it is the trap the note above describes.
DETECTABLE_ROLES: frozenset[str] = frozenset({ROLE_MANAGER, ROLE_LEAF_MANAGER})


def roles_for(bot_id: str, fleet) -> tuple[str, ...]:
    """The detectable roles ``bot_id`` holds in ``fleet`` — THE one place the
    predicates behind DETECTABLE_ROLES are applied, so the composer and the
    validator cannot disagree about who is a manager. ``fleet`` is a
    FleetConfig (duck-typed: this module must not import config)."""
    roles: tuple[str, ...] = ()
    if bot_id in fleet.manager_bots():
        roles += (ROLE_MANAGER,)
        if bot_id in fleet.leaf_manager_bots():
            roles += (ROLE_LEAF_MANAGER,)
    return roles
```

Update the KNOWN BOUND comment above it (lines 364-368): `Today exactly one role is detectable` → `Today two roles are detectable — manager and leaf-manager, both derived from FleetConfig.manager_bots/leaf_manager_bots`.

- [ ] **Step 5: The composer's role tuple**

In `resolve_effective_protocols` (`composer.py:1762`) replace `roles = (defaults.ROLE_MANAGER,) if is_manager else ()` with:

```python
        roles = defaults.roles_for(bot.bot_id, fleet) if is_manager else ()
```

- [ ] **Step 6: The validator's role warning**

In the requirement loop added in Task 4, inside `try:` read the role too, and after the skills loop add the warning:

```python
            try:
                req = _requires.read_requires(ppath)
                role = _requires.read_required_role(ppath)
            except _requires.RequiresError:
                continue
```

```python
            if role and role not in defaults.roles_for(bot_name, fleet):
                report.warnings.append(
                    f"bot '{bot_name}': protocol '{pname}' is written for role "
                    f"'{role}', which this bot does not hold — its required "
                    f"skills still link and run by hand, but a role-gated "
                    f"trigger will not fire for it"
                )
```

(`from . import defaults` at the validator's top-level imports if it is not already imported there.)

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_leaf_manager_role.py tests/test_requires_linking.py tests/test_defaults_registry.py tests/test_shared_docs_default.py tests/test_naked_bot_observe.py -q`
Expected: all pass — `test_only_detectable_roles_are_declared` (`test_defaults_registry.py:157`) still holds because no registry entry names the new role yet.

- [ ] **Step 8: Commit**

```bash
git add claudlobby/config.py claudlobby/defaults.py claudlobby/composer.py claudlobby/validator.py tests/test_leaf_manager_role.py tests/test_requires_linking.py
printf '%s\n' 'feat(defaults): the leaf-manager role — a manager whose reports include a worker' '' 'FleetConfig.leaf_manager_bots(); defaults.roles_for() is the one place the role' 'predicates apply; the composer passes it to the overlay; the validator warns' 'when a protocol written for a role lands on a bot outside it. Spec §10.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c5.txt"
git commit -q -F "$TMPDIR/c5.txt" && git log --oneline -1
```

---

### Task 6: The decision record — severities, `checkin-contract.py`, `checkin-record.sh`

**Files:**
- Modify: `claudlobby/plane/registries.py:54-123` (`SYSTEM_EVENT_SEVERITY`)
- Create: `lib/checkin-contract.py`, `lib/checkin-record.sh`
- Test: `tests/test_checkin_contract.py`, `tests/test_checkin_doors.py`

**Interfaces:**
- Produces: `checkin-contract.py`: `normalize(obj) -> dict` (raises `ContractError(reasons: list[str])`), `mint_checkin_id() -> "ck_<32hex>"`, CLI filter stdin→stdout (rc 0 / 2). `checkin-record.sh [--bot B] [--fleet F] [--dry-run] < decision.json` → stdout `ck_<32hex>`; rc 0 recorded, 1 not recorded, 2 contract violation, 3 no identity. The plane row: `kind='system'`, `event='checkin_decision'`, `source_ref='checkin:<ck_id>'`, `subject_alias='bot:<fleet>/<bot>'`, `detail` = the normalized record, `severity='notice'`.
- Consumed by the skill (Task 10), the read door (Task 8), the empirical gate (Task 11).

- [ ] **Step 1: Write the failing contract tests**

```python
# tests/test_checkin_contract.py
"""The schema-1 decision record (manager check-in spec §7): lib/checkin-contract.py
(stdlib, the dispatch-overdue.py precedent) and the severity registration."""

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

spec = importlib.util.spec_from_file_location("checkin_contract", CONTRACT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


def _decision(**over) -> dict:
    d = {
        "inputs_seen": {"open_tasks": 2, "stalls": 0, "unacked": 1, "issues_considered": 4,
                        "knowledge_hits": 1, "focus_declared": ["shop"],
                        "focus_empirical_top": ["shop"], "unavailable": []},
        "delta": {"tasks_opened": 0, "tasks_completed": 1, "stalls_appeared": 0,
                  "stalls_cleared": 0, "issues_new": 1, "messages_new": 0, "held_pending": 0},
        "action": "nothing",
        "project_key": None,
        "rationale": "All work in flight; nothing new worth starting.",
        "raise": {"decided": False, "reason": "no delta the operator would want", "held": []},
        "targets": {"assignment_ids": [], "work_item_ids": [], "msg_id": None},
    }
    d.update(over)
    return d


def test_a_minimal_decision_normalizes_and_mints_the_id():
    out = cc.normalize(_decision())
    assert out["schema"] == 1
    assert out["checkin_id"].startswith("ck_") and len(out["checkin_id"]) == 35
    assert out["prev_checkin_id"] is None
    assert out["action"] == "nothing"
    assert out["raise"] == {"decided": False, "reason": "no delta the operator would want", "held": []}


def test_a_supplied_id_and_prev_are_kept():
    prev = cc.mint_checkin_id()
    mine = cc.mint_checkin_id()
    out = cc.normalize(_decision(checkin_id=mine, prev_checkin_id=prev))
    assert out["checkin_id"] == mine and out["prev_checkin_id"] == prev


@pytest.mark.parametrize("over, needle", [
    ({"action": "dispatch"}, "must name project_key"),
    ({"action": "ask"}, "raise.decided"),
    ({"action": "coffee"}, "action must be one of"),
    ({"rationale": "x" * 601}, "<= 600"),
    ({"rationale": ""}, "rationale"),
    ({"project_key": "Not-A-Slug"}, "project_key"),
    ({"prev_checkin_id": "nope"}, "prev_checkin_id"),
    ({"inputs_seen": {"open_tasks": -1}}, "inputs_seen.open_tasks"),
    ({"raise": {"decided": "yes"}}, "raise.decided"),
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
    r = subprocess.run([sys.executable, str(CONTRACT)], input="not json",
                       capture_output=True, text=True)
    assert r.returncode == 2 and "not JSON" in r.stderr
    r = subprocess.run([sys.executable, str(CONTRACT)], input=json.dumps(_decision(action="coffee")),
                       capture_output=True, text=True)
    assert r.returncode == 2 and "checkin-contract: action must be one of" in r.stderr
    assert r.stdout == ""


@pytest.mark.parametrize("kind", ["checkin_decision", "task_proposed", "task_rejected"])
def test_the_three_kinds_carry_notice_severity(tmp_path, kind):
    emit_batch(tmp_path, [{
        "event_type": "system", "emitter": "t", "fleet": "f",
        "payload": {"event": kind, "subject_kind": "actor", "subject": "bot:f/mgr",
                    "data": {"schema": 1}}}])
    conn = connect(db_path(tmp_path))
    row = conn.execute("SELECT severity FROM events WHERE kind='system' AND event=?", (kind,)).fetchone()
    conn.close()
    assert row["severity"] == "notice"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_contract.py -q`
Expected: collection fails on the missing `lib/checkin-contract.py` (FileNotFoundError from `exec_module`); the severity test alone would fail with `None`.

- [ ] **Step 3: The severities**

In `claudlobby/plane/registries.py`, inside `SYSTEM_EVENT_SEVERITY` before its closing `}`:

```python
    # manager check-in (spec §7 / §8): the decision record and the intake
    # store's two verbs. notice — the record IS the point; nothing here pages.
    "checkin_decision": "notice",
    "task_proposed": "notice",
    "task_rejected": "notice",
```

- [ ] **Step 4: The contract module**

```python
#!/usr/bin/env python3
# lib/checkin-contract.py
"""The check-in decision record contract (manager check-in spec §7), schema 1.

Stdlib only (the dispatch-overdue.py precedent): checkin-record.sh pipes the
manager's decision JSON through `normalize` before anything reaches the plane,
so a malformed decision is refused AT THE DOOR with every reason named, instead
of landing as a row no reader can join. Usage:

    python3 checkin-contract.py < decision.json     # prints the normalized JSON
    exit 0 ok · 2 contract violation (reasons on stderr, one per line)

The door mints `checkin_id` when the caller omits it; `prev_checkin_id` is the
caller's (READ step 0 of the skill), null when there was none. Every counter
defaults to 0 and every list to [], so a manager that could not measure a
thing records "0 seen", never a missing key — and names what it could not
read in `inputs_seen.unavailable`.
"""
from __future__ import annotations

import json
import re
import secrets
import sys

SCHEMA = 1
ACTIONS = ("dispatch", "propose", "ask", "sprint", "nothing")
RATIONALE_MAX = 600
INPUTS_COUNTS = ("open_tasks", "stalls", "unacked", "issues_considered", "knowledge_hits")
INPUTS_LISTS = ("focus_declared", "focus_empirical_top", "unavailable")
DELTA_COUNTS = ("tasks_opened", "tasks_completed", "stalls_appeared", "stalls_cleared",
                "issues_new", "messages_new", "held_pending")
TARGET_LISTS = ("assignment_ids", "work_item_ids")
ID_RE = re.compile(r"^ck_[0-9a-f]{32}$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")   # projects.yaml key / WorkItem.project_key


class ContractError(ValueError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def mint_checkin_id() -> str:
    return "ck_" + secrets.token_hex(16)


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(s, str) for s in v)


def normalize(obj) -> dict:
    """Return the schema-1 record, or raise ContractError listing EVERY defect."""
    bad: list[str] = []
    if not isinstance(obj, dict):
        raise ContractError(["decision must be a JSON object"])
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
        if not _is_count(v):
            bad.append(f"inputs_seen.{k} must be a non-negative integer")
        out["inputs_seen"][k] = v
    for k in INPUTS_LISTS:
        v = seen.get(k, [])
        if not _is_str_list(v):
            bad.append(f"inputs_seen.{k} must be a list of strings")
        out["inputs_seen"][k] = v

    delta = obj.get("delta")
    if not isinstance(delta, dict):
        bad.append("delta must be an object")
        delta = {}
    out["delta"] = {}
    for k in DELTA_COUNTS:
        v = delta.get(k, 0)
        if not _is_count(v):
            bad.append(f"delta.{k} must be a non-negative integer")
        out["delta"][k] = v

    action = obj.get("action")
    if action not in ACTIONS:
        bad.append(f"action must be one of {', '.join(ACTIONS)}")
    out["action"] = action

    pk = obj.get("project_key")
    if pk is not None and not (isinstance(pk, str) and SLUG_RE.match(pk)):
        bad.append("project_key must be a projects.yaml slug or null")
    out["project_key"] = pk
    if action in ("dispatch", "propose", "sprint") and pk is None:
        bad.append(f"action {action} must name project_key")

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
    reason = raise_.get("reason", "")
    if not isinstance(reason, str):
        bad.append("raise.reason must be a string")
    held = raise_.get("held", [])
    if not _is_str_list(held):
        bad.append("raise.held must be a list of strings")
    out["raise"] = {"decided": decided, "reason": reason, "held": held}
    if action == "ask" and decided is not True:
        bad.append("action ask requires raise.decided = true (an ask IS a surfacing)")

    targets = obj.get("targets")
    if targets is None:
        targets = {}
    if not isinstance(targets, dict):
        bad.append("targets must be an object")
        targets = {}
    out["targets"] = {}
    for k in TARGET_LISTS:
        v = targets.get(k, [])
        if not _is_str_list(v):
            bad.append(f"targets.{k} must be a list of ids")
        out["targets"][k] = v
    msg = targets.get("msg_id")
    if msg is not None and not isinstance(msg, str):
        bad.append("targets.msg_id must be a string or null")
    out["targets"]["msg_id"] = msg

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

- [ ] **Step 5: Run the contract tests**

Run: `./.venv/bin/pytest tests/test_checkin_contract.py tests/test_plane_system_events.py -q`
Expected: all pass.

- [ ] **Step 6: Write the failing door tests**

```python
# tests/test_checkin_doors.py
"""The check-in's write doors (manager check-in spec §7, §8): checkin-record.sh
and checkin-propose.sh, plus dispatch-task.sh --project / --work-item.

Two rigs: a STUB lib-common that captures the batch (tests/test_briefing_trigger.py's
pattern), and the REAL shim landing rows in a scratch plane through the cold
CLI rung (tests/test_task_id_dispatch.py's _fake_lib / plane_env)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _scrubbed_env
from tests.plane_fixtures import ro as _ro

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

STUB_LIB_COMMON = """\
#!/bin/bash
install_error_trap() { :; }
plane_armed() { [ "${PLANE_EMIT_DISABLED:-0}" != "1" ]; }
json_escape() { printf '%s' "$1" | python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read())[1:-1])'; }
plane_mint_id() { printf '%s_%s' "$1" "$(python3 -c 'import secrets; print(secrets.token_hex(16))')"; }
plane_emit_events() { cat > "$EMIT_CAPTURE"; PLANE_EMIT_LAST_RC="${STUB_EMIT_RC:-0}"; }
PLANE_EMIT_LAST_RC=0
"""


def _decision() -> dict:
    return {
        "inputs_seen": {"open_tasks": 1}, "delta": {}, "action": "nothing",
        "project_key": None, "rationale": "nothing worth starting",
        "raise": {"decided": False, "reason": "no delta", "held": []},
        "targets": {},
    }


def _stub_rig(tmp_path: Path) -> tuple[Path, dict]:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "lib-common.sh").write_text(STUB_LIB_COMMON)
    for name in ("checkin-record.sh", "checkin-propose.sh", "checkin-contract.py"):
        shutil.copy(LIB / name, lib / name)
        (lib / name).chmod(0o755)
    env = {"EMIT_CAPTURE": str(tmp_path / "emit.json"), "FLEET_NAME": "f", "BOT_NAME": "mgr",
           "PATH": os.environ["PATH"]}
    return lib, env


def _run(lib: Path, script: str, env: dict, stdin: str = "", *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(lib / script), *args], input=stdin, capture_output=True,
                          text=True, env=_scrubbed_env(**env), timeout=60)


def _captured(env: dict) -> dict:
    return json.loads(Path(env["EMIT_CAPTURE"]).read_text())


# --- checkin-record.sh -------------------------------------------------------

def test_record_lands_one_actor_anchored_decision(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    ck = r.stdout.strip()
    assert ck.startswith("ck_") and len(ck) == 35
    ev = _captured(env)["events"]
    assert len(ev) == 1
    e = ev[0]
    assert e["event_type"] == "system" and e["emitter"] == "checkin-record"
    assert e["source_ref"] == f"checkin:{ck}"
    assert e["payload"]["event"] == "checkin_decision"
    assert e["payload"]["subject_kind"] == "actor" and e["payload"]["subject"] == "bot:f/mgr"
    assert e["payload"]["data"]["checkin_id"] == ck and e["payload"]["data"]["action"] == "nothing"


def test_record_refuses_a_malformed_decision_and_records_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad))
    assert r.returncode == 2
    assert "action must be one of" in r.stderr and "nothing recorded" in r.stderr
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_says_so_when_the_plane_did_not_record(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "STUB_EMIT_RC": "5"}, json.dumps(_decision()))
    assert r.returncode == 1
    assert "NOT recorded" in r.stderr


def test_record_is_silenced_only_by_the_harness_exemption(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "PLANE_EMIT_DISABLED": "1"}, json.dumps(_decision()))
    assert r.returncode == 1 and "PLANE_EMIT_DISABLED" in r.stderr
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_needs_an_identity(tmp_path):
    lib, env = _stub_rig(tmp_path)
    env = {k: v for k, v in env.items() if k not in ("FLEET_NAME", "BOT_NAME")}
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 3 and "identity" in r.stderr


def test_record_dry_run_validates_and_writes_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--dry-run")
    assert r.returncode == 0 and r.stdout.strip().startswith("ck_")
    assert not Path(env["EMIT_CAPTURE"]).exists()


# --- the REAL spine: the row lands in a scratch plane --------------------------

REAL_DOOR_FILES = ("checkin-record.sh", "checkin-propose.sh", "checkin-contract.py",
                   "lib-common.sh", "plane-emit.sh", "plane-socket-client.py")


def _real_rig(tmp_path: Path) -> tuple[Path, dict]:
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in REAL_DOOR_FILES:
        (lib / name).symlink_to(LIB / name)
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    env = {
        "CLAUDLOBBY_ROOT": str(tmp_path), "FLEET_NAME": "f", "BOT_NAME": "mgr",
        "PLANE_EMIT_CLI": str(CLI), "PLANE_SOCKET": str(tmp_path / "no-daemon.sock"),
        "HOME": str(tmp_path), "PATH": os.environ["PATH"],
        "PROJECT_TIER_SHOP": "review", "PROJECT_INITIATIVE_SHOP": "propose",
        "PROJECT_TIER_TOOLS": "auto", "PROJECT_INITIATIVE_TOOLS": "none",
    }
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
```

(The `checkin-propose.sh` and `dispatch-task.sh` tests are appended in Task 7; the file compiles with `checkin-propose.sh` absent because the stub rig only copies it — add `if (LIB / name).exists()` around the copy for now, and remove that guard in Task 7.)

- [ ] **Step 7: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q`
Expected: every test fails on the missing `lib/checkin-record.sh`.

- [ ] **Step 8: The record door**

```bash
#!/bin/bash
# lib/checkin-record.sh
# THE write door for a manager check-in decision (manager check-in spec §7).
# The skill hands it the decision JSON on stdin; it validates the schema-1
# contract (checkin-contract.py), mints the checkin_id when absent, and lands
# ONE actor-anchored system event `checkin_decision` on the plane, stamped
# source_ref checkin:<checkin_id> -- the task-recheck stamp idiom: the ref is
# the address a reader joins on, and the record needs no other key.
#
# Usage: checkin-record.sh [--bot <id>] [--fleet <name>] [--dry-run] < decision.json
#   stdout: the checkin_id (one line)
#   exit:   0 recorded (committed or spooled)
#           1 not recorded (disclosed on stderr)
#           2 contract violation (every reason on stderr; nothing recorded)
#           3 no identity (fleet or bot unknown)
#
# RECORD BEFORE ACT is the skill's rule, so unlike the doors whose real action
# is elsewhere, THIS door's action IS the record: an unrecorded decision is a
# failure it says so about (rc 1), never a silent 0. The plane is always on;
# PLANE_EMIT_DISABLED=1 (the harness exemption) is the one thing that silences
# it, and the door says that too.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

BOT="${BOT_NAME:-${BOT_ID:-}}"
FLEET="${FLEET_NAME:-${CLAUDLOBBY_FLEET:-}}"
DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --bot)     BOT="${2:?--bot needs a value}"; shift 2 ;;
        --fleet)   FLEET="${2:?--fleet needs a value}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) printf 'checkin-record: unknown flag %s\n' "$1" >&2; exit 2 ;;
    esac
done
if [ -z "$BOT" ] || [ -z "$FLEET" ]; then
    printf 'checkin-record: no identity -- need BOT_NAME and FLEET_NAME (a manager session sources them from bot.conf), or --bot/--fleet\n' >&2
    exit 3
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
    exit 1
fi
utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf -v batch '{"events":[{"event_type":"system","emitter":"checkin-record","source_ref":"checkin:%s","fleet":"%s","occurred_at":"%s","payload":{"event":"checkin_decision","subject_kind":"actor","subject":"bot:%s/%s","data":%s}}]}' \
    "$checkin_id" "$(json_escape "$FLEET")" "$utc" "$(json_escape "$FLEET")" "$(json_escape "$BOT")" "$normalized"
plane_emit_events checkin-record <<<"$batch"
if [ "${PLANE_EMIT_LAST_RC:-0}" -ne 0 ]; then
    printf 'checkin-record: plane record failed rc=%s -- decision %s NOT recorded\n' "$PLANE_EMIT_LAST_RC" "$checkin_id" >&2
    exit 1
fi
printf '%s\n' "$checkin_id"
```

`chmod 0755 lib/checkin-record.sh lib/checkin-contract.py`.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py tests/test_checkin_contract.py tests/test_bash_parse.py -q`
Expected: all pass, including `test_the_decision_lands_on_a_real_plane` (the shim's cold-CLI rung records; its "daemon unavailable — falling back to cold CLI" stderr line is expected).

- [ ] **Step 10: Commit**

```bash
git add claudlobby/plane/registries.py lib/checkin-contract.py lib/checkin-record.sh tests/test_checkin_contract.py tests/test_checkin_doors.py
printf '%s\n' 'feat(checkin): the decision record — schema-1 contract + the record door' '' 'lib/checkin-contract.py (stdlib) refuses a malformed decision with every reason;' 'lib/checkin-record.sh lands ONE actor-anchored checkin_decision, source_ref' 'checkin:<id>, rc 1 when unrecorded (for this door the record IS the action).' 'Severities registered for checkin_decision / task_proposed / task_rejected. Spec §7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c6.txt"
git commit -q -F "$TMPDIR/c6.txt" && git log --oneline -1
```

---

### Task 7: The intake door and the dispatch door's two new flags

**Files:**
- Create: `lib/checkin-propose.sh`
- Modify: `lib/dispatch-task.sh:5-24` (flag docs), `:78` (init), `:104-117` (parse), `:395-405` (envelope), `:474-494` (ids), `:681-689` (batch)
- Test: `tests/test_checkin_doors.py` (append)

**Interfaces:**
- Produces: `checkin-propose.sh propose --title T --repo owner/name --project key [--body B] [--checkin ck_id]` → stdout `wi_<32hex>`; rc 0 / 1 unrecorded / 2 refused (missing field, bad repo shape, project not in `projects.yaml` i.e. `PROJECT_TIER_<SLUG>` unset, `PROJECT_INITIATIVE_<SLUG>` is `none`) / 3 no identity. `checkin-propose.sh reject <wi_id> --reason R [--checkin ck_id]` → rc 0 / 1 / 2 / 3. Plane: a `work_item` (repo, project_key, body, `source_ref proposal:<wi>`) + `task_proposed` system event (`data: {work_item_id, checkin_id, repo, project_key, tier, initiative, title}`); rejection: `task_rejected` (`data: {work_item_id, reason, checkin_id}`).
- `dispatch-task.sh --project KEY` (slug; `| project:KEY` in the envelope; `project_key` on the work item) and `--work-item wi_<32hex>` (reuse the proposal's work item: no second `work_item` event, the assignment references the given id).

- [ ] **Step 1: Append the failing tests**

```python
# append to tests/test_checkin_doors.py

# --- checkin-propose.sh ------------------------------------------------------

def test_propose_lands_a_work_item_and_the_proposal_event(tmp_path):
    lib, env = _real_rig(tmp_path)
    r = _run(lib, "checkin-propose.sh", env, "", "propose", "--title", "Add the price feed",
             "--repo", "acme/shop", "--project", "shop", "--body", "why: revenue", "--checkin", "ck_" + "a" * 32)
    assert r.returncode == 0, r.stderr
    wi = r.stdout.strip()
    assert wi.startswith("wi_") and len(wi) == 35
    with _ro(tmp_path) as conn:
        item = conn.execute("SELECT title, repo, project_key, body, source_ref FROM work_items WHERE work_item_id=?", (wi,)).fetchone()
        ev = conn.execute("SELECT detail, severity FROM events WHERE kind='system' AND event='task_proposed'").fetchone()
        asg = conn.execute("SELECT COUNT(*) FROM assignments WHERE work_item_id=?", (wi,)).fetchone()[0]
    assert item["repo"] == "acme/shop" and item["project_key"] == "shop" and item["title"] == "Add the price feed"
    assert item["source_ref"] == f"proposal:{wi}"
    d = json.loads(ev["detail"])
    assert d["work_item_id"] == wi and d["tier"] == "review" and d["initiative"] == "propose"
    assert d["checkin_id"] == "ck_" + "a" * 32 and ev["severity"] == "notice"
    assert asg == 0   # a proposal is a work item with NO assignment


@pytest.mark.parametrize("args, needle", [
    (["--title", "t", "--repo", "acme/shop"], "--project"),
    (["--title", "t", "--repo", "shop", "--project", "shop"], "owner/name"),
    (["--title", "t", "--repo", "acme/x", "--project", "unknown"], "not in projects.yaml"),
    (["--title", "t", "--repo", "acme/x", "--project", "tools"], "initiative: none"),
    (["--repo", "acme/x", "--project", "shop"], "--title"),
])
def test_propose_refuses_below_the_well_defined_bar(tmp_path, args, needle):
    lib, env = _real_rig(tmp_path)
    r = _run(lib, "checkin-propose.sh", env, "", "propose", *args)
    assert r.returncode == 2, r.stderr
    assert needle in r.stderr
    assert not (tmp_path / "state" / "plane" / "plane.db").exists()


def test_reject_lands_the_rejection(tmp_path):
    lib, env = _real_rig(tmp_path)
    wi = _run(lib, "checkin-propose.sh", env, "", "propose", "--title", "t", "--repo", "acme/shop",
              "--project", "shop").stdout.strip()
    r = _run(lib, "checkin-propose.sh", env, "", "reject", wi, "--reason", "duplicate of an open issue")
    assert r.returncode == 0, r.stderr
    with _ro(tmp_path) as conn:
        ev = conn.execute("SELECT detail FROM events WHERE kind='system' AND event='task_rejected'").fetchone()
    d = json.loads(ev["detail"])
    assert d["work_item_id"] == wi and d["reason"] == "duplicate of an open issue"


def test_reject_needs_a_reason(tmp_path):
    lib, env = _real_rig(tmp_path)
    r = _run(lib, "checkin-propose.sh", env, "", "reject", "wi_" + "b" * 32)
    assert r.returncode == 2 and "--reason" in r.stderr


# --- dispatch-task.sh --project / --work-item ---------------------------------

from tests.test_task_id_dispatch import _bash, _fake_lib, plane_dispatch_row

DISPATCH_STUB = "#!/bin/bash\nprintf '%s\\n' \"$2\" > \"$DISPATCH_CAPTURE\"\nexit 0\n"


def test_dispatch_project_flag_reaches_the_envelope_and_the_work_item(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --repo acme/shop --project shop w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    assert "| project:shop" in (tmp_path / "sent.txt").read_text()
    with _ro(tmp_path) as conn:
        row = conn.execute("SELECT repo, project_key FROM work_items").fetchone()
    assert row["repo"] == "acme/shop" and row["project_key"] == "shop"


def test_dispatch_refuses_a_non_slug_project(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    r = _bash(f'"{libdir}/dispatch-task.sh" --project "Not Slug" w1 "x"', env=env)
    assert r.returncode == 1 and "project" in r.stderr


def test_dispatch_work_item_reuses_the_proposal(tmp_path):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    prop_env = {**env, "PROJECT_TIER_SHOP": "review", "PROJECT_INITIATIVE_SHOP": "autonomous",
                "BOT_NAME": "lead", "PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    for name in ("checkin-propose.sh", "checkin-contract.py"):
        (libdir / name).symlink_to(LIB / name)
    wi = _run(libdir, "checkin-propose.sh", prop_env, "", "propose", "--title", "t", "--repo", "acme/shop",
              "--project", "shop").stdout.strip()
    r = _bash(f'"{libdir}/dispatch-task.sh" --work-item {wi} --repo acme/shop --project shop w1 "t"', env=env)
    assert r.returncode == 0, r.stderr
    with _ro(tmp_path) as conn:
        items = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
        asg = conn.execute("SELECT work_item_id FROM assignments").fetchone()
    assert items == 1          # the proposal's work item, not a second one
    assert asg["work_item_id"] == wi
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py -q -k "propose or reject or dispatch"`
Expected: propose/reject fail on the missing script; the dispatch tests fail with `unknown flag '--project'` / `'--work-item'`.

- [ ] **Step 3: The intake door**

```bash
#!/bin/bash
# lib/checkin-propose.sh
# THE intake door (manager check-in spec §8): a proposed task is a work item
# with NO assignment plus a task_proposed system event; a veto is task_rejected.
# The well-defined bar is enforced HERE, mechanically: a proposal must carry a
# title, an owner/name repo and a projects.yaml project whose tier resolves
# from the env this session sources (PROJECT_TIER_<SLUG>) -- and the project
# must grant initiative (PROJECT_INITIATIVE_<SLUG> autonomous|propose); a
# `none` project originates nothing, which is the grant rule made a refusal.
#
# Usage:
#   checkin-propose.sh propose --title <t> --repo <owner/name> --project <key>
#                              [--body <why>] [--checkin <ck_id>]
#       stdout: the work_item_id (wi_<32hex>)
#   checkin-propose.sh reject <wi_id> --reason <why> [--checkin <ck_id>]
#   exit: 0 recorded (committed or spooled) · 1 not recorded · 2 refused · 3 no identity
#
# A proposal that was already DISPATCHED (initiative: autonomous) is withdrawn
# through the existing task door -- task-act.sh withdraw <task-id> --reason --
# and rejected here for the record; this door never touches assignments.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

usage() { sed -n '2,20p' "$0"; }
refuse() { printf 'checkin-propose: %s (nothing recorded)\n' "$1" >&2; exit 2; }

VERB="${1:-}"
[ -n "$VERB" ] || { usage >&2; exit 2; }
shift
case "$VERB" in propose|reject) ;; -h|--help) usage; exit 0 ;; *) refuse "unknown verb $VERB" ;; esac

BOT="${BOT_NAME:-${BOT_ID:-}}"
FLEET="${FLEET_NAME:-${CLAUDLOBBY_FLEET:-}}"
TITLE="" REPO="" PROJECT="" BODY="" CHECKIN="" REASON="" WI=""
if [ "$VERB" = "reject" ]; then
    WI="${1:-}"
    case "$WI" in wi_*) shift ;; *) refuse "reject needs a work_item_id (wi_<32hex>) first" ;; esac
fi
while [ $# -gt 0 ]; do
    case "$1" in
        --title)   TITLE="${2:?--title needs a value}"; shift 2 ;;
        --repo)    REPO="${2:?--repo needs a value}"; shift 2 ;;
        --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
        --body)    BODY="${2:-}"; shift 2 ;;
        --checkin) CHECKIN="${2:?--checkin needs a value}"; shift 2 ;;
        --reason)  REASON="${2:-}"; shift 2 ;;
        --bot)     BOT="${2:?--bot needs a value}"; shift 2 ;;
        --fleet)   FLEET="${2:?--fleet needs a value}"; shift 2 ;;
        *) refuse "unknown flag $1" ;;
    esac
done
if [ -z "$BOT" ] || [ -z "$FLEET" ]; then
    printf 'checkin-propose: no identity -- need BOT_NAME and FLEET_NAME (a manager session sources them from bot.conf), or --bot/--fleet\n' >&2
    exit 3
fi
ck_frag='null'
if [ -n "$CHECKIN" ]; then
    case "$CHECKIN" in ck_*) ck_frag="\"$(json_escape "$CHECKIN")\"" ;; *) refuse "--checkin must be a ck_<32hex> id" ;; esac
fi
utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
safe_fleet=$(json_escape "$FLEET")
actor="bot:$(json_escape "$FLEET")/$(json_escape "$BOT")"

if [ "$VERB" = "reject" ]; then
    [ -n "$REASON" ] || refuse "reject needs --reason (a veto nobody can later explain is not a record)"
    printf -v batch '{"events":[{"event_type":"system","emitter":"checkin-propose","source_ref":"proposal:%s","fleet":"%s","occurred_at":"%s","payload":{"event":"task_rejected","subject_kind":"actor","subject":"%s","data":{"work_item_id":"%s","reason":"%s","checkin_id":%s}}}]}' \
        "$(json_escape "$WI")" "$safe_fleet" "$utc" "$actor" "$(json_escape "$WI")" "$(json_escape "$REASON")" "$ck_frag"
else
    [ -n "$TITLE" ]   || refuse "a proposal needs --title"
    [ -n "$REPO" ]    || refuse "a proposal needs --repo owner/name"
    [ -n "$PROJECT" ] || refuse "a proposal needs --project <key> (a projects.yaml project)"
    case "$REPO" in */*) ;; *) refuse "--repo must be owner/name, got $REPO" ;; esac
    case "$REPO" in *" "*|*/*/*) refuse "--repo must be owner/name, got $REPO" ;; esac
    printf '%s' "$PROJECT" | grep -Eq '^[a-z][a-z0-9-]*$' || refuse "--project must be a projects.yaml slug, got $PROJECT"
    slug=$(printf '%s' "$PROJECT" | tr 'a-z-' 'A-Z_')
    eval "tier=\${PROJECT_TIER_$slug-}"
    eval "initiative=\${PROJECT_INITIATIVE_$slug-none}"
    [ -n "$tier" ] || refuse "project $PROJECT is not in projects.yaml (PROJECT_TIER_$slug is unset) -- not well-defined"
    case "$initiative" in
        autonomous|propose) ;;
        *) refuse "project $PROJECT grants initiative: none -- a none project originates nothing" ;;
    esac
    WI="$(plane_mint_id wi)"
    body_frag=""
    [ -n "$BODY" ] && body_frag=",\"body\":\"$(json_escape "$BODY")\""
    printf -v wi_ev '{"event_type":"work_item","emitter":"checkin-propose","source_ref":"proposal:%s","fleet":"%s","occurred_at":"%s","payload":{"work_item_id":"%s","title":"%s","created_by":"%s","repo":"%s","project_key":"%s"%s}}' \
        "$WI" "$safe_fleet" "$utc" "$WI" "$(json_escape "$TITLE")" "$actor" "$(json_escape "$REPO")" "$(json_escape "$PROJECT")" "$body_frag"
    printf -v ev '{"event_type":"system","emitter":"checkin-propose","source_ref":"proposal:%s","fleet":"%s","occurred_at":"%s","payload":{"event":"task_proposed","subject_kind":"actor","subject":"%s","data":{"work_item_id":"%s","checkin_id":%s,"repo":"%s","project_key":"%s","tier":"%s","initiative":"%s","title":"%s"}}}' \
        "$WI" "$safe_fleet" "$utc" "$actor" "$WI" "$ck_frag" "$(json_escape "$REPO")" "$(json_escape "$PROJECT")" "$(json_escape "$tier")" "$(json_escape "$initiative")" "$(json_escape "$TITLE")"
    printf -v batch '{"events":[%s,%s]}' "$wi_ev" "$ev"
fi

if ! plane_armed checkin-propose; then
    printf 'checkin-propose: plane silenced (PLANE_EMIT_DISABLED=1) -- %s NOT recorded\n' "$VERB" >&2
    exit 1
fi
plane_emit_events checkin-propose <<<"$batch"
if [ "${PLANE_EMIT_LAST_RC:-0}" -ne 0 ]; then
    printf 'checkin-propose: plane record failed rc=%s -- %s NOT recorded\n' "$PLANE_EMIT_LAST_RC" "$VERB" >&2
    exit 1
fi
[ "$VERB" = "propose" ] && printf '%s\n' "$WI"
exit 0
```

`chmod 0755 lib/checkin-propose.sh`. Note the last line: `[ … ] && printf` under `set -e` needs the explicit `exit 0` after it, which is why it is there.

- [ ] **Step 4: The dispatch door's flags**

In `lib/dispatch-task.sh`:

(a) flag docs, after the `--ref URL` line (10):
```bash
#   --project KEY      projects.yaml project (adds project:<KEY> to envelope and
#                      project_key to the plane work item — the well-defined bar)
#   --work-item ID     Reuse an EXISTING plane work item (a proposal, wi_<32hex>)
#                      instead of minting one: approving a proposal is one row,
#                      not two. The assignment references the given id.
```
(b) init beside `DISPATCH_REPO=""` (78): `DISPATCH_PROJECT=""` and `DISPATCH_WORK_ITEM=""`.
(c) parse loop (104-117), beside `--repo`:
```bash
        --project)      DISPATCH_PROJECT=$(_flag_val "$1" "${2:-}"); shift 2 ;;
        --work-item)    DISPATCH_WORK_ITEM=$(_flag_val "$1" "${2:-}"); shift 2 ;;
```
and directly after the loop:
```bash
if [ -n "$DISPATCH_PROJECT" ] && ! printf '%s' "$DISPATCH_PROJECT" | grep -Eq '^[a-z][a-z0-9-]*$'; then
    echo "dispatch-task: --project must be a projects.yaml slug ([a-z][a-z0-9-]*), got '$DISPATCH_PROJECT'" >&2; exit 1
fi
if [ -n "$DISPATCH_WORK_ITEM" ] && ! printf '%s' "$DISPATCH_WORK_ITEM" | grep -Eq '^wi_[0-9a-f]{32}$'; then
    echo "dispatch-task: --work-item must be a plane work item id (wi_<32hex>), got '$DISPATCH_WORK_ITEM'" >&2; exit 1
fi
```
(d) envelope (395-400), after the `repo:` line:
```bash
    [ -n "$DISPATCH_PROJECT" ]    && DISPATCH_MSG="$DISPATCH_MSG | project:$DISPATCH_PROJECT"
```
(e) ids (492-494), after `PLANE_WI_ID="$(plane_mint_id wi)"`:
```bash
    # A reused work item (an approved proposal) keeps its id: the row already
    # exists, and a dispatch that minted a second one would make one piece of
    # work two rows (spec §8).
    [ -n "$DISPATCH_WORK_ITEM" ] && PLANE_WI_ID="$DISPATCH_WORK_ITEM"
```
(f) the batch (681-689): add the project fragment beside `repo_frag`, and omit `wi_ev` when the work item is reused:
```bash
        local proj_frag=""
        [ -n "$DISPATCH_PROJECT" ] && proj_frag=",\"project_key\":\"$(json_escape "$DISPATCH_PROJECT")\""
        wi_ev="{\"event_type\":\"work_item\",\"emitter\":\"dispatch-task\",\"source_ref\":\"$dispatch_ref\",\"fleet\":\"$safe_fleet\",\"payload\":{\"work_item_id\":\"$PLANE_WI_ID\",\"title\":\"$safe_task\",\"created_by\":\"$safe_sender\"${ws_frag}${repo_frag}${proj_frag}}}"
        ...
        local _batch _wi_part=""
        [ -z "$DISPATCH_WORK_ITEM" ] && _wi_part="$wi_ev,"
        printf -v _batch '{"events":[%s%s,%s%s]}' "$_wi_part" "$asg_ev" "$comm" "${sup_ev:+,$sup_ev}"
```
(Keep the `asg_ev`, `comm` and `plane_emit_events` lines as they are; `proj_frag` must be declared `local` beside `ws_frag`/`repo_frag` in `_plane_emit_intent`.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_doors.py tests/test_task_id_dispatch.py tests/test_dispatch_type.py tests/test_bash_parse.py -q && bash tests/test_dispatch_task.sh`
Expected: all pass (`test_dispatch_type.py` parses the protocol docs against `DISPATCH_TYPES` — untouched; `tests/test_dispatch_task.sh` is the shell-native suite).

- [ ] **Step 6: Commit**

```bash
git add lib/checkin-propose.sh lib/dispatch-task.sh tests/test_checkin_doors.py
printf '%s\n' 'feat(checkin): the intake door — propose / reject; dispatch-task --project and --work-item' '' 'A proposal is a work item with no assignment + task_proposed; the well-defined' 'bar (title, owner/name repo, a projects.yaml project with a grant) is a refusal,' 'not prose. Approving reuses the work item (one row, not two). Spec §8.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c7.txt"
git commit -q -F "$TMPDIR/c7.txt" && git log --oneline -1
```

---

### Task 8: `claudlobby checkins` — the minimal read door

**Files:**
- Modify: `claudlobby/plane/queries.py` (append two constants)
- Create: `claudlobby/commands/checkins.py`
- Modify: `claudlobby/commands/_parsers.py:189-197` (register beside `workstreams`), its imports
- Test: `tests/test_checkins_cli.py`

**Interfaces:**
- Consumes: `brief.plane_session(paths, fleet) -> (plane, note)` (`brief.py:252`; `plane.conn`, `plane.fleet`, `plane.close()`), `brief.resolve_fleet_name(paths)`, `commands._parsers._resolve_paths` idiom, `queries.fleet_alias_range` / `fleet_range_params`.
- Produces: `CHECKIN_ROWS_SQL` (binds: fleet, fleet, since-cutoff) → `(checkin_id, prev_checkin_id, subject_alias, occurred_at, action, project_key, raise_decided, rationale, detail)`; `PROPOSALS_SQL` (binds: fleet, fleet) → open proposals; `cmd_checkins(args) -> int` with `--fleet`, `--bot`, `--since 7d`, `--last`, `--proposals`, `--json`; rc 0 / 2 no fleet / 3 plane unreachable (the `task recheck` ladder, `commands/task.py:701-708`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkins_cli.py
"""`claudlobby checkins` — the minimal read door (manager check-in spec §11):
rows, --last, --proposals, --json; rc 3 on an unreachable plane. Seeded through
the real emit spine like tests/test_plane_stale_task.py."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby.commands import checkins as cmd
from claudlobby.plane.emit_api import emit_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
F = "ck-fleet"


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
        self.proposals = kw.get("proposals", False)
        self.json = kw.get("json", False)


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _decision(root, bot: str, ck: str, *, age_h: float, action="nothing", prev=None, decided=False):
    emit_batch(root, [{
        "event_type": "system", "emitter": "checkin-record", "fleet": F,
        "source_ref": f"checkin:{ck}", "occurred_at": _ago(hours=age_h),
        "payload": {"event": "checkin_decision", "subject_kind": "actor", "subject": f"bot:{F}/{bot}",
                    "data": {"schema": 1, "checkin_id": ck, "prev_checkin_id": prev,
                             "inputs_seen": {}, "delta": {}, "action": action, "project_key": None,
                             "rationale": f"r-{ck[-4:]}", "raise": {"decided": decided, "reason": "", "held": []},
                             "targets": {}}}}])


def _proposal(root, wi: str, *, project="shop", rejected=False, assigned=False):
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "checkin-propose", "fleet": F, "source_ref": f"proposal:{wi}",
         "payload": {"work_item_id": wi, "title": f"t-{wi[-4:]}", "created_by": f"bot:{F}/mgr",
                     "repo": "acme/shop", "project_key": project}},
        {"event_type": "system", "emitter": "checkin-propose", "fleet": F, "source_ref": f"proposal:{wi}",
         "payload": {"event": "task_proposed", "subject_kind": "actor", "subject": f"bot:{F}/mgr",
                     "data": {"work_item_id": wi, "project_key": project, "tier": "review", "initiative": "propose"}}}])
    if rejected:
        emit_batch(root, [{"event_type": "system", "emitter": "checkin-propose", "fleet": F,
                           "payload": {"event": "task_rejected", "subject_kind": "actor", "subject": f"bot:{F}/mgr",
                                       "data": {"work_item_id": wi, "reason": "dup"}}}])
    if assigned:
        emit_batch(root, [{"event_type": "assignment", "emitter": "dispatch-task", "fleet": F,
                           "payload": {"assignment_id": "asg_" + wi[3:], "work_item_id": wi,
                                       "assignee": f"bot:{F}/w1", "assigned_by": f"bot:{F}/mgr"}}])


CK1, CK2, CK3 = ("ck_" + c * 32 for c in "abc")


def test_rows_newest_first_scoped_to_the_fleet_and_bot(root, capsys):
    _decision(root, "mgr", CK1, age_h=5)
    _decision(root, "mgr", CK2, age_h=1, prev=CK1, action="dispatch")
    _decision(root, "other", CK3, age_h=2)
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = json.loads(capsys.readouterr().out)["checkins"]
    assert [r["checkin_id"] for r in rows] == [CK2, CK3, CK1]
    assert rows[0]["prev_checkin_id"] == CK1 and rows[0]["action"] == "dispatch" and rows[0]["bot"] == "mgr"
    assert cmd.cmd_checkins(_Args(root, bot="mgr", json=True)) == 0
    rows = json.loads(capsys.readouterr().out)["checkins"]
    assert [r["checkin_id"] for r in rows] == [CK2, CK1]


def test_last_returns_one_row(root, capsys):
    _decision(root, "mgr", CK1, age_h=5)
    _decision(root, "mgr", CK2, age_h=1)
    assert cmd.cmd_checkins(_Args(root, bot="mgr", last=True, json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["checkin_id"] for r in out["checkins"]] == [CK2]


def test_since_window_is_honoured(root, capsys):
    _decision(root, "mgr", CK1, age_h=30)
    _decision(root, "mgr", CK2, age_h=1)
    assert cmd.cmd_checkins(_Args(root, since="24h", json=True)) == 0
    assert [r["checkin_id"] for r in json.loads(capsys.readouterr().out)["checkins"]] == [CK2]


def test_proposals_is_the_projection(root, capsys):
    _proposal(root, "wi_" + "1" * 32)
    _proposal(root, "wi_" + "2" * 32, rejected=True)
    _proposal(root, "wi_" + "3" * 32, assigned=True)
    assert cmd.cmd_checkins(_Args(root, proposals=True, json=True)) == 0
    rows = json.loads(capsys.readouterr().out)["proposals"]
    assert [r["work_item_id"] for r in rows] == ["wi_" + "1" * 32]
    assert rows[0]["project_key"] == "shop" and rows[0]["tier"] == "review"


def test_an_empty_fleet_plane_answers_no_checkins_at_rc_0(root, capsys):
    # A plane that has SEEN the fleet (one identity row — the roster the session
    # opens on, plane-readers.roster) but holds no decision is EMPTY, not
    # unreachable. Seed one actor-anchored event and nothing else.
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkins_cli.py -q`
Expected: `ImportError: cannot import name 'checkins'`.

- [ ] **Step 3: The two queries**

Append to `claudlobby/plane/queries.py`:

```python

# --- the manager check-in (manager check-in spec §7, §8, §11) -----------------
# A check-in is ONE system event, `checkin_decision`, actor-anchored on the
# manager, stamped source_ref checkin:<checkin_id>; the record is its detail
# (schema 1). Read newest-first by ingest_seq; `json_type` guards keep a row
# whose detail was truncated (detail_truncated=1 IS the parse guard) from
# aborting the read. Binds: fleet, fleet, since-cutoff (iso).
CHECKIN_ROWS_SQL = (
    "SELECT json_extract(e.detail, '$.checkin_id') AS checkin_id,"
    " json_extract(e.detail, '$.prev_checkin_id') AS prev_checkin_id,"
    " e.subject_alias AS subject_alias, e.occurred_at AS occurred_at,"
    " json_extract(e.detail, '$.action') AS action,"
    " json_extract(e.detail, '$.project_key') AS project_key,"
    " json_extract(e.detail, '$.raise.decided') AS raise_decided,"
    " json_extract(e.detail, '$.rationale') AS rationale,"
    " e.detail AS detail, e.ingest_seq AS ingest_seq"
    " FROM events e"
    " WHERE e.kind = 'system' AND e.event = 'checkin_decision'"
    " AND e.detail_truncated = 0 AND json_valid(e.detail)"
    f" AND {fleet_alias_range('e.subject_alias')}"
    f" AND {_epoch('e.occurred_at')} >= {_epoch('?')}"
    " ORDER BY e.ingest_seq DESC"
)

# The intake queue is a PROJECTION, never a table: work items carrying a
# task_proposed event with neither an assignment nor a task_rejected. The
# proposal's project/tier/initiative ride the event's detail (the door stamps
# them), the title/repo the work item. Binds: fleet, fleet.
PROPOSALS_SQL = (
    "SELECT w.work_item_id AS work_item_id, w.title AS title, w.repo AS repo,"
    " w.project_key AS project_key, w.occurred_at AS proposed_at,"
    " json_extract(p.detail, '$.tier') AS tier,"
    " json_extract(p.detail, '$.initiative') AS initiative,"
    " json_extract(p.detail, '$.checkin_id') AS checkin_id,"
    " p.subject_alias AS proposed_by"
    " FROM work_items w"
    " JOIN events p ON p.kind = 'system' AND p.event = 'task_proposed'"
    "   AND json_extract(p.detail, '$.work_item_id') = w.work_item_id"
    " WHERE NOT EXISTS (SELECT 1 FROM assignments a WHERE a.work_item_id = w.work_item_id)"
    " AND NOT EXISTS (SELECT 1 FROM events r WHERE r.kind = 'system' AND r.event = 'task_rejected'"
    "   AND json_extract(r.detail, '$.work_item_id') = w.work_item_id)"
    f" AND {fleet_alias_range('p.subject_alias')}"
    " ORDER BY w.ingest_seq DESC"
)
```

- [ ] **Step 4: The command**

```python
# claudlobby/commands/checkins.py
"""`claudlobby checkins` — the check-in's read door (manager check-in spec §11),
minimal form: the decision rows, `--last`, and the intake projection
(`--proposals`). `--summary` and the outcome join land in chunk 3.

Unreachable is not empty (source_state.py): a plane that cannot answer refuses
at rc 3, never a clean "no check-ins". One plane session per run, through
brief.plane_session — the ONE door for every plane read in the package."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

from ..plane.queries import (
    CHECKIN_ROWS_SQL,
    PROPOSALS_SQL,
    fleet_range_params,
)
from ._parsers import _resolve_paths

_SINCE_RE = re.compile(r"^(\d+)([hd])$")


def parse_since(text: str) -> timedelta:
    m = _SINCE_RE.match(text or "")
    if not m:
        raise ValueError(f"--since wants <N>h or <N>d, got {text!r}")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


def _bot_of(alias: str | None) -> str:
    return alias.rsplit("/", 1)[-1] if alias else ""


def collect_checkins(conn, fleet: str, *, since: datetime, bot: str | None = None,
                     last: bool = False) -> list[dict]:
    rows = []
    for r in conn.execute(CHECKIN_ROWS_SQL, (*fleet_range_params(fleet), since.isoformat())):
        b = _bot_of(r["subject_alias"])
        if bot and b != bot:
            continue
        rows.append({
            "checkin_id": r["checkin_id"], "prev_checkin_id": r["prev_checkin_id"],
            "bot": b, "occurred_at": r["occurred_at"], "action": r["action"],
            "project_key": r["project_key"], "raised": bool(r["raise_decided"]),
            "rationale": r["rationale"], "record": json.loads(r["detail"]),
        })
        if last:
            break
    return rows


def collect_proposals(conn, fleet: str) -> list[dict]:
    return [dict(r) for r in conn.execute(PROPOSALS_SQL, fleet_range_params(fleet))]


def cmd_checkins(args) -> int:
    paths = _resolve_paths(args)
    from ..brief import plane_session, resolve_fleet_name

    fleet = getattr(args, "checkins_fleet", None) or resolve_fleet_name(paths)
    if not fleet:
        print("checkins: no fleet is named (--fleet <name>, or a fleet.yaml naming one)"
              " — the plane's rows are per fleet", file=sys.stderr)
        return 2
    try:
        window = parse_since(args.since)
    except ValueError as exc:
        print(f"checkins: {exc}", file=sys.stderr)
        return 2
    plane, note = plane_session(paths, fleet)
    if plane is None:
        print(f"checkins: {note} — unreachable, not empty", file=sys.stderr)
        return 3
    try:
        if args.proposals:
            proposals = collect_proposals(plane.conn, fleet)
            if args.json:
                print(json.dumps({"schema": 1, "fleet": fleet, "proposals": proposals}, indent=2))
                return 0
            print(f"open proposals — fleet {fleet}: {len(proposals)}")
            for p in proposals:
                print(f"  {p['work_item_id']}  {p['project_key']}/{p['tier']}  {p['title']}  ({p['repo']})")
            return 0
        since = datetime.now(timezone.utc) - window
        rows = collect_checkins(plane.conn, fleet, since=since, bot=args.bot, last=args.last)
    finally:
        plane.close()
    if args.json:
        print(json.dumps({"schema": 1, "fleet": fleet, "since": since.isoformat(),
                          "checkins": rows}, indent=2))
        return 0
    scope = f"fleet {fleet}" + (f", bot {args.bot}" if args.bot else "") + f", last {args.since}"
    if not rows:
        print(f"no check-ins — {scope}")
        return 0
    print(f"check-ins — {scope}: {len(rows)}")
    for r in rows:
        raised = " · raised" if r["raised"] else ""
        proj = f" [{r['project_key']}]" if r["project_key"] else ""
        print(f"  {r['occurred_at']}  {r['bot']}  {r['action']}{proj}{raised}  {r['checkin_id']}")
        print(f"      {r['rationale']}")
    return 0
```

Register it in `claudlobby/commands/_parsers.py` directly after the `workstreams` block (ends line 197); import `cmd_checkins` beside `cmd_workstreams` (line 18):

```python
    pck = sub.add_parser("checkins", help="The manager check-in's decisions and the intake queue (plane read)")
    pck.add_argument("--fleet", dest="checkins_fleet", default=None,
                     help="fleet whose rows to read (default: the fleet.yaml this root names)")
    pck.add_argument("--bot", default=None, help="one manager's rows only")
    pck.add_argument("--since", default="7d", help="window, <N>h or <N>d (default 7d)")
    pck.add_argument("--last", action="store_true", help="only the newest row")
    pck.add_argument("--proposals", action="store_true", help="the open intake queue instead of check-ins")
    pck.add_argument("--json", action="store_true", help="machine-facing envelope")
    pck.set_defaults(func=cmd_checkins)
```

(`dest="checkins_fleet"`, not `fleet`: a subparser copies its namespace over the parent's, so a second `--fleet` on `dest="fleet"` would erase a global one — `_parsers.py:223-225`.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkins_cli.py tests/test_plane_queries.py tests/test_main.py -q`
Expected: all pass (if `tests/test_plane_queries.py` does not exist, drop it from the command; if a bench-plan test asserts every `queries.py` constant avoids a bare `events` scan (`is_bare_events_scan`), `CHECKIN_ROWS_SQL` passes because it filters on `kind`/`event`).

- [ ] **Step 6: Commit**

```bash
git add claudlobby/plane/queries.py claudlobby/commands/checkins.py claudlobby/commands/_parsers.py tests/test_checkins_cli.py
printf '%s\n' 'feat(cli): claudlobby checkins — the decision rows and the intake projection' '' 'CHECKIN_ROWS_SQL / PROPOSALS_SQL in queries.py (one definition); rows, --last,' '--proposals, --json; rc 3 on an unreachable plane. Summary + outcomes: chunk 3.' 'Spec §11.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c8.txt"
git commit -q -F "$TMPDIR/c8.txt" && git log --oneline -1
```

---
### Task 9: The protocol, and the four supersede edits

**Files:**
- Create: `library/protocols/checkin.md`
- Modify: `library/protocols/worker-lifecycle.md:52, 102, 119-125, 196-197`, `library/protocols/proactivity-discipline.md:7-19`, `library/protocols/continuous-autonomous-mode.md:19-25`, `library/protocols/token-efficiency.md:39`, `library/protocols/inbound-acknowledgment.md:58`
- Test: `tests/test_checkin_protocol.py`

**Interfaces:**
- Produces: the protocol `checkin` with `requires: {skills: [checkin], role: leaf-manager}`; two H2 sections `## Manager` / `## Worker` (composed as `###` after the loader's demotion). The skill (Task 10) is what `requires.skills` names — Task 10 must land before `claudlobby validate` passes on a fleet declaring the protocol (the Task 4 error), so the two tasks are committed together in Task 10's commit.
- The `EXCLUSIVE_GROUPS` mechanism (`defaults.py:332`) is availability-gated (`available(entry, facts)`), not preference-gated, and the check-in has no availability fact — so the supersession is by EDIT of the superseded texts, never by an exclusive group. The spec's "where a clean swap applies" resolves to: nowhere.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkin_protocol.py
"""The check-in protocol (manager check-in spec §9) composes both audiences into
one file; the cadence rules it supersedes are gone from the library; the skill
(§6) is coupled to its doors by name."""

import re
import shutil
from pathlib import Path

from claudlobby.composer import compose_claude_md
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.requires import read_required_role, read_requires
from tests.conftest import install_real_template

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"


def test_the_protocol_declares_its_equipment():
    p = LIB / "protocols" / "checkin.md"
    assert read_requires(p) == {"skills": ["checkin"]}
    assert read_required_role(p) == "leaf-manager"


def test_both_sections_compose_for_an_equipped_manager(fleet_dir):
    install_real_template(fleet_dir)   # the fixture's stub template may not render protocols
    shutil.copy(LIB / "protocols" / "checkin.md", fleet_dir / "library" / "protocols" / "checkin.md")
    shutil.copytree(LIB / "skills" / "checkin", fleet_dir / "library" / "skills" / "checkin")
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n", "    lead:\n      protocols: [checkin]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    md = compose_claude_md(fleet.bots["lead"], fleet, Paths(root=fleet_dir, fleet_dir=fleet_dir))
    assert "### Manager" in md and "### Worker" in md   # H2 in the file, demoted once by the loader
    assert "Silence is the default" in md


SUPERSEDED = [
    ("protocols/worker-lifecycle.md", r"every 2.3 min"),
    ("protocols/worker-lifecycle.md", r"Every 2-3 min"),
    ("protocols/proactivity-discipline.md", r"Idle silence is a bug"),
    ("protocols/continuous-autonomous-mode.md", r"beacon every 10.15 min"),
    ("protocols/token-efficiency.md", r"milestone cadence, wait-point beacons"),
    ("protocols/inbound-acknowledgment.md", r"wait-point beacons"),
]


def test_the_superseded_cadence_rules_are_gone():
    for rel, pat in SUPERSEDED:
        text = (LIB / rel).read_text()
        assert not re.search(pat, text, re.IGNORECASE), f"{rel} still says {pat!r}"


def test_the_replacement_wording_is_present():
    assert "Idle silence is recorded, not posted" in (LIB / "protocols" / "proactivity-discipline.md").read_text()
    cam = (LIB / "protocols" / "continuous-autonomous-mode.md").read_text()
    assert "the check-in is" in cam and "the beat" in cam
    wl = (LIB / "protocols" / "worker-lifecycle.md").read_text()
    assert "No milestone cadence" in wl


DOORS = ["claudlobby brief --bot", "claudlobby checkins", "claudron lookup", "gh issue list",
         "lib/checkin-record.sh", "lib/checkin-propose.sh", "lib/dispatch-task.sh",
         "PROJECT_INITIATIVE_", "PROJECT_TIER_", "PROJECT_MISSION.md"]


def test_the_skill_is_coupled_to_its_doors():
    text = (LIB / "skills" / "checkin" / "SKILL.md").read_text()
    for d in DOORS:
        assert d in text, d
    for action in ("dispatch", "propose", "ask", "sprint", "nothing"):
        assert f"**{action}**" in text, action
    assert "RECORD before ACT" in text and "unavailable" in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/pytest tests/test_checkin_protocol.py -q`
Expected: all fail (no `checkin.md`, no skill dir, the superseded phrases present).

- [ ] **Step 3: The protocol**

```markdown
---
title: Check-in
description: The idle-manager check-in — the manager's own re-engagement cycle — and the thin human edge it enforces. Silence at the human edge is the default; a post is the exception the surfacing judgment must justify and record.
requires:
  skills: [checkin]
  role: leaf-manager
---

# Check-in

The fleet is loud on the inside and quiet at the human edge. Bot-to-bot traffic is
the work happening; the operator hears from the manager in clean updates and
requests for direction, and from workers in one thin line each. Every line, on any
carrier, is a recorded communication — the plane recreates every occurrence, so
nothing is lost to a channel and volume is always measurable.

## Manager

**The check-in is yours, not the operator's.** When the `manager-checkin` job
injects `/checkin`, or when you reach a natural idle point, run it: read the SSOT,
decide one project and one action, record the decision, then act. The operator
never sees a check-in; they see only what the surfacing judgment decides they
should.

**Whether to post at all is the surfacing judgment — the skill's DECIDE step.
Silence is the default; a post is the exception you must justify and record**
(`raise.decided`, `raise.reason`, `raise.held` in the decision). A check-in that
dispatches, proposes or chooses `nothing` posts **nothing** — it is in the plane
(`claudlobby checkins`).

**When the judgment does say post, the shape is fixed:**

- **one status line** — what changed, in plain terms; never a restatement of what was already said;
- **one ask with named options** — the fork only the operator can resolve, or, when the intake is empty, "nothing worth starting here — anything you want first?";
- **one pointer** — the plane URL, the PR, the proposal id.

At most **one post per check-in**. Several qualifying items coalesce into one
message **across all projects — one post covers the portfolio, never one per
project**; what does not fit is `held` for the next justified post. Never restate
a project's rigor tier in the message; point to it.

**Held asks are not nagged.** Two open, unanswered asks mean the fleet proceeds on
its best tier-gated judgment or waits quietly — it does not pile on a third.

## Worker

**One thin line on start, done and blocked — to Telegram where you are configured
for it** (and you should be, where you are); plane-only where you are not. No
milestone cadence, no wait-point beacons: idle silence is recorded, not posted.
Detail goes to your manager and the plane through
`$CLAUDLOBBY_ROOT/lib/report-back.sh`, never to the channel. Shape — `<verb>:
<what>`: `start: #123 price feed` · `done: #123 PR #130` · `blocked: #123 needs
the API key`.
```

- [ ] **Step 4: The supersede edits** (confirm each anchor with `grep -n` before editing; the line numbers are as of main)

`library/protocols/worker-lifecycle.md`
- line 52 `5. IMPLEMENT ─── role-specific work, Telegram milestones` → `5. IMPLEMENT ─── role-specific work, [BOTREPORT] progress rows`
- line 102 `2. Post a one-line Telegram update: 'Planning: <what you're surveying>'` → `2. Post your one start line — 'start: <what>' (the check-in protocol's Worker shape)`
- lines 119-125 (the `**Telegram milestones every 2–3 minutes of active work:**` block through its `Format:` line) → 

```markdown
**No milestone cadence.** The channel hears one thin line on start, done and
blocked (the check-in protocol's Worker section); everything in between goes to
your manager and the plane as `[BOTREPORT]` progress rows. A scope surprise is a
progress row, not a post.
```

- line 196 `| Planning start (if applicable) | "Planning: ..." | — |` → `| Task started | "start: <what>" (one line) | — |`
- line 197 `| Every 2-3 min during work | One-line milestone | — |` → `| During work | — (no cadence) | `<bot-name> progress "<milestone>" --task <id>` as needed |`
- line 199 `"Done: ... PR: <url>"` → `"done: ... PR <url>"`; line 200 `"Blocked: ..."` → `"blocked: ..."`

`library/protocols/proactivity-discipline.md`
- line 7 → `Idle silence is recorded, not posted. A wait-point is a plane fact — a `[BOTREPORT]` progress or blocked row, an open assignment with a deadline — and the manager's check-in reads it; the channel hears one line only when the surfacing judgment (the check-in protocol) says the human is the one being waited on.`
- line 9 `Wait-points:` → `Wait-points (each is recorded; only the ones where the human is the blocker are posted):`
- line 17 → `Silence on the channel is the default; silence in the plane is the bug. Every wait-point above is recorded, none is posted unless the human must act.`
- line 19 `Format: one sentence…` → `Format when posted: one sentence, ≤120 chars, no emoji/markdown, tag the human only if they need to act.`

`library/protocols/continuous-autonomous-mode.md` lines 19-25 →

```markdown
**Wait-point discipline:**

When the manager has nothing to dispatch and is genuinely waiting, the check-in is
the beat: `/checkin` reads the plane, decides, and records — including `nothing`.
The channel hears one line only when the surfacing judgment says the human must
act (the check-in protocol). No beacons: "still waiting" is a recorded fact
(`claudlobby checkins`), not a post.
```

`library/protocols/token-efficiency.md` line 39 → `**Density, never frequency or routing.** Acks, heartbeats and channel-routing rules stand unchanged — substantive analysis still goes through the channel (messaging-channel-discipline); it just arrives dense. This protocol governs what a message contains, not whether it is sent; *whether* is the check-in protocol's surfacing judgment (managers) and its one line on start/done/blocked (workers).`

`library/protocols/inbound-acknowledgment.md` line 58: `(manager wait-point beacons)` → `(manager wait-points, recorded not posted)`.

- [ ] **Step 5: Run the protocol tests (the skill test still fails until Task 10)**

Run: `./.venv/bin/pytest tests/test_checkin_protocol.py tests/test_dispatch_type.py tests/test_skill_refs.py -q`
Expected: the four protocol tests pass; `test_the_skill_is_coupled_to_its_doors` and `test_both_sections_compose…` still fail on the missing skill dir; `test_dispatch_type.py` (parses `worker-lifecycle.md`'s `[BOTCOMMAND]` list) and `test_skill_refs.py` (backtick `/checkin` must resolve — it will, after Task 10) are the regression guards.

No commit yet — Task 10 commits both.

---

### Task 10: The `/checkin` skill — the reasoning contract

**Files:**
- Create: `library/skills/checkin/SKILL.md`
- Test: `tests/test_checkin_protocol.py` (already written)

**Interfaces:**
- Consumes every door by name: `claudlobby brief --bot $BOT_ID --json` (keys `dispatches{open,overdue,orphaned,dispatched}`, `workstreams{active,stalled}`, `reports{unacked}`, `alerts[]`, `mission`, `degraded[]`), `claudlobby checkins --bot $BOT_ID --last --json`, `claudlobby checkins --proposals --json`, `claudron lookup --limit 5 <project>`, `gh issue list`, `PROJECT_TIER_<SLUG>` / `PROJECT_REPOS_<SLUG>` / `PROJECT_INITIATIVE_<SLUG>`, `$CLAUDLOBBY_ROOT/lib/checkin-record.sh`, `$CLAUDLOBBY_ROOT/lib/checkin-propose.sh`, `$CLAUDLOBBY_ROOT/lib/dispatch-task.sh`.

- [ ] **Step 1: The skill**

```markdown
---
name: checkin
description: "The idle-manager check-in: read the SSOT (the plane through brief and checkins, Claudron, the mission with each project's tier and initiative, the GitHub backlog), decide ONE project and ONE action, record the decision BEFORE acting, and let the surfacing judgment decide whether the operator hears anything at all. Silence is the default."
argument-hint: "[--dry-run]"
tool_grants:
  - "Bash(claudlobby *)"
  - "Bash(claudron *)"
  - "Bash(gh *)"
  - "Bash(bash *)"
  - "Bash(cat *)"
  - "Read"
---

# Check-in

Your own re-engagement cycle. Nobody is watching it; what they may see is only what
the surfacing judgment (DECIDE, below) lets through. **Every read goes through a
named door and every write through a named door** — never a hand-rolled query,
never a hand-built plane envelope. That coupling is what makes your reasoning
inspectable (`claudlobby checkins`) and the edges deterministic.

The lib doors are invoked as `bash "$CLAUDLOBBY_ROOT/lib/<door>"`. `$BOT_ID`,
`$FLEET_NAME`, `$CLAUDLOBBY_ROOT` and the `PROJECT_*` map come from your
`bot.conf`.

## Arguments

Parse `$ARGUMENTS`:
- `--dry-run`: do every READ and the DECIDE, print the decision JSON, record nothing
  (`checkin-record.sh --dry-run` validates it), act on nothing.

## READ — in order, all cheap, all SSOT

Each step that fails is **recorded**, never guessed around: add its name to
`inputs_seen.unavailable` and continue.

0. **The previous check-in** — `claudlobby checkins --bot $BOT_ID --last --json`.
   Its `record.inputs_seen` is *the state at the last check-in*; its `action` and
   `record.raise` are what was done and what was held. Keep its `checkin_id` for
   `prev_checkin_id`. None → this is the first; `prev_checkin_id: null`.
1. **The fleet's present** — `claudlobby brief --bot $BOT_ID --json`: `dispatches`
   (open / overdue / orphaned / dispatched — with `escalated`, `nudged`,
   `last_progress_at`), `workstreams` (`active`, `stalled`), `reports.unacked`,
   `alerts` (last 24h critical), `mission`. Honour `degraded[]`: a field named there
   is labeled or omitted — treat its section as unavailable, never as zero.
2. **Knowledge** — `claudron lookup --limit 5 <project>` for each project with open
   work or a grant. Count the hits (`knowledge_hits`); read what is relevant.
3. **The goal and each project's rigor and grant** — `PROJECT_MISSION.md` (via
   `mission.charter` / `$FLEET_MISSION_FILE`), and per project from your env:
   `PROJECT_TIER_<SLUG>` (how work CLOSES), `PROJECT_REPOS_<SLUG>` (its repos),
   `PROJECT_INITIATIVE_<SLUG>` (what you may ORIGINATE: `autonomous` · `propose` ·
   `none`). `SLUG` is the project key upper-cased with `-` → `_`.
4. **The external backlog** — `gh issue list --repo <owner/name> --state open
   --limit 50 --json number,title,labels,updatedAt` per repo in `PROJECT_REPOS_*`;
   filter to mission-aligned items; group by project; count `issues_considered`.
5. **The intake queue and focus** — `claudlobby checkins --proposals --json` (the
   proposals waiting, per project). Focus: **unavailable in this chunk** — record
   `"focus"` in `unavailable` and leave `focus_declared` / `focus_empirical_top`
   empty until the focus door lands.
6. **The delta** — now versus step 0, per project: tasks opened / completed /
   stalled / cleared, new issues, new messages, held items still pending. **The
   delta is the primary signal** for both the action and the surfacing judgment:
   an unchanged world argues for `nothing` and silence.

**Everything above is read per project.** The check-in is a portfolio decision.

## DECIDE — one project, then exactly one action

**The allocation rule — how the project is chosen.** Attention is a weighting,
never a ranking:

- **Floor:** every project with open work or a blocker is checked on every check-in,
  regardless of grant or focus — open work is managed everywhere.
- **Tilt:** *new* effort (proposals, picking backlog issues) goes preferentially to
  projects with recent attention — never exclusively; one project cannot starve
  the rest.
- **Grant:** proposals only where `PROJECT_INITIATIVE_<SLUG>` is `autonomous` or
  `propose`; a `none` project originates nothing (the door refuses anyway).
- **Never stalest-first:** no attention + no focus + no open work = left alone. A
  dormant project is revived only by explicit direction.
- Mission alignment and backlog depth weigh in; the `rationale` records the weighing.

**The actions:**

| action | when | through |
|---|---|---|
| **dispatch** | an open/backlog item fits an idle worker; you choose the worker, and the rationale says why | `bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh" --repo <owner/name> --project <key> [--ref <issue-url>] [--work-item <wi_id>] <worker> "<task>"` — `--work-item` when dispatching an approved proposal, so one piece of work stays one row |
| **propose** | no well-defined work exists on a project whose grant is `autonomous` or `propose`; you generate task(s) from mission + knowledge + recent work | `bash "$CLAUDLOBBY_ROOT/lib/checkin-propose.sh" propose --title "<t>" --repo <owner/name> --project <key> --body "<why>" --checkin <ck_id>` — at most `CHECKIN_MAX_PROPOSALS` (3) per check-in; on an `autonomous` project, dispatch it in the same breath with `--work-item`; on `propose`, it waits for the operator |
| **ask** | the surfacing judgment (below) concludes the operator should hear something — a fork only they can resolve, or nothing worthwhile can be found ("ask for tasks") | one Telegram post shaped by the check-in protocol: one line, one ask with named options, one pointer |
| **sprint** | **unavailable in this chunk** — record `nothing` with rationale `sprint unavailable` when the conditions would otherwise hold (≥ 5 well-defined unstarted items on one `autonomous` project in focus, ≥ 2 idle workers, no unresolved sprint work) | — |
| **nothing** | all work in flight, nothing worthwhile — **recorded**, so "checked and chose nothing" is a fact, not silence | — |

**The surfacing judgment.** Its default answer is **no**. Weigh, at minimum:
does this genuinely need a human (a `requires-approval` boundary in the mission, a
tier that mandates sign-off, a proposal on a `propose` project, conflicting
priorities)? · would the operator want to know (a deliverable ready, a blocker that
stalls the fleet, a failure with cost)? · what changed since they were last told (the
delta against step 0 and the last post's `held`) · has enough accumulated to be worth
one message · are two asks already open and unanswered (then proceed on your best
tier-gated judgment or wait quietly — never a third) · what Claudron says about how
the operator wants to be engaged · the urgency floor (a `blocked` that stalls
everything breaks through regardless). Record the judgment in `raise`: `decided`,
`reason`, and what you `held`.

**Degraded inputs narrow the actions to `ask | nothing` — never propose from partial
information.**

## RECORD before ACT

Build the decision as JSON (schema 1) and record it FIRST — the decision exists even
if the action then fails:

```bash
ck_id=$(cat <<'EOF' | bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh"
{"prev_checkin_id": <from step 0 or null>,
 "inputs_seen": {"open_tasks": N, "stalls": N, "unacked": N, "issues_considered": N,
                 "knowledge_hits": N, "focus_declared": [], "focus_empirical_top": [],
                 "unavailable": ["focus"]},
 "delta": {"tasks_opened": N, "tasks_completed": N, "stalls_appeared": N, "stalls_cleared": N,
           "issues_new": N, "messages_new": N, "held_pending": N},
 "action": "dispatch|propose|ask|sprint|nothing",
 "project_key": "<slug or null>",
 "rationale": "<your words, <= 600 chars: the weighing, the worker, the why>",
 "raise": {"decided": false, "reason": "<why it surfaced, or why not>", "held": []},
 "targets": {"assignment_ids": [], "work_item_ids": [], "msg_id": null}}
EOF
)
```

The door prints the `checkin_id`; rc 2 means the decision was refused (every reason
on stderr — fix and re-record), rc 1 means the plane did not record it (say so in
your next post if one is due; do not act on an unrecorded `dispatch` or `propose`).
Then ACT through the door in the table, passing `--checkin "$ck_id"` where a door
takes it. A proposal you reject later: `bash "$CLAUDLOBBY_ROOT/lib/checkin-propose.sh"
reject <wi_id> --reason "<why>"`.

## Rules

- One project, one action, one record per check-in. Never two actions.
- Never freelance a read (no ad-hoc SQL, no reading state files) or a write (no
  hand-built plane envelopes): the doors above are the whole contract.
- Never restate a project's tier in a post; point to it.
- `--dry-run` never records and never acts.
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_checkin_protocol.py tests/test_skill_refs.py tests/test_validator.py -q`
Expected: all pass. Then confirm the grant shapes on a real validate: from the worktree, `./.venv/bin/claudlobby --root "$(pwd)" validate 2>&1 | grep -i 'checkin' || echo "no checkin findings"` — expected `no checkin findings` (a malformed `tool_grants` entry would print a warning naming it).

- [ ] **Step 3: Commit (protocol + supersede edits + skill together — the requirement resolves only with both)**

```bash
git add library/protocols/checkin.md library/skills/checkin/SKILL.md library/protocols/worker-lifecycle.md library/protocols/proactivity-discipline.md library/protocols/continuous-autonomous-mode.md library/protocols/token-efficiency.md library/protocols/inbound-acknowledgment.md tests/test_checkin_protocol.py
printf '%s\n' 'feat(library): the check-in protocol and the /checkin skill; the cadence rules retired' '' 'One file, two sections (Manager: the surfacing judgment; Worker: one thin line on' 'start/done/blocked). The skill is a thin reasoning wrapper coupled to its doors' 'by name. Superseded: 2-3 min milestones, "Idle silence is a bug", 10-15 min' 'beacons, and the token-efficiency clause that protected them. Spec §6, §9.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c10.txt"
git commit -q -F "$TMPDIR/c10.txt" && git log --oneline -1
```

---

### Task 11: The empirical gate — the two doors on a real plane through the harness

**Files:**
- Modify: `lib/validate-bot-change.sh` (append a scenario block before the harness summary; the `#1481` block at `:618-686` is the template — `val_plane_ready`, `val_sql`, `harness_check`, `$VAL_CLI`, `$VAL_REPO`)

This is the CLAUDE.md-mandated gate for a runtime change: unit tests prove the envelopes with a stub transport; only running the real doors through the real shim, with the identity env a manager session carries, proves the rows LAND and the read door JOINS them.

- [ ] **Step 1: Append the block**

```bash
# ===========================================================================
# manager check-in chunk 1 -- the two write doors, end to end on a real plane.
# Unit tests pin the contract and the envelopes with a stub transport; what
# only running the real doors proves is that a decision and a proposal LAND as
# rows the read door can join, through the real shim, under the identity env
# a manager session carries (BOT_NAME, FLEET_NAME, the PROJECT_* map).
# ===========================================================================
echo ""
echo "=== validate manager check-in: the record and intake doors land on the plane ==="
CK_FLEET="valckf"
val_plane_ready "$ROOT" "$CK_FLEET"
ck_decision='{"inputs_seen":{"open_tasks":0},"delta":{},"action":"nothing","project_key":null,"rationale":"harness: nothing worth starting","raise":{"decided":false,"reason":"no delta","held":[]},"targets":{}}'
ck_id=$(printf '%s' "$ck_decision" | env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET" BOT_NAME="valckmgr" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$ROOT/no-daemon.sock" \
    bash "$VAL_REPO/lib/checkin-record.sh" 2> "$ROOT/ck-record.err" || true)
case "$ck_id" in ck_*) r=yes ;; *) r=no ;; esac
harness_check "checkin: the record door returns a checkin_id" "$r"
ck_row=$(val_sql "$ROOT" "SELECT json_extract(detail,'\$.action') || '|' || severity || '|' || subject_alias FROM events WHERE kind='system' AND event='checkin_decision' AND source_ref='checkin:$ck_id'")
[ "$ck_row" = "nothing|notice|bot:$CK_FLEET/valckmgr" ] && r=yes || r=no
harness_check "checkin: ...and the decision LANDED as one actor-anchored notice row (source_ref checkin:<id>)" "$r"
ck_wi=$(env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET" BOT_NAME="valckmgr" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$ROOT/no-daemon.sock" \
    PROJECT_TIER_VALPROJ=review PROJECT_INITIATIVE_VALPROJ=propose \
    bash "$VAL_REPO/lib/checkin-propose.sh" propose --title "harness proposal" --repo acme/valproj --project valproj --checkin "$ck_id" 2> "$ROOT/ck-propose.err" || true)
case "$ck_wi" in wi_*) r=yes ;; *) r=no ;; esac
harness_check "checkin: the intake door returns a work_item_id" "$r"
ck_prop=$(val_sql "$ROOT" "SELECT w.project_key || '|' || w.repo || '|' || (SELECT COUNT(*) FROM assignments a WHERE a.work_item_id = w.work_item_id) FROM work_items w WHERE w.work_item_id = '$ck_wi'")
[ "$ck_prop" = "valproj|acme/valproj|0" ] && r=yes || r=no
harness_check "checkin: ...as a work item carrying repo + project_key and NO assignment" "$r"
ck_listed=$(CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$CK_FLEET" --proposals --json 2>/dev/null | grep -c "$ck_wi" || true)
[ "${ck_listed:-0}" -ge 1 ] && r=yes || r=no
harness_check "checkin: the read door lists the open proposal" "$r"
env CLAUDLOBBY_ROOT="$ROOT" FLEET_NAME="$CK_FLEET" BOT_NAME="valckmgr" \
    PLANE_EMIT_CLI="$VAL_CLI" PLANE_SOCKET="$ROOT/no-daemon.sock" \
    bash "$VAL_REPO/lib/checkin-propose.sh" reject "$ck_wi" --reason "harness veto" 2>> "$ROOT/ck-propose.err" || true
ck_listed2=$(CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$CK_FLEET" --proposals --json 2>/dev/null | grep -c "$ck_wi" || true)
[ "${ck_listed2:-0}" -eq 0 ] && r=yes || r=no
harness_check "checkin: ...and a rejected proposal leaves the queue (a projection, not a table)" "$r"
```

(No apostrophes inside comments within `$( )` — the `val_sql` quoting above is inside a string argument, which is fine; `\$.action` keeps `$.` from expanding in the double-quoted string.)

- [ ] **Step 2: Run the harness unsandboxed and read the six lines**

Run: `bash lib/validate-bot-change.sh 2>&1 | tee "$TMPDIR/vbc.txt" | grep -E 'checkin:|PASS|FAIL' | tail -20`
Expected: six `PASS` lines beginning `checkin:`; the harness's overall verdict unchanged from main's (run main's harness once for the baseline if unsure). If the harness needs tmux/bots that the scenario does not, that is pre-existing and out of scope — the six lines are what this task certifies. **Paste the six lines into the PR body** (Task 13).

- [ ] **Step 3: Commit**

```bash
git add lib/validate-bot-change.sh
printf '%s\n' 'test(harness): the check-in record and intake doors land on a real plane' '' 'validate-bot-change.sh gains the chunk-1 scenario: record -> row, propose ->' 'work item with no assignment, read door lists it, reject -> leaves the queue.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c11.txt"
git commit -q -F "$TMPDIR/c11.txt" && git log --oneline -1
```

---

### Task 12: `CLAUDE.md`, `CHANGELOG.md`

**Files:** `CLAUDE.md` (the `lib/` table; the `## Python Package Structure` block; `## Key Commands`), `CHANGELOG.md` (`[Unreleased]`).

- [ ] **Step 1: The three lib rows** — append to the `lib/` table in `CLAUDE.md`, in the house style (one row each, the WHY inside):

```markdown
| `checkin-record.sh` | THE write door for a manager check-in decision (manager check-in spec §7). Validates the schema-1 record through `checkin-contract.py`, mints the `ck_` id, lands ONE actor-anchored `checkin_decision` system event stamped `source_ref checkin:<id>` — the task-recheck stamp idiom, so the read door joins on the ref and the record needs no other key. **For this door the record IS the action**: rc 1 when the plane did not record (never a silent 0), rc 2 when the contract refuses (every reason named), rc 3 without an identity. The plane is always on; `PLANE_EMIT_DISABLED=1` is the one silencer and it says so |
| `checkin-contract.py` | The schema-1 decision record, stdlib (the `dispatch-overdue.py` precedent): `normalize()` lists EVERY defect rather than the first, mints `checkin_id`, defaults every counter to 0 and every list to `[]` so "could not measure" is recorded as `unavailable`, never as a missing key. Also the CLI filter the door pipes through |
| `checkin-propose.sh` | THE intake door (spec §8): `propose` lands a work item with NO assignment plus `task_proposed`; `reject` lands `task_rejected`. The well-defined bar is a REFUSAL, not prose — title, `owner/name` repo, a `projects.yaml` project whose tier resolves from the session env, and a grant (`PROJECT_INITIATIVE_<SLUG>` autonomous\|propose; `none` originates nothing). Approval reuses the work item through `dispatch-task.sh --work-item`, so one piece of work stays one row; a dispatched proposal is withdrawn through `task-act.sh withdraw` |
```

- [ ] **Step 2: The package structure and the key commands** — in the `## Python Package Structure` block add, after `skill_refs.py`:

```
  requires.py         — Equipment linking (manager check-in spec §10): reads a library item's `requires:` frontmatter (re-parse, the `_read_tool_grants` pattern — `LibraryItem` carries title/description/body only), resolves a protocol list to the skills it requires, and reads `requires.role`. `compose_bot` links the requirements through `link_skills` after the declared list; the validator errors on an unresolvable requirement and warns when a protocol written for a role lands on a bot outside it
```

and in `commands/` mention `checkins`; under `## Key Commands` → `# Operations` add:

```bash
claudlobby checkins [--bot B] [--since 7d] [--last] [--json]   # the manager check-in's decisions (plane read)
claudlobby checkins --proposals                                # the open intake queue (a projection)
```

- [ ] **Step 3: CHANGELOG** — under `[Unreleased]`, one bullet per landed piece (initiative, requires, leaf-manager, the three doors, the read door, the protocol/skill, the retired cadence rules).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md CHANGELOG.md
printf '%s\n' 'docs: CLAUDE.md rows and CHANGELOG for the check-in contract (chunk 1)' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > "$TMPDIR/c12.txt"
git commit -q -F "$TMPDIR/c12.txt" && git log --oneline -1
```

---

### Task 13: The gauntlet, the PR, the merge, the deploy

This is the operator's standing loop for every chunk. Nothing merges without all of it, and the PR body cites each observation — claimed evidence is not evidence.

- [ ] **Step 1: Review lenses** — run `/simplify`, then `/review-work`, then `/verify-completion` on the branch (the phase-finalization gate). Fold every finding as its own commit; re-run the touched test files.

- [ ] **Step 2: Committed-code mutants, in a detached worktree** — never against uncommitted code (a `git checkout` restore would wipe a fix pass). For each mutant: apply, run the named tests, expect ≥ 1 failure, restore with `git checkout -- <file>`.

```python
# $TMPDIR/mut-ck1-defs.py
MUTANTS = [
    ("leaf-inverted", "claudlobby/config.py",
     "if any(r not in managers for r in reports):", "if all(r in managers for r in reports):",
     ["tests/test_leaf_manager_role.py"]),
    ("requires-not-linked", "claudlobby/composer.py",
     "required=_requires.required_skills(paths, _effective, log=_emit))", "required=())",
     ["tests/test_requires_linking.py"]),
    ("requires-error-downgraded", "claudlobby/validator.py",
     "report.errors.append(\n                        f\"bot '{bot_name}': protocol '{pname}' requires skill \"",
     "report.warnings.append(\n                        f\"bot '{bot_name}': protocol '{pname}' requires skill \"",
     ["tests/test_requires_linking.py"]),
    ("initiative-default-propose", "claudlobby/config.py",
     "initiative: str = \"none\"", "initiative: str = \"propose\"",
     ["tests/test_projects_planning.py"]),
    ("rationale-cap-off", "lib/checkin-contract.py",
     "elif len(rationale) > RATIONALE_MAX:", "elif False:",
     ["tests/test_checkin_contract.py"]),
    ("record-swallows-failure", "lib/checkin-record.sh",
     "if [ \"${PLANE_EMIT_LAST_RC:-0}\" -ne 0 ]; then", "if false; then",
     ["tests/test_checkin_doors.py"]),
    ("proposals-keep-rejected", "claudlobby/plane/queries.py",
     "AND NOT EXISTS (SELECT 1 FROM events r WHERE r.kind = 'system' AND r.event = 'task_rejected'",
     "AND NOT EXISTS (SELECT 1 FROM events r WHERE r.kind = 'system' AND r.event = 'never_this'",
     ["tests/test_checkins_cli.py"]),
    ("rows-oldest-first", "claudlobby/plane/queries.py",
     "\" ORDER BY e.ingest_seq DESC\"\n)\n\n# The intake queue", "\" ORDER BY e.ingest_seq ASC\"\n)\n\n# The intake queue",
     ["tests/test_checkins_cli.py"]),
    ("propose-ignores-grant", "lib/checkin-propose.sh",
     "autonomous|propose) ;;", "autonomous|propose|none) ;;",
     ["tests/test_checkin_doors.py"]),
    ("role-warning-silenced", "claudlobby/validator.py",
     "if role and role not in defaults.roles_for(bot_name, fleet):", "if False:",
     ["tests/test_requires_linking.py"]),
]
```

Every anchor must occur exactly once in its file (assert `text.count(old) == 1` before applying); a mutant that survives is a missing test — add the test, not a weaker mutant.

- [ ] **Step 3: The two-leg full-suite gate** — `before.txt` is Task 0's; the after leg runs on the FINAL committed tip:

```bash
./.venv/bin/pytest --tb=no -ra > "$TMPDIR/run_after.txt" 2>&1; echo "rc=$?"
awk "/short test summary info/,0" "$TMPDIR/run_after.txt" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u > "$TMPDIR/after.txt"
comm -13 "$TMPDIR/before.txt" "$TMPDIR/after.txt"     # failures YOU introduced — must be empty
tail -1 "$TMPDIR/run_before.txt"; tail -1 "$TMPDIR/run_after.txt"   # failed counts must match; passed grows by the new tests
```
Both rc must be 1 (the red baseline). Known load flakes on this host: `test_boot_capture.sh:203 (dur=1)` and the `test_github_app_wrapper` refresh loop — re-run a flake alone before calling it a regression. The after leg's `passed` count must equal before's plus the number of tests this plan added (count them: `grep -c "^def test_" tests/test_projects_planning.py tests/test_requires_linking.py tests/test_leaf_manager_role.py tests/test_checkin_contract.py tests/test_checkin_doors.py tests/test_checkins_cli.py tests/test_checkin_protocol.py`, plus parametrized expansions).

- [ ] **Step 4: Push, open the PR, CI on Linux**

```bash
git push -u origin checkin/chunk1-contract
gh pr create --title "feat(checkin): chunk 1 — the contract (initiative, requires:, leaf-manager, the doors, the skill)" --body-file "$TMPDIR/pr-body.md"
```

The PR body (`$TMPDIR/pr-body.md`) carries, in this order: what landed (one line per task); **the empirical observation** — the six `checkin:` harness lines pasted verbatim from Task 11 plus the `test_the_decision_lands_on_a_real_plane` name; the two-leg gate result (`comm -13` empty; before/after count lines pasted); the mutant table (10 names, each with the test that killed it); the naked-bot note ("no `DEFAULT_*` changed; the leaf-manager arm and the baseline re-record are chunk 5"); the spec link. End with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Wait for CI green; a `test_boot_capture.sh` load flake is re-run, not waved through.

- [ ] **Step 5: Admin-merge with the explicit squash body** (the operator's standing authorization for gauntleted work) — `gh pr merge --squash --admin --body-file "$TMPDIR/pr-body.md"`; then delete the branch.

- [ ] **Step 6: Deploy to the Mini and verify live**

Chunk 1 ships **no default and no trigger**: nothing composes differently on the estate except every `bot.conf` gaining `PROJECT_INITIATIVE_<SLUG>=none` lines (read at session start; inert). So the deploy is a pull plus a per-fleet `validate` + `generate` + `diff`, no restart:

```bash
ssh -o BatchMode=yes mini 'bash -s' <<'EOF'
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd ~/Projects/claudlobby
git status --porcelain | grep -q . && { echo "dirty checkout — stop"; exit 1; }
git fetch -q origin main && [ "$(git rev-list --count HEAD..origin/main)" -gt 0 ] || { echo "nothing to pull"; }
git pull --ff-only
for f in $(ls local/*/ -d 2>/dev/null | xargs -n1 basename); do
  .venv/bin/claudlobby --fleet "$f" validate && .venv/bin/claudlobby --fleet "$f" generate && .venv/bin/claudlobby --fleet "$f" diff | head -20
done
.venv/bin/claudlobby checkins --fleet "$(ls local/home | head -1)" ; echo "rc=$?"
EOF
```
Expected: `validate` clean on every fleet (no `requires`/`planning` findings), `diff` shows only the `PROJECT_INITIATIVE_` lines, and `claudlobby checkins` prints `no check-ins — fleet …` at rc 0 (the read door answers on the live plane; an rc 3 here means the install's `lib/` predates the door — pull again). Record the result on the PR as a comment.

---

## Self-review (run against the spec after writing; findings folded above)

**Spec coverage → task.** §5 trigger — *chunk 2 (not this plan)*. §6 skill → Task 10 (READ 0–6, DECIDE table, allocation rule, surfacing judgment, RECORD before ACT, failure posture, `CHECKIN_MAX_PROPOSALS`); focus read records `unavailable` (§12.1) → Task 10 step 5; sprint action unavailable until 1c (§12.1c) → Task 10 table. §6b sprint — *chunk 1c*. §7 decision record → Task 6 (schema 1 verbatim; `source_ref checkin:<id>`; severity line). §8 intake store → Task 7 (work item + `task_proposed`; `task_rejected`; well-defined bar as refusal; grant gate; `--work-item` reuse; withdraw through `task-act.sh`) and Task 8 (the projection). §8b initiative → Tasks 1–2 (values, default `none`, composition beside the tier, `known_values`, validator, schema doc, no `priority:`). §8c focus — *chunk 1b*. §9 protocol → Task 9 (one file two sections; the four supersede edits + the token-efficiency clause that protected them; no `EXCLUSIVE_GROUPS` entry, reasoned). §10 requires → Tasks 3–4 (reader, compositor union before `link_skills`, validator error, `list-library`, opt-out semantics fall out of the effective list); leaf-manager + `requires.role` warning → Task 5; the naked-bot arm → *chunk 5*. §11 read door → Task 8 (minimal; summary/outcomes chunk 3). §13 unit tests → every task's Step 1; empirical → Task 11; naked-bot delta → chunk 5. §14 failure posture → Task 6 (record door rc ladder), Task 7 (refusals), Task 8 (rc 3), Task 10 (degraded → `ask | nothing`).

**Placeholder scan.** No TBD/TODO; every code step carries code; "similar to Task N" avoided by repeating the rig helpers in each test file. One deliberate "confirm before editing" in Task 9 step 4 (line anchors for the supersede edits) — it names the exact old and new text, so it is a verification, not a placeholder.

**Type consistency.** `requires.read_requires -> dict[str, list[str]]`, `read_required_role -> str | None`, `required_skills -> list[str]` (Task 3) match their uses in Tasks 4, 5, 9. `link_skills(..., *, required)` (Task 3) matches the `compose_bot` call (Task 3 step 5). `defaults.roles_for(bot_id, fleet)` (Task 5) matches the composer and validator call sites. `checkin-record.sh` rc ladder (0/1/2/3) is the same in Task 6's tests, the script, the CLAUDE.md row and the skill. `CHECKIN_ROWS_SQL` binds `(fleet, fleet, since)` and `PROPOSALS_SQL` `(fleet, fleet)` — matched in `commands/checkins.py`. The severity trio is named identically in Task 6's registry lines and tests. `_Args.checkins_fleet` matches the parser's `dest="checkins_fleet"`.
