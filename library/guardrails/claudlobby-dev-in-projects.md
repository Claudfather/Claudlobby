---
title: Claudlobby dev work uses projects/ checkout
description: Do claudlobby development in your projects/ checkout — the shared install is CLAUDLOBBY_ROOT for every bot on the host, so branching there swaps supervision and dispatch scripts estate-wide
---

# Claudlobby dev work uses projects/ checkout

When working on claudlobby code, use your `projects/` checkout — never branch or commit from the shared install at `{{CLAUDLOBBY_ROOT}}`. The shared install is for runtime only (generate, spin-up, lib/ execution).
