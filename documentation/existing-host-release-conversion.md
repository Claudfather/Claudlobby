# Converting an existing source-pulling host

The selected-release watchdog guard cannot protect a host that is still running
an older `pull-root` timer from its mutable checkout. **Before merging code that
requires release admission into the branch that old timer follows**, the host
operator must stop and disable that timer with the host's native service manager,
verify it is no longer scheduled or running, and prevent a concurrent manual
root pull. A disabled setting in new source is insufficient: the old timer can
pull that source before the new setting is installed.

Then build and seal an immutable candidate from the reviewed commit, stage its
configuration plan, inspect the diff, and activate it through `host activate`.
Verify the selected release, watchdog admission and each fleet after activation.
Retire the old installed `pull-root` unit during that coordinated change. This
repository no longer packages or enrolls the root-pulling operation; sibling
repository updates remain a separate, explicit host job.

If an old host has already pulled incompatible code, keep its source-pulling
timer disabled and recover through the selected-release activation owner. Do
not treat a green timer exit or an idle bot session as proof that watchdog
admission succeeded.
