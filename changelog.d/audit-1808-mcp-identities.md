### Fixed — MCP declarations that name one server twice are refused before composition (#1808)

Two MCP declarations that expand to the same runtime server name, such as
`audit` with the instance `work` beside `audit-work`, or one instance listed
twice, used to collapse into one server: one could silently replace the other
or duplicate its grants. Composition, the MCP permission and integration-grant
builders, and validation now refuse such a pair, naming both declarations,
before any bot file is written. A manifest that has one fails until its
declarations are corrected; nothing is renamed automatically. Valid names,
environment prefixes, grants, first-seen fragment behavior and explicit empty
instance lists are unchanged.
