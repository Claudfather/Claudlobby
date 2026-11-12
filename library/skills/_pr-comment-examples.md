---
title: PR Comment Examples (Shared Partial)
description: The pr-comment-hygiene protocol's sample findings, fork preferences and verdicts. Reference only; the rules are in the protocol.
---

# PR Comment Examples

Reference for the `pr-comment-hygiene` protocol, which holds every rule; nothing here adds one. Each heading below is the protocol section its text illustrates.

## Finding

Examples:

```
[bot-a] [extension-check] [major] Phase 2b proposes a new SkillValidator class, but scripts/validate-skills.py already provides this functionality.

Consolidate into the existing validator rather than building a parallel path. The existing script handles SKILL_CONTRACT.md validation, frontmatter checks, and CI integration.
```

```
[bot-b] [align-to-mission] [minor] Phase 4d (documentation + changelog) is standard housekeeping — aligned but low mission-impact.

Consider deferring 4d until after the first real /ironclad cycle produces learnings worth documenting.
```

```
[bot-c] [cost-benefit] [info] Phase 2 skills are all M-sized and fully parallel — good ROI on parallelization if 3+ engineers are available.
```

## Fork Preference

Examples:

```
[bot-a] [FORK F1] (a) — clauDNA is the right home. The lenses are useful standalone — a developer should be able to run /align-to-mission without a fleet.

[bot-b] [FORK F4] (a) — plan-only scope. Single responsibility keeps the convergence gate fast and predictable.
```

## Verdict

Examples:

```
**[bot-a] [VERDICT] request-changes** — reviewed at a1b2c3d. 2 major findings: parallel path in Phase 2b, missing rollback strategy.

**[bot-b] [VERDICT] approve** — reviewed at a1b2c3d. All phases align with PROJECT_MISSION.md. One minor deferral suggestion posted.

**[bot-c] [VERDICT] comment** — reviewed at a1b2c3d. Cost-benefit analysis posted. No blocking issues, but Phase 3a is the highest-risk item.
```
