# projects.yaml Schema Reference

`projects.yaml` is the third config tier — **WHAT the work is, and what
"done" requires per project** (`system.yaml` = HOW the platform runs,
`fleet.yaml` = WHO the bots are). It is optional and sits beside
`fleet.yaml`: overlay mode `local/<fleet>/projects.yaml`, root mode
`<root>/projects.yaml`. Copy `projects.yaml.example` to get started.

Validated by `claudlobby validate` (bad tiers, empty repos, unknown keys —
all with did-you-mean suggestions). Composed by `claudlobby generate`.

## What composition emits

- **Every bot's `bot.conf`** gets the full repo→tier map:
  `PROJECT_TIER_<SLUG>` and `PROJECT_REPOS_<SLUG>` per project (slug = the
  project key upper-cased, `-`→`_`). Any sprint or autonomous-runner bot
  resolves a working repo's closure bar locally — there is no "sprint owner".
- **Manager CLAUDE.md** gets a `## Projects` table (key, title, repos, tier,
  mission pointer). Workers rely on the env map, not prose (context budget).

## Fields

```yaml
projects:
  <project-key>:            # lowercase kebab-case [a-z][a-z0-9-]* — becomes the env slug
    title: <string>         # human label (defaults to the key)
    repos: [<org>/<repo>]   # REQUIRED, non-empty — the join key mapping work to this project
    mission_file: <path>    # optional; relative to this file's directory (stays out of git)
    validation:             # optional; the closure bar (default tier: review)
      tier: auto | review | preview | human
      preview:              # free-form details for the preview tier
        source: <string>    #   e.g. vercel — where preview links come from
        require_ack: <bool> #   hold the close until the operator acks in Telegram
      notes: <string>       # rationale, shown to reviewers of this config
```

## Validation tiers

| Tier | Work in this project's repos may close when… |
|---|---|
| `auto` | CI is green |
| `review` | a reviewer verdict marker approves (default) |
| `preview` | a preview link was posted to Telegram **and** the operator acked |
| `human` | the operator explicitly approved |

A repo claimed by two projects draws a validator warning — tier resolution
would be ambiguous. A repo matching no project resolves to the fleet default
tier, loudly (`tier:default(<tier>)` in the terminal report; see the
goal-aware-fleet plan).

## Reserved

`metrics:` is reserved for the metrics plan and is **not** part of the v1
schema — declaring it is ignored with a validator warning. See the commented
block in `projects.yaml.example` for the intended shape.
