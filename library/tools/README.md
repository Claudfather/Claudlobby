# Tools

Composited bot scripts. A tool is a **directory** `library/tools/<name>/` (fleet
overlay `local/<fleet>/library/tools/<name>/` wins) that the compositor renders
into the bot dir at `generate` time.

**Tool, or a helper inside a skill?** This category exists for **compose-time
parameterization** — values baked into the script at `generate` time via
`tool.yaml` + Jinja. A script that reads everything from runtime env gains
nothing here, and splitting one capability across two library categories costs
cohesion: ship it beside its `SKILL.md` instead. See `library/skills/README.md`,
"Shell helpers". Unlike `data/` content (bot-owned, mutable,
never regenerated), a tool is generated output like `CLAUDE.md`: never
hand-edited, always recreated by `claudlobby generate`, reconciled on every run
(detaching a tool removes its rendered file).

## Layout

```
library/tools/<name>/
├── tool.yaml            # manifest (required)
└── <name>.py.j2         # Jinja template — emitted as tools/<name>.py, chmod 0755
```

## tool.yaml

```yaml
type: script                  # required — the only type today
# template: custom-name.py.j2 # optional — defaults to the directory's sole *.j2
params:                       # optional — compose-time structure baked into the render
  snapshot_subdir:
    default: snapshots        # literal value; template joins it onto {{ data_dir }}
  lookback_days:
    default: 7
  ledger_name:
    required: true            # no default → fleet.yaml must supply it
env:                          # optional — runtime env contract (validator warns if unset)
  - SIMPLEFIN_ACCESS_URL
```

Param values (defaults and per-bot overrides) are **literal values** — they are
context inputs, not re-rendered templates. Compose absolute paths inside the
template around context vars: `SNAPSHOT_DIR = "{{ data_dir }}/{{ snapshot_subdir }}"`.

### `env:` declares NAMES — and belongs in git

The `env:` list holds env var **names**, never values, and a `tool.yaml` carrying it is a
**tracked, shared** file. `SIMPLEFIN_ACCESS_URL` above is committed; only the value lives in the
gitignored `.env`.

This trips people up because the var is credential-adjacent and often fleet-specific, which makes
"put the declaration in the overlay" feel right. It is not: the name is the *interface* the
validator warns against and an operator provisions from, so it has to be visible. Same rule as
`library/mcp/*.json`, which are tracked while declaring `${GITHUB_PAT}`. Full statement of the
contract: [`documentation/environment-variables.md`](../../documentation/environment-variables.md).

A tool belongs in the shared `library/tools/` whenever the *pattern* generalizes, even if today only
one fleet sets the value. Reach for a `local/<fleet>/library/` overlay only when the tool's logic
itself is fleet-specific.

## Template context

Templates render with **StrictUndefined** — an undeclared or unset variable is
a compose-time error, never a silently-empty executable. Always available:

| var | value |
|-----|-------|
| `bot_id` / `bot_name` | the attached bot |
| `fleet_name` | the fleet |
| `bot_dir` | absolute bot directory |
| `data_dir` | absolute `bot_dir/data` — where a tool's runtime outputs belong |

plus every resolved param. Param names may not shadow these.

## Attaching (fleet.yaml)

```yaml
bots:
  mybot:
    tools:
      - audit-tracker                    # bare ref — manifest defaults
      - portfolio-snapshot:              # per-bot param overrides
          params:
            lookback_days: 30
```

The rendered script lands at `<bot_dir>/tools/<name>.py` (0755). Reference it
from skills/cron as `$BOT_DIR/tools/<name>.py` — `BOT_DIR` is exported in every
`bot.conf`.

## The two bright lines

1. **Secrets never pass through params/Jinja.** Rendered tools are 0755
   world-readable. Secrets are runtime `os.environ` reads, declared under
   `env:` so the validator can check the contract (warn-only, MCP parity).
2. **Runtime outputs go to `data/`, never `tools/`.** `tools/` is
   compositor-owned and reconciled every generate; anything a tool writes
   beside itself would be deleted. Point output paths at `{{ data_dir }}/...`.
