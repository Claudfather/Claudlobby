# Host infrastructure

`claudlobby host spec` reads the host the fleets run on. It prints each fact about the host with the place it was read from, then what the OS can enforce here, with the reason. It is a read: it writes no file and starts no unit, scope or job.

```bash
claudlobby host spec         # one line per fact, then one per enforcement row
claudlobby --json host spec  # the same, as one schema-1 result object
```

The reader is `claudlobby/infra/spec.py`.

## Facts

Each fact has a `source`, and either a `value` or, when the source could not be read, `unreadable` with the reason. A failed reading is never shown as 0. A `/proc/meminfo` line that is not a number of kB is refused by its name, for example `the MemAvailable line of /proc/meminfo is not a number of kB: 'MemAvailable:'`. A fact the host has nothing for reads `none`: no zram device, no `systemd-run` on PATH, no user manager.

On Linux:

| Fact | Value | Read from |
|---|---|---|
| `ram` | MiB | `/proc/meminfo` MemTotal |
| `mem_available` | MiB free at the time of the read | `/proc/meminfo` MemAvailable |
| `swap` | each device, with its type, size, use and priority, and the totals | `/proc/swaps` |
| `zram` | each zram device: its size, algorithm, memory limit (`none` for no limit), and what it holds in how much memory | `/sys/block/zram*`: `disksize`, `comp_algorithm` and `mm_stat`. The kernel makes `mem_limit` write-only, so the limit comes from `mm_stat`. |
| `cpus` | online CPUs | `/sys/devices/system/cpu/online` |
| `os` | the distribution's name | `/etc/os-release` PRETTY_NAME |
| `kernel` | the kernel release | `/proc/sys/kernel/osrelease` |
| `init` | the name of PID 1 | `/proc/1/comm` |
| `systemd_booted` | whether systemd runs the host | `/run/systemd/system`, systemd's own test |
| `systemd_run` | its path, or `none` | PATH |
| `cgroup` | `v2`, `hybrid`, `v1` or `none` | `/proc/self/mounts` |
| `controllers_root` | the controllers the kernel offers | `/sys/fs/cgroup/cgroup.controllers` |
| `user_manager` | whether the caller's systemd user manager runs | its cgroup, `user@UID.service` |
| `controllers_user_manager` | the controllers delegated to the user manager, which bound every unit and scope under it | its `cgroup.controllers` |
| `own_cgroup` | the caller's own cgroup (a bot's unit, when a bot runs the read) | `/proc/self/cgroup` |
| `controllers_own` | the controllers in that cgroup | its `cgroup.controllers` |
| `pressure` | the pressure stall files present | `/proc/pressure` |
| `kernel_cmdline` | only the command line's `cgroup*`, `psi*`, `systemd.unified_cgroup_hierarchy` and `swapaccount` settings | `/proc/cmdline` |
| `root_fs` | size in MiB, and percent used as `df` counts it | `statvfs /` |
| `bots` | the bots and fleets declared on the data root, and the root (`--root`, else the one the CLI selects) | each `fleet.yaml` under the data root |

On macOS: `ram` (`sysctl hw.memsize`), `swap` (`sysctl vm.swapusage`: total and used, since macOS sizes its swap files as it goes), `cpus` (`sysctl hw.ncpu`), `os` (`sw_vers`), `kernel` (`sysctl kern.osrelease`), `init` (`ps -o comm= -p 1`), `vm_stat` (its path), `root_fs` (the data volume, `/System/Volumes/Data`) and `bots`.

## What the OS can enforce

Each row reads one of four states:
- `available`: the OS enforces it here.
- `inert`: the setting would be accepted and enforce nothing.
- `unavailable`: this host has no such mechanism.
- `unknown`: a fact the row needs is unreadable, and the reason names it.

Where a host setting would change a row, `changes_it` names it. Every such setting is the operator's: `host spec` only reads.

| Row | `available` when | Otherwise |
|---|---|---|
| `scope` | systemd runs the host, cgroup v2 is mounted at `/sys/fs/cgroup`, `systemd-run` is on PATH and the user manager runs. A heavy run can then be held in a transient user scope and ended whole through its cgroup. | `unavailable`: a heavy run is held by its process group only. |
| `memory_cap` | a scope is available, and the user manager is delegated the `memory` controller. A `MemoryMax` on a run's scope is then enforced. | `inert` when the controller is missing: the kernel offers none (the reason names `cgroup_disable=memory` when the command line carries it), or the controller is not delegated to the user manager. `unavailable` with no scope. |
| `bot_throttle` | bots run under the user manager, and it is delegated the `memory` controller. A `MemoryHigh` on a bot's unit is then enforced. | As `memory_cap`. |
| `free_memory` | the memory stop can read free memory: MemAvailable in `/proc/meminfo` on Linux, `vm_stat` on macOS. | `unavailable`, naming the line or the tool it could not read. |
| `pressure` | the kernel exposes `/proc/pressure/memory`, which systemd-oomd needs before it can act on memory pressure. | `unavailable`. |

On macOS, `scope`, `memory_cap`, `bot_throttle` and `pressure` read `unavailable`: there is no systemd, no documented memory cap for a process tree or throttle for a launchd agent, and no pressure stall files.

## When the kernel has no memory controller

Raspberry Pi firmware can put `cgroup_disable=memory` on the kernel command line. The kernel then offers no memory controller. `memory_cap` and `bot_throttle` read `inert` and name that setting: systemd accepts a `MemoryMax=` or a `MemoryHigh=`, and nothing enforces it.

Enabling the controller is a boot setting (`cgroup_enable=memory` on the command line, then a reboot), and it is the operator's call. Whether it takes depends on the kernel, so read `host spec` again after the reboot rather than assume it.
