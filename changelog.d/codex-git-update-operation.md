### Changed — consolidate update recovery and effective configuration foundations

Add a source-independent current-update record and Git cutover primitives. A
pending render and its publication progress now fit within that operation,
without creating another permanent plan or activation journal. The committed
configuration reader is independent of whether every consumer started, and
native quiet checks share one implementation. These are internal foundations;
the common updater and host migration are still being integrated.

### Changed — prepare a first fleet through the same Git updater

Replace the bundle bootstrap and its publishing workflow with an explicit trusted
Git source and hash-locked Python environment. First-fleet preview retains the
same update request that launch continues; concurrent setup cannot replace its
configuration or advance a different request. Retry checks verify the installed
dependencies without silently repairing them. Initial recorder and registry setup
share the update coordinator, and new native consumers use the existing supervision
adapter. The CLI guide and CI installation rehearsal follow this path; browser
first-team integration, existing-host migration and native acceptance remain pending.

### Changed — use the Git update request for browser first-team setup

Browser setup now prepares and launches the same Git operation as the CLI. It
retains the original request before launch and shows its receipt after reconnects
or uncertain responses, without sending a replacement. First-owner binding survives
an interrupted configuration publication. Historical sealed setup records remain
readable as recovery evidence and cannot authorize a Git launch.

Remove the public sealed activation, repair, abort and plan commands and the unused
release assembly module. Configuration diff reads the pending Git render or the
committed generated-output snapshot. Existing-host conversion and native/browser
cold-start acceptance are still required before rollout; the remaining legacy
coordinator internals are being retired separately.

### Changed — retire the sealed runtime coordinator

Operational reads, actions and native units now use the managed Git installation
and committed configuration. Remove the old activation engine, its enrollment and
handoff implementations, and its release-based runtime admission. Historical
activation and migration records remain read-only diagnostic evidence. Bot move
and removal explicitly refuse without effects until the common updater supports
roster changes; they cannot invoke the retired coordinator. Human task feedback
also refuses admitted native maintenance children.

### Changed — fetch updates from the prepared Git source

`host update apply` fetches the installation’s configured trusted ref, or an
explicit full commit, before freezing a new operation. A pending operation keeps
its original target and request without another fetch. Remove unused sealed
release/configuration writers and their rollback path; the shared Git publication
owner retains interrupted-write recovery. Old records remain readable.

### Changed — align operator guidance with the Git updater

Remove obsolete sealed-update instructions from the current runbooks and CLI
recovery hints. Distinguish authored source review from published-output drift,
and document the remaining conversion, roster-change and restored-root startup
limitations instead of suggesting commands that no longer exist.

### Fixed — reject invalid first-fleet input before preparing an update

Validate an authored fleet with the canonical configuration parser before copying
it or recording an update. Invalid input now leaves setup ready for a corrected
file, without a recovery operation or any native service effects.

### Changed — resume native conversations without model-written update handoffs

Routine Git updates and planned restarts wait for an idle bot, retain its exact
Claude session, and resume that conversation with current composed instructions.
They send no checkpoint or restore prompt. The mandatory SessionStart hook keeps
the saved identity tied to its account, working directory and native process;
unknown prior sends are never replayed. Background updates measure Claude using
the same tool path as the bot. Older bots without a native session receipt still
need an explicit one-time adoption before this update path can stop them.
