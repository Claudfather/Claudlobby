### Added — skills and integrations can declare `ask_grants` and `deny_grants` (#2476)

A skill's or an integration's frontmatter may now list `ask_grants:` and
`deny_grants:` beside `tool_grants:`, each entry one Claude Code permission
rule. A list that would not compose as written, a value that is not a list or
an entry that is not a rule, is an error that names its source, since a rule
dropped from either list fails open. An ask rule lands in the composed
`permissions.ask`: Claude Code asks before a matching call in every permission
mode, auto included, even where an allow rule also matches. The fleet grants
one by naming the same rule, exactly, under `tool_permissions.allow`, and the
composer then drops it from the ask list. A deny rule lands in
`permissions.deny`, where no allow rule overrides it. On an integration, an
`mcp__<name>__` rule in either list names each server instance, as its
`tool_grants` do. The `gitguardian` skill now denies hand-written `curl` and
`wget` calls to `gitguardian.com`, so its helper stays the way in; it declares
no ask rule, since the helper has no write. `bot capabilities` lists the
composed ask rules. `settings.local.json` gains an `ask` list only when a rule
survives, and no shipped item declares one, so no bot without the
`gitguardian` skill composes differently.
