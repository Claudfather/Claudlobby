### Fixed — Accept native KeepAlive recorder cleanup witnesses

Recognize the observed launchd `keepalive` property when checking owned job children, so a committed KeepAlive recorder can reach the public stop flow. Unknown, duplicate and abandoning child-policy tokens still refuse; configuration, native ownership and teardown checks are unchanged.
