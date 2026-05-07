---
title: No backwards-compat shims
description: Cut cleanly when the team owns all callers — no shims, wrappers, or deprecated aliases
---

When the team owns every caller of its own code (no external consumers), do not ship backwards-compat shims, thin wrappers, deprecated aliases, or "keep old path working for a cycle" layers. Cut cleanly.

- When dispatching a refactor, default to "cut cleanly — no shims, no aliases, no legacy wrappers."
- When a plan includes shims, flag it as YAGNI during pressure-test — cut before implementation.
- If a shim sneaks through review, file an immediate follow-up strip PR.
- LLM tool surfaces are team-owned — tools can be renamed, removed, reshaped freely.

**Exception:** external API contracts (third-party consumers, published endpoints, semver public libs).
