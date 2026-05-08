---
title: Visibility AND speed
description: Never trade speed for visibility or vice versa — engineer affordances around existing work
---

# Visibility AND speed

When designing UX, streaming, or progress affordances, never trade speed for visibility or vice versa. Visibility comes from engineered affordances around work that fires anyway (status events emitted during tool calls, progress chips on in-flight operations), not from synthetic pre-warm that costs wall-clock.

- If a proposed visibility feature adds measurable latency, the design is wrong — reshape.
- Look for visibility affordances that piggyback on existing work: streaming tool-use events, status chips that render work already in progress.
- If you can't engineer visibility without adding latency, file the constraint and escalate.
