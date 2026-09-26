# Generate output inventory guard

`tests/test_generate_output_inventory.py` calls the real `cmd_generate` and
generation writers in a synthetic two-bot fleet. Its finite, independently
enumerated path/operation set fails when a writer adds, drops or changes an
observed output operation. It does not derive expectations from the writers or
silently accept every file under a known directory.

The four measured scenarios are fresh and stale generation, each for the whole
fleet and for `--bot alpha`. The stale case starts with a real full generation,
then seeds retired generated outputs and operator-owned content. The fixture has
an App-identity bot, a non-App bot, a library skill and tool, a mount, Telegram
access state, fleet timers, armed and dormant host timers, a resident host
service, shared-document directories, and a nested sibling fleet.

| Output family | What is recorded and checked |
| --- | --- |
| Bot artifacts | Instructions, MCP/config/settings, service/plist, App Git files, tool writes and 0755 mode, non-App Git-file removal |
| Links and cleanup | Skill/mount creation, retired skill directory and tool/mount removal, unchanged mount target and regular user files |
| Fleet artifacts | Full-generation provenance, fleet/bot env writes and 0600 mode; both command scopes' timer files, temporary sidecar writes/renames and named retired-unit removals |
| Host artifacts | Guard lists, timer/service/plist files, dormant unit/manifest removal, resident-service state-directory intent |
| Channel state | Private HOME's `access.json` creation/reconciliation; retained pending requests, extra group and allowlisted users |
| Shared/mutable data | Full-generation shared-directory creation; unchanged seeded memory, data/events, projects, logs, shared knowledge, mount contents and unselected bot |
| Environment | Operator values survive full scaffolding; `--bot` leaves env files/modes alone; host/root env files are read-only |
| Registry | One real scan request with emission disabled; no database/socket output |

The measured unique operation-intent counts are **90 fresh/full, 52 fresh/bot,
102 stale/full and 60 stale/bot**. These are a coverage floor for this fixture,
not a count of all outputs the framework can produce. Directory creation and
removal attempts count even when the path already exists or is already absent.
Afterward, independent snapshots check node existence, explicit owned modes,
link targets, and unchanged content outside the finite mutation set. Contents
of generated artifacts retain their existing renderer-specific tests; this
guard does not replace them. Full and selected-bot invocation both write host
and fleet artifacts, which a bot-directory-only snapshot would miss.

Eleven injected-writer controls demonstrate refusal of new outputs across six
domains and of added append, chmod, removal, mkdir and symlink operations. Seven
boundary controls demonstrate refusal before outside-estate mutation, escaped
links, unknown process execution and network connection. A new intentional
output requires review of this contract and its preview/rollout classification;
updating a count alone is insufficient.

## Recording and isolation boundary

The recorder observes CPython filesystem **operation intentions**, not system
calls or successful-write receipts. It tracks open-for-write/append, mkdir,
chmod, unlink/rmdir, symlink and rename events; extra truncate, ownership,
timestamp or hard-link events also fail the finite contract. It measures unique
operations, not their frequency or order. It does not instrument native
extensions or bare writes through descriptors opened before recording. Result
snapshots detect unexpected persistent files but cannot prove every transient
operation in such code.

Every test gets private HOME, root, env tiers, temp, XDG, channel, Plane database
and socket paths. Service/model/outbound commands are absent from its closed
PATH and denied at the Python subprocess boundary. Real subprocesses are
limited to the copied env-tier query and private Git provenance read. The env
query receives explicit private TMPDIR because its small production child
environment otherwise omits it. That shell's scratch creation/removal is
outside Python's audit-event count; the final snapshot checks no residue.
The environment query must return four private rows successfully. Refusals are
retained even if a best-effort caller catches their exceptions.

This is test-only characterization. A valid existing parent passes it; injected
writer changes fail. It adds no public diff report, runtime policy, production
write or deployment step. It does not certify all configuration combinations,
enabled registry appends, native enrollment, historical composition integrity,
replay completeness, or the absence of a race after observation. Those remain
separate contracts and approval-gated rollout evidence.
