"""`claudlobby host spec`: a host's facts, each with its source, and what its OS can enforce.

Four fixtures stand for the hosts the reader has to tell apart: a Raspberry Pi 5 whose firmware
disables the memory controller, a Mac, a Linux host with no systemd, and a Linux host whose
controllers include memory. Every fact either has a value or says why it is unreadable; none
stands in a 0.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from claudlobby.__main__ import main
from claudlobby.context import resolve_paths
from claudlobby.infra import spec
from claudlobby.infra.spec import Host, UnsupportedHost, read_spec
from tests.conftest import MINIMAL_FLEET_YAML

GIB = 1024 ** 3
UID = 1000
MANAGER = f"/sys/fs/cgroup/user.slice/user-{UID}.slice/user@{UID}.service"
OWN = f"{MANAGER}/app.slice/com.test.bot.service"


class FixtureHost(Host):
    """A host made of files, directories, commands on PATH and command outputs."""

    def __init__(self, platform, files, *, dirs=(), which=None, commands=None, statvfs=None):
        super().__init__(platform)
        self.files = dict(files)
        self.dirs = set(dirs)
        for path in self.files:
            self.dirs.update(str(parent) for parent in PurePosixPath(path).parents)
        self.paths = dict(which or {})
        self.commands = dict(commands or {})
        self.sizes = dict(statvfs or {})

    def uid(self):
        return UID

    def text(self, path):
        value = self.files.get(path)
        if isinstance(value, OSError):
            raise value
        return value

    def listdir(self, path):
        if path not in self.dirs:
            return None
        prefix = path.rstrip("/") + "/"
        return sorted({entry[len(prefix):].split("/")[0] for entry in (*self.files, *self.dirs)
                       if entry.startswith(prefix)})

    def which(self, name):
        return self.paths.get(name)

    def run(self, argv):
        key = (PurePosixPath(argv[0]).name, *argv[1:])
        if key not in self.commands:
            raise spec.Unreadable(f"`{' '.join(argv)}` exited 1")
        return self.commands[key]

    def statvfs(self, path):
        return self.sizes[path]


MEMINFO = "MemTotal:       16608304 kB\nMemFree:          512000 kB\nMemAvailable:    4041728 kB\nSwapTotal:      10485752 kB\n"
SWAPS = ("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
         "/dev/zram0                              partition\t6291452\t\t5463012\t\t100\n"
         "/var/swap                               file\t\t4194300\t\t0\t\t-2\n")
ROOT_FS = {"/": (1983 * GIB, 1745 * GIB, 1650 * GIB)}


def linux_files(*, root="cpuset cpu io pids", manager="cpu pids", own="pids", cmdline="", pressure=()):
    files = {
        "/proc/meminfo": MEMINFO,
        "/proc/swaps": SWAPS,
        "/sys/block/zram0/disksize": "6442450944\n",
        "/sys/block/zram0/comp_algorithm": "lzo-rle lzo lz4 lz4hc 842 [zstd]\n",
        "/sys/block/zram0/mm_stat": "5441060864 1066273189 1097269248 2621440000 1235566592 1874 1225908 961 13563\n",
        "/sys/block/sda/size": "3907029168\n",
        "/sys/devices/system/cpu/online": "0-3\n",
        "/etc/os-release": 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\n',
        "/proc/sys/kernel/osrelease": "6.12.75+rpt-rpi-2712\n",
        "/proc/1/comm": "systemd\n",
        "/proc/self/mounts": ("/dev/sda2 / ext4 rw,noatime 0 0\n"
                              "cgroup2 /sys/fs/cgroup cgroup2 rw,nosuid,nodev,noexec,relatime 0 0\n"),
        "/sys/fs/cgroup/cgroup.controllers": root + "\n",
        f"{MANAGER}/cgroup.controllers": manager + "\n",
        "/proc/self/cgroup": f"0::{OWN[len('/sys/fs/cgroup'):]}\n",
        f"{OWN}/cgroup.controllers": own + "\n",
        "/proc/cmdline": f"console=tty1 root=/dev/sda2 rootfstype=ext4 {cmdline} rootwait\n",
    }
    files.update({f"/proc/pressure/{name}": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" for name in pressure})
    return files


SYSTEMD = {"which": {"systemd-run": "/usr/bin/systemd-run"},
           "commands": {("systemd-run", "--version"): "systemd 252 (252.39-1~deb12u1)\n+PAM +AUDIT\n"},
           "dirs": {"/run/systemd/system"}}


def pi(**overrides):
    files = linux_files(cmdline="cgroup_disable=memory")
    files.update(overrides)
    return FixtureHost("linux", files, statvfs=ROOT_FS, **SYSTEMD)


def memory_host():
    files = linux_files(root="cpuset cpu io memory pids", manager="cpu io memory pids", own="memory pids",
                        pressure=("cpu", "io", "memory"))
    return FixtureHost("linux", files, statvfs=ROOT_FS, **SYSTEMD)


def no_systemd_host():
    files = linux_files(root="cpu io memory pids", pressure=("cpu", "io", "memory"))
    files.update({"/proc/1/comm": "tini\n", "/proc/self/cgroup": "0::/\n"})
    del files[f"{MANAGER}/cgroup.controllers"], files[f"{OWN}/cgroup.controllers"]
    return FixtureHost("linux", files, statvfs=ROOT_FS)


def mac():
    return FixtureHost(
        "darwin", {}, dirs={"/System/Volumes/Data"},
        which={"sysctl": "/usr/sbin/sysctl", "vm_stat": "/usr/bin/vm_stat"},
        commands={("sysctl", "-n", "hw.memsize"): "17179869184\n",
                  ("sysctl", "-n", "vm.swapusage"): "total = 2048.00M  used = 1022.25M  free = 1025.75M  (encrypted)\n",
                  ("sysctl", "-n", "hw.ncpu"): "8\n",
                  ("sysctl", "-n", "kern.osrelease"): "23.5.0\n",
                  ("sw_vers",): "ProductName:\t\tmacOS\nProductVersion:\t\t14.5\nBuildVersion:\t\t23F79\n",
                  ("ps", "-o", "comm=", "-p", "1"): "/sbin/launchd\n"},
        statvfs={"/System/Volumes/Data": (460 * GIB, 200 * GIB, 190 * GIB)})


def facts(host, **kwargs):
    return read_spec(host, paths_error="no root in this test", **kwargs).as_dict()["facts"]


def states(host):
    return {name: row["state"] for name, row in read_spec(host, paths_error="none").as_dict()["enforcement"].items()}


def test_this_pi_reads_every_fact_with_its_source():
    assert facts(pi()) == {
        "ram": {"source": "/proc/meminfo MemTotal", "value": 16219},
        "mem_available": {"source": "/proc/meminfo MemAvailable", "value": 3947},
        "swap": {"source": "/proc/swaps", "value": {
            "devices": [{"name": "/dev/zram0", "type": "partition", "size_mib": 6144, "used_mib": 5335, "priority": 100},
                        {"name": "/var/swap", "type": "file", "size_mib": 4096, "used_mib": 0, "priority": -2}],
            "total_mib": 10240, "used_mib": 5335}},
        "zram": {"source": "/sys/block/zram*: disksize, comp_algorithm, mm_stat", "value": [
            {"device": "zram0", "size_mib": 6144, "algorithm": "zstd", "mem_limit_mib": 2500,
             "stored_mib": 5189, "compressed_mib": 1017, "memory_used_mib": 1046}]},
        "cpus": {"source": "/sys/devices/system/cpu/online", "value": 4},
        "os": {"source": "/etc/os-release PRETTY_NAME", "value": "Debian GNU/Linux 12 (bookworm)"},
        "kernel": {"source": "/proc/sys/kernel/osrelease", "value": "6.12.75+rpt-rpi-2712"},
        "init": {"source": "/proc/1/comm", "value": "systemd"},
        "systemd_booted": {"source": "/run/systemd/system", "value": True},
        "systemd_run": {"source": "PATH, then systemd-run --version",
                        "value": {"path": "/usr/bin/systemd-run", "version": 252}},
        "cgroup": {"source": "/proc/self/mounts", "value": "v2"},
        "controllers_root": {"source": "/sys/fs/cgroup/cgroup.controllers", "value": ["cpuset", "cpu", "io", "pids"]},
        "user_manager": {"source": MANAGER, "value": True},
        "controllers_user_manager": {"source": f"{MANAGER}/cgroup.controllers", "value": ["cpu", "pids"]},
        "controllers_own": {"source": "/proc/self/cgroup, then its cgroup.controllers",
                            "value": {"cgroup": OWN[len("/sys/fs/cgroup"):], "controllers": ["pids"]}},
        "pressure": {"source": "/proc/pressure", "value": []},
        "kernel_cmdline": {"source": "/proc/cmdline, its cgroup, psi and swapaccount settings only",
                           "value": ["cgroup_disable=memory"]},
        "root_fs": {"source": "statvfs /", "value": {"path": "/", "size_mib": 2030592, "used_pct": 13}},
        "bots": {"source": "each fleet.yaml under the data root",
                 "unreadable": "no data root: no root in this test"},
    }


def test_this_pi_reads_the_cap_inert_naming_the_boot_setting():
    rows = read_spec(pi(), paths_error="none").as_dict()["enforcement"]
    assert {name: row["state"] for name, row in rows.items()} == {
        "scope": "available", "memory_cap": "inert", "bot_throttle": "inert",
        "free_memory": "available", "pressure": "unavailable"}
    assert "cgroup_disable=memory" in rows["memory_cap"]["reason"]
    assert "cgroup_enable=memory" in rows["memory_cap"]["changes_it"]
    assert "systemd-oomd cannot act" in rows["pressure"]["reason"]


def test_a_host_whose_controllers_include_memory_can_cap_and_throttle():
    assert states(memory_host()) == {"scope": "available", "memory_cap": "available", "bot_throttle": "available",
                                     "free_memory": "available", "pressure": "available"}
    assert facts(memory_host())["pressure"]["value"] == ["cpu", "io", "memory"]


def test_a_host_with_no_systemd_holds_a_run_by_its_process_group_only():
    host = no_systemd_host()
    read = facts(host)
    assert read["init"]["value"] == "tini" and read["systemd_booted"]["value"] is False
    assert read["systemd_run"]["value"] == "none" and read["user_manager"]["value"] is False
    assert read["controllers_user_manager"]["value"] == "none"
    assert read["controllers_own"]["value"] == {"cgroup": "/", "controllers": ["cpu", "io", "memory", "pids"]}
    rows = read_spec(host, paths_error="none").as_dict()["enforcement"]
    assert {name: row["state"] for name, row in rows.items()} == {
        "scope": "unavailable", "memory_cap": "unavailable", "bot_throttle": "unavailable",
        "free_memory": "available", "pressure": "available"}
    assert rows["scope"]["reason"] == "systemd is not the init system, so a heavy run is held by its process group only"
    assert rows["memory_cap"]["reason"] == ("a MemoryMax on a heavy run's scope cannot be set: systemd is not the "
                                            "init system, so a heavy run is held by its process group only")


def test_a_mac_reads_the_cap_unavailable():
    host = mac()
    read = facts(host)
    assert {name: fact.get("value") for name, fact in read.items()} == {
        "ram": 16384, "swap": {"total_mib": 2048, "used_mib": 1022, "encrypted": True}, "cpus": 8,
        "os": "macOS 14.5", "kernel": "23.5.0", "init": "launchd", "vm_stat": "/usr/bin/vm_stat",
        "root_fs": {"path": "/System/Volumes/Data", "size_mib": 471040, "used_pct": 58}, "bots": None}
    rows = read_spec(host, paths_error="none").as_dict()["enforcement"]
    assert {name: row["state"] for name, row in rows.items()} == {
        "scope": "unavailable", "memory_cap": "unavailable", "bot_throttle": "unavailable",
        "free_memory": "available", "pressure": "unavailable"}
    assert rows["memory_cap"]["reason"] == "macOS has no documented memory cap for a process tree"


@pytest.mark.parametrize(("line", "name"), [("MemAvailable:", "MemAvailable"),
                                            ("MemAvailable:    lots kB", "MemAvailable"),
                                            ("MemTotal:       16608304", "MemTotal")])
def test_a_meminfo_line_that_is_not_a_number_is_refused_by_name(line, name):
    meminfo = "\n".join(line if row.startswith(f"{name}:") else row for row in MEMINFO.splitlines()) + "\n"
    host = pi(**{"/proc/meminfo": meminfo})
    read = facts(host)
    fact = read["mem_available" if name == "MemAvailable" else "ram"]
    assert fact == {"source": f"/proc/meminfo {name}",
                    "unreadable": f"the {name} line of /proc/meminfo is not a number of kB: {line!r}"}
    rows = read_spec(host, paths_error="none").as_dict()["enforcement"]
    if name == "MemAvailable":
        assert read["ram"]["value"] == 16219
        assert rows["free_memory"] == {"state": "unavailable",
                                       "reason": f"the memory stop cannot read free memory: {fact['unreadable']}"}
    else:
        assert read["mem_available"]["value"] == 3947 and rows["free_memory"]["state"] == "available"


def test_no_failed_reading_becomes_a_number():
    host = pi(**{"/proc/meminfo": None, "/proc/swaps": "Filename\n/dev/zram0 partition many 0 100\n",
                 "/sys/devices/system/cpu/online": "3-0\n",
                 "/sys/block/zram0/mm_stat": "5441060864 1066273189 1097269248\n"})
    read = facts(host)
    assert read["ram"] == {"source": "/proc/meminfo MemTotal", "unreadable": "/proc/meminfo does not exist"}
    assert read["swap"]["unreadable"] == ("a /proc/swaps row is not a name, type, size, used and priority: "
                                          "'/dev/zram0 partition many 0 100'")
    assert read["cpus"]["unreadable"] == "/sys/devices/system/cpu/online is not a list of CPU ranges: '3-0'"
    assert read["zram"]["unreadable"] == "/sys/block/zram0/mm_stat does not start with four byte counts"
    assert all(("value" in fact) != ("unreadable" in fact) for fact in read.values())


def test_a_memory_controller_kept_from_the_user_manager_is_inert_and_says_where():
    files = linux_files(root="cpuset cpu io memory pids")
    rows = read_spec(FixtureHost("linux", files, statvfs=ROOT_FS, **SYSTEMD), paths_error="none").enforcement
    assert rows["memory_cap"].state == "inert"
    assert rows["memory_cap"].reason.endswith("it is not delegated to the user manager")
    assert rows["memory_cap"].changes_it == "Delegate= on user@.service, which is the operator's"


def test_an_unreadable_controllers_file_leaves_the_cap_unknown():
    host = pi(**{f"{MANAGER}/cgroup.controllers": PermissionError(13, "Permission denied")})
    rows = read_spec(host, paths_error="none").enforcement
    assert facts(host)["controllers_user_manager"]["unreadable"] == "[Errno 13] Permission denied"
    assert rows["memory_cap"].state == rows["bot_throttle"].state == "unknown"
    assert rows["scope"].state == "available"


def test_another_platform_is_refused():
    with pytest.raises(UnsupportedHost, match="reads Linux and macOS hosts, and this one is win32"):
        read_spec(FixtureHost("win32", {}))


def fleet_root(tmp_path: Path, second: str | None) -> Path:
    root = tmp_path / "data"
    (root / "local" / "second").mkdir(parents=True)
    (root / "fleet.yaml").write_text(MINIMAL_FLEET_YAML)
    if second is not None:
        (root / "local" / "second" / "fleet.yaml").write_text(second)
    return root


def test_the_bots_are_counted_over_every_fleet_on_the_data_root(tmp_path):
    root = fleet_root(tmp_path, MINIMAL_FLEET_YAML.replace("name: test-fleet", "name: second"))
    paths = resolve_paths(root=root)
    assert read_spec(pi(), paths=paths).as_dict()["facts"]["bots"] == {
        "source": f"each fleet.yaml under {paths.root}", "value": {"bots": 4, "fleets": 2}}


def test_a_fleet_that_cannot_be_read_leaves_the_count_unreadable(tmp_path):
    root = fleet_root(tmp_path, "fleet: [unclosed\n")
    paths = resolve_paths(root=root)
    bots = read_spec(pi(), paths=paths).as_dict()["facts"]["bots"]
    assert bots["unreadable"].startswith(f"{paths.root / 'local' / 'second' / 'fleet.yaml'} cannot be read: ")


def test_the_verb_reads_this_host_and_says_every_source(tmp_path, capsys):
    root = fleet_root(tmp_path, None)
    assert main(["--root", str(root), "--json", "host", "spec"]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    body = json.loads(out)
    assert body["ok"] is True and body["command"] == "host.spec" and body["error"] is None
    data = body["data"]
    assert data["platform"] in ("linux", "darwin")
    assert data["facts"]["bots"]["value"] == {"bots": 2, "fleets": 1}
    for fact in data["facts"].values():
        assert fact["source"] and set(fact) in ({"source", "value"}, {"source", "unreadable"})
    assert set(data["enforcement"]) == {"scope", "memory_cap", "bot_throttle", "free_memory", "pressure"}
    for row in data["enforcement"].values():
        assert row["state"] in ("available", "inert", "unavailable", "unknown") and row["reason"]
    assert main(["--root", str(root), "host", "spec"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"host spec ({data['platform']}): each fact, then [where it was read]"
    assert "what the OS can enforce here" in lines
    assert f"  bots: 2 in 1 fleet(s) [each fleet.yaml under {root.resolve()}]" in lines
