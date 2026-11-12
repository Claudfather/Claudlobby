#!/usr/bin/env python3
"""browser-scan.py -- the browsers running on this host outside a render scope (#2332).

    browser-scan.py --pattern REGEX [--min-age SECONDS] [--unit LABEL=OWNER ...] [--proc DIR]

Prints one tab-separated line per browser that runs outside a heavy-slot render
scope and is at least --min-age seconds old (default 900), oldest first:

    <pid> <start> <age s> <processes> <pss MiB> <owner or -> <unit> <name>

- A browser is a process whose executable's name matches --pattern (fleet-pulse
  passes lib-common's BROWSER_EXE_PATTERN, the names orphan-browser-reaper.sh
  reaps by) and whose parent's does not, with every process below it and any
  crash handler started beside it (same parent). The name is the executable's
  own (/proc/PID/exe), which the kernel does not cut at 15 characters as it does
  a process's comm; a crash handler is never a browser by itself.
- `start` is its start time in clock ticks since boot: with the pid, a key that
  a recycled pid does not repeat.
- It is inside a render scope when its cgroup, or an ancestor's, is a heavy-slot
  render scope (`heavy-render-*.scope`, #2372). Ancestors count because Chromium
  moves itself into a systemd scope of its own when it can reach the session bus.
- Its owner is the OWNER of the first --unit LABEL=OWNER whose `LABEL.service`
  holds it or, walking up by parent link, an ancestor of it: the bot whose
  session started it, even after it moved out of that bot's unit. `-` when none.
- `unit` is the last part of its own cgroup path.

It reads /proc and signals nothing. Only this user's processes count. With no
/proc to read (macOS) it prints nothing and exits 0. Exit 2: usage. Test seam:
BROWSER_SCAN_PROC (or --proc) names another directory to read as /proc.
"""

import argparse
import os
import re
import sys
from pathlib import Path

RENDER_SCOPE = re.compile(r"^heavy-render-.+\.scope$")
CRASH_HANDLER = re.compile(r"crashpad")
MAX_DEPTH = 64


def _read(path):
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _exe_name(d):
    """The executable's basename; comm when exe cannot be read."""
    try:
        name = os.path.basename(os.readlink(str(d / "exe")))
    except OSError:
        comm = _read(d / "comm")
        return comm.strip() if comm else ""
    # a binary replaced on disk (a package upgrade) reads "NAME (deleted)"
    return name[:-len(" (deleted)")] if name.endswith(" (deleted)") else name


def _cgroup(text):
    """The unified (v2) cgroup path, else the last hierarchy's."""
    lines = [line for line in (text or "").splitlines() if line]
    for line in lines:
        if line.startswith("0::"):
            return line[3:]
    return lines[-1].split(":", 2)[-1] if lines else ""


def _kib(d):
    """PSS in KiB (smaps_rollup), else RSS (status), else 0."""
    for name, key in (("smaps_rollup", "Pss:"), ("status", "VmRSS:")):
        for line in (_read(d / name) or "").splitlines():
            if line.startswith(key):
                try:
                    return int(line.split()[1])
                except (IndexError, ValueError):
                    break
    return 0


def processes(proc):
    """pid -> (ppid, start ticks, exe name, cgroup path) for this user's processes."""
    uid = os.getuid()
    table = {}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
        except OSError:
            continue
        stat = _read(d / "stat")
        if not stat or ")" not in stat:
            continue
        # comm may hold spaces and parentheses: the fields after it follow the last ")"
        rest = stat[stat.rindex(")") + 2:].split()
        try:
            ppid, start = int(rest[1]), int(rest[19])
        except (IndexError, ValueError):
            continue
        table[int(d.name)] = (ppid, start, _exe_name(d), _cgroup(_read(d / "cgroup")))
    return table


def scan(proc, pattern, min_age, units):
    table = processes(proc)
    try:
        uptime = float((_read(proc / "uptime") or "").split()[0])
    except (IndexError, ValueError):
        return []
    tick = os.sysconf("SC_CLK_TCK")
    browser = {pid for pid, (_, _, name, _) in table.items() if name and pattern.search(name)}
    children = {}
    for pid, (ppid, _, _, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    tops = [pid for pid in browser if table[pid][0] not in browser]
    roots = sorted(pid for pid in tops if not CRASH_HANDLER.search(table[pid][2]))
    beside = [pid for pid in tops if CRASH_HANDLER.search(table[pid][2])]
    rows = []
    for root in roots:
        ppid, start, name, cgroup = table[root]
        age = int(uptime - start / tick)
        if age < min_age:
            continue
        line, pid, owner, render = [root], ppid, "-", False
        while pid in table and len(line) < MAX_DEPTH:
            line.append(pid)
            pid = table[pid][0]
        for pid in line:
            parts = table[pid][3].split("/")
            if any(RENDER_SCOPE.match(part) for part in parts):
                render = True
                break
            if owner == "-" and parts[-1].endswith(".service"):
                owner = units.get(parts[-1][:-len(".service")], "-")
        if render:
            continue
        tree, todo = set(), [root] + [pid for pid in beside if table[pid][0] == ppid]
        while todo:
            pid = todo.pop()
            if pid not in tree:
                tree.add(pid)
                todo.extend(children.get(pid, ()))
        pss = sum(_kib(proc / str(pid)) for pid in tree) // 1024
        unit = cgroup.rsplit("/", 1)[-1] or "-"
        rows.append((age, root, start, len(tree), pss, owner, unit, name))
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [(root, start, age, count, pss, owner, unit, name)
            for age, root, start, count, pss, owner, unit, name in rows]


def main(argv):
    ap = argparse.ArgumentParser(prog="browser-scan.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--pattern", required=True, help="regex over an executable's name")
    ap.add_argument("--min-age", type=int, default=900, help="seconds (default 900)")
    ap.add_argument("--unit", action="append", default=[], metavar="LABEL=OWNER")
    ap.add_argument("--proc", default=os.environ.get("BROWSER_SCAN_PROC") or "/proc",
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        pattern = re.compile(args.pattern)
    except re.error as e:
        ap.error(f"--pattern does not compile: {e}")
    units = {}
    for pair in args.unit:
        label, sep, owner = pair.partition("=")
        if not sep or not label or not owner:
            ap.error(f"--unit takes LABEL=OWNER, got {pair!r}")
        units[label] = owner
    proc = Path(args.proc)
    if not (proc / "uptime").is_file():
        return 0
    for row in scan(proc, pattern, args.min_age, units):
        print("\t".join(str(field) for field in row))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
