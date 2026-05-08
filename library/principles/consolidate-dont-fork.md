---
title: Consolidate, don't fork
description: When the team owns a surface with parallel paths that could diverge, collapse to one
---

# Consolidate, don't fork

When the team owns a surface and that surface has (or is about to grow) parallel paths that could diverge over time, consolidate to one. The wrong default ossifies fast.

- When adding a second path to an existing surface (a second dispatch tool, a second chart engine, a second API route), first ask: can we replace the existing path rather than coexist? Usually yes.
- When surveying existing code, flag parallel paths as tech-debt candidates.
- The longer we ship with two paths, the harder it is to collapse later. If consolidation is the right move, ship it before habits form.

**Exception:** legitimate external-contract reasons (public APIs, third-party integrations). Everything team-owned consolidates.
