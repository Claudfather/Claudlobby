---
title: Visual changes ship as toggles
description: New visual styles are user-toggleable modes, not forced app-wide replacements
---

When introducing a new visual style (chart theme, color palette, font), the default shape is a user-toggleable mode that net-adds the new look while preserving the prior one. Do NOT force-flip the existing style app-wide unless explicitly told to kill the old style.

- Default question when dispatching visual/styling work: "is this a toggle, or a replacement?" Default to toggle when the existing style is functional.
- Architect for both modes from the start: CSS variables, theme tokens that flip per mode, persistence layer.
- Net-add visuals are cheap; reverts are expensive.
