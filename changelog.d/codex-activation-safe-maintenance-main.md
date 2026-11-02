### Fixed — preserve verified checkpoints through host activation

Host activation verifies each live bot's checkpoint and quiesces that bot before requesting the next checkpoint. Interrupted activation retains exact capture and stop witnesses without resending unknown handoffs, and a verified quiet stop can retain its capture without a new freshness deadline. Named Claude account paths now resolve home shorthand consistently in composed startup configuration.
