---
title: "Lesson: tmux's one-server-per-user rule and how it affects multi-bot env"
---

`tmux` runs **one server per user**, by default at socket `/tmp/tmux-$(id -u)/default`. Every `tmux new-session` invocation by that user attaches to the same server. Critically: **the server's environment is whatever the first process to start it had** — and every session spawned afterward inherits that frozen env, not the env of the *current* process running `new-session`.

> **Update (per-bot socket isolation, #414):** the fleet no longer shares one server. Each bot's lifecycle now runs on its **own** tmux server — a private `-L <socket>` equal to its `BOT_SERVICE`, resolved via `tmux_socket_for_bot` in `lib-common.sh` — so the cross-bot env inheritance described below can no longer happen: a session only ever inherits *its own* bot's server env. The inline-pass / `.tmux-env` mechanism is **kept as belt-and-suspenders for v1** (retiring it is deferred); the rest of this lesson is the historical root cause the isolation removes.

## Why this matters for fleets

Imagine two bots, `clog` and `kev`, each with their own `.env` containing distinct `TELEGRAM_BOT_TOKEN` values. Each one's `start-bot.sh`:

1. Sources its own `.env` → `TELEGRAM_BOT_TOKEN` is in start-bot.sh's process env.
2. Runs `tmux new-session -d -s <bot> "claude ..."`.

If `clog`'s start-bot.sh runs **first** on a fresh boot, the tmux server is created with clog's env (clog's `TELEGRAM_BOT_TOKEN`). Later when kev's start-bot.sh runs, the server is already running — kev's `new-session` reuses it. The new session inherits **clog's** env. Kev's claude posts to Telegram under `@crogs_assistant_bot`. Same surface for any inherited identity var. (Real bug observed 2026-05-04 — all 7 worker bots posted as the manager.)

## The fix that works (PR #29)

Don't trust server-inherited env for per-bot identity. Pass identity vars **inline** as a leading prefix to the new-session command:

```bash
tmux new-session -d -s "$BOT_NAME" \
  "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN \
   TELEGRAM_BOT_HANDLE=$TELEGRAM_BOT_HANDLE \
   TELEGRAM_GROUP_CHAT_ID=$TELEGRAM_GROUP_CHAT_ID \
   claude ..."
```

The inline assignments override whatever the server passes. Each session gets the right values regardless of which bot started the server first.

## The fix that doesn't (and tempts you)

`tmux kill-session -t "$BOT_NAME"` only kills the session, not the server. So a "kill session and recreate" pattern won't refresh server env. The server stays alive across session lifecycles as long as *any* session exists or has existed without explicit `kill-server`.

`tmux kill-server` does refresh — but it's nuclear: it tears down every session for that user, including unrelated bots. Don't reach for it from one bot's start-bot.sh.

## Single-bot fleets are luckier

If a host runs only one bot for a given user, the tmux server is always created by that bot's own start-bot.sh, so server-env equals bot-env. The bug doesn't surface. But the moment you add a second bot, the lurking inheritance bites. **Treat the inline-pass pattern as the default**; don't rely on "this is single-bot today."

## Operator's debugging hook

When a bot reports under the wrong identity, suspect tmux server inheritance first. Verify:

```bash
ps -ef | grep tmux              # one server, started by which process?
cat /proc/<claude-pid>/environ   # what env does the running claude actually see?
                                 # (NUL-separated; pipe through `tr '\0' '\n'`)
```

If the running claude has *another bot's* token in its env, you've reproduced the inheritance bug. Inline-pass is the answer.

## Generalizes beyond tmux

Same shape shows up in any "shared daemon with first-writer-wins env" system: a long-running shell server, a debug adapter that survives client reconnects, an init system holding child env. When a process can outlive its environment provenance, distinguish identity vars (must be per-instance) from defaults (server-inherited is fine).
