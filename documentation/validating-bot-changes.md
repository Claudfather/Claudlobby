# Validating changes to how a bot behaves

claudlobby's job is to make bots behave a certain way. A change can compose perfectly — the env var lands in `bot.conf`, the hook lands in `settings.local.json` — and still not *behave* as intended: the event doesn't fire, the alert doesn't send, the guardrail doesn't bite. **Unit tests prove composition. Only running proves behavior.**

So any change to how a bot behaves at runtime is validated by an empirical loop, not just a green test suite.

## The loop: Deliver → Add config → Recompose → Observe

| Step | What you do |
|------|-------------|
| **Deliver** | Make the code/library change (a claudlobby/_runtime_scripts/ script, hook, protocol, guardrail, principle, or composer field). |
| **Add config** | Set the relevant field(s) in `fleet.yaml` (e.g. `observability.activity_stuck_threshold: 60`). |
| **Recompose** | Commit the reviewed source and apply it with `host update apply FULL_COMMIT` from an operator shell, using the stable installation CLI ([Managed Git delivery](fleet-update-lifecycle.md#managed-git-delivery)). `apply` is effectful, not a preview; inspect it with `host update status`. Confirm the change in the composed `bot.conf` / `.claude/settings.local.json` / `CLAUDE.md`. `config diff` reports drift of published output only; it does not preview edited authored source. |
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

Some changes (a skill's actual output, a guardrail's enforcement, a protocol's workflow) can't be asserted by the headless harness. For those, run the loop by hand in a native canary: an independent installation with its own data root and native labels, never a production root or another fleet inside one (`library/protocols/canary-rollout.md`). Use that installation's stable CLI (`INSTALL/bin/claudlobby`) with an explicit `--root` on every command. Applying and inspecting follow [Managed Git delivery](fleet-update-lifecycle.md#managed-git-delivery); first-fleet preparation follows [Getting started](getting-started.md). Updates cover the whole installation, so they take no `--fleet` or `--seed`:

```bash
"$CLI" --root "$CANARY_DATA" host update apply FULL_COMMIT --json
"$CLI" --root "$CANARY_DATA" host update status --json
# After an interruption, continue the original operation:
"$CLI" --root "$CANARY_DATA" host update continue --json
# drive the affected path, then observe:
"$CLI" --root "$CANARY_DATA" event list --bot <bot> --limit 20   # the plane's rows for the bot
"$CLI" --root "$CANARY_DATA" fleet uptime --bot <bot>             # heartbeat history, from the plane
"$CLI" --root "$CANARY_DATA" fleet utilization                    # busy share of observed heartbeat time; no history is unknown
"$CLI" --root "$CANARY_DATA" --fleet <fleet> bot session BOT      # inspect the selected private session
```

Inspect the update's outcome, including any bot restarts it performs, before driving the path.

Write down what you saw: *"composed `<fleet>`, restarted `<bot>`, invoked `/<skill>`, observed `<result>`."*

### Reaping a throwaway (guaranteed teardown)

The Git updater currently refuses removal of declared native consumers, and `bot remove` reports that limitation without deleting anything. Do not remove a declaration and assume its service disappeared. `bot stop BOT` pauses a declared bot; it is not permanent teardown. Retain the isolated canary root and its evidence until an operator has verified native teardown. This limitation must be resolved before treating disposable-canary cleanup as a supported product flow.

## In review

Cite the observation in the PR body — claimed evidence is not evidence. See `library/lessons/review/empirical-verification.md`; reviewers gate bot-behavior PRs on a cited Observe step, not on "the composer test passes."

## The gate as the root CLAUDE.md stated it

The root `CLAUDE.md` stated this gate in full until #2035; it now keeps a summary.

Any change that affects **how a bot behaves at runtime** (claudlobby/_runtime_scripts/ supervision & observability scripts, hooks, skills, protocols, guardrails, principles, composed `bot.conf` env) must be **empirically validated** before merge. Unit tests prove *composition* — that the env var lands in `bot.conf`. Only running the code proves *behavior* — that the event actually fires, the alert actually sends, the bot actually does the thing. Follow the loop:

1. **Deliver** — make the code/library change.
2. **Add config** — set the relevant field(s) in the canary's authored `fleet.yaml`.
3. **Apply in an independent canary** — a separate installation with its own data root and native labels (distinct fleet/bot labels, host `unit_prefix`, Plane state and throwaway channels). Prepare it per [Getting started](getting-started.md) and apply the reviewed commit with `host update apply FULL_COMMIT` ([Managed Git delivery](fleet-update-lifecycle.md#managed-git-delivery)); inspect with `host update status`. Confirm config, skills and permissions in the published output; `config diff` covers published-output drift only. An update is a whole-installation operation, so never use a production root to simulate a single-bot canary.
4. **Observe** — run it and watch the real behavior:
   - For observability/trust-loop behaviors: `bash harness/validate-bot-change.sh` stands up a throwaway bot + tmux sessions and asserts the events fire end-to-end. Extend it when you add a new event/check.
   - For other behavior: use the canary's stable CLI to drive the affected path, then inspect `event list --bot BOT` and the receiving session. Keep existing production no-restart holds in force.

**Cite the observation in the PR body** ("ran `validate-bot-change.sh` → activity_stuck + overdue_dispatch fired; manager notified") — claimed evidence is not evidence.

**When a change matches an externally produced shape** (a plugin's injection, a carrier's response, a tool's output), ground the canonical fixture in a **live capture** — a transcript or real response — never in reading the producer's source; and commit the capture's **shape, never its identifiers** (the repo is public). Four gauntlet rounds on the Telegram carrier (#1404/#1411) each found the same class: fixtures certifying a shape reality does not produce, the last one shipped and silently dropped the operator's first live message. This is also how latent bugs surface: the harness above caught a `fleet-pulse.sh` sweep-abort that every unit test missed.

**This gate proves the code; it does not prove the rollout.** Clearing it is mandatory for every runtime change. Separately, when a change to the framework itself (claudlobby, clauDNA, claudron) ships **live fleet-wide** — supervision/`lib` scripts, plugins, the bridge, composed `bot.conf` — the manager should *by default* validate an independent canary root before coordinated production rollout: a strong default for fleet-wide framework changes, not a universal mandate (skip it for single-bot, product-repo, or non-runtime work). See the `canary-rollout` protocol.

## Boundary: this is not Claudosseum

This loop is **pre-merge change validation** — does *this* change work. It's a claudlobby dev/operator discipline. **Longitudinal scoring** of which behaviors actually perform across hundreds of real runs ("trials and combat") is Claudosseum's job; claudlobby only *emits* the structured telemetry (the plane's rows, the same the fleet's own doors read) for it to consume. See `PROJECT_MISSION.md` sibling boundaries.
