### Changed — the root `CLAUDE.md` keeps every rule and moves its reference text behind pointers (#2273)

A Claude Code session attaches the repository's root `CLAUDE.md` whole the first time it reads a file inside a Claudlobby checkout, and carries it on every later call. Its rules stay word for word. Its reference text moves, word for word, behind four pointers:

- what claudlobby is beside clauDNA and Claudron, the architecture diagram, six of the eight key concepts, and bot supervision with the canonical task commands, to `documentation/architecture/overview.md`;
- how Claude Code and Codex load instruction files, to a new `documentation/instruction-files.md`;
- the command list, to a new `documentation/key-commands.md`. The reboot snapshot and the test setup stay, because they carry rules.

The file goes from 28,903 to 23,150 bytes, and from 11,040 to 8,957 tokens by Claude Code's own `/context`. A session that reads a file in a checkout carries 2,083 fewer tokens on every later call. Codex's tightest instruction chain gains the same bytes of room: 86 bytes are left of its 32 KiB on main, and 5,839 after this change. Whether sessions follow the rules as well with this text behind a pointer is not measured. `AGENTS.md` stays a byte copy.
