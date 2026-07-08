---
title: "System Defaults Tier"
type: plan
status: partially-completed
owner: mason
created: 2026-06-09
updated: 2026-07-06
tags: [claudlobby, compositor, system-defaults, observability, infrastructure]
shipped: Core engineering (three-layer merge, SystemDefaultsConfig opt-out, hook dedup, compose_fleet_timers(), runtime/fleet/ output, thin-wrapper installs, fleet.yaml.example migration) shipped as spec'd in f6874d7 (PR #392, merged 2026-06-10) with 34+ tests -- all 4 decision forks (F1-F4) locked exactly as below. Later renamed system_defaults.yaml -> system.yaml and fleet_timers -> jobs, plus added a new host tier this doc doesn't describe (see "Post-Ship Evolution" below). Never shipped -- the 4 proposed claudlobby doctor checks and claudlobby validate informational output (doctor.py/validator.py have zero system-defaults awareness); fleet.system_defaults also lacks a field-reference entry in fleet-yaml-schema.md.
---

# System Defaults Tier

Compositor-hardcoded infrastructure that every fleet gets automatically. Adds a system defaults layer below fleet.yaml so new fleets start with working observability, keepalive, and log rotation without manual configuration.

## Post-Ship Evolution

**Read this before implementing against any name below -- the shipped system uses different names than this plan.** The engineering core here shipped essentially as written (commit `f6874d7`, PR #392, merged 2026-06-10, 34 new tests) -- all four decision forks (F1-F4) locked exactly as proposed below. A later, undocumented initiative then evolved the concept past this plan:

- **`claudlobby/system_defaults.yaml` was renamed to `claudlobby/system.yaml`.** `claudlobby/config.py`'s `_resolve_system_yaml()` actively guards against the old name -- it raises `RuntimeError` if a stale `system_defaults.yaml` is found on disk, telling the caller it was renamed.
- **`fleet_timers:` was renamed to `jobs:`** (nested under `defaults:` in `system.yaml`).
- **A new top-level `host:` tier was added to `system.yaml`** for host-global singleton jobs (`claude-update`, `notify-behind`, `disk-monitor`, `fleet-memory-check`) -- enrolled once per host by `setup-system`, not per-fleet. This plan predates the `host:` tier and does not describe it anywhere below.

The mechanics below (three-layer merge, `SystemDefaultsConfig`, hook dedup, opt-out, `compose_fleet_timers()`) are still an accurate description of how the shipped system works in spirit -- only the file name and the `fleet_timers` key renamed. **For the current, authoritative field names and schema, see `documentation/fleet-yaml-schema.md` and `claudlobby/config.py`'s `SystemDefaultsConfig` class and system.yaml-loading code (`_resolve_system_yaml`, `_merge_system_into_defaults`).** The rest of this document is left as originally written/revised, preserved as the historical record of what was proposed -- do not write new code against `system_defaults.yaml` or `fleet_timers`.

## Problem

Today claudlobby's compositor is purely declarative -- it composes exactly what fleet.yaml declares and nothing more. If a user copies fleet.yaml.example and runs generate, they get zero observability unless they manually configure hooks and timers. The entire trust loop (bot-vitals, fleet-pulse, keepalive) only works because our fleet.yaml manually declares it. A cold-start user gets none of it.

There's no system layer. Infrastructure behaviors that define HOW claudlobby works (not WHAT a specific fleet does) require manual configuration. This leads to:

1. New fleets starting broken by default
2. Observability features shipped but never enabled
3. Tribal knowledge about which hooks/timers to configure

## Solution

Add a system defaults tier to the compositor. System defaults are always injected -- they represent how claudlobby works as a platform. User config (fleet.yaml) can override or disable them, but omission means you get the default, not nothing.

## Architecture

### Three-Layer Merge Order

**Ship status: COMPLETED** -- `claudlobby/config.py:820` `_merge_system_into_defaults()`; `config.py:855-919` `load_fleet()` calls it before `_coerce_bot()`; `_OBS_DEFAULT_*` constants fully removed from `config.py`/`composer.py` (zero grep matches). Commit `f6874d7` (PR #392).

```
system_defaults.yaml    (lowest priority -- platform infrastructure)
       |
fleet.yaml defaults:    (mid -- fleet-wide user config)
       |
fleet.yaml bot stanza   (highest -- per-bot user config)
       =
final BotConfig
```

System defaults load from `claudlobby/system_defaults.yaml` (inside the Python package directory). Before `_coerce_bot()` runs, system defaults are pre-merged into the fleet defaults dict. `_coerce_bot()` still sees two layers (merged-defaults, bot-stanza) -- the system tier is folded in before it arrives.

**Single source of truth for observability defaults.** Today observability defaults are dual-sourced:

1. `config.py` hardcodes `_OBS_DEFAULT_PULSE_INTERVAL`, `_OBS_DEFAULT_REAP_DAYS`, `_OBS_DEFAULT_ACTIVITY_STUCK_THRESHOLD`, and `_OBS_DEFAULT_DISPATCH_DEADLINE` as module-level constants. `_merge_observability()` falls back to them when both layers are None.
2. `composer.py` (lines 376-406) imports these same constants and uses them as fallbacks in `compose_bot_conf()` when `bot.observability` fields are None.

This PR removes the constants from `config.py` and the imports + None-fallback pattern from `composer.py`. Observability defaults live exclusively in `system_defaults.yaml`. After system defaults are pre-merged into the fleet defaults dict, `_merge_observability()` simplifies to a two-layer merge (override wins when not None, else default) with no third fallback. And `compose_bot_conf()` reads directly from `bot.observability` fields — they are guaranteed non-None after the system tier merge.

```python
# composer.py — before (lines 376-406):
from .config import (_OBS_DEFAULT_PULSE_INTERVAL, ...)
obs = bot.observability
pi = obs.pulse_interval if obs.pulse_interval is not None else _OBS_DEFAULT_PULSE_INTERVAL
# ... same pattern for all 4 fields

# composer.py — after:
obs = bot.observability
lines.append(f"export OBSERVABILITY_PULSE_INTERVAL={_shq(obs.pulse_interval)}")
lines.append(f"export OBSERVABILITY_REAP_DAYS={_shq(obs.reap_days)}")
lines.append(f"export OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD={_shq(obs.activity_stuck_threshold)}")
lines.append(f"export OBSERVABILITY_DISPATCH_DEADLINE={_shq(obs.dispatch_deadline)}")
```

**Opt-out edge case:** When `system_defaults: { observability: false }`, no system observability values are injected. If the fleet also omits observability in its defaults, `bot.observability` fields will be None. `compose_bot_conf()` must handle this — skip the observability section entirely when all fields are None:

```python
if any(v is not None for v in [obs.pulse_interval, obs.reap_days,
                                obs.activity_stuck_threshold, obs.dispatch_deadline]):
    lines.append("")
    lines.append("# Observability")
    # ... emit only non-None fields
```

### Data Flow

```
system_defaults.yaml ---+
                        +-- _merge_system_into_defaults() --> merged_defaults
fleet.yaml defaults: --+                                         |
                                                                  +-- _coerce_bot() --> BotConfig
fleet.yaml bot stanza -----------------------------------------------+       |
                                                                              +-- compose_bot() --> per-bot files
                                                                              +-- compose_fleet_timers() --> runtime/fleet/ units
```

## system_defaults.yaml

**Ship status: COMPLETED, but renamed/evolved.** Shipped at this exact path in `f6874d7`, then renamed to `claudlobby/system.yaml` in `e90ac90` (#458). `config.py:774-790` `_resolve_system_yaml()` raises `RuntimeError` if a stale `system_defaults.yaml` is found on disk. The renamed file also gained a new `host:` tier and a `fleet_timers` -> `jobs` rename -- see "Post-Ship Evolution" above.

Lives at `claudlobby/system_defaults.yaml`. Shipped with the repo, versioned. Declarative, readable -- users can inspect it to understand what the platform injects.

```yaml
# System defaults -- platform infrastructure injected into every fleet.
# These represent HOW claudlobby works, not WHAT a specific fleet does.
# Fleet.yaml system_defaults: can override or disable.

hooks:
  PreToolUse:
    - command: "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh"
  PostToolUse:
    - command: "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh"

observability:
  pulse_interval: 300
  reap_days: 7
  activity_stuck_threshold: 1800
  dispatch_deadline: 1800

fleet_timers:
  fleet-pulse:
    script: "$CLAUDLOBBY_ROOT/lib/fleet-pulse.sh"
    interval_from: observability.pulse_interval
    type: oneshot
  keepalive:
    script: "$CLAUDLOBBY_ROOT/lib/keepalive-all.sh"
    interval: 60
    type: oneshot
  log-rotation:
    script: "$CLAUDLOBBY_ROOT/lib/log-rotate-fleet.sh"
    interval: 86400
    type: oneshot
  creds-check:
    script: "$CLAUDLOBBY_ROOT/lib/creds-check.sh"
    schedule: "*-*-* 06:00:00"
    type: oneshot
```

`fleet_timers` defines fleet-level systemd/launchd timers. Three scheduling modes:

- `interval_from` — references a field from the final merged config (so fleet.yaml overrides to `pulse_interval: 600` propagate to the timer).
- `interval` — static value in seconds, emits `OnBootSec` + `OnUnitActiveSec`.
- `schedule` — systemd `OnCalendar` expression for daily-at-time scheduling (e.g., `*-*-* 06:00:00`). For launchd, converted to `StartCalendarInterval`.

## Merge Logic

### Pre-merge: System into Defaults

In `config.py`, a new function merges system defaults under fleet defaults before `_coerce_bot()` runs:

```python
def _merge_system_into_defaults(system: dict, defaults: dict) -> dict:
    """Merge system defaults under fleet defaults. Fleet defaults win."""
    merged = {}
    all_keys = set(system) | set(defaults)

    for key in all_keys:
        sys_val = system.get(key)
        usr_val = defaults.get(key)

        if key == "hooks":
            merged[key] = _merge_hooks_dedup(sys_val or {}, usr_val or {})
        elif key == "observability":
            # Shallow merge: user fields override system fields
            merged[key] = {**(sys_val or {}), **(usr_val or {})}
        elif usr_val is not None:
            merged[key] = usr_val
        else:
            merged[key] = sys_val

    return merged
```

This runs once in `load_fleet()`, before any per-bot coercion.

### Hook Deduplication

**Decision Fork F1.** Lean: (a) command-based dedup.

**Ship status: COMPLETED** -- `claudlobby/config.py:544` `_merge_hooks_dedup()` (replaces `_merge_hooks()`), used at merge points including bot-level (`config.py:727`) and system-into-defaults (`config.py:830`). Matches locked option (a) exactly.

Hooks are deduplicated by `(command, matcher)` tuple. When merging layers, if a higher-priority layer declares a hook with the same command and matcher, the lower-priority version is dropped.

```python
def _hook_key(entry: dict) -> tuple[str, str]:
    """Identity key for deduplication."""
    return (entry.get("command", ""), entry.get("matcher", ""))

def _merge_hooks_dedup(
    base: dict[str, list[dict]],
    override: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Merge hooks, deduplicating by (command, matcher). Override wins on collision.

    Preserves current base-first ordering: base entries appear first,
    override entries appended after. When a collision occurs (same
    command+matcher in both layers), the override version replaces the
    base version in-place.
    """
    merged = {}
    for event in sorted(set(base) | set(override)):
        override_map: dict[tuple[str, str], dict] = {}
        for e in override.get(event, []):
            override_map[_hook_key(e)] = e

        seen: set[tuple[str, str]] = set()
        entries: list[dict] = []
        # Base entries first (preserves existing order). If override
        # declares the same command+matcher, use the override version.
        for e in base.get(event, []):
            key = _hook_key(e)
            if key not in seen:
                seen.add(key)
                entries.append(override_map.get(key, e))
        # Override-only entries appended after base
        for e in override.get(event, []):
            key = _hook_key(e)
            if key not in seen:
                seen.add(key)
                entries.append(e)
        merged[event] = entries
    return merged
```

This replaces the existing `_merge_hooks()` which concatenates without dedup. The new function handles all three merge points: system-into-defaults, defaults-into-bot, and the existing fleet-default + bot-stanza merge.

**Backwards compatibility:** Existing fleets that declare bot-vitals hooks in fleet.yaml get zero behavioral change -- their hooks win via dedup and no duplicate appears. The dedup replacement applies to all hook merge points, not just system-defaults injection. Any fleet that previously concatenated identical hooks (same command + matcher across fleet defaults and bot stanza) now deduplicates them — the higher-priority layer's version wins. This is intentional: concatenating identical hooks was always a bug (double-firing), not a feature. Hook ordering is preserved: base (lower-priority) entries appear first, override entries appended after — matching the current `_merge_hooks()` behavior.

## Opt-Out

**Decision Fork F2.** Lean: (c) both kill switch and per-category.

**Ship status: COMPLETED** -- `claudlobby/config.py:62-69` `SystemDefaultsConfig` dataclass (`enabled`, `hooks`, `timers`, `observability`); `config.py:754-771` `_coerce_system_defaults()` parses `fleet.system_defaults` as bool or dict; wired into `load_fleet()` at `config.py:881-896`. Matches this section's design almost verbatim.

Fleet.yaml gains a `system_defaults` field at the fleet level:

```yaml
fleet:
  # Disable all system defaults
  system_defaults: false

  # Or granular control
  system_defaults:
    hooks: false        # no system-injected hooks
    timers: false       # no fleet-level timer generation
    observability: true # keep observability defaults (default)
```

Parsed as:

```python
@dataclass
class SystemDefaultsConfig:
    enabled: bool = True      # master switch
    hooks: bool = True
    timers: bool = True
    observability: bool = True
```

`system_defaults: false` sets `enabled=False`, which skips all system default injection. Per-category bools disable individual categories. When `enabled=False`, all categories are off regardless of their individual values.

### Wiring per-category opt-out

`_merge_system_into_defaults` receives the full `system_defaults.yaml` dict regardless of opt-out settings. The filtering happens before the merge call — `load_fleet()` builds an `effective_system` dict gated by the `SystemDefaultsConfig` booleans:

```python
# In load_fleet(), after parsing system_defaults_cfg:
raw_system = yaml.safe_load((pkg_dir / "system_defaults.yaml").read_text()) or {}

if not system_defaults_cfg.enabled:
    effective_system = {}
else:
    effective_system = {}
    if system_defaults_cfg.hooks:
        effective_system["hooks"] = raw_system.get("hooks", {})
    if system_defaults_cfg.observability:
        effective_system["observability"] = raw_system.get("observability", {})
    # fleet_timers are not merged into defaults — they're consumed
    # directly by compose_fleet_timers(), gated there by system_defaults_cfg.timers

merged_defaults = _merge_system_into_defaults(effective_system, defaults)
```

This keeps `_merge_system_into_defaults` simple (it merges whatever it receives) and puts the opt-out gate in one place.

Added to `FleetConfig`:

```python
@dataclass
class FleetConfig:
    # ... existing fields ...
    system_defaults: SystemDefaultsConfig = field(default_factory=SystemDefaultsConfig)
```

## Fleet-Level Timer Generation

**Decision Fork F3.** Lean: (a) `generate` emits them.
**Decision Fork F4.** Lean: (a) `runtime/fleet/` directory.

**Ship status: COMPLETED** -- `claudlobby/composer.py:1643` `compose_fleet_timers()`; called from `claudlobby/commands/core.py:85-90` (F3). `claudlobby/paths.py:345` `runtime_fleet` property (F4). Both forks resolved exactly as locked.

`compose_fleet_timers()` is a new top-level function in `composer.py`. It is called once in `__main__.py`'s `generate` command, after the per-bot `compose_bot()` loop completes. It emits systemd service+timer units and launchd plists into `runtime/fleet/timers/`.

`load_fleet()` returns a `(FleetConfig, dict)` tuple — the fleet config and the `merged_defaults` dict produced by `_merge_system_into_defaults()`. Callers that don't need `merged_defaults` can ignore the second element. `_load_fleet_or_exit()` in `commands/_helpers.py` is updated to return the same tuple.

```python
# config.py
def load_fleet(fleet_yaml: Path) -> tuple[FleetConfig, dict]:
    # ... existing parsing ...
    merged_defaults = _merge_system_into_defaults(effective_system, defaults)
    bots = {name: _coerce_bot(name, raw, merged_defaults) for name, raw in ...}
    return FleetConfig(...), merged_defaults

# commands/_helpers.py
def _load_fleet_or_exit(paths: Paths) -> tuple[FleetConfig, dict]:
    fleet, merged_defaults = load_fleet(paths.fleet_yaml)
    return fleet, merged_defaults

# commands/core.py — cmd_generate
fleet, merged_defaults = _load_fleet_or_exit(paths)
# ... validation, per-bot compose_bot loop ...
out = compose_fleet(fleet, paths)
if fleet.system_defaults.enabled and fleet.system_defaults.timers:
    compose_fleet_timers(fleet, paths, merged_defaults)
```

`compose_fleet_timers` receives `merged_defaults` for `interval_from` resolution. The `raw_system` dict for `fleet_timers` entries is loaded directly from `system_defaults.yaml` inside `compose_fleet_timers` — timer definitions are not part of the defaults merge (they're fleet-level artifacts, not bot-level config).

`Paths` gains a `runtime_fleet` property:

```python
@property
def runtime_fleet(self) -> Path:
    return self.runtime / "fleet"
```

### Generated Artifacts

For each timer in `system_defaults.yaml`'s `fleet_timers`:

| File | Purpose |
|------|---------|
| `runtime/fleet/timers/<prefix>.<name>.service` | systemd oneshot service |
| `runtime/fleet/timers/<prefix>.<name>.timer` | systemd timer unit |
| `runtime/fleet/timers/<prefix>.<name>.plist` | launchd LaunchAgent |

Example for fleet-pulse with `service_prefix=com.example.eng`:

```ini
# com.example.eng.fleet-pulse.service
[Unit]
Description=claudlobby fleet-pulse (my-fleet)

[Service]
Type=oneshot
Environment=CLAUDLOBBY_ROOT=/home/user/claudlobby
Environment=CLAUDLOBBY_FLEET=my-fleet
ExecStart=/home/user/claudlobby/lib/fleet-pulse.sh my-fleet

# com.example.eng.fleet-pulse.timer
[Unit]
Description=claudlobby fleet-pulse timer (my-fleet) -- tick every 300s

[Timer]
OnBootSec=300
OnUnitActiveSec=300
AccuracySec=10

[Install]
WantedBy=timers.target
```

### Interval Resolution

Timer intervals are resolved from the final merged config:

- `interval_from: observability.pulse_interval` -- reads the final merged observability config value. If fleet.yaml overrides `pulse_interval: 600`, the timer gets 600.
- `interval: 60` -- static value, not resolvable from config.

```python
def _resolve_timer_schedule(timer_cfg: dict, merged_defaults: dict) -> dict:
    """Resolve timer scheduling from config.

    Returns a dict describing the schedule type:
      {"type": "interval", "seconds": 300}
      {"type": "calendar", "expression": "*-*-* 06:00:00"}

    Uses the merged defaults dict (system + fleet defaults, before per-bot
    coercion) — fleet timers are fleet-level, not bot-level.
    """
    if "schedule" in timer_cfg:
        return {"type": "calendar", "expression": timer_cfg["schedule"]}
    if "interval_from" in timer_cfg:
        ref = timer_cfg["interval_from"]
        section, _, field = ref.partition(".")
        if section == "observability":
            obs = merged_defaults.get("observability", {})
            val = obs.get(field)
            if val is not None:
                return {"type": "interval", "seconds": int(val)}
    return {"type": "interval", "seconds": timer_cfg.get("interval", 300)}
```

Timer unit generation uses the schedule type to pick the right systemd directive:

- `interval` → `OnBootSec` + `OnUnitActiveSec` (periodic)
- `calendar` → `OnCalendar` (daily-at-time, e.g., `*-*-* 06:00:00`)

`compose_fleet_timers()` receives the `merged_defaults` dict (the product of `_merge_system_into_defaults()`) so interval resolution operates on fleet-level config, not bot-level overrides.

### Install Script Changes

**Ship status: COMPLETED (and further consolidated)** -- `lib/install-fleet-pulse-systemd.sh`, `lib/install-keepalive-systemd.sh`, `lib/install-creds-check-systemd.sh` are now ~11-line wrappers that all `exec lib/install_fleet_timer.sh <name> "$@"` -- a single shared helper, even thinner than what's proposed below.

Existing `install-fleet-pulse-systemd.sh` and `install-keepalive-systemd.sh` become thin wrappers. Instead of generating units inline, they copy from `runtime/fleet/` and enroll:

```bash
# New pattern: copy generated unit + enable
TIMER_DIR="$FLEET_DIR/runtime/fleet/timers"
if [[ ! -d "$TIMER_DIR" ]]; then
    echo "Error: $TIMER_DIR not found — run 'claudlobby generate' first." >&2
    exit 1
fi
cp "$TIMER_DIR/$NAME.service" "$HOME/.config/systemd/user/"
cp "$TIMER_DIR/$NAME.timer" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"
```

Install scripts guard against missing `runtime/fleet/timers/` — if it doesn't exist, the user hasn't run `claudlobby generate` yet. This keeps a single source of truth (compositor generates, scripts install).

## Visibility

### claudlobby diff

**Ship status: COMPLETED** -- `claudlobby/diff.py:73` `diff_fleet_timers()`, wired into `commands/core.py:209-220`. A real bug (passing a 2-attr shadow instead of real `Paths`) was caught and fixed post-ship in `bb02d56`/`88731f1` -- exactly the kind of gap the empirical-validation loop exists to catch.

Extended to diff fleet-level timer units:

```python
def diff_fleet_timers(fleet: FleetConfig, paths: Paths, system_defaults: dict) -> str:
    """Diff fleet-level timer units against what generate would produce."""
    timers_dir = paths.runtime_fleet / "timers"
    # Compare each expected timer unit against actual on disk
    # Return unified diff or "no drift"
```

Called from the existing `diff` command alongside per-bot diffs.

### claudlobby doctor

**Ship status: PENDING -- not shipped.** `claudlobby/doctor.py` (439 lines) has exactly 6 checks (`env-vars`, `mcp-configs`, `npx-cache`, `services`, `credentials`, `fleet-yaml`); zero matches for `system_defaults`/`system-defaults`/`timer`/`hook`. None of the 4 checks below exist under any name.

New checks:

| Check | Status | Detail |
|-------|--------|--------|
| `system-defaults-loaded` | pass/warn | "system defaults active: hooks (2), observability (4), timers (4)" |
| `fleet-timers-installed` | pass/warn | Checks each timer's systemd/launchd enrollment status |
| `system-defaults-overrides` | info | "fleet.yaml overrides: observability.reap_days (14 overrides system 7)" |
| `system-defaults-disabled` | info | "system defaults disabled: hooks, timers" (when user opts out) |

### claudlobby validate

**Ship status: PENDING -- not shipped.** `claudlobby/validator.py` (519 lines) has no `system_defaults`/`system-defaults` references and no generic `[info]`-style override-detection output.

Informational output appended to validation report:

```
[info] system defaults: hooks (2 events), observability (4 fields), timers (4)
[info] fleet overrides system defaults: observability.reap_days (14)
[info] fleet declares hooks also in system defaults: PreToolUse bot-vitals.sh (deduped, fleet version wins)
```

No errors or warnings from system defaults -- purely informational.

## fleet.yaml.example Migration

**Ship status: COMPLETED** -- `fleet.yaml.example:71-82` -- hooks/observability removed from `defaults:`, replaced with explanatory comments matching the block below almost verbatim.

Remove hooks and observability from the defaults section. Replace with comments explaining system defaults:

```yaml
defaults:
    model: opus
    effort: max
    account: default
    prompt_suggestions: false
    mcp: [github]
    guardrails: [no-push-main, no-destructive-git, pii-protection, no-fabrication, no-merge-own-pr]
    protocols: [report-back, context-management, telegram-routing, telegram-formatting]
    # Hooks (bot-vitals.sh) and observability come from system defaults.
    # Override: declare hooks/observability here (your version wins).
    # Disable: system_defaults: false (or per-category: system_defaults: { hooks: false })
    telegram:
      token_env: TELEGRAM_BOT_TOKEN
      require_mention: true
    sandbox:
      enabled: false
      auto_allow_bash: true
      network_allowed_domains:
        - api.github.com
        - api.telegram.org
        - "*.anthropic.com"
```

Existing user fleet.yaml files that declare hooks/observability continue to work unchanged -- their declarations override system defaults via dedup.

## Decision Forks

### F1: Hook dedup strategy

- **(a) Command-based dedup** -- Deduplicate by `(command, matcher)` tuple. No new config fields. The command string is a natural identity. Two hooks with the same command+matcher do the same thing.
- **(b) Named hook IDs** -- System hooks get explicit `_id` fields. User overrides by matching `_id`. More explicit but adds schema complexity.
- **Locked:** (a). Command-based dedup by `(command, matcher)` tuple. Higher-priority layer wins on collision.
- **Ratifier:** Human
- **Shipped:** COMPLETED -- `claudlobby/config.py:544` `_merge_hooks_dedup()`, matching locked option (a) exactly.

### F2: Opt-out granularity

- **(a) Single kill switch** -- `system_defaults: false`. All or nothing.
- **(b) Per-category only** -- `system_defaults: { hooks: false, timers: false }`.
- **(c) Both** -- Top-level bool for kill-all, per-category for surgical control.
- **Locked:** (c). Master switch + category overrides via `SystemDefaultsConfig` with 4 booleans.
- **Ratifier:** Human
- **Shipped:** COMPLETED -- `claudlobby/config.py:62-69` `SystemDefaultsConfig` dataclass, matching almost verbatim.

### F3: Timer generation ownership

- **(a) `claudlobby generate` emits them** into `runtime/fleet/`. Install scripts become thin copy+enable wrappers. Single source of truth.
- **(b) Keep generation in lib/ install scripts.** `doctor` warns if not installed.
- **Locked:** (a). `generate` emits fleet-level timer units. Install scripts become thin copy+enroll wrappers.
- **Ratifier:** Human
- **Shipped:** COMPLETED -- `claudlobby/composer.py:1643` `compose_fleet_timers()`, called from `commands/core.py:85-90`.

### F4: Fleet-level unit location

- **(a) `runtime/fleet/`** -- Alongside `runtime/bots/`. Can hold more than timers later. Timer units go in `runtime/fleet/timers/` subdirectory.
- **(b) `runtime/timers/`** -- More specific name.
- **Locked:** (a). `runtime/fleet/` as the fleet-level output directory, with `timers/` subdirectory for generated units. Structure: `runtime/fleet/timers/<prefix>.<name>.service|timer|plist`. Parallels `runtime/bots/` cleanly and leaves room for future fleet-level artifacts.
- **Ratifier:** Human
- **Shipped:** COMPLETED -- `claudlobby/paths.py:345` `runtime_fleet` property; `compose_fleet_timers()` writes to `<runtime_fleet>/timers/`.

## Files Changed

| File | Change |
|------|--------|
| `claudlobby/system_defaults.yaml` | **New.** Platform infrastructure defaults definition. |
| `claudlobby/config.py` | Load system defaults, `SystemDefaultsConfig` dataclass, `_merge_system_into_defaults()`, replace `_merge_hooks()` with `_merge_hooks_dedup()`, remove `_OBS_DEFAULT_*` constants, simplify `_merge_observability()`. |
| `claudlobby/commands/core.py` | `cmd_generate` unpacks `(fleet, merged_defaults)` tuple, calls `compose_fleet_timers()` after per-bot loop. |
| `claudlobby/commands/_helpers.py` | `_load_fleet_or_exit()` returns `(FleetConfig, dict)` tuple. |
| `claudlobby/composer.py` | `compose_fleet_timers()` for fleet-level units. Remove `_OBS_DEFAULT_*` imports and None-fallback pattern in `compose_bot_conf()`. |
| `claudlobby/validator.py` | Informational output for system defaults status + override detection. |
| `claudlobby/paths.py` | `system_defaults_file` and `runtime_fleet` properties. |
| `claudlobby/diff.py` | `diff_fleet_timers()` for fleet-level unit drift detection. |
| `claudlobby/doctor.py` | System defaults checks (loaded, timers installed, hooks active). |
| `fleet.yaml.example` | Remove hooks/observability from defaults, add system_defaults comments. |
| `documentation/fleet-yaml-schema.md` | Document `system_defaults` field, precedence rules, opt-out. |
| `lib/install-fleet-pulse-systemd.sh` | Thin wrapper: copy from `runtime/fleet/timers/` + enroll. |
| `lib/install-keepalive-systemd.sh` | Thin wrapper: copy from `runtime/fleet/timers/` + enroll. |
| `lib/install-creds-check-systemd.sh` | Thin wrapper: copy from `runtime/fleet/timers/` + enroll. |
| `tests/test_system_defaults.py` | **New.** Unit tests for merge logic, dedup, opt-out, timer generation. |
| `tests/test_config.py` | Extended: three-layer merge, hook dedup, SystemDefaultsConfig parsing. |
| `tests/test_composer.py` | Extended: fleet timer generation, opt-out skips timers. |

**Status note (2026-07-06):** Most rows above shipped as planned. Divergences: `claudlobby/validator.py` and `claudlobby/doctor.py` rows are **PENDING** -- neither file gained any system-defaults awareness (see Visibility section above). `documentation/fleet-yaml-schema.md` is **PARTIALLY** shipped -- it documents the `jobs`/`enroll` merge mechanics but has no dedicated `fleet.system_defaults` field-reference entry. `tests/test_config.py` and `tests/test_composer.py` were **not** extended as this table describes -- coverage was instead consolidated into the new `tests/test_system_defaults.py` (932 lines, 14 test classes), a filing deviation with no coverage gap.

## Testing Strategy

### Unit Tests (pytest)

**Ship status: COMPLETED, filing deviated.** `tests/test_system_defaults.py` (932 lines, 14 test classes) covers everything below and more (host timers, dormant manifest). But per the status note above, `test_config.py`/`test_composer.py` were not extended as the Files Changed table describes -- all coverage landed in the one new file instead.

- **Merge precedence:** system < fleet-defaults < bot-stanza for hooks, observability.
- **Hook dedup:** same command+matcher dedupes; different matcher keeps both; user version wins on collision.
- **Opt-out:** `system_defaults: false` produces identical output to pre-feature behavior. Per-category disables are respected.
- **Timer generation:** correct systemd + launchd units emitted. Interval resolution from config references works.
- **Backwards compat:** existing fleet.yaml with manual hooks/observability produces identical BotConfig to pre-feature (dedup absorbs duplicates).

### Empirical Validation (lib/ changes)

- `validate-bot-change.sh`: verify bot-vitals hooks fire from system defaults (not fleet.yaml declaration). **Not evidenced** -- `grep -n "system.default\|bot-vitals\|hook" lib/validate-bot-change.sh` returns zero matches; no direct evidence this check was added to the harness.
- Spin up a fleet with no hooks/observability in fleet.yaml, confirm events land in `data/events/`.
- `reconcile-fleet.sh`: verify fleet-level timers appear in health audit. **COMPLETED** -- `lib/reconcile-fleet.sh:88-94` reads `$CLAUDLOBBY_ROOT/local/$FLEET/runtime/fleet/timers`, checking merged `system.yaml` `defaults.jobs` + opt-ins and dormant units.

## Constraints

- No backwards-compat shims. Clean cut.
- Existing fleets that already declare hooks get zero behavioral change (dedup).
- System defaults visible in `claudlobby diff` / `claudlobby doctor`. **(Partially true: `diff` shipped; `doctor` did not -- see Visibility section above.)**
- Works on both Linux (systemd) and macOS (launchd).
- No PII or real credentials in system_defaults.yaml (uses `$CLAUDLOBBY_ROOT` placeholder).
