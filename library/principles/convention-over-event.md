---
title: Convention over event
description: Code comments reference durable conventions and semantics, not point-in-time events
---

In code (comments, config inline notes, model docstrings): reference the **durable convention or semantic**, NOT the **point-in-time event** that introduced the change.

- Bad: `-- Reclassified per Q1 decision (2026-04-30)`
- Good: describe the semantic meaning of the value or configuration

Time-stamped temporal context belongs in: (1) commit messages, (2) PR descriptions, (3) decision docs / planning files.

**Pattern for new conventions:** when introducing a value-with-meaning, document the convention ONCE near the schema (column docstring, type definition), so individual instances are self-documenting without per-instance commentary.

When dispatching work where a worker may add comments, include this convention-over-event principle as guidance.
