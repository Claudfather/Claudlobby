---
title: Home Assistant
tool_grants:
  - "mcp__homeassistant__domain_summary_tool"
  - "mcp__homeassistant__get_dashboard_config"
  - "mcp__homeassistant__get_entities_by_area"
  - "mcp__homeassistant__get_entity"
  - "mcp__homeassistant__get_error_log"
  - "mcp__homeassistant__get_history"
  - "mcp__homeassistant__get_history_range"
  - "mcp__homeassistant__get_statistics"
  - "mcp__homeassistant__get_statistics_range"
  - "mcp__homeassistant__get_version"
  - "mcp__homeassistant__list_automations"
  - "mcp__homeassistant__list_dashboard_backups"
  - "mcp__homeassistant__list_dashboards"
  - "mcp__homeassistant__list_entities"
  - "mcp__homeassistant__list_view_sections"
  - "mcp__homeassistant__search_entities_tool"
  - "mcp__homeassistant__system_overview"
---

# Home Assistant

Wire config: `library/mcp/homeassistant.json` (uses `${HA_URL}`, `${HA_TOKEN}`). It runs the community server hass-mcp (github.com/voska/hass-mcp), pinned to 0.6.0, over stdio through `uvx`. HA's built-in MCP server, the "Model Context Protocol Server" integration at `/api/mcp`, is not used: its tools are HA's Assist intents (`HassTurnOn`, `GetLiveContext` and the like), whose names change with the entities HA exposes, and it has no error log, automation list, history or arbitrary service call.

**Skill:** `/home` — control devices, check room status, get home reports.

**Common Ops:**
- `mcp__homeassistant__get_entity` — state of one entity (`entity_id`; `detailed: true` returns every attribute)
- `mcp__homeassistant__search_entities_tool` — find entities by keyword (`query`)
- `mcp__homeassistant__entity_action` — `action` is `on`, `off` or `toggle`; service data such as `brightness` goes in `params`
- `mcp__homeassistant__call_service_tool` — any service: `domain`, `service`, and `data` holding the `entity_id`
- `mcp__homeassistant__list_entities` — entities, filtered by `domain` or `search_query`
- `mcp__homeassistant__system_overview` — high-level status of the home
- `mcp__homeassistant__get_history` — an entity's state history (`hours`, default 24)
- `mcp__homeassistant__get_error_log` — HA's error log, filtered by `level`, `integration` or `search_term`

`entity_action` and `call_service_tool` change devices, so they prompt unless the bot's fleet.yaml grants them (below). The other six only read.

**What a host installs:** `uv`, which provides `uvx`. Nothing is added to HA. hass-mcp 0.6.0 needs Python 3.13 or newer, and uv downloads one on first run when the host has none. `claudlobby host cache warm` fetches the package ahead of the first session; without it, the first session start downloads it.

**What runs without a prompt:** the 17 tools that only read, each granted by its exact name, never the whole server. They are `read_only_tools` in `library/mcp/homeassistant.json`, mirrored in `tool_grants` above: `domain_summary_tool`, `get_dashboard_config`, `get_entities_by_area`, `get_entity`, `get_error_log`, `get_history`, `get_history_range`, `get_statistics`, `get_statistics_range`, `get_version`, `list_automations`, `list_dashboard_backups`, `list_dashboards`, `list_entities`, `list_view_sections`, `search_entities_tool` and `system_overview`. The other 12 change HA, and they prompt: `entity_action` and `call_service_tool`, which control devices; `restart_ha`; and nine tools that rewrite dashboards: `set_dashboard_config`, `add_card`, `update_card`, `remove_card`, `move_card`, `add_view`, `update_view`, `remove_view` and `restore_dashboard`.

**A bot that controls devices on its own** needs the two device-control tools granted in its fleet.yaml. No library file can grant them: compose refuses any library grant beyond the read set.

```yaml
bots:
  <bot>:
    tool_permissions:
      allow:
        - mcp__homeassistant__entity_action
        - mcp__homeassistant__call_service_tool
```

Without that grant, a headless bot waits on its first device-control prompt. Leave `restart_ha` and the dashboard tools prompting: each one restarts HA or rewrites a dashboard in a single call, so do those deliberately, by answering the prompt or through the REST API below.

**What the token can reach:** the narrow grant takes the one-step destructive tools out of what runs unprompted. It is not a hard boundary. `call_service_tool` calls any service, `homeassistant.restart` included, and every tool acts with the full reach of the token's HA user: with an admin's token, that is all of HA, dashboards included. Issue the token from an HA user whose reach you want the bot to have.

**Check it:**
- URL and token: `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/"` prints `200`.
- The server: in a session, `/mcp` lists `homeassistant` as connected and `get_version` answers. Outside one, start `uvx --from hass-mcp==0.6.0 hass-mcp` with `HA_URL` and `HA_TOKEN` set, and send an MCP `initialize`, then `tools/list`, on its stdin: 0.6.0 lists 29 tools, the ones `_permissions_contract.tools` names.

**When the server does not connect, use HA's REST API** with the same token (`-H "Authorization: Bearer $HA_TOKEN"`):
- states: `GET $HA_URL/api/states`, or `GET $HA_URL/api/states/<entity_id>` for one
- a service: `POST $HA_URL/api/services/<domain>/<service>` with a JSON body such as `{"entity_id": "light.living_room"}`
- the error log: `GET $HA_URL/api/error_log`
- integrations: `GET $HA_URL/api/config/config_entries/entry`

**Use cases:**
- Weather context for briefings → get `person.user_name` for location (home/not_home) + `sensor.user_phone_battery_level`
- Smart home control → lights, switches, automations via call_service
- Location awareness → `person.user_name` state drives location-based decisions

**Gotchas:**
- Entity IDs use `domain.name` format (e.g., `light.living_room`, `sensor.temperature`)
- HA API is local network — requires Nabu Casa or direct LAN access
- State values are strings — "on"/"off", not booleans
- A new hass-mcp release can add or rename tools. Moving the pin means checking its `tools/list` against `_permissions_contract.tools`, this page and the `/home` skill. A tool the new release adds prompts until it is listed in `read_only_tools` and `tool_grants`, and only a tool whose source shows it cannot change anything belongs there.
