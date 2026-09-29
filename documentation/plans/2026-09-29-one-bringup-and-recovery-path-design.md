---
title: One path for fleet bring-up and recovery, and the host lease every restarter takes
type: spec
status: draft
date: 2026-09-29
issue: "#1964"
builds-on: documentation/plans/2026-09-20-boot-admission-and-supervision-consolidation-design.md
---

# One path for fleet bring-up and recovery

## Summary

On the Sep 28 macOS reboot (#1964), one root cause set off about 15 alerts. Five mechanisms restarted bots, up to four of them the same bot within a minute, and none of them owned the outcome.

This design gives each part of the work one owner:
- **One thing brings a bot up:** `start-bot.sh`, behind the Sep 20 admission gate.
- **One host lease decides who may replace a running session:** the boot's owner during a boot, keepalive one heal at a time afterwards, and one door for deliberate restarts.
- **Only the lease holder reports**, as one episode per root cause. The Sep 28 boot becomes one message instead of about 15.

Nothing here is built. Forks F1–F6 are Chris's to decide.

## Where main is (`85e66d6`)

- **Shipped:** PR A of #1573 (#1684). It put the `BOOT_*` policy and `MCP_TIMEOUT` in `bot.conf`, made the readiness ceiling wall-clock, limited plugin updates to once per boot, and added `lib/supervisor.sh`'s five verbs.
- **Not built:** PR B ([plan](2026-09-20-boot-admission-pr-b-the-gate.md)), which is the gate, the `.boot-queued` marker and keepalive's first rung.
  - Nothing in `lib/` reads `BOOT_ADMISSION_*` yet.
  - `start-bot.sh:25-45` still takes the 8 s `$TMPDIR` lock that was abandoned at 120 s on Sep 28 (#1646).

## 1. The path

### 1.1 Three jobs, one owner each

| job | owner | today |
|---|---|---|
| **Do:** bring a bot up | `start-bot.sh`, behind the admission gate (PR B) | `start-bot.sh`, behind an 8 s lock that is abandoned at 120 s |
| **Decide:** replace a running session, or act on a health signal | the host lease holder | five actors that do not know about each other |
| **Report:** tell a human | the lease holder, one episode per cause | five emitters for one fact (§4) |

**The rule:**
- Starting a stopped bot needs only the gate.
- Replacing or stopping a running session, or acting on a health signal, needs the host lease.
- A bot that is mid-start (a fresh `.boot-queued`, PR B) does not count as stopped.

### 1.2 The host lease

| property | design |
|---|---|
| where | `$CLAUDLOBBY_ROOT/state/boot/lease/`, beside the gate's `tickets/` and `slots/`. It is host-wide by construction (Sep 20 design §6.2). |
| mechanism | The gate's own primitive: an atomic `mkdir` that records purpose, pid, bots in scope, taken-at, deadline and a token, and is reaped when its pid dies. `with_lock` is the wrong tool: it guards critical sections of a few seconds, with a 30 s budget. |
| purposes | `bringup` (the boot's owner), `heal` (keepalive), `restart` (the deliberate-restart door) |
| deadline | Declared when the lease is taken, and never open-ended:<br>• bring-up: `BOOT_ADMISSION_WAIT_MAX_S + RC_READY_TIMEOUT_S`<br>• a heal: `RC_READY_TIMEOUT_S` plus a margin<br>• a restart run: its per-bot ceiling × the number of bots; a calendar run is also capped by its `run-bounded` budget (#1982) |
| stale | A dead pid or a passed deadline. The next taker reclaims the lease and records one `lease_reclaimed` event naming the lost holder. A lost `bringup` holder also opens the bring-up episode (§4), so a dead owner is never silent. |
| re-entry | The holder's children present the token (`CLAUDLOBBY_LEASE`). `start-bot.sh`, which the supervisor runs rather than the holder, reads the lease's scope instead. |
| held, and keepalive wants it | keepalive stands down for that tick: no restart, no page. It logs `SKIP — host lease held by <purpose> (<pid>)`. |
| held, and a door run wants it | The run waits, bounded, and names the holder. Past the bound it fails, with one alert. |
| granularity | One lease per host, so one decision at a time. A holder may act on up to `BOOT_ADMISSION_SLOTS` bots per hold, and the gate still bounds how many start at once (F2). |

### 1.3 Bring-up: a boot

1. The supervisors start every launcher at load, as today. Each `start-bot.sh` queues in the gate (PR B): managers first, `BOOT_ADMISSION_SLOTS` at a time, with `.boot-queued` visible to keepalive on both OSes.
2. The bring-up owner (F1) takes the lease as `bringup` for this boot (`resolve_boot_epoch`) and reaps stale tickets and slots. It reports the platform floors (#917, #1963) in one line, rather than as an error per bot.
3. Each bot gets **one health check**: the verdict its readiness poll already computes, either `READY` or `TIMEOUT <last_state>` with `auth_cache_armed` (`start-bot.sh:299-433`). The verdict is an event, not an alert.
4. When every declared bot (`declared_bots_strict`) has a verdict, or at the deadline, the owner emits one `bringup_result` (for example, `17/17 ready in 212 s`) and releases the lease. If any bot is dark, that result is an alert naming the bots grouped by cause. It opens an episode that the recovery actor then owns (§4).

### 1.4 Recovery: after bring-up

keepalive is the one recovery actor. It still ticks per fleet, with four changes:

1. **Lease first.** It takes the lease as `heal` before any restart or heal, and stands down while another holder has it.
2. **Classify before acting.** A cause is host-wide when the auth cache is armed, or when two or more bots go dark with the same `last_state` in one window. A host-wide cause gets one episode and no burst of restarts:
   - its remedy runs once, at the next start (today that is #1969's clear at every spawn), and then bots are healed one hold at a time;
   - a host-wide cause with no remedy is not bounced at all, because bouncing into one is what amplified #1573.
3. **A per-host rate limit.** A host heal budget per time window, on top of the per-bot `BRIDGE_HEAL_MAX_ATTEMPTS`. When either runs out, the episode escalates once and bouncing stops.
4. **Inputs, not alerts.** `rc_timeout`, the bring-up verdict and fleet-pulse's bridge state all feed the actor. fleet-pulse stops acting and stops paging on bot health. It pages only the one thing it alone can see: the actor itself has gone silent (no keepalive heartbeat for N ticks).

`OBSERVABILITY_BRIDGE_HEAL` stays opt-in by default, because a heal bounces a live session. With it off, the actor still opens and closes episodes but does not bounce.

### 1.5 Deliberate restarts

- **One door: `rolling-restart.sh`.** It already takes a fleet or `--all`, `--workers-only`, `--managers-only` and `--skip-healthy`. It gains:
  - `--bot <name>`;
  - the safe-worker-restart busy check (#1648);
  - the lease, with purpose `restart`.

  It still restarts through `spin-up-bot.sh` (on the adapter after PR B), then `start-bot.sh` and the gate, and waits for each bot's verdict.
- **Who uses it:** managers (for context-degraded restarts), the weekly job, and operators. `spin-up-bot.sh` run by hand on a live bot takes the lease itself.
- **`start-bot.sh` becomes idempotent for a live session.** It replaces a live session only when the lease names that bot; otherwise it exits 0 with one log line. That stops a duplicate launch (a supervisor relaunch, or a racing kickstart) from becoming a sixth restarter.

## 2. What it replaces

These are the five mechanisms that restarted bots on Sep 28, followed by the jobs beside them. Line numbers are at main `85e66d6`.

| # | mechanism | fate |
|---|---|---|
| 1 | **launchd/systemd units:** start every launcher at load, and relaunch a failed launcher (`KeepAlive SuccessfulExit=false` / `Restart=on-failure`) | **Keep, for exactly those two jobs (F3).** A relaunch queues in the gate, and once `start-bot.sh` is idempotent it cannot replace a live session. Every other kickstart or restart goes behind `svc_kick` (the #1573 ratchet). |
| 2 | **`start-bot.sh`:** the `$TMPDIR` lock (`:25-45`); it kills the prior session unconditionally (`:138`); it pages twice, with `rc_timeout` (`:433`) and with `bridge_down` via `bridge_bringup_verify` (`:531-558`, `lib-common.sh:1481-1511`) | **Keep, as the only thing that brings a bot up.** Delete the lock: its release runs in the background, and that release is what was lost at 18:05 on Sep 28. `:138` replaces a live session only under the lease. The two pages become one verdict event. |
| 3 | **keepalive:** an inline restart ladder (`keepalive.sh:189-212`); kickstarts mid-boot, because `service_is_starting` returns 1 off Linux (`lib-common.sh:4699`, called at `keepalive.sh:338`, #1593); a bridge-heal ladder with its own escalation (`:223-277`) | **Keep, as the one recovery actor (§1.4).** It gains the lease, cause classification, a host budget, and PR B's `svc_kick` and `.boot-queued` rung. Its escalation becomes the episode's. |
| 4 | **`rolling-restart.sh`:** unaware of the other actors, and no busy gate (`:169`, #1648) | **Keep, as the one deliberate-restart door (§1.5).** |
| 5 | **manager restarts:** by judgement, on a FLEET ALERT. Library skills and protocols name `spin-up-bot.sh` 17 times, and raw `systemctl` and `launchctl` once each. | **Delete as a mechanism.** Managers use the door for work reasons, never for health (F4). |
| + | **`weekly-worker-restart.sh`:** its own pre-stop → spin-up → `BRIDGE_READY` loop, and its own `bridge_down` page (`:118`) | **Delete the loop.** The timer runs the door (`--all --workers-only`) under the lease (F6). |
| + | **setup-fleet's bot leg:** runs `spin-up-bot.sh` on each bot whose unit is missing or whose session is dead, and skips a live session (`setup-fleet:319-329`) | **Keep.** It starts only stopped bots, so it needs the gate but not the lease, once a mid-start bot counts as not stopped. |
| + | **`reload-fleet.sh`:** `/reload` at an idle tick, and `setup-fleet --jobs-only`, which skips the bot leg | **Keep unchanged.** It touches no live session. |
| + | **fleet-pulse:** pages `bridge_down` (`fleet-pulse.sh:481`), burst-pages `rc_timeout`, and its summary lists any critical event in a time window (`:712`, `:985-1003`) | **Stop paging on bot health.** Page when the actor goes silent. The summary shows open episodes instead (§4). |

**Deleted outright:**
- the `$TMPDIR` lock;
- weekly-worker-restart's restart loop;
- start-bot's two per-bot pages;
- fleet-pulse's `bridge_down` page;
- keepalive's separate heal escalation;
- the library's raw supervisor commands.

PR B already retires the systemd `ExecStartPre` stagger and `boot_rung_for`.

## 3. The fixes in flight

| PR | what it does | where it sits |
|---|---|---|
| #1969 | Empties the host-global MCP needs-auth cache before each spawn and on each readiness tick, and records `auth_cache_cleared` | **The door's pre-spawn step**, so every bring-up gets it, at boot or in a heal. Its record is how the classifier recognises a host-wide cause. Its extra clear at TIMEOUT tells an entry recorded after the last tick (a restart heals it) from one that cannot be removed (escalate, do not bounce). The owner does not clear the cache a second time, so there is one copy of the remedy. |
| #1975 | The session digest's `claude -p` loads no MCP server, plugin or hook | It removes the steady-state writer that armed the cache at every session end on a host running the digest, so fewer episodes open in the first place. The path itself does not change. |
| #1978 | `with_timeout` bounds its command on a Mac with no `timeout` or `gtimeout` | **A prerequisite.** A lease is only safe if every holder is bounded, and on the macOS host every bound was a no-op. |
| #1982 | launchd timer jobs capture their output and stop at a budget | **A prerequisite for calendar holders.** The weekly run's budget caps its hold, and its log shows a stall. |

#1963 (the Linux-only calls, Chris's MUST) is a separate fix. The bring-up owner only reports the platform floors.

## 4. Alert collapse

**An alert belongs to an episode, and an episode belongs to the lease holder acting on it.** An episode is two plane records under one id:
- `recovery_episode_opened {cause, bots, opened_by}`;
- `recovery_episode_closed {resolution: recovered | escalated | expired}`.

Everything between the two (verdicts, heals, skips) is an event, never a page.

- **One cause, one episode.** A bot with the same cause joins the episode already open, so a host-wide cause pages once however many bots it hits.
- **Pages follow episode state:** one when an episode opens (after its grace period) and one when it closes. The close names what recovered.
- **A stale `bridge_down` cannot outlive a `READY`.** #1964's 18:15 summary listed an 18:04:48 event after an 18:05:19 recovery, because today's summary lists events in a time window rather than current state.
- **A clean bring-up pages nobody.** `17/17 ready` is a notice.
- **Managers get the same single message as Telegram**, and no per-bot health FLEET ALERTs.

The Sep 28 boot, replayed:

| signal | Sep 28 | this design |
|---|---|---|
| per-bot `bridge_down` FLEET ALERT at bring-up | 11 | 0 (verdict events) |
| fleet-pulse `bridge_down` | 1 | 0 |
| keepalive bridge-heal bounces | 3 | 0 under the lease; afterwards, one heal at a time inside one episode |
| mid-boot kickstarts | 17 | 0 (`.boot-queued` and the lease) |
| one bot restarted by two actors in a minute | yes, by up to four layers | impossible: there is one lease |
| pages to a human | ~15 | 1 bring-up result and 1 close, or 1 notice if all 17 come up (#1969 clears Sep 28's cause at every spawn) |

The ~120 false `script_error`s belong to #1963 and are outside this count.

## 5. Decision forks

Chris decides all six. All six are open.

### F1: Who owns bring-up (your decision 3)

- **(a)** A **host bring-up job**: one `host.jobs` unit, started at load through the same plumbing `plane-daemon` uses, run once per boot. It takes the lease, runs pre-flight, watches the gate drain and emits one result. It starts no bots.
- **(b)** The same job, but it **also starts the bots**. The units stop starting at load.
- **(c)** **No new unit.** The first `start-bot.sh` of a boot takes the lease and forks a detached watcher.
- **(d)** **No owner.** The gate alone, with per-bot alerts deduplicated by fleet-pulse.

**Lean: (a).** An owner of the outcome is the one thing the gate cannot provide. Under (a), a dead job means today's behaviour plus one "owner lost" page, never zero bots.
- (b) puts one more unit in front of every boot, and changes both unit renderers.
- (c) hangs the outcome on one bot's launcher and a detached release, the same shape as the release lost at 18:05 on Sep 28.
- (d) keeps about 11 alerts.

### F2: Lease granularity

- **(a)** **One host lease**, acting on up to `BOOT_ADMISSION_SLOTS` bots per hold.
- **(b)** **Per-bot leases**, with the gate bounding how many run at once.

**Lean: (a).** The failures that need coordination are host-wide (the auth cache, contention during MCP startup). Per-bot leases would let two fleets' keepalives heal into the same cause in parallel. The cost is that recovery is serial, bounded by slots per hold.

### F3: The supervisors' own relaunch

- **(a)** **Keep** relaunch-on-failure; `start-bot.sh` becomes idempotent for a live session.
- **(b)** **Remove** it; the actor starts crashed launchers on its next tick.

**Lean: (a).** A relaunch is the tail end of a failed start, not a recovery decision. Removing it costs up to one tick of downtime per crash and a renderer change on both OSes, and gains no coordination.

### F4: Managers

- **(a)** **The door only.** Managers restart for work reasons through `rolling-restart.sh --bot`; health belongs to the actor.
- **(b)** **Never.** Managers escalate to a human.
- **(c)** Status quo.

**Lean: (a).** A context-degraded restart is a manager's job under the context-management protocol, and (b) would hand it to a human.

### F5: Pages during bring-up

- **(a)** **Events only while the `bringup` lease is held**, then one result.
- **(b)** Per-bot pages continue, grouped by fleet-pulse each sweep.

**Lean: (a).** (b) keeps two alerting layers, and still pages every five minutes per fleet.

### F6: The weekly worker restart

- **(a)** **Keep the timer and delete its loop;** it runs the door under the lease.
- **(b)** **Delete the job;** staged binaries (#1768) take effect at natural restarts.

**Lean: (a).** It removes the duplicated code without changing what the job does. (b) changes behaviour, and the job is opt-in anyway.

## 6. Out of scope

- **#1963 and #917** (the Linux-only calls; macOS tool floors) are separate fixes. Bring-up only reports them.
- **A per-bot `CLAUDE_CONFIG_DIR`**, #1962's isolation option, was not chosen.
- **The admission slot count, and a memory-aware gate.** `auto` slots were ratified on Sep 20. The proof reboot measures swap, and a follow-up takes it if needed.
- **The default for bridge-heal.** It stays opt-in.
- **How Claude Code starts or retries MCP servers.**
- **#933's boot-integrity verdicts and the self-start instrument (#1002).** They read these records and are not replaced.
- **Implementation.** This PR is the design only.

## Sequencing

| phase | contents | size | needs |
|---|---|---|---|
| 0 | #1969, #1975, #1978, #1982, #1963 | in flight | — |
| 1 | PR B of #1573 as planned, plus deleting the `$TMPDIR` lock | M | 0 |
| 2 | the lease; keepalive stands down under it; an idempotent `start-bot.sh`; the door (`--bot`, busy check, lease); the weekly job runs the door | M | 1 |
| 3 | the bring-up owner (F1) and `bringup_result` | M | 2 |
| 4 | episodes: the classifier, the host budget, pages only from episodes; fleet-pulse pages when the actor goes silent; the summary shows open episodes | L | 2, 3 |
| 5 | library: managers use the door, and the raw supervisor commands are removed | S | 2 |

The critical path is 0 → 1 → 2 → 3 → 4. Phase 5 can run alongside phase 3.

## Risks

| risk | mitigation |
|---|---|
| A holder hangs and holds the host. | Every hold has a deadline; #1978 makes bounds real on macOS; a stale reclaim is recorded as an event. |
| The bring-up owner dies mid-boot. | Bots still start at load through the gate, and the reclaim pages "owner lost, N bots unverified". |
| Recovery is slow when many bots are dark. | A host-wide cause is fixed once at spawn (#1969), and each hold covers up to `slots` bots. |
| Folding pages together hides a second, unrelated cause. | Episodes group by cause, not by time, so a different cause opens its own episode. |
| Two fleets' keepalives starve each other. | The lease is released between holds. A starved fleet shows as `SKIP` lines, and at the extreme as a silent actor. |

## Verification (the proof)

- **Unit tests per phase:**
  - taking the lease, standing down under it, and reclaiming it;
  - keepalive skipping while the lease is held;
  - `start-bot.sh` leaving a live session alone;
  - the door waiting for the lease;
  - opening and closing an episode;
  - the summary reading open episodes.
- **The harness:** PR B's scenario, with a second actor added.
- **Then Chris's planned reboot of the macOS host:**
  - [ ] At most 1 page per root cause for the boot (Sep 28: ~15), and 0 when every bot comes up.
  - [ ] No bot restarted by two actors within one lease window; keepalive's `SKIP — host lease held` lines appear instead.
  - [ ] A `bringup_result` recorded for the boot, with the per-bot verdicts it summarises.
  - [ ] Time from boot to the last `READY`, against Sep 28 (17:53:31 to 18:05:19).
  - [ ] Swap and load through the boot, for the slot question in §6.
