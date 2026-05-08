# library/guardrails/

Composable safety rules that constrain bot behavior. Each guardrail is a self-contained rule set (e.g. `no-push-main`, `pii-protection`) appended to the bot's CLAUDE.md.

## What belongs here

One `.md` file per rule. Use YAML frontmatter with `title:` (and optionally `description:`), followed by an H1 heading and the rule body. Keep each file focused on a single concern.

## Composition

Guardrails accumulate: fleet-level defaults plus bot-level additions. In fleet.yaml:

```yaml
defaults:
  guardrails: [no-push-main, pii-protection]
bots:
  lead:
    guardrails: [merge-policy-human]  # added on top of defaults
```

## Naming

Lowercase, hyphenated, descriptive: `no-push-main.md`, `pii-protection.md`, `no-fabrication.md`.
