"""What this host is, and what its OS can enforce here: the read behind `claudlobby host spec`.

Every fact names the place it was read from. A fact that cannot be read says why instead of
standing in a number, so a failed reading never becomes 0. In the text, a fact that this host
has nothing for reads `none`; documentation/host-infra.md says what `--json` carries for each.
Reading changes nothing: it opens files under /proc, /sys and /etc, runs read-only commands on
macOS (`sysctl -n`, `sw_vers`, `ps`), and starts no unit, scope or job.

What the OS can enforce is worked out from the facts alone. Each row reads `available` (the OS
enforces it here), `inert` (the setting would be accepted and enforce nothing), `unavailable`
(this host has no such mechanism) or `unknown` (a fact it needs is unreadable), always with the
reason, and with what would change it where that is a host setting.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

MIB = 1024 * 1024
RUN_TIMEOUT_S = 10
NONE = "none"
# Only these kernel command line settings are disclosed: they decide the controllers and pressure.
CMDLINE_SETTINGS = ("cgroup", "psi", "systemd.unified_cgroup_hierarchy", "swapaccount")
SCOPE_ONLY = "a heavy run is held by its process group only"
NO_CGROUP_V2 = "cgroup v2 is not mounted at /sys/fs/cgroup"
State = Literal["available", "inert", "unavailable", "unknown"]


class Unreadable(Exception):
    """A fact's source gave no usable value; the message is the reason, naming the source."""


class UnsupportedHost(Exception):
    """The platform is neither Linux nor macOS."""


@dataclass(frozen=True)
class Fact:
    source: str
    value: object = None
    shown: str = ""
    unreadable: str | None = None

    def as_dict(self) -> dict:
        if self.unreadable is not None:
            return {"source": self.source, "unreadable": self.unreadable}
        return {"source": self.source, "value": self.value}

    def line(self, name: str) -> str:
        said = self.shown if self.unreadable is None else f"unreadable: {self.unreadable}"
        return f"  {name}: {said} [{self.source}]"


@dataclass(frozen=True)
class Enforcement:
    state: State
    reason: str
    changes_it: str | None = None

    def as_dict(self) -> dict:
        row = {"state": self.state, "reason": self.reason}
        if self.changes_it:
            row["changes_it"] = self.changes_it
        return row

    def line(self, name: str) -> str:
        tail = f"; what would change it: {self.changes_it}" if self.changes_it else ""
        return f"  {name}: {self.state}: {self.reason}{tail}"


@dataclass(frozen=True)
class HostSpec:
    platform: str
    facts: dict[str, Fact]
    enforcement: dict[str, Enforcement]

    def as_dict(self) -> dict:
        return {"platform": self.platform,
                "facts": {name: fact.as_dict() for name, fact in self.facts.items()},
                "enforcement": {name: row.as_dict() for name, row in self.enforcement.items()}}

    def lines(self) -> tuple[str, ...]:
        return (f"host spec ({self.platform}): each fact, then [where it was read]",
                *(fact.line(name) for name, fact in self.facts.items()),
                "what the OS can enforce here",
                *(row.line(name) for name, row in self.enforcement.items()))


class Host:
    """The running host, read only. Tests pass a fixture with the same reads."""

    def __init__(self, platform: str | None = None) -> None:
        self.platform = sys.platform if platform is None else platform

    def uid(self) -> int:
        return os.getuid()

    def text(self, path: str) -> str | None:
        """The file's text, or None when there is no such file."""
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, NotADirectoryError):
            return None

    def listdir(self, path: str) -> list[str] | None:
        """The directory's entries, or None when there is no such directory."""
        try:
            return sorted(os.listdir(path))
        except (FileNotFoundError, NotADirectoryError):
            return None

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, argv: list[str]) -> str:
        """A read-only command's output; anything but a clean exit is Unreadable."""
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"},
                                  check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unreadable(f"`{' '.join(argv)}` could not run: {exc}") from exc
        if done.returncode != 0:
            raise Unreadable(f"`{' '.join(argv)}` exited {done.returncode}")
        return done.stdout

    def statvfs(self, path: str) -> tuple[int, int, int]:
        """The filesystem's size, free bytes and bytes available to an unprivileged user."""
        st = os.statvfs(path)
        return st.f_blocks * st.f_frsize, st.f_bfree * st.f_frsize, st.f_bavail * st.f_frsize

    def paths(self, root: Path | None):
        """The data root, explicit or the one the CLI would select; its fleets give the bot count."""
        from ..context import resolve_paths

        return resolve_paths(root=root)


