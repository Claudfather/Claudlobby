---
name: extension-check
description: "Use when an implementation PR might duplicate existing functionality or create parallel paths. Checks whether the change extends, replaces, or conflicts with existing code patterns. Applies to implementation PRs and mixed PRs."
argument-hint: "[--dispatch]"
---

# Extension Check

Your job is to determine whether the proposed changes work with the codebase's grain or against it. Find duplicated functionality, parallel paths, missed consolidation opportunities, and integration risks with existing code.

## Procedure

### 1. Map Existing Patterns

Read the PR diff. For each new function, class, module, or pattern introduced, search the codebase for existing equivalents:

- **Same name, different location** — is this a duplication?
- **Different name, same purpose** — is this a parallel path?
- **Extends existing** — does this correctly build on the existing pattern, or subtly diverge?

### 2. Apply Extension Lenses

- **Parallel paths** — does the change introduce a second way to do something already handled? If consolidation is possible, flag it.
- **Missed reuse** — does the codebase already provide utilities, helpers, or abstractions the PR reinvents?
- **Integration points** — where does the change touch existing systems? Are the boundaries clean or does it reach into internals?
- **Convention alignment** — does the change follow the codebase's established patterns (naming, structure, error handling)?
- **Dead code** — does the change make existing code unreachable or redundant without removing it?

### 3. Produce Findings

Write findings to the result path using the format specified by the dispatcher (see `result-format.md`). Cite specific file:line references for existing code that overlaps with or is affected by the change.

### `--dispatch` Mode

When dispatched by `/ironclad`, operate non-interactively. Read the source path provided, write findings to the result path, and report back. Do not post to the PR, create issues, or prompt for input.
