---
title: "System Defaults Tier"
type: plan
status: draft
owner: mason
created: 2026-06-09
updated: 2026-06-09
tags: [claudlobby, compositor, system-defaults, observability, infrastructure]
---

# System Defaults Tier

Compositor-hardcoded infrastructure that every fleet gets automatically. Adds a system defaults layer below fleet.yaml so new fleets start with working observability, keepalive, and log rotation without manual configuration.

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
```

`fleet_timers` defines fleet-level systemd/launchd timers. `interval_from` references a field from the final merged observability config (so fleet.yaml overrides to `pulse_interval: 600` propagate to the timer). `interval` is a static value in seconds.

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

Hooks are deduplicated by `(command, matcher)` tuple. When merging layers, if a higher-priority layer declares a hook with the same command and matcher, the lower-priority version is dropped.

```python
def _hook_key(entry: dict) -> tuple[str, str]:
    """Identity key for deduplication."""
    return (entry.get("command", ""), entry.get("matcher", ""))

def _merge_hooks_dedup(
    base: dict[str, list[dict]],
    override: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Merge hooks, deduplicating by (command, matcher). Override wins."""
    merged = {}
    for event in sorted(set(base) | set(override)):
        seen: set[tuple[str, str]] = set()
        entries: list[dict] = []
        # Override entries first (they win on collisions)
        for e in override.get(event, []):
            key = _hook_key(e)
            if key not in seen:
                seen.add(key)
                entries.append(e)
        # Base entries (skipped if override already declared same command+matcher)
        for e in base.get(event, []):
            key = _hook_key(e)
            if key not in seen:
                seen.add(key)
                entries.append(e)
        merged[event] = entries
    return merged
```

This replaces the existing `_merge_hooks()` which concatenates without dedup. The new function handles all three merge points: system-into-defaults, defaults-into-bot, and the existing fleet-default + bot-stanza merge.

**Backwards compatibility:** Existing fleets that declare bot-vitals hooks in fleet.yaml get zero behavioral change -- their hooks win via dedup and no duplicate appears.

## Opt-Out

**Decision Fork F2.** Lean: (c) both kill switch and per-category.

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

`compose_fleet()` gains a `compose_fleet_timers()` step that emits systemd service+timer units and launchd plists into `runtime/fleet/`.

### Generated Artifacts

For each timer in `system_defaults.yaml`'s `fleet_timers`:

| File | Purpose |
|------|---------|
| `runtime/fleet/<prefix>.<name>.service` | systemd oneshot service |
| `runtime/fleet/<prefix>.<name>.timer` | systemd timer unit |
| `runtime/fleet/<prefix>.<name>.plist` | launchd LaunchAgent |

Example for fleet-pulse with `service_prefix=com.crog.eng`:

```ini
# com.crog.eng.fleet-pulse.service
[Unit]
Description=claudlobby fleet-pulse (crog-eng-team)

[Service]
Type=oneshot
Environment=CLAUDLOBBY_ROOT=/home/crog/claudlobby
Environment=CLAUDLOBBY_FLEET=crog-eng-team
ExecStart=/home/crog/claudlobby/lib/fleet-pulse.sh crog-eng-team

# com.crog.eng.fleet-pulse.timer
[Unit]
Description=claudlobby fleet-pulse timer (crog-eng-team) -- tick every 300s

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
def _resolve_timer_interval(timer_cfg: dict, fleet: FleetConfig) -> int:
    """Resolve interval from config reference or static value."""
    if "interval_from" in timer_cfg:
        # Parse "observability.pulse_interval" -> fleet-level merged value
        ref = timer_cfg["interval_from"]
        section, _, field = ref.partition(".")
        if section == "observability":
            # Read from any bot's observability (all share fleet defaults)
            first_bot = next(iter(fleet.bots.values()))
            return getattr(first_bot.observability, field)
    return timer_cfg.get("interval", 300)
```

### Install Script Changes

Existing `install-fleet-pulse-systemd.sh` and `install-keepalive-systemd.sh` become thin wrappers. Instead of generating units inline, they copy from `runtime/fleet/` and enroll:

```bash
# New pattern: copy generated unit + enable
cp "$FLEET_DIR/runtime/fleet/$NAME.service" "$HOME/.config/systemd/user/"
cp "$FLEET_DIR/runtime/fleet/$NAME.timer" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"
```

This keeps a single source of truth (compositor generates, scripts install).

## Visibility

### claudlobby diff

Extended to diff fleet-level timer units:

```python
def diff_fleet_timers(fleet: FleetConfig, paths: Paths, system_defaults: dict) -> str:
    """Diff fleet-level timer units against what generate would produce."""
    fleet_dir = paths.runtime / "fleet"
    # Compare each expected timer unit against actual on disk
    # Return unified diff or "no drift"
```

Called from the existing `diff` command alongside per-bot diffs.

### claudlobby doctor

New checks:

| Check | Status | Detail |
|-------|--------|--------|
| `system-defaults-loaded` | pass/warn | "system defaults active: hooks (2), observability (4), timers (3)" |
| `fleet-timers-installed` | pass/warn | Checks each timer's systemd/launchd enrollment status |
| `system-defaults-overrides` | info | "fleet.yaml overrides: observability.reap_days (14 overrides system 7)" |
| `system-defaults-disabled` | info | "system defaults disabled: hooks, timers" (when user opts out) |

### claudlobby validate

Informational output appended to validation report:

```
[info] system defaults: hooks (2 events), observability (4 fields), timers (3)
[info] fleet overrides system defaults: observability.reap_days (14)
[info] fleet declares hooks also in system defaults: PreToolUse bot-vitals.sh (deduped, fleet version wins)
```

No errors or warnings from system defaults -- purely informational.

## fleet.yaml.example Migration

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
- **Lean:** (a). Semantically correct without new fields. Override by declaring the same command with different params (timeout, async).
- **Ratifier:** Human

### F2: Opt-out granularity

- **(a) Single kill switch** -- `system_defaults: false`. All or nothing.
- **(b) Per-category only** -- `system_defaults: { hooks: false, timers: false }`.
- **(c) Both** -- Top-level bool for kill-all, per-category for surgical control.
- **Lean:** (c). Power users want granularity. New users want simplicity. Cheap to implement.
- **Ratifier:** Human

### F3: Timer generation ownership

- **(a) `claudlobby generate` emits them** into `runtime/fleet/`. Install scripts become thin copy+enable wrappers. Single source of truth.
- **(b) Keep generation in lib/ install scripts.** `doctor` warns if not installed.
- **Lean:** (a). `generate` already owns all generated artifacts. Consistent model.
- **Ratifier:** Human

### F4: Fleet-level unit location

- **(a) `runtime/fleet/`** -- Alongside `runtime/bots/`. Can hold more than timers later.
- **(b) `runtime/timers/`** -- More specific name.
- **Lean:** (a). Natural home for fleet infrastructure that isn't bot-specific.
- **Ratifier:** Human

## Files Changed

| File | Change |
|------|--------|
| `claudlobby/system_defaults.yaml` | **New.** Platform infrastructure defaults definition. |
| `claudlobby/config.py` | Load system defaults, `SystemDefaultsConfig` dataclass, `_merge_system_into_defaults()`, hook dedup in `_merge_hooks_dedup()`. |
| `claudlobby/composer.py` | `compose_fleet_timers()` for fleet-level units. Called from `compose_fleet()`. |
| `claudlobby/validator.py` | Informational output for system defaults status + override detection. |
| `claudlobby/paths.py` | `system_defaults_file` and `runtime_fleet` properties. |
| `claudlobby/diff.py` | `diff_fleet_timers()` for fleet-level unit drift detection. |
| `claudlobby/doctor.py` | System defaults checks (loaded, timers installed, hooks active). |
| `fleet.yaml.example` | Remove hooks/observability from defaults, add system_defaults comments. |
| `documentation/fleet-yaml-schema.md` | Document `system_defaults` field, precedence rules, opt-out. |
| `lib/install-fleet-pulse-systemd.sh` | Thin wrapper: copy from `runtime/fleet/` + enroll. |
| `lib/install-keepalive-systemd.sh` | Thin wrapper: copy from `runtime/fleet/` + enroll. |
| `tests/test_system_defaults.py` | **New.** Unit tests for merge logic, dedup, opt-out, timer generation. |
| `tests/test_config.py` | Extended: three-layer merge, hook dedup, SystemDefaultsConfig parsing. |
| `tests/test_composer.py` | Extended: fleet timer generation, opt-out skips timers. |

## Testing Strategy

### Unit Tests (pytest)

- **Merge precedence:** system < fleet-defaults < bot-stanza for hooks, observability.
- **Hook dedup:** same command+matcher dedupes; different matcher keeps both; user version wins on collision.
- **Opt-out:** `system_defaults: false` produces identical output to pre-feature behavior. Per-category disables are respected.
- **Timer generation:** correct systemd + launchd units emitted. Interval resolution from config references works.
- **Backwards compat:** existing fleet.yaml with manual hooks/observability produces identical BotConfig to pre-feature (dedup absorbs duplicates).

### Empirical Validation (lib/ changes)

- `validate-bot-change.sh`: verify bot-vitals hooks fire from system defaults (not fleet.yaml declaration).
- Spin up a fleet with no hooks/observability in fleet.yaml, confirm events land in `data/events/`.
- `reconcile-fleet.sh`: verify fleet-level timers appear in health audit.

## Constraints

- No backwards-compat shims. Clean cut.
- Existing fleets that already declare hooks get zero behavioral change (dedup).
- System defaults visible in `claudlobby diff` / `claudlobby doctor`.
- Works on both Linux (systemd) and macOS (launchd).
- No PII or real credentials in system_defaults.yaml (uses `$CLAUDLOBBY_ROOT` placeholder).
