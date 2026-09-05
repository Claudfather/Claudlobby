# system.yaml — schema reference

`system.yaml` is the first config tier — **HOW the platform runs**
(`fleet.yaml` = WHO the bots are, `projects.yaml` = WHAT the work is; see
those docs for the other two tiers). Unlike the other two, it is
**package-owned, not fleet-owned**: the live file is `claudlobby/system.yaml`,
shipped inside the compositor install. There is no per-fleet overlay copy —
every fleet on a host reads the same file, from the same install.
`system.yaml.example` at the repo root is a read-only reference copy (kept
byte-identical to the package file by a unit test, per its own header
comment) so the three-tier story is readable without installing anything;
**do not copy it into a fleet** the way you would `fleet.yaml.example` or
`projects.yaml.example` — editing it means editing `claudlobby/system.yaml`
in the shared install itself, which affects every fleet that install serves.

Composed by `claudlobby generate` — unconditionally, on every run, for any
fleet (see [Composition & enrollment](#composition--enrollment)) — and by the
fleet-independent `claudlobby host-timers` subcommand. Enrolled by
`lib/setup-system` (the `host:` tier, once per host) and `lib/setup-fleet`
(the `defaults:` tier's jobs, per fleet) — composing and enrolling are
separate steps, and getting that split right matters for the dormancy
semantics below. There is **no dedicated `claudlobby validate` or
`claudlobby doctor` coverage of this file** — see
[Validation & visibility](#validation--visibility).

## Top-level shape

```yaml
host:                          # host-global singletons -- one instance per host,
  jobs:                        # fixed `claudlobby-<name>` unit identity, NOT layered
    <job-name>:                # through any fleet.yaml merge (see "Dormancy" below)
      script: "$CLAUDLOBBY_ROOT/lib/<script>.sh"   # required; anchored on $CLAUDLOBBY_ROOT
      schedule: "<systemd OnCalendar expr>"        # calendar-style scheduling
      interval: <seconds>                          # OR: fixed-interval scheduling
      type: oneshot                                # systemd Type= (default: oneshot)
      persistent: true | false                     # systemd Persistent= (default: false)
      randomized_delay: <seconds>                  # systemd RandomizedDelaySec= (default: 0)
      enroll: true | false                         # dormancy -- semantics differ by shape, see below
      unit: service                                # marks a RESIDENT service instead of a timer

defaults:                      # per-fleet defaults -- mirrors fleet.yaml's `defaults:` shape,
                                # merged UNDER it (system < fleet < bot)
  hooks:                       # same shape as fleet.yaml defaults.hooks / bots.<name>.hooks
    <EventName>:
      - command: "$CLAUDLOBBY_ROOT/lib/<script>.sh"
        matcher: <tool-pattern>                    # omit for all tools
        timeout: <seconds>                         # optional

  observability:               # the same fields as fleet.yaml bots.<name>.observability
    pulse_interval: <seconds>
    activity_stuck_threshold: <seconds>
    dispatch_deadline: <seconds>

  jobs:                        # fleet-level timer jobs -- same entry shape as host.jobs.<name>,
    <job-name>:                 # minus `unit: service` (host-only; see below)
      script: "$CLAUDLOBBY_ROOT/lib/<script>.sh"
      schedule: "<systemd OnCalendar expr>"        # OR interval / interval_from
      interval_from: "observability.<field>"       # dynamic interval from the merged config
      type: oneshot
      enroll: true | false                         # fleet opts in/out via fleet.yaml, see below
```

There is no third top-level key. In particular, **there is no `defaults.protocols` or
`defaults.guardrails`** — see [`fleet.system_defaults` — what it gates, from this
side](#fleetsystem_defaults--what-it-gates-from-this-side) for why, if you came here
expecting one.

## Job entry fields

The same entry shape is shared by `host.jobs.<name>` and `defaults.jobs.<name>`,
built by one emitter (`_write_timer_units` in `composer.py`) for both:

| Field | Type | Applies to | Default | Meaning |
|---|---|---|---|---|
| `script` | string | both | required | Command to run. Conventionally `$CLAUDLOBBY_ROOT`-anchored. A raw absolute path fails the same L1 source guard `generate` enforces on every compose source (`path_audit.denied_source_paths`) — anchor on `$CLAUDLOBBY_ROOT` or the job fails to compose. |
| `schedule` | string | both | — | A systemd `OnCalendar` expression (e.g. `"*-*-* 04:00:00"`, `"Sun *-*-* 04:30:00"`) — **not 5-field cron**, the same dialect as `fleet.yaml`'s `sweep.schedule` / `briefing.slots`. Takes priority over `interval`/`interval_from` when present. |
| `interval` | int (seconds) | both | `300` | Fixed-interval scheduling — emits systemd `OnBootSec=`/`OnUnitActiveSec=`, launchd `StartInterval`. Used when neither `schedule` nor a resolvable `interval_from` is present. |
| `interval_from` | string, `"observability.<field>"` | `defaults.jobs` only | — | Reads `<field>` from the **merged** `observability` config at compose time, so a fleet's own `observability.pulse_interval` override propagates to the timer. **Inert on `host.jobs`**: `compose_host_timers` resolves schedules against an empty config (`_resolve_timer_schedule(cfg, {})`), so `interval_from` on a host job silently falls back to `interval`/300 rather than erroring. Today only `fleet-pulse` uses it. |
| `type` | string | both | `"oneshot"` | Passed through verbatim as the systemd `[Service] Type=`. Not validated against a known set — every shipped job uses `oneshot`. |
| `persistent` | bool | both | `false` | systemd `Persistent=true` — catches up a missed run after downtime. No launchd equivalent; plists silently ignore it. |
| `randomized_delay` | int (seconds) | both | `0` | systemd `RandomizedDelaySec=` — start jitter, used to desync jobs across multi-fleet hosts hitting one shared resource (e.g. `creds-check` against the Telegram API). No launchd equivalent. |
| `enroll` | bool | both | shape-dependent | Dormancy control. **The enforcement mechanism, and even the default polarity, differ by scope and shape** — see [Dormancy](#dormancy-enroll-semantics-differ-by-scope). Do not assume it behaves the same everywhere in this file. |
| `unit` | string | `host.jobs` only | — | The only recognized value is `"service"` — see below. Declaring it under `defaults.jobs` has no effect; `compose_fleet_timers` never reads it. |

### Scheduling caveat: launchd only sees the first `HH:MM`

`schedule:`'s systemd `OnCalendar` dialect is translated to launchd's
`StartCalendarInterval` by a single regex, `re.search(r"(\d{1,2}):(\d{2})",
expression)`, that pulls the first `HH:MM`-shaped substring anywhere in the
string, plus (separately) a single leading weekday name if present
(`composer.py`, the `_write_timer_units` plist branch). Two consequences worth
knowing before you write a `schedule:`:

- A weekday **range or list** (`Mon..Fri`) has no launchd equivalent and falls
  back to firing daily — only a single leading weekday (`Sun *-*-* …`) is
  mapped.
- A **wildcard-hour** expression degrades silently. `host-health-check`'s
  schedule is `"*-*-* *:00:00"` (hourly, deliberately — the job exists to
  catch a storage stall that "can wedge the host within the hour," per its
  own comment in `system.yaml`). The regex has no concept of the hour field
  being a wildcard; it matches the first `HH:MM`-shaped substring it finds,
  which in this string is the **minute:second** pair `00:00`. The resulting
  plist fires once a day at midnight, not hourly. This is the general
  pattern `orphan-browser-reaper.sh`'s own schedule comment in `system.yaml`
  warns about ("a multi-hour OnCalendar would silently run once a day on
  macOS while running four times on Linux") — `host-health-check` is the one
  job that currently has a wildcard-hour schedule and so is the one this
  actually bites, on macOS hosts, as implemented. Prefer `interval:` (which
  maps cleanly to `StartInterval` on both platforms) for anything that needs
  to fire more often than daily.

## `host.jobs` — the current roster

| Job | Schedule | `persistent` | `randomized_delay` | `enroll` |
|---|---|---|---|---|
| `claude-update` | `*-*-* 04:00:00` | true | 600s | *(absent — enrolled)* |
| `notify-behind` | `*-*-* 08:00:00` | true | 600s | *(absent — enrolled)* |
| `update-siblings` | `Sun *-*-* 04:30:00` | true | 300s | `false` (see flag below) |
| `plane-daemon` | — (`unit: service`) | — | — | `false` |
| `disk-monitor` | `*-*-* 05:00:00` | true | 600s | *(absent — enrolled)* |
| `fleet-memory-check` | `*-*-* 05:30:00` | true | 600s | *(absent — enrolled)* |
| `orphan-browser-reaper` | `*-*-* 05:45:00` | true | 600s | *(absent — enrolled)* |
| `host-health-check` | `*-*-* *:00:00` (hourly) | true | 300s | *(absent — enrolled)* |

What each script does is documented in the root `CLAUDE.md`'s "Key lifecycle
scripts" table — this doc covers the config shape, not the script bodies.

### `unit: service` — resident host services

A job with `unit: service` is a long-lived, supervision-restarted process
(the observable plane's ingest daemon, `plane-daemon`, is the only tenant
today) rather than a scheduled timer. Only `script` and `enroll` apply — the
scheduling fields (`schedule`, `interval`, `interval_from`, `type`,
`persistent`, `randomized_delay`) are not read by the service emitter
(`_write_service_units`). Composed as systemd `Type=simple` +
`Restart=always` + `RestartSec=5` with **no `.timer` unit at all**; launchd
gets `RunAtLoad` + `KeepAlive`. On Linux it is enrolled through a dedicated
installer, `lib/install-host-service-systemd.sh`, distinct from the generic
timer enroller — `setup-system` tells the two apart by whether a `.timer`
sibling exists next to the `.service` file. On macOS there is no separate
leg: launchd's plist glob enrolls a service plist the same way it enrolls a
timer plist.

**Arm/disarm is asymmetric, and the source comment states the recipe
precisely — worth quoting rather than paraphrasing.** Arming
(`enroll: true`, then `generate` + `setup-system`) is one clean cycle. To
disarm: flip `enroll` back to `false` and re-run `generate` — this **prunes**
the composed unit files, so no future setup run can re-enroll it — then
**stop the already-installed unit yourself**, since pruning composed files on
disk cannot reach a unit that is already loaded into systemd/launchd:

```
linux:  systemctl --user disable --now claudlobby-plane-daemon.service
macos:  launchctl bootout gui/$UID/claudlobby-plane-daemon
```

## Dormancy: `enroll` semantics differ by scope

This is the field most worth getting right before touching it, because the
enforcement mechanism — and even which value counts as the default — is
**not** the same across the three places `enroll` appears:

| Scope / shape | Default when `enroll` is absent | Where the dormancy is enforced | Mechanism |
|---|---|---|---|
| Fleet job (`defaults.jobs`, e.g. `weekly-worker-restart`) | enrolled (`enroll` defaults to `True`) | compose-time listing **and** enroll-time skip | The unit files ARE written; the job's basename is additionally added to a `DORMANT` manifest sidecar in the fleet's `runtime/fleet/timers/`. `lib/setup-fleet` and `reconcile-fleet.sh`'s job-drift audit both call the shared `unit_is_dormant()` helper (`lib-common.sh`) against that manifest and skip enrolling/flagging anything listed in it. A fleet opts a dormant job in with `defaults: { jobs: { <name>: { enroll: true } } }` in its own `fleet.yaml` — see [`fleet-yaml-schema.md`'s `fleet.defaults.jobs.<name>.enroll`](fleet-yaml-schema.md#fleetdefaultsjobsnameenroll). |
| Host service (`host.jobs`, `unit: service`, e.g. `plane-daemon`) | **dormant** (`cfg.get("enroll") is True` — a strict identity check, so absence or any non-`True` value is dormant) | compose-time only | If `enroll` is not exactly `true`, **zero files are written** — there is nothing for `setup-system` to find, let alone enroll. Note the default direction is the *opposite* of a plain timer job: a service is dormant unless explicitly armed; a timer is enrolled unless explicitly parked. |
| Host timer (`host.jobs`, no `unit: service`, e.g. `update-siblings`) | enrolled (no gate reads `enroll` at all) | **not enforced by code today** | See below. |

### The gap: `update-siblings`'s `enroll: false` is not enforced

**Tracked as [#1385](https://github.com/Claudfather/Claudlobby/issues/1385).**

`update-siblings` is, as of this writing, the only `host.jobs` entry that is
a plain timer (not `unit: service`) and also carries `enroll: false`. Tracing
both sides of the enrollment path shows that flag currently has **no code
effect**:

- **Compose side.** `compose_host_timers` (`composer.py`) only reads `enroll`
  inside the `cfg.get("unit") == "service"` branch. For every other job —
  the branch `update-siblings` falls into — `_write_timer_units` is called
  unconditionally, with no `enroll` check at all. Its `.timer`/`.service`
  files are written every `generate`, armed or not.
- **Enroll side.** `lib/setup-system`'s `phase_host_jobs` globs
  `claudlobby-*.timer` (Linux) / `claudlobby-*.plist` (macOS) in the composed
  host-timers directory and enrolls **every file it finds**, unconditionally.
  It never calls `unit_is_dormant()` (that helper has exactly two callers,
  both in fleet-scoped scripts — `lib/setup-fleet` and
  `lib/reconcile-fleet.sh`) and there is no host-level `DORMANT` manifest for
  it to check in the first place — `compose_host_timers` never writes one.

So a plain `lib/setup-system` run enrolls `update-siblings` regardless of
`enroll: false`. The inline comment beside the job in `system.yaml` — and the
`update-siblings.sh` row in root `CLAUDE.md`'s lifecycle-scripts table —
both describe arming it from a fleet with
`defaults: { jobs: { update-siblings: { enroll: true } } }`. That recipe is
the mechanism for a **fleet-level** dormant job (the `weekly-worker-restart`
row in the table above); it cannot reach a **host** job. `load_host_jobs()`
(`config.py`) reads only `system.yaml`'s `host:` section, independent of any
`FleetConfig` — its own docstring states the property directly: host jobs
"deliberately bypass the fleet defaults merge." `plane-daemon`'s comment,
three weeks newer (added 2026-08-26 vs. `update-siblings`'s 2026-08-05),
states the correct version for a host job: arm it "in that host's own
system.yaml (host jobs deliberately bypass the fleet defaults merge, so
fleet.yaml cannot arm this one)." It's a plausible read of the history that
compose-time dormancy enforcement was built for the `unit: service` shape
`plane-daemon` introduced and was never retrofitted onto the plain-timer
branch `update-siblings` had already been using — but regardless of how it
got this way, **as implemented, a host running `lib/setup-system` today will
enroll `update-siblings` even with `enroll: false` in `system.yaml`.** Until
the timer branch gains the same compose-time gate `unit: service` has, the
only reliable way to keep it dormant is operator discipline: don't enroll it
(skip that unit when running the installer, or disable it manually after),
not the `enroll:` flag.

## `defaults.hooks`

Same entry shape as `fleet.yaml`'s `bots.<name>.hooks` / `defaults.hooks` —
see [`fleet-yaml-schema.md`'s `bots.<name>.hooks`](fleet-yaml-schema.md#botsnamehooks)
for the field table (`command`, `matcher`, `type`, `timeout`, `async`); this
file doesn't add or change any of them, it just supplies the platform-wide
base layer. Merge is `_merge_hooks_dedup` (`config.py`) at every layer —
system, then fleet `defaults.hooks`, then bot-level — identity is
`(command, matcher)`, later layers win a collision, order is otherwise
preserved. Currently ships:

| Event | Command | Notes |
|---|---|---|
| `PreToolUse` | `lib/bot-vitals.sh` | always active |
| `PreToolUse` | `lib/gh-mention-guard.sh` | always active — rewrites `@handle` out of GitHub-bound tool calls (#1019) |
| `PostToolUse` | `lib/bot-vitals.sh` | always active |
| `SessionEnd` | `lib/transcript-digest.sh` | composed on every bot; **self-gated** on `SESSION_DIGEST_ENABLED=1` in the fleet's own `env:` — see [Two dormancy patterns](#two-dormancy-patterns-composed-vs-enrolled) below |
| `SessionStart` | `lib/plane-session-start.sh` | composed on every bot; **self-gated** on `PLANE_EMIT_ENABLED=1` |

A fleet disables the whole category with `system_defaults: { hooks: false }`
in its own `fleet.yaml`, or overrides/extends individual events by declaring
`defaults.hooks` itself (its version wins the dedup).

## `defaults.observability`

Same 4 fields as `fleet.yaml`'s `bots.<name>.observability` — see
[`fleet-yaml-schema.md`'s `bots.<name>.observability`](fleet-yaml-schema.md#botsnameobservability)
for the emitted env vars and per-field validation. This file supplies the
platform floor; a fleet's own `defaults.observability` (or a bot's own
`observability:`) overrides per-field via a shallow merge — declaring one
field doesn't reset the others. Current floor values: `pulse_interval: 300`,
`activity_stuck_threshold: 1800`, `dispatch_deadline: 1800`.

**`dispatch_deadline` in particular carries a long, load-bearing comment in
the source** recording a specific measured trade (a bimodal completion-time
distribution across 90 tracked dispatches, and why raising the default would
trade false alerts for slower detection of a genuinely wedged bot) — read
`claudlobby/system.yaml` itself before changing that one number; the
rationale is deliberately kept at the number rather than in an issue thread.

## `defaults.jobs`

Same entry shape as `host.jobs.<name>` (see [Job entry fields](#job-entry-fields)
above), minus `unit: service`. These compose into **fleet-level** systemd/launchd
units — one set per fleet, not one per host. Current roster:

| Job | Schedule | `enroll` |
|---|---|---|
| `fleet-pulse` | `interval_from: observability.pulse_interval` | *(absent — enrolled)* |
| `keepalive` | `interval: 60` | *(absent — enrolled)* |
| `log-rotation` | `interval: 86400` | *(absent — enrolled)* |
| `creds-check` | `*-*-* 06:00:00` (+600s jitter) | *(absent — enrolled)* |
| `reload-fleet` | `*-*-* 03:30:00` | *(absent — enrolled)* |
| `weekly-worker-restart` | `Sun *-*-* 05:00:00` | `false` (enforced — see [Dormancy](#dormancy-enroll-semantics-differ-by-scope)) |
| `data-sweep` | `Sat *-*-* 07:00:00` (script carries `--purge`) | *(absent — enrolled)* |

Merge (`_merge_system_into_defaults`'s `jobs` branch, `config.py`) is
**by job name**, with **field-level shallow spread within a job**: a fleet's
`defaults.jobs.weekly-worker-restart: { enroll: true }` overlays just that
one field onto the system entry rather than replacing the whole job — the
fleet doesn't have to repeat `script`/`schedule`/`type` to opt in. Sibling
jobs the fleet doesn't mention are carried through untouched. The whole
category is gated on `fleet.system_defaults.timers` (and `.enabled`) — see
[`fleet-yaml-schema.md`'s `fleet.system_defaults`](fleet-yaml-schema.md#fleetsystem_defaults)
for the opt-out shape, and its
[`fleet.defaults.jobs.<name>.enroll`](fleet-yaml-schema.md#fleetdefaultsjobsnameenroll)
section for the per-job opt-in recipe — both documented from the fleet.yaml
side already; this doc is the system.yaml side of the same merge.

## `fleet.system_defaults` — what it gates, from this side

`fleet.yaml`'s `system_defaults:` field (full shape documented in
[`fleet-yaml-schema.md`](fleet-yaml-schema.md#fleetsystem_defaults)) reads six
booleans off `SystemDefaultsConfig`. Only three of them gate content that
actually lives in *this* file:

| `system_defaults` key | Gates | Declared in `system.yaml`? |
|---|---|---|
| `enabled` | master switch — `false` disables all five categories below | n/a |
| `hooks` | `defaults.hooks` | yes |
| `observability` | `defaults.observability` | yes |
| `timers` | `defaults.jobs` | yes |
| `guardrails` | `DEFAULT_GUARDRAILS` — currently `claudlobby-dev-in-projects` | **no** |
| `protocols` | the INSTRUCT-tier protocol default(s) — currently the `shared-documentation` / `shared-documentation-vault` pair | **no** |

The last two don't correspond to a `guardrails:`/`protocols:` key in this
file — there isn't one, at either the `host:` or `defaults:` level — because
they don't gate *content declared here*. They gate a separate,
compositor-internal default registry, `claudlobby/defaults.py`'s
`REGISTRY`, which is Python code (with its own tier system — RESTRICT / INSTRUCT
/ WIRE — and its own admission bar per tier), not YAML. If you're looking for
where a new estate-wide default guardrail or protocol gets declared, it's
that registry, not this file.

## Composition & enrollment

Output locations:

- **Host jobs** compose to `runtime/_host/timers/` under the **install
  root** (`paths.root`) — never under a fleet directory, since host jobs are
  host-global. `claudlobby generate` composes this directory **on every run,
  for any fleet** (`compose_host_timers(paths)` is called unconditionally
  from `cmd_generate`, right after the fleet-level timers, with a comment
  explicitly noting host jobs are "platform equipment, not fleet config"),
  and the fleet-independent `claudlobby host-timers` subcommand composes the
  same thing without needing a `fleet.yaml` at all — useful on a cold host
  before any fleet has been set up.
- **Fleet jobs** (`defaults.jobs`) compose to `<fleet-runtime>/fleet/timers/`
  — `local/<fleet>/runtime/fleet/timers/` in overlay mode, `runtime/fleet/timers/`
  in root mode — alongside a `DORMANT` manifest and, independently, a
  `BRIEFING_EXPECTED` manifest for the unrelated per-(bot,slot) briefing
  timer family.

Enrollment is a **separate step** from composition in both cases —
`generate` never touches systemd/launchd directly:

- `lib/setup-system` enrolls the `host:` tier — once per host, not per fleet.
  Its `phase_host_jobs` phase first runs `claudlobby host-timers` to
  (re-)compose, then enrolls through the same generic installer scripts
  fleet timers use (`install_fleet_timer.sh` / `install_fleet_timer_launchd.sh`,
  pointed at the host timers dir via `TIMER_DIR`/`UNIT_NAME` overrides), plus
  the service-specific `install-host-service-systemd.sh` for a `.service`
  with no `.timer` sibling.
- `lib/setup-fleet` enrolls the `defaults.jobs` tier — per fleet. It skips
  anything the `DORMANT` manifest lists (`unit_is_dormant()`), and
  `reconcile-fleet.sh`'s job-drift audit applies the same skip so a
  composed-but-dormant job never reads as "missing" in a health check.

## Changing a running fleet or host

- **A `host:` edit** means hand-editing `claudlobby/system.yaml` in the
  shared install — there's no per-host overlay to edit instead. It reaches a
  host in two steps: `claudlobby generate` (any fleet) or
  `claudlobby host-timers` recomposes `runtime/_host/timers/`, then
  `lib/setup-system` re-enrolls. Because the install is shared, the change is
  **host-wide**: every fleet running out of that install picks it up, not
  just the one whose `generate` happened to trigger the recompose.
- **A `defaults:` edit** (hooks/observability/jobs) flows through the normal
  per-fleet `generate` cycle like any other system-tier-sourced default, and
  is subject to the same carrier-dependent canary-window rules as everything
  else the compositor writes — see the root `CLAUDE.md`'s "Changing a running
  fleet" paragraph and [`fleet-update-lifecycle.md`](fleet-update-lifecycle.md)
  for what that means for a hook script vs. a composed `bot.conf` value vs. a
  timer unit specifically (a changed timer unit additionally needs
  `lib/setup-fleet` to re-enroll it — systemd/launchd don't pick up an edited
  unit file on their own).

## Two dormancy patterns: composed vs. enrolled

This file participates in two structurally different dormancy mechanisms
used throughout the codebase, and it's worth keeping them distinct:

1. **Env-var self-gate inside an always-composed artifact.** A hook script
   is composed onto *every* bot unconditionally (cheap, harmless on its own)
   but checks an env var at runtime and no-ops unless armed. Both dormant
   hooks in `defaults.hooks` above (`transcript-digest.sh` /
   `plane-session-start.sh`) work this way — `SESSION_DIGEST_ENABLED=1` and
   `PLANE_EMIT_ENABLED=1` are armed **in a fleet's own `fleet.yaml` `env:`
   block**, not anywhere in `system.yaml`. This file's only role is
   composing the always-present hook entry; the arming lever lives entirely
   downstream, per fleet.
2. **`enroll:` gates whether the scheduling artifact exists or gets enrolled
   at all** — a property of the job/service entry itself, not runtime
   behavior inside an always-present script. This is the mechanism the
   [Dormancy](#dormancy-enroll-semantics-differ-by-scope) table above covers,
   and — as that section documents — it is enforced differently for a fleet
   job, a host service, and a host timer. Don't assume an `enroll: false`
   you see in `system.yaml` behaves like the other two just because the key
   name is the same.

## Validation & visibility

- **`claudlobby validate`** has exactly one gate that touches
  `system.yaml`-sourced content: `_validate_timers` (`validator.py`)
  re-checks the **merged** `fleet.defaults.jobs.*.script` values for
  un-anchored absolute paths — the same L1 source guard `generate` itself
  enforces — gated on `fleet.system_defaults.enabled`/`.timers` so it never
  flags a job `generate` wouldn't emit. It covers `defaults.jobs` only. There
  is **no validation of `host.jobs` at all** — host jobs bypass `fleet.yaml`
  (and therefore `validate`) entirely, by the same `load_host_jobs()`
  property that makes them un-armable from a fleet.
- **`claudlobby doctor`** has **zero system-defaults awareness** as of this
  writing (`grep -c "system.default" claudlobby/doctor.py` returns 0). A
  2026-06-09 plan proposed four checks — `system-defaults-loaded`,
  `fleet-timers-installed`, `system-defaults-overrides`,
  `system-defaults-disabled` — none of which were built; see
  [`plans/2026-06-09-system-defaults-tier.md`](plans/2026-06-09-system-defaults-tier.md)
  for the full history and its own re-audit notes.
- **`claudlobby diff`** *does* cover fleet-level drift: `diff_fleet_timers`
  compares a fleet's composed `defaults.jobs` units against what `generate`
  would produce today. There's no equivalent for `host.jobs` — host timer
  drift isn't part of any fleet's diff.

## History

The file used to be `claudlobby/system_defaults.yaml`, and what is now
`defaults.jobs` used to be a bare top-level `fleet_timers:` key — the
enclosing `defaults:` wrapper didn't exist yet either; the original schema
was just `hooks:` / `observability:` / `fleet_timers:` at the top level, no
`host:` tier at all. `_resolve_system_yaml` in `config.py` raises loudly if a
stale `system_defaults.yaml` is ever found on disk, rather than silently
ignoring it. The `host:` tier, the `defaults:` wrapper, and the
compose-time-dormant `unit: service` shape were all added later, as one
restructuring; the original design predates all three. Full narrative, decision forks, and a
line-by-line "shipped vs. never shipped" re-audit dated 2026-08-28:
[`plans/2026-06-09-system-defaults-tier.md`](plans/2026-06-09-system-defaults-tier.md).

## See also

- [`fleet-yaml-schema.md`](fleet-yaml-schema.md) — the second config tier
  (WHO the bots are), including `fleet.system_defaults` (the opt-out gate)
  and `fleet.defaults.jobs.<name>.enroll` (the fleet-side opt-in recipe this
  doc's `defaults.jobs` section points back to).
- [`projects-yaml-schema.md`](projects-yaml-schema.md) — the third config
  tier (WHAT the work is).
- [`fleet-update-lifecycle.md`](fleet-update-lifecycle.md) — carrier-by-carrier
  rules for whether and when a composed change reaches an already-running bot.
- [`environment-variables.md`](environment-variables.md) — the env vars this
  file's `defaults.observability` and dormant hooks emit/consume.
- [`plans/2026-06-09-system-defaults-tier.md`](plans/2026-06-09-system-defaults-tier.md)
  — design history, decision forks, and the current ship-status audit.