def read_spec(host: Host | None = None, *, root: Path | None = None) -> HostSpec:
    """Read the host. `root` selects the data root whose fleets give the bot count, as `--root` does."""
    host = Host() if host is None else host
    if host.platform.startswith("linux"):
        platform, readers, enforce = "linux", _linux_readers(host), _linux_enforcement
    elif host.platform == "darwin":
        platform, readers, enforce = "darwin", _MACOS_READERS, _macos_enforcement
    else:
        raise UnsupportedHost(f"host spec reads Linux and macOS hosts, and this one is {host.platform}")
    facts: dict[str, Fact] = {}
    for name, source, reader in readers:
        facts[name] = _read(source, partial(reader, host))
    facts["bots"] = _read("each fleet.yaml under the data root", partial(_bots, host, root))
    return HostSpec(platform, facts, enforce(facts))


def _read(source: str, read) -> Fact:
    try:
        value, shown = read()
    except (Unreadable, OSError, ValueError) as exc:
        return Fact(source, unreadable=str(exc))
    return Fact(source, value=value, shown=shown)


def _need(host: Host, path: str) -> str:
    text = host.text(path)
    if text is None:
        raise Unreadable(f"{path} does not exist")
    return text


def _mib(n_bytes: int) -> int:
    return round(n_bytes / MIB)


def _meminfo_kib(host: Host, name: str) -> int:
    """One /proc/meminfo line's value. A line that is not a number of kB is refused by its name."""
    for line in _need(host, "/proc/meminfo").splitlines():
        key, colon, rest = line.partition(":")
        if colon and key.strip() == name:
            if not re.fullmatch(r"\s*[0-9]+ kB\s*", rest):
                raise Unreadable(f"the {name} line of /proc/meminfo is not a number of kB: {line.strip()!r}")
            return int(rest.split()[0])
    raise Unreadable(f"/proc/meminfo has no {name} line")


def _meminfo_mib(host: Host, name: str) -> tuple[int, str]:
    mib = round(_meminfo_kib(host, name) / 1024)
    return mib, f"{mib:,} MiB"


def _swap_linux(host):
    rows = _need(host, "/proc/swaps").splitlines()
    if not rows or not rows[0].startswith("Filename"):
        raise Unreadable("/proc/swaps does not start with its Filename header")
    devices = []
    for row in rows[1:]:
        fields = row.split()
        if not fields:
            continue
        if len(fields) != 5 or not all(re.fullmatch(r"-?[0-9]+", f) for f in fields[2:]):
            raise Unreadable(f"a /proc/swaps row is not a name, type, size, used and priority: {row.strip()!r}")
        name, kind, size, used, priority = fields
        devices.append({"name": name, "type": kind, "size_mib": round(int(size) / 1024),
                        "used_mib": round(int(used) / 1024), "priority": int(priority)})
    total = sum(d["size_mib"] for d in devices)
    used = sum(d["used_mib"] for d in devices)
    shown = NONE if not devices else f"{total:,} MiB, {used:,} MiB used: " + "; ".join(
        f"{d['name']} ({d['type']}, {d['size_mib']:,} MiB, priority {d['priority']})" for d in devices)
    return {"devices": devices, "total_mib": total, "used_mib": used}, shown


