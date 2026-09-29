# Validating changes to how a bot behaves

claudlobby's job is to make bots behave a certain way. A change can compose perfectly — the env var lands in `bot.conf`, the hook lands in `settings.local.json` — and still not *behave* as intended: the event doesn't fire, the alert doesn't send, the guardrail doesn't bite. **Unit tests prove composition. Only running proves behavior.**

So any change to how a bot behaves at runtime is validated by an empirical loop, not just a green test suite.

## The loop: Deliver → Add config → Recompose → Observe

| Step | What you do |
|------|-------------|
| **Deliver** | Make the code/library change (a lib/ script, hook, protocol, guardrail, principle, or composer field). |
| **Add config** | Set the relevant field(s) in `fleet.yaml` (e.g. `observability.activity_stuck_threshold: 60`). |
| **Recompose** | Stage the edited source with `claudlobby config plan --release RELEASE_ID`, review `claudlobby config diff PLAN_ID`, then run `claudlobby host activate PLAN_ID --install-directory INSTALL_DIR` from an operator shell. Confirm the change in the composed `bot.conf` / `.claude/settings.local.json` / `CLAUDE.md`. |
| **Observe** | Run it and watch the real behavior fire. |

The first three are cheap and deterministic. The fourth is the one that matters and the one teams skip — so claudlobby ships a harness for it.

## `harness/validate-bot-change.sh` — the Observe step, runnable

For the observability / trust-loop behaviors, this harness *is* the Observe step. It:

1. stands up a throwaway bot + a manager session in a temp root (no Claude auth, no real fleet),
2. seeds a stale tool-call marker and a past-deadline dispatch,
3. runs the real `fleet-pulse.sh` sweep against it, and
4. **asserts** that `activity_stuck` and `overdue_dispatch` events land on the plane (`claudlobby event list --bot <bot>`) and that the manager receives a `[FLEET-PULSE]` push.

```bash
bash harness/validate-bot-change.sh   # exit 0 = behavior matched intent
```

When you add a new pulse check or event type, extend the harness with an assertion for it. That keeps "the behavior fires" under test, not just "the config composes."

> This harness already earned its keep: it caught a `fleet-pulse.sh` bug where a `bot.conf` missing a `BOT_SERVICE` line aborted the **entire** fleet sweep under `set -euo pipefail` — invisible to every composer unit test, because composition was fine; only *running* the sweep exposed it.

## When the behavior needs a live bot

Some changes (a skill's actual output, a guardrail's enforcement, a protocol's workflow) can't be asserted by the headless harness. For those, run the loop by hand:

```bash
claudlobby --fleet <fleet> config plan --release RELEASE_ID
claudlobby config diff PLAN_ID
claudlobby host activate PLAN_ID --install-directory INSTALL_DIR
# Activation performs the required bot restarts; inspect its outcome.
# drive the affected path, then observe:
claudlobby event list --bot <bot> --limit 20      # the plane's rows for the bot
claudlobby fleet uptime --bot <bot>                 # heartbeat history, from the plane
claudlobby fleet utilization                        # busy share of observed heartbeat time; no history is unknown
claudlobby --fleet <fleet> bot session BOT  # inspect the selected private session
```

Write down what you saw: *"composed `<fleet>`, restarted `<bot>`, invoked `/<skill>`, observed `<result>`."*

### Reaping a throwaway (guaranteed teardown)

For a selected throwaway bot, first remove its declaration from authored `fleet.yaml`, stage and activate that change, then run `claudlobby --fleet <fleet> bot remove BOT --purge` from an operator shell. Removal verifies native ownership and refuses uncommitted work. `bot stop BOT` only pauses a declared bot; it is not permanent teardown. This reviewed sequence cannot be replaced by a shell exit trap.

## In review

Cite the observation in the PR body — claimed evidence is not evidence. See `library/lessons/review/empirical-verification.md`; reviewers gate bot-behavior PRs on a cited Observe step, not on "the composer test passes."

## Boundary: this is not Claudosseum

This loop is **pre-merge change validation** — does *this* change work. It's a claudlobby dev/operator discipline. **Longitudinal scoring** of which behaviors actually perform across hundreds of real runs ("trials and combat") is Claudosseum's job; claudlobby only *emits* the structured telemetry (the plane's rows, the same the fleet's own doors read) for it to consume. See `PROJECT_MISSION.md` sibling boundaries.
