# Research: Claude Code Permissions Model

Research for the smart permissions compositor feature (see `docs/design-smart-permissions.md`).

Sources: Claude Code official documentation, current `~/.claude/settings.json` reference implementation, `claudlobby/composer.py` existing code.

---

## Permission Pattern Syntax

Claude Code supports these pattern formats in `permissions.allow` and `permissions.deny`:

### Plain tool names

Match any invocation of the tool, regardless of arguments.

```
Read
Write
Edit
Bash
Glob
Grep
WebFetch
WebSearch
Agent
```

`Bash` alone is equivalent to `Bash(*)` — matches all Bash commands.

### Bash command patterns

`Bash(<pattern>)` — glob-style matching against the command string.

```
Bash(git *)        — matches "git commit -m ..." but NOT "gitk"
Bash(git commit *) — matches "git commit" with any args
Bash(git * main)   — matches "git checkout main", "git merge main"
Bash(* install)    — matches any command ending with " install"
Bash(ls*)          — matches "ls -la" AND "lsof" (no space = no word boundary)
Bash(ls *)         — matches "ls -la" but NOT "lsof" (space enforces boundary)
```

**Important behaviors:**

- `:*` suffix is equivalent to trailing ` *`, so `Bash(git:*)` = `Bash(git *)`
- **Compound commands** (`&&`, `||`, `;`, `|`) — each subcommand is matched independently. A rule matching `safe-cmd *` does NOT allow `safe-cmd && dangerous-cmd`.
- **Process wrappers** are transparently stripped before matching: `timeout`, `time`, `nice`, `nohup`, `stdbuf`, bare `xargs`. But `docker exec`, `npm run`, `devbox run` are NOT stripped — include them explicitly if needed.
- When a compound command is approved interactively ("Yes, don't ask again"), Claude saves a separate rule for each subcommand (up to 5 per compound).

### MCP tool patterns

Format: `mcp__<server-name>__<tool-name>`

```
mcp__github__search_code            — specific tool
mcp__github__get_pull_request       — specific tool
mcp__puppeteer                      — all tools from server (server-level wildcard)
mcp__puppeteer__*                   — equivalent server-level wildcard
```

The server name comes from the key in `.mcp.json`'s `mcpServers` object. For multi-instance MCP (e.g., `gws` with `personal` and `work` instances), the instance name is part of the server name:

```
mcp__gws-personal__search_gmail_messages
mcp__gws-work__search_gmail_messages
```

**Plugin tools** use the plugin's namespaced server name:

```
mcp__plugin_telegram_telegram__reply
mcp__plugin_telegram_telegram__edit_message
```

**Claude.ai built-in MCP** uses `mcp__claude_ai_<ServiceName>__<tool>`:

```
mcp__claude_ai_Google_Calendar__list_events
mcp__claude_ai_Google_Drive__read_file_content
```

### Skill patterns

```
Skill(lifecycle)     — match the skill by name
Skill(lifecycle:*)   — match the skill and all sub-skills
```

Both forms are needed for a skill to work without prompting. The compositor should emit both for each skill.

### Subagent patterns

```
Agent(Explore)
Agent(Plan)
Agent(my-custom-agent)
```

### Read/Edit path-scoped patterns

Gitignore-style path matching:

```
Read(.env)                — relative to current directory (also matches at any depth)
Edit(/src/**/*.ts)        — relative to project root (leading / = project-relative)
Read(~/Documents/*)       — relative to home directory
Read(//Users/alice/file)  — absolute filesystem path (double-slash prefix)
```

**Gotcha:** `Edit(.env)` and `Edit(**/.env)` are equivalent — both match at any depth.

**Symlink behavior differs between allow and deny:**
- **Allow rules:** apply only when BOTH symlink path AND target match
- **Deny rules:** apply when EITHER symlink path OR target matches (more restrictive, safer)

**Critical limitation:** Read/Edit deny rules only block the Read/Edit tools. They do NOT block `cat .env` via Bash. For OS-level enforcement, use `sandbox.filesystem.denyRead`.

### WebFetch domain patterns

```
WebFetch(domain:example.com)  — restrict to a specific domain
```

### PowerShell patterns (Windows)

Same syntax as Bash: `PowerShell(Get-ChildItem *)`, `PowerShell(Remove-Item *)`.

---

## Allow/Deny Precedence Rules

**Evaluation order: deny → ask → allow. First match wins.**

1. If a **deny** rule matches → tool is blocked, regardless of allow rules
2. If an **ask** rule matches → user is prompted, even if allow rules would permit
3. If an **allow** rule matches → tool executes without prompting
4. If nothing matches → user is prompted (interactive) or denied (headless)

