---
name: weather
description: "Use when the user asks about weather, temperature, rain, or conditions — uses HA phone GPS for location-aware forecasts. Also used by /briefing for weather context."
argument-hint: "[today|tomorrow|hourly|week]"
---

# Weather

Location-aware weather using Home Assistant phone GPS + Weather.gov API (free, no API key).

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

## Step 1: Get Location from Home Assistant

```
Tool: mcp__homeassistant__get_entity
entity_id: <HA_PERSON>
detailed: true
```

Extract `latitude` and `longitude` from attributes. Also note the `state` (home/not_home) for context.

## Step 2: Get Weather.gov Grid Point

The Weather.gov API requires a two-step process: first resolve coordinates to a grid point, then fetch the forecast.

```bash
curl -s -H "User-Agent: claude-assistant" "https://api.weather.gov/points/{lat},{lon}"
```

From the response, extract:
- `properties.forecast` — daily forecast URL
- `properties.forecastHourly` — hourly forecast URL
- `properties.relativeLocation.properties.city` — nearest city name
- `properties.relativeLocation.properties.state` — state

**Cache note:** The grid point for a given lat/lon doesn't change. If the location hasn't moved significantly (same city), you can reuse the forecast URLs without re-resolving.

## Step 3: Fetch Current Observations + Forecast (parallel)

**Current conditions (always fetch — this is the actual temperature right now):**

First get the nearest station from the grid point response (`properties.observationStations`), then fetch latest observation from the first station:
```
WebFetch or curl: {observationStations_url}
→ extract first station ID (e.g., "KXXX")
→ fetch: https://api.weather.gov/stations/{station_id}/observations/latest
```

Extract from observation:
- `properties.temperature.value` — in Celsius, convert to Fahrenheit: `(C × 9/5) + 32`
- `properties.textDescription` — current conditions ("Clear", "Rain", etc.)
- `properties.windSpeed.value` — in km/h, convert to mph: `km/h × 0.621`
- `properties.timestamp` — when the observation was taken

**IMPORTANT:** The daily forecast returns the HIGH for the period, not the current temperature. Always use observations for "right now" and forecast for "today's high" / "tonight's low".

**Daily forecast:**
```bash
curl -s -H "User-Agent: claude-assistant" "{forecast_url}"
```

Returns periods: "This Afternoon", "Tonight", "Monday", "Monday Night", etc. Each has: temperature, temperatureUnit, shortForecast, detailedForecast, windSpeed, windDirection, probabilityOfPrecipitation.

**Hourly forecast (when `hourly` argument):**
```bash
curl -s -H "User-Agent: claude-assistant" "{forecastHourly_url}"
```

Returns hour-by-hour for the next 7 days. Limit to next 12 hours for display.

## Step 4: Get HA Context (parallel with Step 2-3)

Fetch these in parallel with the weather API calls:

```
Tool: mcp__homeassistant__get_entity
entity_id: sensor.<your_phone>_battery_level
```

This adds useful context (battery level) to the weather report.

## Output Format

Send via `mcp__plugin_telegram_telegram__reply` to chat_id `<your-chat-id>` with `format: "markdownv2"`.

### Default (current + today + tonight + tomorrow)

```
🌤️ *WEATHER* — <HOME_CITY>

*Right Now* \(observed 8:51 AM\)
━━━━━━━━━━━━
34°F — Clear ☀️
Wind: 14 mph
📱 Battery: 100%

*Today*
━━━━━━━━━━━━
• High: 50°F — Sunny
• Tonight: 35°F — Frost likely

*Tomorrow*
━━━━━━━━━━━━
• Thursday: 54°F — Mostly Sunny
• Thursday Night: 37°F — Mostly Clear
```

### Hourly View

```
🌤️ *WEATHER* — <HOME_CITY> \(hourly\)

• 2PM: 53°F 🌧️ Rain Showers
• 3PM: 55°F 🌧️ Rain Showers
• 4PM: 55°F 🌧️ Rain Showers
• 5PM: 54°F 🌧️ Rain Showers
• 6PM: 54°F 🌧️ Chance Rain
• 7PM: 51°F ☁️ Cloudy
• 8PM: 49°F ☁️ Cloudy
• 9PM: 47°F ☁️ Cloudy
```

### Weather Emoji Mapping

Use these based on the shortForecast text:
- ☀️ Sunny, Clear
- 🌤️ Mostly Sunny, Partly Sunny
- ⛅ Partly Cloudy
- ☁️ Mostly Cloudy, Cloudy, Overcast
- 🌧️ Rain, Rain Showers, Drizzle
- ⛈️ Thunderstorms
- 🌨️ Snow, Snow Showers, Flurries
- 🌫️ Fog, Haze, Mist
- 💨 Windy (wind > 25 mph)

### Location Context

If `<HA_PERSON>` state is "not_home", note the resolved city in the header. If "home", just show "Home" or the home city name.

## Arguments

| Argument | Behavior |
|----------|----------|
| (none) / `today` | Current period + tonight + tomorrow |
| `tomorrow` | Tomorrow day + night only |
| `hourly` | Next 12 hours, hour by hour |
| `week` | Next 7 day/night periods |

## Instructions

1. **Always get fresh location** — don't cache GPS coords across calls, the user moves
2. **Parallel queries** — fetch HA location, then Weather.gov grid + forecast in sequence (grid depends on coords), but fetch battery in parallel
3. **Handle Weather.gov errors gracefully** — the API occasionally returns 500s. If it fails, retry once, then report "Weather data temporarily unavailable"
4. **Concise** — this is a quick-glance weather check, not a detailed meteorology report
5. **Precipitation probability** — mention it when > 50% (e.g., "70% chance of rain")
6. Send via Telegram to chat_id `<your-chat-id>`

## Usage by Other Skills

`/briefing` uses the same Weather.gov pattern. The briefing one-liner should show CURRENT observed temp + today's high + tonight:
```
🌤️ Currently 34°F Clear | High 50°F Sunny | Tonight 35°F Frost
```
Always use observations for "currently" — never show the forecast high as the current temperature.

$ARGUMENTS
