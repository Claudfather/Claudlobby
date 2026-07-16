---
title: Docker
type: mcp
tool_grants:
  - "mcp__docker__*"
---

# Docker

Wire config: `library/mcp/docker.json`.

### Common ops

- List containers: `mcp__docker__list_containers`
- View logs: `mcp__docker__fetch_container_logs`
- Start/stop: `mcp__docker__start_container`, `mcp__docker__stop_container`
- Build image: `mcp__docker__build_image`

### Gotchas

- Docker daemon must be running on the host — if tools return connection errors, check `docker info`
- Container names must be unique — `create_container` fails silently on name collision
- `remove_container` requires the container to be stopped first
