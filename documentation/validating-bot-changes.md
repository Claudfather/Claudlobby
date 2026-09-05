# Validating changes to how a bot behaves

claudlobby's job is to make bots behave a certain way. A change can compose perfectly — the env var lands in `bot.conf`, the hook lands in `settings.local.json` — and still not *behave* as intended: the event doesn't fire, the alert doesn't send, the guardrail doesn't bite. **Unit tests prove composition. Only running proves behavior.**

So any change to how a bot behaves at runtime is validated by an empirical loop, not just a green test suite.

## The loop: Deliver → Add config → Recompose → Observe

| Step | What you do |
|------|-------------|
| **Deliver** | Make the code/library change (a lib/ script, hook, protocol, guardrail, principle, or composer field). |
| **Add config** | Set the relevant field(s) in `fleet.yaml` (e.g. `observability.activity_stuck_threshold: 60`). |
| **Recompose** | `claudlobby --fleet <fleet> generate`. Confirm the change is in the composed `bot.conf` / `.claude/settings.local.json` / `CLAUDE.md`. |
| **Observe** | Run it and watch the real behavior fire. |

The first three are cheap and deterministic. The fourth is the one that matters and the one teams skip — so claudlobby ships a harness for it.

## `lib/validate-bot-change.sh` — the Observe step, runnable

For the observability / trust-loop behaviors, this harness *is* the Observe step. It:

1. stands up a throwaway bot + a manager session in a temp root (no Claude auth, no real fleet),
2. seeds a stale tool-call marker and a past-deadline dispatch,
3. runs the real `fleet-pulse.sh` sweep against it, and
4. **asserts** that `activity_stuck` and `overdue_dispatch` events land on the plane (`claudlobby events --bot <bot>`) and that the manager receives a `[FLEET-PULSE]` push.

```bash
bash lib/validate-bot-change.sh   # exit 0 = behavior matched intent
```

When you add a new pulse check or event type, extend the harness with an assertion for it. That keeps "the behavior fires" under test, not just "the config composes."

> This harness already earned its keep: it caught a `fleet-pulse.sh` bug where a `bot.conf` missing a `BOT_SERVICE` line aborted the **entire** fleet sweep under `set -euo pipefail` — invisible to every composer unit test, because composition was fine; only *running* the sweep exposed it.

## When the behavior needs a live bot

Some changes (a skill's actual output, a guardrail's enforcement, a protocol's workflow) can't be asserted by the headless harness. For those, run the loop by hand:

```bash
claudlobby --fleet <fleet> generate
lib/spin-up-bot.sh <bot-dir>           # or restart the affected bot
# drive the affected path, then observe:
claudlobby events --bot <bot> --tail 20      # the plane's rows for the bot
claudlobby uptime --bot <bot>                 # heartbeat history, from the plane
tmux attach -t <bot>                   # watch the pane
```

Write down what you saw: *"composed `<fleet>`, restarted `<bot>`, invoked `/<skill>`, observed `<result>`."*

### Reaping a throwaway (guaranteed teardown)

When the live bot is a **throwaway canary** (a scratch fleet spun up only to observe), reap it with `lib/spin-down-bot.sh` — the inverse of `spin-up-bot.sh`. It stops + removes the systemd user unit / launchd agent, kills the per-bot tmux server, surgically drops the bot's `fleet-state.json` key (a locked single-key delete, never a prune), and with `--purge` also removes the bot dir. Cross-platform and idempotent.

Wire it under a `trap` so the throwaway is reaped even if the driver crashes or the session dies mid-run:

```bash
BOT_DIR=<bot-dir>
trap 'lib/spin-down-bot.sh --purge "$BOT_DIR"' EXIT INT TERM HUP
lib/spin-up-bot.sh "$BOT_DIR"
# ... drive + observe the canary ...
sleep <n> & wait          # interruptible wait — a mid-run SIGTERM then fires the trap promptly
```

The trap always fires on normal or `set -e` exit; `INT TERM HUP` add Ctrl-C, kill, and terminal-close. Use an **interruptible** wait (`sleep & wait`, not a bare foreground `sleep`) — bash defers a trap until the current foreground command returns, so a bare `sleep` delays the reap. (SIGKILL is uncatchable by design; for that case `lib/spin-down-bot.sh <bot-dir>` reaps the orphan when run manually — it is idempotent.)

## In review

Cite the observation in the PR body — claimed evidence is not evidence. See `library/lessons/review/empirical-verification.md`; reviewers gate bot-behavior PRs on a cited Observe step, not on "the composer test passes."

## Boundary: this is not Claudosseum

This loop is **pre-merge change validation** — does *this* change work. It's a claudlobby dev/operator discipline. **Longitudinal scoring** of which behaviors actually perform across hundreds of real runs ("trials and combat") is Claudosseum's job; claudlobby only *emits* the structured telemetry (the same `data/events` / `report-back.jsonl` stream) for it to consume. See `PROJECT_MISSION.md` sibling boundaries.