def _zram(host):
    names = host.listdir("/sys/block")
    if names is None:
        raise Unreadable("/sys/block does not exist")
    devices = []
    for name in (n for n in names if re.fullmatch(r"zram[0-9]+", n)):
        base = f"/sys/block/{name}"
        size = _int_file(host, f"{base}/disksize")
        algorithm = re.search(r"\[([^\]]+)\]", _need(host, f"{base}/comp_algorithm"))
        # mm_stat: stored, compressed, memory used, memory limit (0 for none), ... in bytes. The
        # limit is read here because the kernel makes the mem_limit file write-only.
        stat = _need(host, f"{base}/mm_stat").split()
        if algorithm is None:
            raise Unreadable(f"{base}/comp_algorithm names no selected algorithm")
        if len(stat) < 4 or not all(f.isdigit() for f in stat[:4]):
            raise Unreadable(f"{base}/mm_stat does not start with four byte counts")
        limit = int(stat[3])
        devices.append({"device": name, "size_mib": _mib(size), "algorithm": algorithm.group(1),
                        "mem_limit_mib": _mib(limit) if limit else NONE,
                        "stored_mib": _mib(int(stat[0])), "compressed_mib": _mib(int(stat[1])),
                        "memory_used_mib": _mib(int(stat[2]))})
    shown = NONE if not devices else "; ".join(
        f"{d['device']}: {d['size_mib']:,} MiB ({d['algorithm']}), memory limit "
        + (f"{d['mem_limit_mib']:,} MiB" if d["mem_limit_mib"] != NONE else NONE)
        + f", holds {d['stored_mib']:,} MiB in {d['memory_used_mib']:,} MiB" for d in devices)
    return devices, shown


def _int_file(host: Host, path: str) -> int:
    text = _need(host, path).strip()
    if not text.isdigit():
        raise Unreadable(f"{path} is not a number: {text!r}")
    return int(text)


def _cpus_linux(host):
    text = _need(host, "/sys/devices/system/cpu/online").strip()
    count = 0
    for part in text.split(","):
        bounds = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", part)
        if bounds is None or int(bounds.group(2) or bounds.group(1)) < int(bounds.group(1)):
            raise Unreadable(f"/sys/devices/system/cpu/online is not a list of CPU ranges: {text!r}")
        count += int(bounds.group(2) or bounds.group(1)) - int(bounds.group(1)) + 1
    return count, str(count)


def _os_linux(host):
    for line in _need(host, "/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            name = line.partition("=")[2].strip().strip("\"'")
            if name:
                return name, name
    raise Unreadable("/etc/os-release has no PRETTY_NAME line")


def _one_line(host: Host, path: str) -> tuple[str, str]:
    text = _need(host, path).strip()
    if not text:
        raise Unreadable(f"{path} is empty")
    return text, text


def _systemd_booted(host):
    booted = host.listdir("/run/systemd/system") is not None
    return booted, "yes" if booted else "no (/run/systemd/system is absent)"


def _on_path(host: Host, name: str) -> tuple[str, str]:
    path = host.which(name)
    return (NONE, "none (not on PATH)") if path is None else (path, path)


def _cgroup(host):
    mounts = [line.split()[1:3] for line in _need(host, "/proc/self/mounts").splitlines()
              if len(line.split()) > 2]
    if ["/sys/fs/cgroup", "cgroup2"] in mounts:
        version = "v2"
    elif any(kind == "cgroup" for _, kind in mounts):
        version = "hybrid" if any(kind == "cgroup2" for _, kind in mounts) else "v1"
    else:
        version = NONE
    return version, version


def _controllers(text: str) -> tuple[list[str], str]:
    names = text.split()
    return names, " ".join(names) or "none offered"


def _controllers_root(host):
    text = host.text("/sys/fs/cgroup/cgroup.controllers")
    return (NONE, "none (no cgroup v2 at /sys/fs/cgroup)") if text is None else _controllers(text)


def _user_manager_dir(uid: int) -> str:
    return f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"


def _user_manager(host):
    running = host.listdir(_user_manager_dir(host.uid())) is not None
    return running, "running" if running else "none (no user manager's cgroup)"


def _controllers_user_manager(host):
    text = host.text(f"{_user_manager_dir(host.uid())}/cgroup.controllers")
    return (NONE, "none (no user manager's cgroup)") if text is None else _controllers(text)


def _own_cgroup_path(host: Host) -> str | None:
    lines = [line[3:] for line in _need(host, "/proc/self/cgroup").splitlines() if line.startswith("0::")]
    return lines[0] if lines else None


def _own_cgroup(host):
    path = _own_cgroup_path(host)
    return (NONE, "none (this process is in no cgroup v2 hierarchy)") if path is None else (path, path)


def _controllers_own(host):
    path = _own_cgroup_path(host)
    if path is None:
        return NONE, "none (this process is in no cgroup v2 hierarchy)"
    return _controllers(_need(host, f"/sys/fs/cgroup{path.rstrip('/')}/cgroup.controllers"))


