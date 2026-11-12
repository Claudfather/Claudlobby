### Fixed — `claudlobby host spec` names cgroup v2 for the bot throttle on a cgroup v1 or hybrid host, and never reads a disk with no blocks as 0% used (#2848)

Four follow-ups from the review of #2846:

- **A cgroup v1 or hybrid host.** The `bot_throttle` row still reads `unavailable` there, and its reason now says that cgroup v2, which `MemoryHigh` needs, is not mounted at `/sys/fs/cgroup`. It used to say that bots do not run under a systemd user manager. On such a host the spec cannot tell that, because it reads the user manager from cgroup v2's layout.
- **A filesystem with no blocks in use or available.** `root_fs` leaves out `used_pct` instead of reading 0, as `df` prints `-` there.
- **A `cgroup_disable=` list.** A list that includes `memory`, such as `cgroup_disable=hugetlb,memory`, is now named in the memory rows' reason, as `cgroup_disable=memory` already was.
- **An unknown holder.** When a fact that the scope or the bot manager needs is unreadable, `memory_cap` and `bot_throttle` read `unknown` with that fact's reason. They no longer say the limit "cannot be set". `documentation/host-infra.md` now says what `--json` carries for a fact the host has nothing for: `false` for `user_manager` and an empty list for `zram`, where the text reads `none`.

Tests now cover every row state the review listed as untested.
