---
title: Claudlobby dev work uses projects/ checkout
description: Do claudlobby development in your projects/ checkout — the shared install is CLAUDLOBBY_ROOT for every bot on the host, so branching there swaps supervision and dispatch scripts estate-wide
---

# Claudlobby dev work uses projects/ checkout

When working on claudlobby code, use your `projects/` checkout — never branch or commit from the active data root at `{{CLAUDLOBBY_ROOT}}`. A sealed release supplies runtime code; config changes go through `claudlobby --root <data-root> config plan --release <sealed-release-id>` and then `claudlobby --root <data-root> host activate <plan-id> --install-directory <native-user-unit-dir>`.
