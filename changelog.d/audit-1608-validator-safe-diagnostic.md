### Fixed — package-check warnings no longer repeat the env resolver's error text (#1608)

When `config validate` could not read whether the MCP package probe is
armed, its warning quoted the `.env` tier resolver's exception text, which
can carry values from the resolver's stderr or from a malformed row. The
warning now says that tier resolution failed and names the script to
check, `claudlobby/_runtime_scripts/env-tiers.sh`, without that text. It
keeps its `mcp-unchecked` category, and the registry check still reports
that it did not run.
