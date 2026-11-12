# Holding an old source-puller

An existing sealed host converts to the Git updater as
[Converting an existing sealed host](fleet-update-lifecycle.md#converting-an-existing-sealed-host)
describes. A host may also still carry an old source-puller: the `pull-root`
timer, service or launchd job of an older installation, which pulls the live
checkout on a schedule. Hold it before the branch it follows moves, and keep it
retired once the host has converted.

## Hold it

Before merging incompatible code into the branch an old source-puller follows,
identify that host's exact installed pull-root timer/service or launchd label by
its executable and checkout path. Either stop and disable that exact scheduler
and verify its service is not running, or verify that the installed old job reads
and enforces a commit ceiling below that code. For a ceiling, inspect the
actual installed script and its configured input; a setting in new source is not
proof. Confirm that expiry keeps holding instead of releasing it, let any running
pull finish, and prevent concurrent manual pulls. Keep the source hold until
conversion is verified; it is not permission to cross the ceiling.

On Linux, the updater targets `systemctl --user` units. Ordinary system-level
units, foreign unit ownership or an incomplete roster require a separately
reviewed conversion; do not silently move them into the user manager or use sudo
to bypass refusal. On macOS, verify the owning GUI/user launchd domain and any
persistent disabled overrides. Never stop jobs by a broad name or process match.

## Keep it retired

After the conversion, confirm that the old source-puller units are retired and
that no scheduled or running unit still executes a deleted path. Keep the old
puller disabled; do not re-enable it.
