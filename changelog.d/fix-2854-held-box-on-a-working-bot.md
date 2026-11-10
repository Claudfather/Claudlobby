### Fixed — a held input box pages `input_held` on a bot that keeps working (#2854)

keepalive reads the bot's input box on every tick, a busy one included, with the send path's own held test. It keeps `data/.held` from the first tick the box holds text until a tick shows it empty, whatever the verdict, with `data/.held-turn` beside it while a turn is running. fleet-pulse pages `input_held` once a fresh hold passes `OBSERVABILITY_INPUT_HELD_THRESHOLD`, however many tool calls the bot makes, and names whether a turn is running. Before, a bot that kept working never raised it, while every message sent to that bot was refused.

`OBSERVABILITY_INPUT_HELD_THRESHOLD=0` now turns the `input_held` page off, and fleet-pulse says so on stderr each sweep a box holds. A held box with no turn running still keeps `activity_stuck` quiet; one under a running turn no longer does, so a hung turn still pages.
