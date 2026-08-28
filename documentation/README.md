# Documentation

Project documentation for claudlobby. Each subdirectory has a specific purpose:

- `getting-started.md` — Clone-to-fleet walkthrough (entry point for new users)
- `system-yaml-schema.md` — Every config field in system.yaml
- `fleet-yaml-schema.md` — Every config field in fleet.yaml
- `projects-yaml-schema.md` — Every config field in projects.yaml
- `architecture/` — System design, data flow, component relationships
- `runbooks/` — Operational procedures (deploy, setup, maintenance)
- `examples/` — Reference config files linked from runbooks (e.g. a GitHub branch-protection ruleset JSON)
- `guides/` — Conceptual walkthroughs and diagnostic guides (decision trees, feature deep-dives) — distinct from the step-by-step procedures in `runbooks/`
- `decisions/` — Architecture Decision Records (ADRs)
- `plans/` — Hand-authored implementation plans, one per PR/feature, dated filenames, `type: plan` frontmatter with a `status` (draft/approved/completed)
- `planning/` — Dated session output from the `/artemis-skills:audit` lens skills (`tech_debt/`, `security/`, `access-paths/`, `phases/`, `product-vision/`, `investigations/` subdirs) — machine-generated remediation plans, not hand-authored
- `baselines/` — Dated JSON snapshots consumed by gates such as `naked-bot-observe.py --baseline` (recorded output, not hand-authored)
- `screenshots/` — Issue/PR reference images (committed on asset branches for private repos)
- `integrations/` — External service integration guides
- `reflections/` — Dated session retrospectives (`type: knowledge`)
- `archive/` — Completed or superseded docs (moved here, not deleted)

Top-level files are general guides. Categorized content goes in subdirectories.