**Deny always wins.** There is no way to override a deny rule with an allow rule at the same scope. This is the key design constraint for the compositor: put broad allows at the base, and let deny rules carve out restrictions.

---

## Headless Mode Behavior

When Claude Code runs headless (e.g., via `-p` flag, tmux session with no terminal, systemd service) and encounters a tool not in the allow list:

- **No terminal available → the process hangs.** It waits indefinitely for interactive approval that can never come.
- This is the core problem the compositor feature solves. Every tool a bot might use must be pre-allowed in `settings.local.json`.
- With `--dangerously-skip-permissions`: all tools auto-allowed (except removals targeting `/` or `~`).

**Auto mode behaviors:**
- If the auto-mode classifier blocks an action 3 times in a row or 20 times total, the session aborts.
- Broad allow rules (`Bash(*)`, `PowerShell(*)`) are dropped when entering auto mode. Narrow rules like `Bash(npm test)` carry over.

**The `--dangerously-skip-permissions` flag:**
- Enables `bypassPermissions` mode for the session
- Disables ALL permission prompts and safety checks
- Tool calls execute immediately
- **Exception:** filesystem root (`/`) and home (`~`) removals still prompt as a circuit breaker
- Cannot be enabled mid-session — requires restart with the flag
- Equivalent: `--permission-mode bypassPermissions`

---

## MCP Tool Pattern Format

The exact naming convention for MCP tool permissions:

```
mcp__<server-key>__<tool-name>
```

Where:
- `<server-key>` = the key from `mcpServers` in `.mcp.json` (e.g., `github`, `notion`, `gws-personal`)
- `<tool-name>` = the tool name as exposed by the MCP server's `tools/list` response

**For the compositor, this means:**

1. The MCP fragment's `_permissions_contract.tools` lists raw tool names (e.g., `search_code`)
2. The compositor resolves the server key from the fragment name + instance:
   - Default instance: `mcp__{fragment_name}__{tool}`
   - Named instance: `mcp__{fragment_name}-{instance}__{tool}`
3. Generate one pattern per tool per instance

**Example resolution:**

```
Fragment: github.json, instances: [default]
Contract tools: [search_code, get_issue, create_pull_request]
Output: mcp__github__search_code, mcp__github__get_issue, mcp__github__create_pull_request

Fragment: gws.json, instances: [personal, work]
Contract tools: [search_gmail_messages, get_events]
Output: mcp__gws-personal__search_gmail_messages, mcp__gws-personal__get_events,
        mcp__gws-work__search_gmail_messages, mcp__gws-work__get_events
```

---

## Skill Pattern Format

Each skill needs two patterns:

```
Skill(<name>)      — matches direct invocation
Skill(<name>:*)    — matches sub-skill invocations (e.g., /skill:sub-command)
```

The compositor already knows the bot's skill list from `bot.skills`. For each skill entry, emit both patterns.

For folder-expanded skills (e.g., `skills/` which expands to all skills under a directory), the compositor must resolve the leaf names and emit patterns for each.

---

## Bash Command Patterns

For the compositor's expertise-based `bash_allow` feature:

```yaml
permissions:
  bash_allow: [git, gh, npm, npx, pip, python, make, docker, curl]
```

Generates:

```json
["Bash(git *)", "Bash(gh *)", "Bash(npm *)", "Bash(npx *)", ...]
```

**Important:** use `Bash(cmd *)` (with space) to enforce word boundaries. `Bash(git*)` would also match `gitk`, `github-cli`, etc.

**Consider also allowing common subcommand patterns:**

```
Bash(cd *)
Bash(ls *)
Bash(cat *)
Bash(head *)
Bash(tail *)
Bash(wc *)
Bash(which *)
Bash(test *)
Bash(mkdir *)
Bash(cp *)
Bash(mv *)
Bash(touch *)
Bash(chmod *)
Bash(pwd *)
Bash(diff *)
Bash(find *)
Bash(grep *)
Bash(lsof *)
```

These are the patterns from the current global settings.json. A "base" set that every bot gets.

---

## Path-Scoped Patterns

