# Plane isolation in tests

Tests run in a disposable checkout, installed into its own `.venv`, with a
scratch HOME. Native service tests also need isolated service ownership or
stubbed transports; a Plane silencer does not isolate launchd, systemd, tmux,
notifications, credentials, or network access. Do not run a full suite on a
live fleet installation merely because the emission sentinels pass.

Use `constructed_env(...)` for child processes. Its PATH and UTF-8 locale are
intentional, and `PLANE_EMIT_DISABLED=1` is always present unless explicitly
overridden. The compatibility helper `_scrubbed_env` removes inherited
`PLANE_*` controls before applying the same default. Its wider denylist
migration belongs to issue #846.

A recording test receives the `scratch_plane_env` fixture and passes it through
its harness helpers:

```python
def test_receipt(tmp_path, scratch_plane_env):
    root = tmp_path / "root"
    root.mkdir()
    env = constructed_env(HOME=tmp_path / "home", **scratch_plane_env(root))
    # Drive the real door, then assert the expected row identity and count.
```

The factory resolves paths before checking pytest ownership, rejects source
roots and symlink escapes, and returns a coupled root, absent socket, venv CLI,
and explicit `PLANE_EMIT_DISABLED=0`. CLI preflight imports the installed package
from outside the checkout so cwd cannot hide an unrelated editable install.
A recorder override must live under pytest's owned directory. Real Unix-daemon
tests allocate `scratch_plane_env.socket_dir()` and pass a socket below that
specific registered directory, keeping macOS paths short without trusting all
of `/tmp`. To test production's default-on behavior, first build a validated
scratch environment and then remove its silencer for that scenario.

Session subprocess fixtures depend explicitly on `_isolate_plane_session`.
Function tests can override the guard with `monkeypatch`; each following test
starts silent again. Collection-time execution is outside fixture protection.
Direct `emit_batch`, `PlaneDaemon`, SQLite connection, and migration tests remain
explicit-root writers; no production flag has been added to those APIs.

## Harness census

The initial census used parent `6927824750abeeb7aa295e5d6e9a1a81437be81f`,
then was repeated after rebasing onto `991cbfe` (including the newly added
dispatch-receipt and daemon cooldown tests):

- Ambient inherited environments: guarded at session and function scope.
  `setup_system_dry_run` is the emission-capable session subprocess fixture;
  the other session subprocess (`rsa_key`) runs only openssl. An AST walk of
  subprocess calls found no module-import subprocess execution.
- Shared replacement environments: `constructed_env` and `_scrubbed_env` are
  independently silent. Incidental replacement dictionaries in lifecycle,
  roster, fleet-state pruning, source-currency, session-CLI and cold-start
  harnesses carry silence too.
- Intentional cold-CLI recording: all former `plane_emit_env` consumers and
  Plane door, gauntlet, event, workstream, keepalive, host-probe, dispatch,
  Telegram, relay, session, check-in, task-id and fleet-pulse harnesses use the
  validated factory. Imported helper callers pass the fixture through rather
  than enabling recording globally.
- In-process gated writers: registry scans validate the actual `_scan` root;
  brief acknowledgement and task nudge/recheck fixtures enable only their
  pytest-owned root. Their existing row assertions remain.
- Stub recording: wedge, crash-loop, manager-check-in, and fleet-pulse error
  probes use private roots and recorder transports. Negative CLI outcomes use
  test-owned wrappers or explicit failing executables after scratch validation.
- Standalone shell suites own their boundary: `test_plane_emit.sh`, receipt,
  error-trap, recipient, pane-send and transcript-digest suites create private
  roots and socket paths and explicitly arm their recorder only there. The shim
  suite preserves its disabled control and every ladder assertion. Host-health
  and orphan-browser incidental emissions remain silent.
- Direct Python writers already receive roots derived from `tmp_path`, the
  shared `plane_root`/`_scene` fixtures, or fixture-owned daemon directories.
  These do not rely on the silencer. Read-only resolver/daemon-launcher tests
  retain their stub or explicit-root contracts.

Recheck both literal replacement dictionaries and inherited environments when
adding a door. Useful inventory searches (not a proof of safety):

```sh
rg -n 'PLANE_EMIT_DISABLED|PLANE_SOCKET|PLANE_EMIT_CLI|emit_batch|os\.environ' tests
rg -n 'subprocess|scope=.session.|env -i' tests
```

`test_plane_test_isolation.py` is the behavioral gate: a real listening ambient
socket and CLI recorder must stay untouched, nested pytest must load the real
conftest and report the expected session/function nodes, and a positive cold
CLI emission must land the expected scratch row. Mutation verification removes
each guard or recording opt-in only in disposable copies; each mutant must fail
its corresponding observation. Preserve the row assertions in recording tests.

This test-only slice does not close issue #1601: hand-run-script protections and
historical production-row cleanup remain separate work. It also does not close
#846's broader environment-construction migration.
