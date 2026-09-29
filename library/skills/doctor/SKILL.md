---
name: doctor
description: "Read fleet and host health through the public CLI and report observed failures and missing evidence."
argument-hint: "[fleet name]"
tool_grants:
  - "Bash(claudlobby --json context show)"
  - "Bash(claudlobby --json config validate)"
  - "Bash(claudlobby --json host doctor --no-delivery)"
  - "Bash(claudlobby --json fleet reconcile)"
  - "Bash(claudlobby --json plane doctor)"
  - "Bash(claudlobby --json host credentials reconcile)"
---

# Doctor

Use the generated context for your fleet. Run each command as one literal Bash
call and read its result directly. For an operator-selected fleet, put the exact
`--fleet FLEET` selector before the command group; never invent a seed fallback
when scope is missing.

```bash
claudlobby --json context show
claudlobby --json config validate
claudlobby --json host doctor --no-delivery
claudlobby --json fleet reconcile
claudlobby --json plane doctor
```

The host doctor owns configuration, cache and other health checks; fleet
reconciliation owns native supervision observations; Plane doctor owns recording
health. Use their returned findings rather than rerunning private scripts,
scanning every host service, or deriving thresholds in this skill.
`--no-delivery` leaves repository delivery probes unchecked; say so in the report.
For a credential finding, `claudlobby --json host credentials reconcile` reads
declared, stored and equipped state without printing values. Provider probes
and repair commands require a separate manager/operator request.

Report the selected fleet, each observed failure or warning, unavailable checks,
and a concrete next step. A command that completed its scan can still report
unhealthy findings. Preserve unknown, degraded and refused outcomes; missing
evidence is not a pass. Keep the report in the current conversation unless the
operator explicitly asks for delivery elsewhere.

This skill diagnoses only. Do not recompose, restart, warm caches, renew secrets
or invoke private native helpers as part of the sweep. For a proposed repair,
consult `claudlobby GROUP VERB --help` and the fleet-ops skill before acting.

$ARGUMENTS