def _pressure(host):
    names = host.listdir("/proc/pressure")
    if names is None:
        return [], "none (/proc/pressure is absent)"
    return names, " ".join(names) or "none"


def _cmdline(host):
    settings = [token for token in _need(host, "/proc/cmdline").split()
                if token.startswith(CMDLINE_SETTINGS)]
    return settings, " ".join(settings) or "none of these settings"


def _memory_disabled_by(cmdline: list[str]) -> str | None:
    """The command line's cgroup_disable= setting whose list names the memory controller, if any."""
    for setting in cmdline:
        key, _, controllers = setting.partition("=")
        if key == "cgroup_disable" and "memory" in controllers.split(","):
            return setting
    return None


def _filesystem(host: Host, path: str) -> tuple[dict, str]:
    """Size, and percent used as df counts it. With no blocks in use or available, df prints `-`,
    and the percent is left out rather than read as 0."""
    size, free, available = host.statvfs(path)
    used = size - free
    value = {"path": path, "size_mib": _mib(size)}
    if used + available == 0:
        return value, f"{value['size_mib']:,} MiB, percent used unknown: no blocks are in use or available"
    value["used_pct"] = math.ceil(100 * used / (used + available))
    return value, f"{value['size_mib']:,} MiB, {value['used_pct']}% used"


def _root_fs_linux(host):
    return _filesystem(host, "/")


def _linux_readers(host: Host):
    manager = _user_manager_dir(host.uid())
    return (
        ("ram", "/proc/meminfo MemTotal", partial(_meminfo_mib, name="MemTotal")),
        ("mem_available", "/proc/meminfo MemAvailable", partial(_meminfo_mib, name="MemAvailable")),
        ("swap", "/proc/swaps", _swap_linux),
        ("zram", "/sys/block/zram*: disksize, comp_algorithm, mm_stat", _zram),
        ("cpus", "/sys/devices/system/cpu/online", _cpus_linux),
        ("os", "/etc/os-release PRETTY_NAME", _os_linux),
        ("kernel", "/proc/sys/kernel/osrelease", partial(_one_line, path="/proc/sys/kernel/osrelease")),
        ("init", "/proc/1/comm", partial(_one_line, path="/proc/1/comm")),
        ("systemd_booted", "/run/systemd/system", _systemd_booted),
        ("systemd_run", "PATH", partial(_on_path, name="systemd-run")),
        ("cgroup", "/proc/self/mounts", _cgroup),
        ("controllers_root", "/sys/fs/cgroup/cgroup.controllers", _controllers_root),
        ("user_manager", manager, _user_manager),
        ("controllers_user_manager", f"{manager}/cgroup.controllers", _controllers_user_manager),
        ("own_cgroup", "/proc/self/cgroup", _own_cgroup),
        ("controllers_own", "the own cgroup's cgroup.controllers", _controllers_own),
        ("pressure", "/proc/pressure", _pressure),
        ("kernel_cmdline", "/proc/cmdline, its cgroup, psi and swapaccount settings only", _cmdline),
        ("root_fs", "statvfs /", _root_fs_linux),
    )


def _sysctl(host: Host, name: str) -> str:
    text = host.run([host.which("sysctl") or "/usr/sbin/sysctl", "-n", name]).strip()
    if not text:
        raise Unreadable(f"sysctl {name} printed nothing")
    return text


def _sysctl_int(host: Host, name: str) -> int:
    text = _sysctl(host, name)
    if not text.isdigit():
        raise Unreadable(f"sysctl {name} is not a number: {text!r}")
    return int(text)


def _ram_macos(host):
    mib = _mib(_sysctl_int(host, "hw.memsize"))
    return mib, f"{mib:,} MiB"


def _swap_macos(host):
    text = _sysctl(host, "vm.swapusage")
    sizes = re.match(r"total = ([0-9.]+)([MG])\s+used = ([0-9.]+)([MG])", text)
    if sizes is None:
        raise Unreadable(f"sysctl vm.swapusage is not 'total = N used = N': {text!r}")
    total, used = (round(float(sizes.group(i)) * (1024 if sizes.group(i + 1) == "G" else 1)) for i in (1, 3))
    return ({"total_mib": total, "used_mib": used, "encrypted": "(encrypted)" in text},
            f"{total:,} MiB, {used:,} MiB used (dynamic swap files)")


