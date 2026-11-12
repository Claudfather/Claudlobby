---
title: Claudlobby dev work uses projects/ checkout
description: Do claudlobby development in your projects/ checkout — the shared install is CLAUDLOBBY_ROOT for every bot on the host, so branching there swaps supervision and dispatch scripts estate-wide
---

# Claudlobby dev work uses projects/ checkout

When working on claudlobby code, use your `projects/` checkout — never branch or commit from the active data root at `{{CLAUDLOBBY_ROOT}}`. The managed Git installation supplies runtime code. Give a reviewed config change to the operator, who applies it with `claudlobby --root <data-root> host update apply FULL_COMMIT` from their own shell outside a generated bot session.