Used for sibling bot isolation (deny reading other bots' dirs):

```json
{
  "deny": [
    "Read(/home/crog/claudlobby/local/fleet/runtime/bots/sibling-a/**)",
    "Read(/home/crog/claudlobby/local/fleet/runtime/bots/sibling-b/**)"
  ]
}
```

**Current compositor behavior** (from `compose_settings_local()`):

```python
for sibling in siblings:
    sibling_dir = str(paths.bot_runtime(sibling))
    deny_patterns.append(f"Read({sibling_dir}/**)")
```

This only blocks the `Read` tool. To also block `Edit`, `Write`, and Bash access, the compositor should also emit:

```
Edit({sibling_dir}/**)
Write({sibling_dir}/**)
```

And for full OS-level isolation, use sandbox `filesystem.denyRead`/`denyWrite` paths (these are merged from all scopes, not replaced).

---

## Programmatic Generation Considerations

### Settings file merging

**Precedence (highest to lowest):**
1. Managed settings (enterprise, cannot be overridden)
2. Command-line arguments (temporary session)
3. `.claude/settings.local.json` (project-local, gitignored) ← **compositor writes here**
4. `.claude/settings.json` (project-shared, committed)
5. `~/.claude/settings.json` (user-global)

**Array merging rules:**
- **Permission arrays (`allow`, `deny`):** replaced by lower-precedence scope, NOT merged. If settings.local.json defines `deny: [...]`, it completely overrides any parent deny list.
- **Sandbox path arrays** (`filesystem.allowWrite`, `denyWrite`, `denyRead`): **merged** across all scopes.

**Implication for the compositor:** The `settings.local.json` permissions block must be self-contained. It cannot rely on patterns from `~/.claude/settings.json` because the local file's `allow` list replaces (not extends) the global one.

This is the single most important design constraint. The compositor must emit a **complete** allow list in `settings.local.json`, including base tools, MCP tools, skills, and channel tools. It cannot assume anything from the global settings will carry through.

### Current compositor output

`compose_settings_local()` currently generates:

```json
{
  "autoMemoryDirectory": "<bot_dir>/memory",
  "permissions": {
    "deny": [
      "Read(<sibling_dir>/**)"
    ],
    "allow": [
      "<tool>(**)"
    ]
  },
  "sandbox": { ... },
  "hooks": { ... }
}
```

**Issue with current pattern format:** The compositor appends `(**)` to tool names from `bot.tools.allow/deny`:

```python
permissions["allow"] = [f"{tool}(**)" for tool in bot.tools.allow]
```

This is incorrect for plain tool names. `Read(**)` is not the same as `Read` — the former is a path-scoped pattern. For tools without path semantics (Bash, Agent, WebFetch), the `(**)` suffix is meaningless noise. The compositor should emit:

- Plain tool names for non-path tools: `Bash`, `Agent`, `WebFetch`
- Path-scoped only where meaningful: `Read(/path/**)`, `Edit(/path/**)`
- Bash patterns for command restrictions: `Bash(git *)`

### Hook matcher syntax

Hooks use a similar but distinct pattern language:

```json
{
  "matcher": "Bash",           // tool name
  "matcher": "Write|Edit",     // pipe-separated alternation
  "matcher": "mcp__github__.*" // regex (must match full tool name)
}
```

The `if` field (v2.1.85+) supports permission-rule syntax for finer filtering:

```json
{
  "matcher": "Bash",
  "hooks": [{
    "if": "Bash(git *)",
    "type": "command",
    "command": "check-git-policy.sh"
  }]
}
```

### Schema reference

JSON Schema for Claude Code settings is published at:
`https://json.schemastore.org/claude-code-settings.json`

---

## Reference: Current settings.json Patterns (Categorized)

Extracted from `~/.claude/settings.json` on the fleet host.

### Base tools (9)

```
Bash
Edit
Glob
Grep
Read
WebFetch
WebSearch
Write
```

### Bash command patterns (21)

```
Bash(cat *)
Bash(chmod *)
Bash(cp *)
Bash(curl *)
Bash(diff *)
Bash(find *)
Bash(gh *)
Bash(git *)
Bash(grep *)
Bash(head *)
Bash(ls *)
Bash(lsof *)
Bash(mkdir *)
Bash(mv *)
Bash(pwd *)
Bash(tail *)
Bash(test *)
Bash(touch *)
Bash(wc *)
Bash(which *)
```

### Skill patterns (14 = 7 skills x 2)

```
Skill(context-resume)
Skill(context-resume:*)
Skill(docs-review)
Skill(docs-review:*)
Skill(frontend-performance-audit)
Skill(frontend-performance-audit:*)
Skill(product-enhance)
Skill(product-enhance:*)
Skill(security-audit)
Skill(security-audit:*)
Skill(session-handoff)
Skill(session-handoff:*)
Skill(tech-debt)
Skill(tech-debt:*)
```

### MCP tools by server

**claude_ai_Google_Calendar** (8):
`list_events`, `create_event`, `delete_event`, `find_meeting_times`, `find_my_free_time`, `get_event`, `list_calendars`, `respond_to_event`, `update_event`

**claude_ai_Gmail** (5):
`search_messages`, `read_message`, `read_thread`, `create_draft`, `get_profile`, `list_drafts`, `list_labels`

**notion** (18):
`API-query-data-source`, `API-retrieve-a-database`, `API-retrieve-a-page`, `API-retrieve-a-page-property`, `API-post-search`, `API-post-page`, `API-patch-page`, `API-get-block-children`, `API-retrieve-a-block`, `API-update-a-block`, `API-delete-a-block`, `API-patch-block-children`, `API-create-a-comment`, `API-retrieve-a-comment`, `API-get-self`, `API-get-user`, `API-get-users`, `API-move-page`, `API-create-a-data-source`, `API-retrieve-a-data-source`, `API-update-a-data-source`, `API-list-data-source-templates`

**github** (25):
`search_code`, `search_issues`, `search_repositories`, `search_users`, `get_file_contents`, `get_issue`, `get_pull_request`, `get_pull_request_comments`, `get_pull_request_files`, `get_pull_request_reviews`, `get_pull_request_status`, `list_commits`, `list_issues`, `list_pull_requests`, `create_branch`, `create_issue`, `create_or_update_file`, `create_pull_request`, `create_pull_request_review`, `create_repository`, `fork_repository`, `push_files`, `add_issue_comment`, `update_issue`, `update_pull_request_branch`, `merge_pull_request`

**homeassistant** (11):
`call_service_tool`, `domain_summary_tool`, `entity_action`, `get_entity`, `get_error_log`, `get_history`, `get_version`, `list_automations`, `list_entities`, `restart_ha`, `search_entities_tool`, `system_overview`

**docker** (16):
`list_containers`, `list_images`, `list_networks`, `list_volumes`, `fetch_container_logs`, `start_container`, `stop_container`, `create_container`, `remove_container`, `recreate_container`, `run_container`, `build_image`, `pull_image`, `push_image`, `remove_image`, `create_network`, `remove_network`, `create_volume`, `remove_volume`

**plugin_telegram_telegram** (4):
`reply`, `edit_message`, `react`, `download_attachment`

**gws-personal** (22):
`search_gmail_messages`, `get_gmail_message_content`, `get_gmail_messages_content_batch`, `get_gmail_thread_content`, `get_gmail_threads_content_batch`, `get_gmail_attachment_content`, `draft_gmail_message`, `send_gmail_message`, `list_gmail_labels`, `list_gmail_filters`, `manage_gmail_label`, `manage_gmail_filter`, `modify_gmail_message_labels`, `batch_modify_gmail_message_labels`, `get_events`, `manage_event`, `list_calendars`, `create_calendar`, `query_freebusy`, `manage_focus_time`, `manage_out_of_office`, `start_google_auth`

**gws-work** (22 — same tool set as gws-personal):
Same 22 tools, namespaced under `mcp__gws-work__`.

**slack** (11):
`channels_list`, `conversations_add_message`, `conversations_history`, `conversations_mark`, `conversations_replies`, `conversations_search_messages`, `conversations_unreads`, `usergroups_create`, `usergroups_list`, `usergroups_me`, `usergroups_update`, `usergroups_users_update`, `users_search`

**granola** (5):
`get_meeting_transcript`, `get_meetings`, `list_meeting_folders`, `list_meetings`, `query_granola_meetings`

**spotify** (5):
`SpotifyGetInfo`, `SpotifyPlayback`, `SpotifyPlaylist`, `SpotifyQueue`, `SpotifySearch`

---

## Key Findings for Compositor Implementation

1. **settings.local.json replaces (not merges) permission arrays from global settings.** The compositor must emit a complete, self-contained allow list. This is the #1 design constraint.

2. **The current `f"{tool}(**)"` pattern in `compose_settings_local()` is wrong.** Plain tool names should be emitted as-is (`Read`, not `Read(**)`). Path-scoped patterns are only meaningful for Read/Edit/Write.

3. **MCP tool contracts are the highest-value Phase 1 target.** Each MCP fragment can declare its tools; the compositor resolves instance-qualified patterns. This eliminates the most painful manual work (10-30 tool names per MCP server).

4. **Skill patterns always need both forms.** `Skill(name)` + `Skill(name:*)`. Folder-expanded skills must be resolved to leaf names.

5. **Telegram plugin tools should auto-include when `bot.telegram.handle` is set.** The 4 plugin tools are static and well-known.

6. **Sandbox paths merge but permission arrays replace.** This means sandbox filesystem restrictions can be layered (global + local), but permission allow/deny must be complete at each scope.

7. **Deny always wins.** The composition order in the design doc (base → expertise → MCP → channel → skill → fleet defaults → bot overrides) is correct, but deny should be accumulated across all layers, never overridden by a later allow.
