# library/permissions/

Access control policies for bot tool permissions. Defines which tools a bot can use automatically vs which require human approval.

## What belongs here

Permission policy files that map to Claude Code's permission model. The `access.json.template` provides the base structure — copy and customize per fleet in `local/<fleet>/`.

## Composition

The compositor applies permission settings to each bot's runtime directory. Fleet-specific overrides live in `local/<fleet>/library/permissions/`.

## Naming

Use descriptive names: `access.json.template` for the base template.
