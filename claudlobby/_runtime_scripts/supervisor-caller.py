#!/usr/bin/env python3
"""Kernel membership predicates for supervisor.sh: 0 external, 1 member, 3 unknown."""

from pathlib import Path
import re
import subprocess
import sys


def ancestry(pid: int) -> list[int]:
    chain = []
    while pid > 1:
        if pid in chain or len(chain) >= 256:
            raise ValueError("invalid process ancestry")
        chain.append(pid)
        result = subprocess.run(["ps", "-p", str(pid), "-o", "ppid="],
                                check=True, text=True, capture_output=True, timeout=5)
        parent = int(result.stdout.strip())
        if parent < 1:
            raise ValueError("incomplete process ancestry")
        pid = parent
    if not chain:
        raise ValueError("caller is not an ordinary process")
    return chain


def main(mode: str, caller: str, target: str) -> int:
    try:
        chain = ancestry(int(caller))
        if mode == "launchd":
            selected, separator, jobs = target.partition(":")
            pids = {int(pid) for pid in jobs.split()}
            if not separator or not pids or any(pid <= 1 for pid in pids):
                raise ValueError("invalid loaded job pids")
            if selected != "-" and int(selected) in chain:
                result = True
            elif pids.intersection(chain):
                result = False
            else:
                raise ValueError("no owning launchd job in caller ancestry")
        elif mode in {"cgroup", "unit"}:
            if mode == "cgroup":
                if not target.startswith("/") or target == "/" or "\n" in target:
                    raise ValueError("invalid unit control group")
            elif not re.fullmatch(r"(?:[A-Za-z0-9_.@:-]|\\x[0-9A-Fa-f]{2})+\.service", target):
                raise ValueError("invalid exact unit name")
            result = False
            for pid in chain:
                groups = []
                for row in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
                    hierarchy, controllers, path = row.split(":", 2)
                    if hierarchy == "0" or "name=systemd" in controllers.split(","):
                        if not path.startswith("/"):
                            raise ValueError("invalid process control group")
                        groups.append(path)
                if not groups:
                    raise ValueError("systemd control group unavailable")
                if mode == "cgroup":
                    result |= any(path == target or path.startswith(target + "/") for path in groups)
                else:
                    for path in groups:
                        components = path.split("/")[1:]
                        if target in components:
                            result = True
                        elif any("\\x" in component and component.endswith(".service")
                                 for component in components):
                            # A kernel-escaped service name might denote this
                            # unit; absence of a plain-text match is no proof.
                            raise ValueError("ambiguous escaped unit cgroup")
        else:
            raise ValueError("unknown membership predicate")
        if ancestry(int(caller)) != chain:
            raise ValueError("process ancestry changed during observation")
        return int(result)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 3


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]) if len(sys.argv) == 4 else 3)
