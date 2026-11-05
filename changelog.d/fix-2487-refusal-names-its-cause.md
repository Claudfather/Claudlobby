### Fixed — a `bot` or `fleet` lifecycle refusal names what stopped it (#2487)

When a `bot` or `fleet` lifecycle command (`bot start`, `fleet reconcile` and the rest) could not read the host's native supervision state, it refused with "native state cannot be established" and exit 6, and dropped the supervision inventory's or the operating system's own message. A unit file removed while its `default.target.wants` link still pointed at it then refused `bot start`, with nothing to say which unit. The refusal now ends with that message, and the JSON result also carries it alone as `data.cause`.
