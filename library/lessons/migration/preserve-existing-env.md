---
title: "Lesson: env-migrate must merge with existing .env, never overwrite"
---

When a migration tool writes a `.env` file, the temptation is to render the dict and call `write_text`. **Don't.** A `.env` is a living document — operators hand-edit it, earlier migration runs populated keys the current pass doesn't know about, and an overwrite silently destroys all of it.

## The bug

`claudlobby env-migrate` originally wrote tier `.env` files from scratch each run:

```python
# bad
lines = [f"# Fleet secrets ...", ""]
for k in sorted(fleet_vars):
    lines.append(f"{k}={fleet_vars[k]}")
fleet_env_path.write_text("\n".join(lines))
```

If the destination already had 12 keys (operator-set: `GITHUB_PAT`, `NEON_API_KEY`, `RAILWAY_*`, `NOTION_TOKEN`, `SNOWFLAKE_*`, ...) and this pass discovered 6 new ones from the legacy source, the result was a file with **only those 6**. The 12 prior keys were gone, and the operator's bot was suddenly broken in non-obvious ways (auth failures days later when the operator wasn't paying attention to the migration).

Real exposure on the test fleet: 12 hand-set vars in `local/<fleet>/.env`. Without the merge fix, they would have been wiped on first `env-migrate --apply`.

## The fix

Read existing → merge new vars over it (new wins on conflict) → write the union:

```python
existing = _read_env_file(path)
merged = {**existing, **new_vars}
path.write_text(_format_env_file(header, merged))
```

The behavior an operator can rely on:

- Anything in the destination but not in the migration plan: **preserved**.
- Anything in the migration plan but not in the destination: **added**.
- Anything in both: **migration plan value wins** (so re-running with refreshed legacy values updates correctly).

Plus an audible signal in the apply output:

```
wrote /home/.../local/<fleet>/.env (6 migrated, 18 total)
```

The `(N migrated, M total)` form makes the preservation visible — when M > N, you know prior content was kept.

## Why dry-run by default also matters

The merge fix is a safety net. Dry-run is the front door. A migration tool that defaults to writing files is one operator-typo away from disaster. Default to read-only `--dry-run`; require explicit `--apply` to commit. This applies to *every* file-writing migration step, not just env.

## Generalize

This pattern holds anywhere a tool writes config the user might also touch:

- `.env` files
- Cron entries (read existing, splice in new lines, never replace whole crontab)
- systemd unit drop-ins (add `*.conf` files, don't overwrite the unit)
- shell rc files (append, never replace)

If a tool you're writing has a "regenerate" mode that rewrites a file from scratch, ask yourself: *what hand-edited content might be in there that I'd be destroying?* If the answer isn't "nothing, by construction," merge instead.