def _cpus_macos(host):
    count = _sysctl_int(host, "hw.ncpu")
    return count, str(count)


def _os_macos(host):
    fields = dict(line.partition(":")[::2] for line in host.run(["sw_vers"]).splitlines())
    name, version = (fields.get(key, "").strip() for key in ("ProductName", "ProductVersion"))
    if not (name and version):
        raise Unreadable("sw_vers names no ProductName and ProductVersion")
    return f"{name} {version}", f"{name} {version}"


def _kernel_macos(host):
    release = _sysctl(host, "kern.osrelease")
    return release, f"Darwin {release}"


def _init_macos(host):
    name = Path(host.run(["ps", "-o", "comm=", "-p", "1"]).strip()).name
    if not name:
        raise Unreadable("ps names no process 1")
    return name, name


def _root_fs_macos(host):
    if host.listdir("/System/Volumes/Data") is None:
        raise Unreadable("/System/Volumes/Data does not exist")
    return _filesystem(host, "/System/Volumes/Data")


_MACOS_READERS = (
    ("ram", "sysctl hw.memsize", _ram_macos),
    ("swap", "sysctl vm.swapusage", _swap_macos),
    ("cpus", "sysctl hw.ncpu", _cpus_macos),
    ("os", "sw_vers", _os_macos),
    ("kernel", "sysctl kern.osrelease", _kernel_macos),
    ("init", "ps -o comm= -p 1", _init_macos),
    ("vm_stat", "PATH", partial(_on_path, name="vm_stat")),
    ("root_fs", "statvfs /System/Volumes/Data, the data volume", _root_fs_macos),
)


