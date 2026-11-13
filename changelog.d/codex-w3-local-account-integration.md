### Fixed — custom default account selection

Generated bot.conf now selects a custom fleet default account directory, using the same expanded and quoted path as account settings composition. Conventional ~/.claude defaults retain implicit behavior, and explicit bot environment overrides keep their precedence.

### Fixed — Accept native KeepAlive recorder cleanup witnesses

Recognize the observed launchd `keepalive` property when checking owned job children, so a committed KeepAlive recorder can reach the public stop flow. Unknown, duplicate and abandoning child-policy tokens still refuse; configuration, native ownership and teardown checks are unchanged. Hold original native definition and enablement-link descriptors through shutdown, so same-byte replacements cannot be mistaken for owned nodes after inode reuse.

### Added — Local first-team setup without a repository connection
- Offer a locally approved existing-project first-team template without GitHub or MCP access, retaining managed preview/launch/recovery and explicit native/provider approval gates. Repository setup remains compatible; native/browser local-template qualification is pending.

### Fixed — Resolve managed source for isolated Darwin job cleanup

Bind the child-policy reader to the selected package source before its isolated Python import. Managed Git installations can then perform the existing fail-closed job witness and public cleanup checks without requiring an editable core install or ambient Python paths.

### Fixed — Wait for macOS job unload after public supervision stop

Wait for the exact macOS job to unload after a successful public supervision stop before checking its remaining process and endpoint witnesses. Delayed unload is bounded; an unavailable observation or a job that remains loaded preserves the installed definition and reports an uncertain attempted stop.
