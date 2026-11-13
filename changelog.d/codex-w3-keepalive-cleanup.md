### Fixed — Accept native KeepAlive recorder cleanup witnesses

Recognize the observed launchd `keepalive` property when checking owned job children, so a committed KeepAlive recorder can reach the public stop flow. Unknown, duplicate and abandoning child-policy tokens still refuse; configuration, native ownership and teardown checks are unchanged. Hold original native definition and enablement-link descriptors through shutdown, so same-byte replacements cannot be mistaken for owned nodes after inode reuse.
