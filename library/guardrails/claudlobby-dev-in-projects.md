---
title: Claudlobby dev work uses projects/ checkout
description: Do claudlobby development in your projects/ checkout — the shared install is CLAUDLOBBY_ROOT for every bot on the host, so branching there swaps supervision and dispatch scripts estate-wide
---

# Claudlobby dev work uses projects/ checkout

When working on claudlobby code, use your `projects/` checkout — never branch or commit from the active data root at `{{CLAUDLOBBY_ROOT}}`. A sealed release supplies runtime code; stage config changes with `claudlobby --root <data-root> config plan --release <sealed-release-id>` and review with `claudlobby --root <data-root> config diff <plan-id>`. Give the reviewed plan to the operator, who runs `claudlobby --root <data-root> host activate <plan-id> --install-directory <native-user-unit-dir>` from their own shell outside a generated bot session.
