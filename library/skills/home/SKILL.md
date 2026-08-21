---
name: home
description: "Use when the user wants to control smart home devices, check room status, or get a home report. Controls lights, switches, sensors, and automations via Home Assistant."
argument-hint: "[room|device|report|scene] [action]"
---

# Home

Smart home control and reporting via Home Assistant.

## Tools

| Tool | Purpose |
|------|---------|
| `mcp__homeassistant__list_entities` | List all entities |
| `mcp__homeassistant__get_entity` | Get entity state/details |
| `mcp__homeassistant__entity_action` | Control a device (turn on/off, set brightness, etc.) |
| `mcp__homeassistant__call_service_tool` | Call any HA service |
| `mcp__homeassistant__search_entities_tool` | Search entities by name/type |
| `mcp__homeassistant__domain_summary_tool` | Summary by domain |
| `mcp__homeassistant__list_automations` | List automations |
| `mcp__homeassistant__get_history` | Get entity history |
| `mcp__homeassistant__system_overview` | HA system overview |

## Rooms & Devices

This section is a *template*. Replace the example rooms/devices below with your actual Home Assistant inventory before deploying. Discover entities at runtime via `mcp__homeassistant__list_entities` or `mcp__homeassistant__domain_summary_tool`.

### Example: Primary Bedroom
- **Lights**: `light.<bedroom_main>`, `light.<gradient_strip>`, `light.<accent>`
- **Power strip**: bed lamps, bedside plug, phone charger
- **Speakers**: bedroom group
- **Sensors**: temp/humidity sensor

### Example: Studio/Office
- **Lights**: `light.<rgb_1>`, `light.<rgb_2>`
- **Power strip**: desk hub, speakers, fan
- **Speakers**: studio speakers

### Example: Living Room
- **Speakers**: living-room speaker group
- **Plugs / switches**: `switch.<tv_accent>`, `switch.<lamp>`
- **Media**: TV, whole-home audio

### Other
- **Network**: `<router>` (WAN status, speed, external IP)
- **Presence**: `<HA_PERSON>` (`<HA_DEVICE_TRACKER>`)
- **Automations**: list via `mcp__homeassistant__list_automations`
- **Climate**: thermostats if integrated

## Operations

### 1. Room Status

Quick check what's on in a room:

```
Tool: mcp__homeassistant__search_entities_tool
query: "bedroom" (or room name)
```

Then get state for each matched entity.

### 2. Control Devices

Natural language → entity action:

**Lights:**
```
Tool: mcp__homeassistant__entity_action
entity_id: light.bedroom_main
action: turn_on
data: {"brightness": 128}  // 0-255, 128 = 50%
```

```
Tool: mcp__homeassistant__entity_action
entity_id: light.bedroom_main
action: turn_off
```

**Switches/Plugs:**
```
Tool: mcp__homeassistant__entity_action
entity_id: switch.living_room_plug_1
action: turn_on
```

**Color:**
```
Tool: mcp__homeassistant__entity_action
entity_id: light.<color_panels>
action: turn_on
data: {"rgb_color": [255, 100, 50], "brightness": 200}
```

### 3. Home Report

Full status of all devices — what's on, what's off, any issues:

```
Tool: mcp__homeassistant__domain_summary_tool
domain: "light"
```

```
Tool: mcp__homeassistant__domain_summary_tool
domain: "switch"
```

```
Tool: mcp__homeassistant__domain_summary_tool
domain: "media_player"
```

```
Tool: mcp__homeassistant__domain_summary_tool
domain: "sensor"
```

Run all in parallel, then summarize.

### 4. Scenes / Bulk Control

"Turn off everything":
```
Tool: mcp__homeassistant__call_service_tool
domain: "light"
service: "turn_off"
entity_id: "all"
```

"Movie mode" (dim lights, TV on):
- Set living room lights to 20%
- Turn off other room lights
- Ensure TV is on

### 5. Automations

List and check automation status:
```
Tool: mcp__homeassistant__list_automations
```

### 6. History / Energy

Check device usage over time:
```
Tool: mcp__homeassistant__get_history
entity_id: "switch.living_room_plug_1"
```

### 7. System Health

```
Tool: mcp__homeassistant__system_overview
```

```
Tool: mcp__homeassistant__get_version
```

## Natural Language Mapping

| User says | Action |
|-----------|--------|
| "turn off the lights" | Turn off all lights |
| "dim the bedroom" | Set bedroom lights to 30% |
| "what's on?" | Home report — list all on devices |
| "turn off bedroom" | Turn off all bedroom entities |
| "set living room to warm" | Set color temp to warm white (~3000K) |
| "movie mode" | Dim living room, off other rooms |
| "goodnight" | Turn off everything |
| "is the TV on?" | Check the TV media_player state |

## Output Formatting

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

**Home report:**
```
HOME

Bedroom: all off
Living Room: light strip on (45%), TV off, Plug 1 on
Kitchen: ceiling light on (100%)
Office: accent panels on (rainbow), desk lamp off
Bathroom: all off

Automations: 3 active, 0 triggered today
```

**Single action:**
```
Done — bedroom lights off.
```

**Device not found:**
```
Couldn't find "kitchen fan" — closest matches: kitchen_ceiling, kitchen_plug_1. Which one?
```

## Instructions

1. For **natural language control**: map user intent to entity_action calls. Search for entities if unsure of exact entity_id.
2. For **room control**: search entities by room name, then control all matches.
3. For **home report**: query all domains in parallel, format by room.
4. When a device name is ambiguous, search and ask for clarification.
5. Confirm destructive actions ("Turn off ALL lights in the house?") before executing.
6. Keep responses short — "Done — bedroom lights set to 50%."

$ARGUMENTS
