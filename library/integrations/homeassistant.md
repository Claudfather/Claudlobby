### Home Assistant

Wire config: `library/mcp/homeassistant.json` (uses `${HA_URL}`, `${HA_TOKEN}`).

**Skill:** `/home` — control devices, check room status, get home reports.

**Common Ops:**
- `mcp__homeassistant__get_entity` — get state of a device/sensor (e.g., `sensor.user_phone_battery_level`, `person.user_name`)
- `mcp__homeassistant__search_entities_tool` — find entities by keyword
- `mcp__homeassistant__call_service_tool` — turn on/off lights, switches, etc.
- `mcp__homeassistant__list_entities` — list all entities in a domain
- `mcp__homeassistant__system_overview` — high-level status of the home
- `mcp__homeassistant__get_history` — historical state for a sensor/device

**Use cases:**
- Weather context for briefings → get `person.user_name` for location (home/not_home) + `sensor.user_phone_battery_level`
- Smart home control → lights, switches, automations via call_service
- Location awareness → `person.user_name` state drives location-based decisions

**Gotchas:**
- Entity IDs use `domain.name` format (e.g., `light.living_room`, `sensor.temperature`)
- HA API is local network — requires Nabu Casa or direct LAN access
- State values are strings — "on"/"off", not booleans
