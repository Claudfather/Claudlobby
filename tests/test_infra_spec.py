"""`claudlobby host spec`: a host's facts, each with its source, and what its OS can enforce.

Fixtures stand for the hosts the reader has to tell apart: a Raspberry Pi 5 whose firmware
disables the memory controller, a Mac, a Linux host with no systemd, a Linux host whose
controllers include memory, a systemd host on cgroup v1 or hybrid, and one whose user runs no
user manager. Every fact either has a value or says why it is unreadable; none stands in a 0.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from claudlobby.__main__ import main
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
        self.on_path = dict(which or {})
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
        if isinstance(self.files.get(path), OSError):
            raise self.files[path]
        if path not in self.dirs:
            return None
        prefix = path.rstrip("/") + "/"
        return sorted({entry[len(prefix):].split("/")[0] for entry in (*self.files, *self.dirs)
                       if entry.startswith(prefix)})

    def which(self, name):
        return self.on_path.get(name)

    def run(self, argv):
        key = (PurePosixPath(argv[0]).name, *argv[1:])
        if key not in self.commands:
            raise spec.Unreadable(f"`{' '.join(argv)}` exited 1")
        return self.commands[key]

    def statvfs(self, path):
        return self.sizes[path]

    def paths(self, root):
        if root is None:
            raise ValueError("no data root in this fixture")
        return super().paths(root)


MEMINFO = "MemTotal:       16608304 kB\nMemFree:          512000 kB\nMemAvailable:    4041728 kB\nSwapTotal:      10485752 kB\n"
SWAPS = ("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
         "/dev/zram0                              partition\t6291452\t\t5463012\t\t100\n"
         "/var/swap                               file\t\t4194300\t\t0\t\t-2\n")
ROOT_FS = {"/": (1983 * GIB, 1745 * GIB, 1650 * GIB)}
ROOT_MOUNT = "/dev/sda2 / ext4 rw,noatime 0 0\n"


def linux_files(*, root="cpuset cpu io pids", manager="cpu pids", own="pids", cmdline="", pressure=(), drop=()):
    """A cgroup v2 systemd host's files; `drop` names path prefixes to leave out."""
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
        "/proc/self/mounts": ROOT_MOUNT + "cgroup2 /sys/fs/cgroup cgroup2 rw,nosuid,nodev,noexec,relatime 0 0\n",
        "/sys/fs/cgroup/cgroup.controllers": root + "\n",
        f"{MANAGER}/cgroup.controllers": manager + "\n",
        "/proc/self/cgroup": f"0::{OWN[len('/sys/fs/cgroup'):]}\n",
        f"{OWN}/cgroup.controllers": own + "\n",
        "/proc/cmdline": f"console=tty1 root=/dev/sda2 rootfstype=ext4 {cmdline} rootwait\n",
    }
    files.update({f"/proc/pressure/{name}": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" for name in pressure})
    return {path: text for path, text in files.items() if not path.startswith(drop)}


SYSTEMD = {"which": {"systemd-run": "/usr/bin/systemd-run"}, "dirs": {"/run/systemd/system"}}


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


V1_MOUNTS = (ROOT_MOUNT
             + "tmpfs /sys/fs/cgroup tmpfs ro,nosuid,nodev,noexec,mode=755 0 0\n"
             "cgroup /sys/fs/cgroup/systemd cgroup rw,nosuid,nodev,noexec,relatime,xattr,name=systemd 0 0\n"
             "cgroup /sys/fs/cgroup/memory cgroup rw,nosuid,nodev,noexec,relatime,memory 0 0\n")


def legacy_host(layout):
    """A systemd host on cgroup v1, or hybrid (v1 controllers, cgroup2 at /sys/fs/cgroup/unified). Its user
    manager runs, but in the v1 layout, so nothing is where cgroup v2 keeps it at /sys/fs/cgroup."""
    files = linux_files(drop=("/sys/fs/cgroup/",))
    unit = OWN[len("/sys/fs/cgroup"):]
    files.update({"/proc/self/mounts": V1_MOUNTS,
                  "/proc/self/cgroup": f"4:memory:/user.slice/user-{UID}.slice/user@{UID}.service\n1:name=systemd:{unit}\n",
                  f"/sys/fs/cgroup/systemd{unit}/cgroup.procs": "1234\n"})
    if layout == "hybrid":
        files["/proc/self/mounts"] += "cgroup2 /sys/fs/cgroup/unified cgroup2 rw,nosuid,nodev,noexec,relatime 0 0\n"
        files["/proc/self/cgroup"] += f"0::{unit}\n"
        files["/sys/fs/cgroup/unified/cgroup.controllers"] = "\n"
    return FixtureHost("linux", files, statvfs=ROOT_FS, **SYSTEMD)


def no_user_manager_host():
    """A systemd host on cgroup v2 whose user runs no user manager: the caller sits in a login session."""
    session = f"/user.slice/user-{UID}.slice/session-3.scope"
    files = linux_files(cmdline="cgroup_disable=memory", drop=(f"{MANAGER}/",))
    files.update({"/proc/self/cgroup": f"0::{session}\n", f"/sys/fs/cgroup{session}/cgroup.controllers": "pids\n"})
    return FixtureHost("linux", files, statvfs=ROOT_FS, **SYSTEMD)


def no_systemd_run_host():
    """This Pi with no systemd-run on PATH."""
    host = pi()
    del host.on_path["systemd-run"]
    return host


def read(host, root=None):
    """The spec's facts and enforcement rows, from one read."""
    data = read_spec(host, root=root).as_dict()
    return data["facts"], data["enforcement"]


def states(rows):
    return {name: row["state"] for name, row in rows.items()}


def test_this_pi_reads_every_fact_with_its_source():
    assert read(pi())[0] == {
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
        "systemd_run": {"source": "PATH", "value": "/usr/bin/systemd-run"},
        "cgroup": {"source": "/proc/self/mounts", "value": "v2"},
        "controllers_root": {"source": "/sys/fs/cgroup/cgroup.controllers", "value": ["cpuset", "cpu", "io", "pids"]},
        "user_manager": {"source": MANAGER, "value": True},
        "controllers_user_manager": {"source": f"{MANAGER}/cgroup.controllers", "value": ["cpu", "pids"]},
        "own_cgroup": {"source": "/proc/self/cgroup", "value": OWN[len("/sys/fs/cgroup"):]},
        "controllers_own": {"source": "the own cgroup's cgroup.controllers", "value": ["pids"]},
        "pressure": {"source": "/proc/pressure", "value": []},
        "kernel_cmdline": {"source": "/proc/cmdline, its cgroup, psi and swapaccount settings only",
                           "value": ["cgroup_disable=memory"]},
        "root_fs": {"source": "statvfs /", "value": {"path": "/", "size_mib": 2030592, "used_pct": 13}},
        "bots": {"source": "each fleet.yaml under the data root",
                 "unreadable": "no data root: no data root in this fixture"},
    }


def test_this_pi_reads_the_cap_inert_naming_the_boot_setting():
    rows = read(pi())[1]
    assert states(rows) == {
        "scope": "available", "memory_cap": "inert", "bot_throttle": "inert",
        "free_memory": "available", "pressure": "unavailable"}
    assert "cgroup_disable=memory" in rows["memory_cap"]["reason"]
    assert "cgroup_enable=memory" in rows["memory_cap"]["changes_it"]
    assert "systemd-oomd cannot act" in rows["pressure"]["reason"]


def test_a_host_whose_controllers_include_memory_can_cap_and_throttle():
    facts, rows = read(memory_host())
    assert states(rows) == {"scope": "available", "memory_cap": "available", "bot_throttle": "available",
                            "free_memory": "available", "pressure": "available"}
    assert facts["pressure"]["value"] == ["cpu", "io", "memory"]


def test_a_host_with_no_systemd_holds_a_run_by_its_process_group_only():
    facts, rows = read(no_systemd_host())
    assert facts["init"]["value"] == "tini" and facts["systemd_booted"]["value"] is False
    assert facts["systemd_run"]["value"] == "none" and facts["user_manager"]["value"] is False
    assert facts["controllers_user_manager"]["value"] == "none"
    assert facts["own_cgroup"]["value"] == "/"
    assert facts["controllers_own"]["value"] == ["cpu", "io", "memory", "pids"]
    assert states(rows) == {
        "scope": "unavailable", "memory_cap": "unavailable", "bot_throttle": "unavailable",
        "free_memory": "available", "pressure": "available"}
    assert rows["scope"]["reason"] == "systemd is not the init system, so a heavy run is held by its process group only"
    assert rows["memory_cap"]["reason"] == ("a MemoryMax on a heavy run's scope cannot be set: systemd is not the "
                                            "init system, so a heavy run is held by its process group only")


def test_a_mac_reads_the_cap_unavailable():
    facts, rows = read(mac())
    assert {name: fact.get("value") for name, fact in facts.items()} == {
        "ram": 16384, "swap": {"total_mib": 2048, "used_mib": 1022, "encrypted": True}, "cpus": 8,
        "os": "macOS 14.5", "kernel": "23.5.0", "init": "launchd", "vm_stat": "/usr/bin/vm_stat",
        "root_fs": {"path": "/System/Volumes/Data", "size_mib": 471040, "used_pct": 58}, "bots": None}
    assert states(rows) == {
        "scope": "unavailable", "memory_cap": "unavailable", "bot_throttle": "unavailable",
        "free_memory": "available", "pressure": "unavailable"}
    assert rows["memory_cap"]["reason"] == "macOS has no documented memory cap for a process tree"


@pytest.mark.parametrize(("line", "name"), [("MemAvailable:", "MemAvailable"),
                                            ("MemAvailable:    lots kB", "MemAvailable"),
                                            ("MemTotal:       16608304", "MemTotal")])
def test_a_meminfo_line_that_is_not_a_number_is_refused_by_name(line, name):
    meminfo = "\n".join(line if row.startswith(f"{name}:") else row for row in MEMINFO.splitlines()) + "\n"
    facts, rows = read(pi(**{"/proc/meminfo": meminfo}))
    fact = facts["mem_available" if name == "MemAvailable" else "ram"]
    assert fact == {"source": f"/proc/meminfo {name}",
                    "unreadable": f"the {name} line of /proc/meminfo is not a number of kB: {line!r}"}
    if name == "MemAvailable":
        assert facts["ram"]["value"] == 16219
        assert rows["free_memory"] == {"state": "unavailable",
                                       "reason": f"the memory stop cannot read free memory: {fact['unreadable']}"}
    else:
        assert facts["mem_available"]["value"] == 3947 and rows["free_memory"]["state"] == "available"


def test_no_failed_reading_becomes_a_number():
    host = pi(**{"/proc/meminfo": None, "/proc/swaps": "Filename\n/dev/zram0 partition many 0 100\n",
                 "/sys/devices/system/cpu/online": "3-0\n",
                 "/sys/block/zram0/mm_stat": "5441060864 1066273189 1097269248\n"})
    facts = read(host)[0]
    assert facts["ram"] == {"source": "/proc/meminfo MemTotal", "unreadable": "/proc/meminfo does not exist"}
    assert facts["swap"]["unreadable"] == ("a /proc/swaps row is not a name, type, size, used and priority: "
                                           "'/dev/zram0 partition many 0 100'")
    assert facts["cpus"]["unreadable"] == "/sys/devices/system/cpu/online is not a list of CPU ranges: '3-0'"
    assert facts["zram"]["unreadable"] == "/sys/block/zram0/mm_stat does not start with four byte counts"
    assert all(("value" in fact) != ("unreadable" in fact) for fact in facts.values())


def test_a_memory_controller_kept_from_the_user_manager_is_inert_and_says_where():
    files = linux_files(root="cpuset cpu io memory pids")
    rows = read_spec(FixtureHost("linux", files, statvfs=ROOT_FS, **SYSTEMD)).enforcement
    assert rows["memory_cap"].state == "inert"
    assert rows["memory_cap"].reason.endswith("it is not delegated to the user manager")
    assert rows["memory_cap"].changes_it == "Delegate= on user@.service, which is the operator's"


def test_an_unreadable_controllers_file_leaves_the_cap_unknown():
    spec = read_spec(pi(**{f"{MANAGER}/cgroup.controllers": PermissionError(13, "Permission denied")}))
    rows = spec.enforcement
    assert spec.facts["controllers_user_manager"].unreadable == "[Errno 13] Permission denied"
    assert rows["memory_cap"].state == rows["bot_throttle"].state == "unknown"
    assert rows["scope"].state == "available"


@pytest.mark.parametrize(("path", "broken", "fact", "reason"), [
    ("/proc/self/mounts", None, "cgroup", "/proc/self/mounts does not exist"),
    (MANAGER, PermissionError(13, "Permission denied"), "user_manager", "[Errno 13] Permission denied"),
], ids=["cgroup", "user_manager"])
def test_an_unreadable_fact_a_holder_needs_leaves_its_rows_unknown_and_names_it(path, broken, fact, reason):
    rows = read(pi(**{path: broken}))[1]
    unknown = {"state": "unknown", "reason": f"the {fact} fact is unreadable: {reason}"}
    assert rows["scope"] == rows["memory_cap"] == rows["bot_throttle"] == unknown


@pytest.mark.parametrize("layout", ["v1", "hybrid"])
def test_a_cgroup_v1_or_hybrid_host_names_cgroup_v2_for_the_throttle(layout):
    facts, rows = read(legacy_host(layout))
    assert facts["cgroup"]["value"] == layout and facts["controllers_root"]["value"] == "none"
    assert facts["user_manager"]["value"] is False
    assert states(rows) == {"scope": "unavailable", "memory_cap": "unavailable", "bot_throttle": "unavailable",
                            "free_memory": "available", "pressure": "unavailable"}
    assert rows["scope"]["reason"] == ("cgroup v2 is not mounted at /sys/fs/cgroup, so a heavy run is held by its "
                                       "process group only")
    assert rows["bot_throttle"]["reason"] == ("a MemoryHigh on a bot's unit cannot be set: cgroup v2 is not mounted "
                                              "at /sys/fs/cgroup, and MemoryHigh needs it")


@pytest.mark.parametrize(("host", "scope", "throttle"), [
    (no_systemd_run_host, "systemd-run is not on PATH", "inert"),
    (no_user_manager_host, "no systemd user manager runs for this user", "unavailable"),
], ids=["no systemd-run", "no user manager"])
def test_a_scope_needs_systemd_run_and_a_user_manager(host, scope, throttle):
    rows = read(host())[1]
    reason = f"{scope}, so a heavy run is held by its process group only"
    assert rows["scope"] == {"state": "unavailable", "reason": reason}
    assert rows["memory_cap"] == {"state": "unavailable",
                                  "reason": f"a MemoryMax on a heavy run's scope cannot be set: {reason}"}
    assert rows["bot_throttle"]["state"] == throttle
    if throttle == "unavailable":
        assert rows["bot_throttle"]["reason"] == ("a MemoryHigh on a bot's unit cannot be set: bots do not run "
                                                  "under a systemd user manager here")


@pytest.mark.parametrize("cmdline", ["console=tty1 rootwait\n", "console=tty1 cgroup_disable=hugetlb rootwait\n", None],
                         ids=["no setting", "a setting that keeps memory", "unreadable"])
def test_a_kernel_with_no_memory_controller_reads_inert_with_no_setting_to_name(cmdline):
    rows = read(pi(**{"/proc/cmdline": cmdline}))[1]
    for name, what in (("memory_cap", "a MemoryMax on a heavy run's scope"), ("bot_throttle", "a MemoryHigh on a bot's unit")):
        assert rows[name] == {
            "state": "inert",
            "reason": f"{what} would be accepted and enforce nothing: the kernel offers no memory controller",
            "changes_it": "a kernel and boot that provide the memory controller; the operator's"}


def test_a_cgroup_disable_list_that_includes_memory_is_named():
    rows = read(pi(**{"/proc/cmdline": "console=tty1 cgroup_disable=hugetlb,memory rootwait\n"}))[1]
    assert rows["bot_throttle"]["reason"] == ("a MemoryHigh on a bot's unit would be accepted and enforce nothing: the "
                                              "kernel offers no memory controller: its command line carries "
                                              "cgroup_disable=hugetlb,memory")
    assert "cgroup_enable=memory" in rows["bot_throttle"]["changes_it"]


def test_an_unreadable_pressure_directory_leaves_pressure_unknown():
    facts, rows = read(pi(**{"/proc/pressure": PermissionError(13, "Permission denied")}))
    assert facts["pressure"] == {"source": "/proc/pressure", "unreadable": "[Errno 13] Permission denied"}
    assert rows["pressure"] == {"state": "unknown",
                                "reason": "the pressure fact is unreadable: [Errno 13] Permission denied"}


def test_a_mac_without_vm_stat_leaves_the_memory_stop_unavailable():
    host = mac()
    del host.on_path["vm_stat"]
    facts, rows = read(host)
    assert facts["vm_stat"] == {"source": "PATH", "value": "none"}
    assert rows["free_memory"] == {"state": "unavailable",
                                   "reason": "vm_stat is not on PATH, so the memory stop cannot read free memory"}


def test_a_filesystem_with_no_blocks_leaves_its_percent_out():
    fs = read_spec(FixtureHost("linux", linux_files(), statvfs={"/": (0, 0, 0)}, **SYSTEMD)).facts["root_fs"]
    assert fs.as_dict() == {"source": "statvfs /", "value": {"path": "/", "size_mib": 0}}
    assert fs.line("root_fs") == "  root_fs: 0 MiB, percent used unknown: no blocks are in use or available [statvfs /]"


def test_a_fact_the_host_has_nothing_for_reads_none_and_keeps_its_shape_in_json():
    files = linux_files(root="cpu io memory pids", drop=("/sys/block/zram0/", f"{MANAGER}/"))
    files.update({"/proc/1/comm": "tini\n", "/proc/self/cgroup": "0::/\n"})
    facts = read_spec(FixtureHost("linux", files, statvfs=ROOT_FS)).facts
    assert {name: (facts[name].value, facts[name].shown) for name in
            ("zram", "systemd_run", "user_manager", "controllers_user_manager", "pressure", "kernel_cmdline")} == {
        "zram": ([], "none"), "systemd_run": ("none", "none (not on PATH)"),
        "user_manager": (False, "none (no user manager's cgroup)"),
        "controllers_user_manager": ("none", "none (no user manager's cgroup)"),
        "pressure": ([], "none (/proc/pressure is absent)"), "kernel_cmdline": ([], "none of these settings")}


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
    assert read(pi(), root)[0]["bots"] == {
        "source": "each fleet.yaml under the data root",
        "value": {"bots": 4, "fleets": 2, "root": str(root.resolve())}}


def test_a_fleet_that_cannot_be_read_leaves_the_count_unreadable(tmp_path):
    root = fleet_root(tmp_path, "fleet: [unclosed\n")
    bots = read(pi(), root)[0]["bots"]
    assert bots["unreadable"].startswith(f"{root.resolve() / 'local' / 'second' / 'fleet.yaml'} cannot be read: ")


def test_the_verb_reads_this_host_and_says_every_source(tmp_path, capsys):
    root = fleet_root(tmp_path, None)
    assert main(["--root", str(root), "--json", "host", "spec"]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    body = json.loads(out)
    assert body["ok"] is True and body["command"] == "host.spec" and body["error"] is None
    data = body["data"]
    assert data["platform"] in ("linux", "darwin")
    assert data["facts"]["bots"]["value"] == {"bots": 2, "fleets": 1, "root": str(root.resolve())}
    for fact in data["facts"].values():
        assert fact["source"] and set(fact) in ({"source", "value"}, {"source", "unreadable"})
    assert set(data["enforcement"]) == {"scope", "memory_cap", "bot_throttle", "free_memory", "pressure"}
    for row in data["enforcement"].values():
        assert row["state"] in ("available", "inert", "unavailable", "unknown") and row["reason"]
    assert main(["--root", str(root), "host", "spec"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"host spec ({data['platform']}): each fact, then [where it was read]"
    assert "what the OS can enforce here" in lines
    assert f"  bots: 2 in 1 fleet(s) under {root.resolve()} [each fleet.yaml under the data root]" in lines