def _bots(host: Host, root: Path | None):
    try:
        paths = host.paths(root)
    except (ValueError, OSError) as exc:
        raise Unreadable(f"no data root: {exc}") from exc
    import yaml

    from ..config import load_fleet
    from ..context import declared_paths

    fleets = declared_paths(paths.root, paths.package)
    count = 0
    for fleet in fleets:
        try:
            config, _defaults = load_fleet(fleet.fleet_yaml)
        except (OSError, yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise Unreadable(f"{fleet.fleet_yaml} cannot be read: {exc}") from exc
        count += len(config.bots)
    return ({"bots": count, "fleets": len(fleets), "root": str(paths.root)},
            f"{count} in {len(fleets)} fleet(s) under {paths.root}")


def _names(fact: Fact) -> list[str]:
    """A controllers fact's names; `none` offers none."""
    return fact.value if isinstance(fact.value, list) else []


def _unknown(facts: dict[str, Fact], *names: str) -> Enforcement | None:
    for name in names:
        if facts[name].unreadable is not None:
            return Enforcement("unknown", f"the {name} fact is unreadable: {facts[name].unreadable}")
    return None


def _scope(facts: dict[str, Fact]) -> Enforcement:
    unknown = _unknown(facts, "systemd_booted", "cgroup", "systemd_run", "user_manager")
    if unknown:
        return unknown
    if not facts["systemd_booted"].value:
        return Enforcement("unavailable", f"systemd is not the init system, so {SCOPE_ONLY}")
    if facts["cgroup"].value != "v2":
        return Enforcement("unavailable", f"{NO_CGROUP_V2}, so {SCOPE_ONLY}")
    if facts["systemd_run"].value == NONE:
        return Enforcement("unavailable", f"systemd-run is not on PATH, so {SCOPE_ONLY}")
    if not facts["user_manager"].value:
        return Enforcement("unavailable", f"no systemd user manager runs for this user, so {SCOPE_ONLY}")
    return Enforcement("available", "a heavy run can be held in a transient user scope "
                       "(systemd-run --user --scope) and ended whole through its cgroup")


def _memory_controller(facts: dict[str, Fact]) -> Enforcement:
    """Whether the user manager can give its units the memory controller: available or inert."""
    unknown = _unknown(facts, "controllers_user_manager", "controllers_root")
    if unknown:
        return unknown
    if "memory" in _names(facts["controllers_user_manager"]):
        return Enforcement("available", "the user manager is delegated the memory controller")
    if "memory" not in _names(facts["controllers_root"]):
        cmdline = facts["kernel_cmdline"].value if facts["kernel_cmdline"].unreadable is None else []
        disable = _memory_disabled_by(cmdline)
        if disable:
            return Enforcement("inert", "the kernel offers no memory controller: its command line "
                               f"carries {disable}",
                               "a boot whose kernel command line enables the memory controller "
                               "(cgroup_enable=memory); the boot setting is the operator's")
        return Enforcement("inert", "the kernel offers no memory controller",
                           "a kernel and boot that provide the memory controller; the operator's")
    return Enforcement("inert", "the kernel has a memory controller, but it is not delegated to the "
                       "user manager", "Delegate= on user@.service, which is the operator's")


def _limit_on(what: str, holder: Enforcement, facts: dict[str, Fact]) -> Enforcement:
    if holder.state == "unknown":
        return holder
    if holder.state != "available":
        return Enforcement(holder.state, f"{what} cannot be set: {holder.reason}")
    memory = _memory_controller(facts)
    if memory.state == "available":
        return Enforcement("available", f"{what} is enforced: {memory.reason}")
    if memory.state == "inert":
        return Enforcement("inert", f"{what} would be accepted and enforce nothing: {memory.reason}",
                           memory.changes_it)
    return memory


def _bot_manager(facts: dict[str, Fact]) -> Enforcement:
    unknown = _unknown(facts, "systemd_booted", "cgroup", "user_manager")
    if unknown:
        return unknown
    if facts["systemd_booted"].value and facts["cgroup"].value != "v2":
        return Enforcement("unavailable", f"{NO_CGROUP_V2}, and MemoryHigh needs it")
    if facts["systemd_booted"].value and facts["user_manager"].value:
        return Enforcement("available", "bots run under the systemd user manager")
    return Enforcement("unavailable", "bots do not run under a systemd user manager here")


def _free_memory_linux(facts: dict[str, Fact]) -> Enforcement:
    """The memory stop's reading. A line it cannot read leaves the stop without it: unavailable, by name."""
    available = facts["mem_available"]
    if available.unreadable is not None:
        return Enforcement("unavailable", f"the memory stop cannot read free memory: {available.unreadable}")
    return Enforcement("available", "the memory stop reads free memory from MemAvailable in /proc/meminfo")


def _pressure_linux(facts: dict[str, Fact]) -> Enforcement:
    unknown = _unknown(facts, "pressure")
    if unknown:
        return unknown
    if "memory" in facts["pressure"].value:
        return Enforcement("available", "the kernel exposes memory pressure (/proc/pressure/memory), "
                           "which systemd-oomd needs to act on it")
    return Enforcement("unavailable", "the kernel exposes no memory pressure (/proc/pressure/memory "
                       "is absent), so systemd-oomd cannot act on it here")


def _linux_enforcement(facts: dict[str, Fact]) -> dict[str, Enforcement]:
    scope = _scope(facts)
    return {
        "scope": scope,
        "memory_cap": _limit_on("a MemoryMax on a heavy run's scope", scope, facts),
        "bot_throttle": _limit_on("a MemoryHigh on a bot's unit", _bot_manager(facts), facts),
        "free_memory": _free_memory_linux(facts),
        "pressure": _pressure_linux(facts),
    }


def _macos_enforcement(facts: dict[str, Fact]) -> dict[str, Enforcement]:
    vm_stat = facts["vm_stat"]
    return {
        "scope": Enforcement("unavailable", f"macOS has no systemd, so {SCOPE_ONLY}"),
        "memory_cap": Enforcement("unavailable", "macOS has no documented memory cap for a process tree"),
        "bot_throttle": Enforcement("unavailable",
                                    "macOS has no documented memory throttle for a launchd agent"),
        "free_memory": (
            Enforcement("unavailable", "vm_stat is not on PATH, so the memory stop cannot read free memory")
            if vm_stat.value == NONE or vm_stat.unreadable is not None
            else Enforcement("available", "the memory stop reads free memory from vm_stat")),
        "pressure": Enforcement("unavailable", "macOS has no pressure stall files, a Linux kernel interface"),
    }
