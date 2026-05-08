# library/mcp/

MCP server wire-config fragments — small JSON files containing one MCP server definition each. The compositor merges selected fragments into a single `.mcp.json` per bot.

## What belongs here

One `.json` file per MCP server. Use `${ENV_VAR}` placeholders for secrets — never hardcode tokens.

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PAT}" }
    }
  }
}
```

## Composition

Bots list MCP servers in fleet.yaml: `mcp: [github, notion]`. The compositor merges each fragment's `mcpServers` into the bot's `.mcp.json`. Pair with `library/integrations/<name>.md` for usage docs.

## Naming

Matches the service name: `github.json`, `notion.json`, `slack.json`.
