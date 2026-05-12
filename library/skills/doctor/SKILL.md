---
name: doctor
description: "Fleet health sweep: services, credentials, disk, memory, npx cache. Produces a structured report with pass/warn/fail per check."
argument-hint: "[--fleet <name>]"
---

# Doctor

Run a graduated health sweep across the fleet infrastructure. Each check produces a PASS, WARN, FAIL, or SKIP verdict. The consolidated report gives the user a single view of fleet health with actionable remediation for anything non-green.

## Checks

Run these in sequence. Each check depends on environment setup from earlier steps.

### 1. Config validation

```bash
claudlobby validate
# Or with fleet overlay:
claudlobby --fleet $FLEET validate
```

PASS if exit 0. FAIL if validation errors. WARN if warnings but no errors.

### 2. Supervision state

```bash
{{CLAUDLOBBY_ROOT}}/lib/reconcile-fleet.sh <fleet>
```

PASS if all bots are healthy (0 orphan, 0 missing, 0 unbound). WARN if unbound services exist. FAIL if any bot is missing or orphaned.

### 3. Credential check

```bash
{{CLAUDLOBBY_ROOT}}/lib/creds-check.sh
```

PASS if all tokens are valid. WARN if any token is nearing expiry. FAIL if any token is invalid or missing.

### 4. Disk usage

```bash
{{CLAUDLOBBY_ROOT}}/lib/disk-monitor.sh
```

PASS if usage is below 80%. WARN if between 80-90%. FAIL if above 90%.

### 5. Memory

```bash
{{CLAUDLOBBY_ROOT}}/lib/fleet-memory-check.sh
```

PASS if fleet RSS is within safe bounds. WARN if approaching host limits. FAIL if memory pressure is critical.

### 6. NPX cache

```bash
{{CLAUDLOBBY_ROOT}}/lib/check-npx-cache.sh --fleet <fleet>
```

PASS if all MCP npx packages are cached. WARN if some are missing (cold starts will be slow). FAIL should not normally occur here.

### 7. Plugin cache

Check `~/.claude/plugins/installed_plugins.json` exists and has entries. PASS if present with entries. WARN if file exists but is empty. SKIP if file does not exist.

### 8. Service units

On Linux:
```bash
systemctl --user list-units '*.service'
```

On macOS:
```bash
launchctl list
```

PASS if all expected bot services are active/loaded. WARN if some are inactive. FAIL if services are missing entirely.

## Report Format

```
FLEET HEALTH REPORT — <fleet-name>
============================================

Config:        PASS — validates clean
Supervision:   PASS — 8 healthy, 0 orphan, 0 missing
Credentials:   WARN — GITHUB_PAT expires in 12 days
Disk:          PASS — 175G free (22% used)
Memory:        PASS — fleet RSS 2.1G / 16G
NPX cache:     PASS — all 6 packages cached
Plugin cache:  PASS — telegram 0.0.6, claudna 0.2.0
Services:      PASS — 8/8 active

Issues:
  - GITHUB_PAT nearing expiry — renew at github.com/settings/tokens
```

## Instructions

1. Run all checks sequentially (some share env setup from earlier steps).
2. For each check, classify as PASS (healthy), WARN (suboptimal but functional), or FAIL (broken).
3. If a check's underlying script does not exist at the expected path, report SKIP instead of crashing. Not all hosts have every script.
4. Present the report to the user (via Telegram if available, otherwise terminal).
5. If any check is FAIL, suggest specific remediation steps below the report.
6. If `--fleet` is provided as an argument, scope all checks to that fleet overlay. Otherwise check the default or seed fleet.

$ARGUMENTS
