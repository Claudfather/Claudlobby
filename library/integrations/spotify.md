---
title: Spotify
type: mcp
tool_grants:
  - "mcp__spotify__*"
---

# Spotify

Wire config: `library/mcp/spotify.json` (uses `${SPOTIFY_CLIENT_ID}`, `${SPOTIFY_CLIENT_SECRET}`).

### Common ops

- Search: `mcp__spotify__SpotifySearch`
- Playback control: `mcp__spotify__SpotifyPlayback`
- Queue management: `mcp__spotify__SpotifyQueue`
- Playlist operations: `mcp__spotify__SpotifyPlaylist`
- Get info: `mcp__spotify__SpotifyGetInfo`

### Gotchas

- Requires an active Spotify Premium account for playback control
- OAuth flow required on first use
- Playback control requires an active device — if no device is playing, commands may fail
