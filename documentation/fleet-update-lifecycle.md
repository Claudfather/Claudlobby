# Fleet update lifecycle

How a running fleet picks up new plugins, skills, and Claude Code binaries — **without losing a bot's working context.** North star: **no context loss**, not "no restart." Restarts are fine; losing context is the pain.

Two update classes, two mechanisms:

| Update type | Stays current (download) | Applied to a *running* bot | Restart? | Cadence |
|---|---|---|---|---|
| Composed skills + clauDNA marketplace plugin | `claude plugin update` + `claudlobby generate` (`lib/reload-fleet.sh`) | `/reload-plugins` + `/reload-skills` broadcast (live) | **No** | Daily, `03:30`, + on-demand |
| Claude Code binary | `npm install -g @anthropic-ai/claude-code@latest` (`lib/update-claude-code.sh`) | new `claude` process at next start | **Yes** (binary swap) | Downloaded daily at `04:00`; applied via natural restarts + a weekly worker-only restart, `Sun 05:00` |

The only update that costs a restart is the binary. That restart is made **rare** (weekly, workers only) and **lossless** (resume-on-every-start, below).

## Mechanism 1 — daily live reload (plugins + skills)

`lib/reload-fleet.sh`, timer job `reload-fleet` (`claudlobby/system.yaml`, `schedule: "*-*-* 03:30:00"`, `type: oneshot`), enrolled via `lib/install-reload-fleet-systemd.sh`.

1. Under a fleet-wide lock (`with_lock`), runs `claude plugin update` for each `FLEET_PLUGINS_REQUIRED` — refreshes the shared host plugin cache (`~/.claude/plugins/cache/`).
2. Runs `claudlobby generate` to completion — re-links composed skill symlinks.
3. Drops `data/.reload-pending` on every **running** bot. It does not send any keystroke itself.

Activation is consolidated in `lib/keepalive.sh`: on its next idle-classification tick (each watchdog pass), if `data/.reload-pending` exists, keepalive sends `/reload-plugins` then `/reload-skills` and clears the marker. This is a single, idle-gated activation path — a bot mid-task is never interrupted, and there's no separate broadcaster racing the idle check. Convergence lag is bounded by the keepalive tick interval (on the order of a minute), which is immaterial for a daily reload.

Runnable **on-demand** (not just on the timer) to push a release immediately — activation still lands at each bot's next idle keepalive tick.

**Applies to every running bot, managers included** — live reload is free and lossless, so there's no reason to exclude managers here (contrast Mechanism 2).

**Loud-failure contract:** a failed `claude plugin update` or `generate` aborts before any marker is dropped — no half-reload — and raises `emit_failure_alert` (fleet event + manager tmux nudge + Telegram escalation on critical failure).

## Mechanism 2 — weekly lossless worker restart (binary)

`lib/update-claude-code.sh` is **download-only**: it installs the latest `claude` binary daily (`claude-update` job, `04:00`) and does not restart any bot. A failed install raises the same `emit_failure_alert` primitive Mechanism 1 uses.

The binary cannot hot-reload, so it reaches a running bot only via restart. `lib/weekly-worker-restart.sh` (job `weekly-worker-restart`, `schedule: "Sun *-*-* 05:00:00"`) bounces every **worker** bot once a week to pick it up:

```
pre-stop-handoff.sh   (writes a session.md handoff, best-effort, never blocks)
  → spin-up-bot.sh    (cross-platform idempotent restart)
  → start-bot.sh       resumes from the handoff (age-gated) on the new session
```

**Managers are excluded** — identified by `MANAGER_TMUX == BOT_ID` (`bot_is_manager()` in `lib/lib-common.sh`) — because a manager's long-horizon orchestration context is the least summarizable. Managers still get Mechanism 1's daily reload; they just never get auto-restarted for the binary. They pick up a new binary on any natural restart or a deliberate human-initiated one. A worker's binary staleness is bounded to ≤1 week.

This job is **composed-but-dormant by default** (`enroll: false` in `claudlobby/system.yaml`) — bouncing workers is disruptive enough that a fleet opts in explicitly:

```yaml
fleet:
  defaults:
    jobs:
      weekly-worker-restart: { enroll: true }
```

**Loud-failure contract:** a worker that doesn't come back raises `emit_failure_alert`, same as Mechanism 1.

## Resume-on-every-start (the age gate)

Every bot start — intentional (restart skill, weekly bounce), crash (keepalive), or an operator's manual restart — is a potential context loss. `lib/start-bot.sh` closes that gap:

- Before `STARTUP_PROMPT`, it injects `/claudna:session-resume --auto` as the first keystroke, gated by `should_resume_session()` (`lib/lib-common.sh`).
- `should_resume_session` reads the handoff's `last_updated:` frontmatter field from `.claude/session.md` (falling back to file mtime for older artifacts) and compares its age against `RESUME_MAX_AGE_S` (env-overridable; default `86400` seconds = 24h).
- **Fresh** checkpoint (age < threshold) → resume fires, the bot picks up its last handoff.
- **Stale** checkpoint (age ≥ threshold) or none → resume is skipped and the bot clean-starts rather than replaying dead state (e.g. re-attempting an already-merged PR).

On the intentional-restart paths (`library/skills/restart`, `weekly-worker-restart.sh`), `lib/pre-stop-handoff.sh` writes a fresh handoff (`/claudna:session-handoff --auto`) before the restart, non-blocking (always exits 0, even on a 30s timeout) — so the restart never stalls on a slow or failed handoff. Crash-restarts (`keepalive.sh`) skip straight to resume-from-last-checkpoint, since there's no live session left to hand off from.

## Relationship to PR #399

PR #399 added `lib/update-claude-code.sh` with a daily **fleet-wide bounce** — every bot, managers included, restarted once a day whenever the binary changed. That bounce is the literal daily-reset context-loss pain this lifecycle removes. It is **retired**: `update-claude-code.sh` is now download-only. Its daily binary download **survives unchanged**. The bounce it used to perform is replaced by the weekly worker-only lossless restart (Mechanism 2) plus natural restarts — both now resume-on-start instead of cold-starting.

## Reference

| Script | Role |
|---|---|
| `lib/reload-fleet.sh` | Mechanism 1: plugin update + generate + mark reload-pending |
| `lib/install-reload-fleet-systemd.sh` | Enrolls the daily reload timer |
| `lib/update-claude-code.sh` | Daily binary download only (no restart) |
| `lib/weekly-worker-restart.sh` | Mechanism 2: weekly worker-only lossless restart |
| `lib/install-weekly-worker-restart-systemd.sh` | Enrolls the weekly restart timer (dormant by default) |
| `lib/keepalive.sh` | Consumes `data/.reload-pending` at each idle tick; also the crash-restart entrypoint |
| `lib/start-bot.sh` | Injects age-gated `/claudna:session-resume --auto` before `STARTUP_PROMPT` on every start |
| `lib/pre-stop-handoff.sh` | Best-effort, non-blocking handoff before an intentional restart |

Full design history, decision forks, and rationale: `documentation/plans/2026-06-14-fleet-skill-plugin-update-lifecycle.md`.
