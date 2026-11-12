# Context-management reference

Reference that the `context-management` protocol points to. The protocol keeps the rules; nothing here is a step.

## Never state a context percentage you have not seen

On the figure's two forms: they come from a read of Claude Code 2.1.322's code,
not from a pane.

- The footer shows `N% until auto-compact` whenever Claude Code holds the
  conversation to an autocompact window, and `NN% context used` only when it
  holds it to none. It holds any window set in settings or in the environment.
  With none set, it holds a model with a native 1M window to its own 1M window
  by default. `auto_compact_window: auto` composes a per-model `"auto"`, which
  Claude Code reads as none set, so the footer is the same under it.
- In `N% until auto-compact`, N is the share left of the room up to the point
  where Claude Code compacts: the window less the autocompact buffer that
  `claude -p "/context"` shows (33k tokens on 2.1.322). In `NN% context used`,
  N is the share used, out of the window less the up-to-20,000 tokens Claude
  Code keeps for its output.
- With autocompact off, the footer reads `Context low (N% remaining)` instead.
- Where either form starts to render has not been measured.

Measured across all 21 bots on this host in August 2026 (#1303), when the
figure read `NN% context used`: one rendered `98% context used` while twenty
rendered nothing, and the panes were structurally identical — a fixed slot whose
*content* is conditional, not an element that appears.

On "a live gauge, not a one-time alarm":

It moved with use: in August 2026 the same bot read `97%` and then `98%`
minutes later.

## Judge headroom by what you can observe

On "Lean on subagents early":

This rung used to be keyed to "above 50%"; the action never
needed the number, and doing it early is close to free.

## Your duty when you notice one

On "state whether a fresh session is safe **right now**":

That is the part only you know, and it is
what decides whether the manager acts immediately or waits.
