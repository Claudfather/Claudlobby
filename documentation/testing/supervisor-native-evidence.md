# Supervisor lifecycle native evidence

The `supervisor native evidence` workflow is the native acceptance lane for the first action-adoption slice of #1607. It supplements the existing pytest jobs; it does not replace them or claim the wider supervisor program is complete.

For a pull request it exports the exact base and head commits, installs each export into its own venv, and runs both on GitHub-hosted Ubuntu and macOS. Manual dispatch requires an exact pre-refactor `parent_sha`. Both revisions run even when the parent fails. The uploaded artifact identifies both revisions and preserves every command result, complete harness output, skipped scenario, failure name, state snapshot, and comparison.

`tests/native_supervisor_evidence.py` refuses ordinary local execution. Native mutations require a GitHub-hosted runner and a user-owned `/tmp/cl-native-*` installation tree. Linux uses a newly created OS user and its real systemd user manager, so the installers write under that manager's actual HOME. macOS uses a private HOME and the production `gui/<uid>` domain; absence of that domain is a failure, not permission to switch the production command to another domain.

The lifecycle observation runs the real spin-up, keepalive, spin-down, installers, renderers, launcher, native supervisors, and tmux. `CLAUDE_BIN` points at an auth-free `cat` session stub; no model or messaging account is needed. The supervision spec's environment is set to the fixture's private HOME, root and socket namespace. Recording is explicitly enabled only after those paths are validated. Observations cover:

- Fresh enrollment starts a stub session.
- Repeated spin-up restarts an installed native service and produces a different pane PID.
- Killing the target session followed by keepalive restores it through the native supervisor.
- Teardown removes supervision, tmux and `.tmux-env`, and deletes only the target state key.
- Repeated teardown, re-enrollment, purge, and repeated purge preserve their contracts.
- A second native bot and an unrelated state key survive target operations unchanged.
- Scratch Plane receipts exist after teardown and survive purge.

Teardown uses a shell trace to identify the actual native command, including Darwin's absolute `/bin/launchctl`. Cleanup addresses only labels created by the observation. Before/after snapshots check preexisting registrations, user-unit files and tmux sessions. A separate process audit detects detached tmux servers even if their socket directories were removed; an owned leak is cleaned up and still fails the result. The dedicated Linux manager is stopped in an unconditional workflow cleanup step.

The driver then runs **all** of `lib/validate-bot-change.sh`, with no selector, mutation, disabled recorder or xfail. Its environment excludes the lifecycle fixture's state path. The Linux native boot-window scenario may not skip; macOS's systemd-only skip and unavailable optional bridge-plugin scenarios remain explicitly listed. Neither skipped evidence nor equal parent/candidate failures count as a pass. The comparison lists shared, added and removed failure names to support diagnosis; both complete runs must be green before this acceptance gate clears.

The full pytest baseline, caller snapshot parity, four falsifying mutants, and the separately documented bridge callback-failure regression remain additional phase-04 review evidence. This workflow is not permission to merge or update a live installation.
