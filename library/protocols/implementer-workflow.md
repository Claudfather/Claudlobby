---
title: Implementer workflow
description: Required pre-push quality gates for bots that ship code
---

# Implementer workflow

Two skills are mandatory before pushing non-trivial changes:

**`/simplify`** — run on changes before the final push whenever the change is non-trivial (>~50 LOC or >2 files). Reviews for reuse, quality, and efficiency. This is a codified rule, not optional — below the threshold it's discretionary, above it's required.

**`/weigh-development-paths`** — run at any decision juncture where the next step isn't obvious. Weigh using four lenses:

1. **Best practice** — the accepted industry pattern
2. **Most future-proof** — leaves the most doors open
3. **Most elegant** — lowest cognitive overhead for a future reader
4. **Most consistent with codebase patterns** — grep adjacent code and let the codebase vote

Engineers decide sub-choices themselves using these lenses. Escalate to the manager only for product-shape questions, schema changes, deploys, external-cost decisions, or scope expansion beyond the issue.

In report-back, mention that both skills were used (or why they weren't applicable) so the manager can verify workflow adherence.
