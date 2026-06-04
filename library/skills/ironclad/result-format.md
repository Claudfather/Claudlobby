---
title: Ironclad Lens Result Format
description: Canonical markdown format that lens workers write to result.md
---

# Ironclad Lens Result Format

Workers write markdown with YAML frontmatter directly to `result.md`. No JSON. No translation layer. `/ironclad` reads this as-is for aggregation.

```markdown
---
lens: <lens-name>
worker: <bot-id>
pr_url: <url>
started: <ISO timestamp>
completed: <ISO timestamp>
status: completed | failed
---

## Findings

### Blockers
- <numbered findings with evidence>

### Risks
- <numbered findings with severity + mitigation>

### Gaps
- <numbered findings>

### Questions
- <numbered ambiguities>

### Observations
- <bullet notes>
```

## Rules

- Sections with zero findings must be **omitted entirely** — do not write empty headers.
- If every section is empty, write a single line under `## Findings`: "No findings surfaced by this lens."
- **Write findings incrementally** — set `status: in-progress` in frontmatter while working, update to `status: completed` when done. Partial findings protect against context compaction losing your research.
