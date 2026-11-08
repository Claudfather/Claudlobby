#!/usr/bin/env python3
"""compaction-slot.py — one compaction at a time on the host (#1053, build PR 3).

A lease for one Claude Code compaction on the host. The build's executor (PR 4)
is to take it just before it types `/compact` into a bot's pane, and to release
it once the compaction is proven or has ended. Nothing takes it yet.

``take --fleet F --bot B [--json]``
    Grants a lease that names the bot, the time it was taken (the send time) and
    an end LEASE_S (10 minutes) later, and prints its lease ID. Refused, exit 75,
    writing nothing: while a lease is live, whoever holds it (the refusal names
    the holder, so a bot can see that its own compaction is still in flight);
    while the host's 1-minute load is LOAD_LINE (40) or more, the heavy slot's
    own stop line; when the load cannot be read; or when another take or release
    holds the slot's lock for more than LOCK_WAIT_S.

``release --fleet F --bot B --lease ID [--json]``
    Ends that lease before its end. A lease that has already ended, or a lease
    ID, fleet or bot that is not the current lease's, changes nothing
    (`not_held`), so a late release never ends a later lease.

``status [--json]``
    `free` or `held`: the live lease, or the last one and how it ended
    (`released`, `expired` at its end, or `reset` by a host reset), and the
    1-minute load beside the line.

The holder is a short-lived process, so a lease is a record, not a held lock: a
holder that dies leaves its lease to end at its 10 minutes, and a host reset
ends it at once, since its boot ID no longer matches. The record is
`lease.json` in the slot's directory, `$CLAUDLOBBY_ROOT/state/compaction-slot/`,
replaced whole (a temporary file, then a rename), so a reader sees one record or
the next. Each take and release decides under a non-blocking flock on `lock`
beside it, so of two racing takes one is granted. A record that cannot be read
holds nothing: `status` names it, and the next take replaces it, so a damaged
file never wedges the slot. Time is the wall clock, so a clock step moves a
live lease's end by the step.

The load is `<proc>/loadavg` where it exists, else `os.getloadavg()` (macOS).

Exit: 0 granted, released, not held or read; 75 refused (retry later); 2 a
usage or setup error (no absolute CLAUDLOBBY_ROOT, a directory or record that
cannot be written).

Test seams: COMPACTION_SLOT_DIR (the slot's directory), COMPACTION_SLOT_PROC (a
directory standing in for /proc's loadavg), COMPACTION_SLOT_BOOT_ID, and
COMPACTION_SLOT_NOW (epoch seconds standing in for the clock).

Stdlib only, Python 3.9.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple

EX_TEMPFAIL = 75  # refused: retry later
LEASE_S = 600  # a lease ends 10 minutes after it was taken
LOAD_LINE = 40.0  # no take while the 1-minute load is this or more (heavy-slot.py's stop line)
LOCK_WAIT_S = 2.0  # a take or a release holds the lock for milliseconds
RECORD_MAX = 65536  # bytes of a record read; a lease is a few hundred
RECORD = "lease.json"
LOCK = "lock"
_NAME = re.compile(r"[^\s/\x00-\x1f\x7f]{1,128}")


class SetupError(Exception):
    """The slot cannot be used: no data root, or its files cannot be written."""


def slot_dir() -> Path:
    seam = os.environ.get("COMPACTION_SLOT_DIR")
    if seam:
        return Path(seam)
    # This file ships inside an immutable release, one directory per release: a
    # slot found from its own location would split the host's one slot across
    # releases. Only the data root names it.
    root = os.environ.get("CLAUDLOBBY_ROOT") or ""
    if not os.path.isabs(root):
        raise SetupError("compaction-slot: no absolute CLAUDLOBBY_ROOT data directory, so no "
                         "host slot")
    return Path(root) / "state" / "compaction-slot"


def boot_id() -> str:
    seam = os.environ.get("COMPACTION_SLOT_BOOT_ID")
    if seam:
        return seam
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    try:  # macOS
        out = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"], capture_output=True,
                             text=True, timeout=5, stdin=subprocess.DEVNULL).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _now() -> float:
    seam = os.environ.get("COMPACTION_SLOT_NOW")
    return float(seam) if seam else time.time()


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def load1() -> float:
    """The host's 1-minute load. OSError or ValueError when it cannot be read: a
    gate that cannot read the host does not guess."""
    loadavg = Path(os.environ.get("COMPACTION_SLOT_PROC") or "/proc") / "loadavg"
    if loadavg.exists():
        with open(loadavg) as fh:
            return float(fh.read(256).split()[0])
    return os.getloadavg()[0]


def read_record(d: Path) -> Tuple[Optional[dict], Optional[str]]:
    """(record, None); (None, None) when there is none; (None, why) when there is
    one that cannot be read, which holds nothing."""
    path = d / RECORD
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"unreadable: {e.strerror}"
    if not stat.S_ISREG(info.st_mode):
        return None, "unreadable: not a regular file"
    if info.st_size > RECORD_MAX:
        return None, f"unreadable: over {RECORD_MAX} bytes"
    try:
        with open(path, "rb") as fh:
            rec = json.loads(fh.read(RECORD_MAX).decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as e:
        return None, f"unreadable: {type(e).__name__}"
    ok = (isinstance(rec, dict)
          and all(isinstance(rec.get(k), str) for k in ("lease", "fleet", "bot", "boot_id"))
          and all(isinstance(rec.get(k), (int, float)) for k in ("taken", "ends")))
    if not ok:
        return None, "unreadable: not a lease"
    return rec, None


def judge(rec: dict, now: float, boot: str) -> str:
    """live, released, reset (taken on another boot) or expired (at its end)."""
    if isinstance(rec.get("released"), (int, float)):
        return "released"
    if rec["boot_id"] != boot:
        return "reset"
    return "live" if now < rec["ends"] else "expired"


def _write_record(d: Path, rec: dict) -> None:
    tmp = d / f".{RECORD}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, d / RECORD)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise SetupError(f"compaction-slot: the lease cannot be written: {e}") from e


@contextlib.contextmanager
def _locked(d: Path) -> Iterator[bool]:
    """Holds the slot's lock for one decision: yields True, or False when it was
    not had within LOCK_WAIT_S. The fd is not inherited, so no child holds it."""
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        raise SetupError(f"compaction-slot: {d} cannot be used: {e}") from e
    try:
        deadline, had = time.monotonic() + LOCK_WAIT_S, False
        while not had:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                had = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
        yield had
    finally:
        os.close(fd)


def take(fleet: str, bot: str) -> Tuple[int, dict]:
    d, boot = slot_dir(), boot_id()  # read before the lock: on macOS it runs sysctl
    with _locked(d) as had:
        if not had:
            return EX_TEMPFAIL, {"outcome": "refused", "reason": "lock_busy"}
        now = _now()
        rec, _problem = read_record(d)
        if rec is not None and judge(rec, now, boot) == "live":
            return EX_TEMPFAIL, {"outcome": "refused", "reason": "held",
                                 "lease": {**rec, "left_s": round(rec["ends"] - now, 1)}}
        try:
            load = load1()
        except (OSError, ValueError, IndexError) as e:
            return EX_TEMPFAIL, {"outcome": "refused", "reason": "load_unreadable",
                                 "error": str(e) or type(e).__name__}
        if load >= LOAD_LINE:
            return EX_TEMPFAIL, {"outcome": "refused", "reason": "load", "load1": load,
                                 "load_line": LOAD_LINE}
        lease = {"lease": os.urandom(8).hex(), "fleet": fleet, "bot": bot, "taken": now,
                 "taken_at": _iso(now), "ends": now + LEASE_S, "ends_at": _iso(now + LEASE_S),
                 "boot_id": boot, "pid": os.getpid(), "load1": load}
        _write_record(d, lease)
        return 0, {"outcome": "granted", "lease": lease}


def release(fleet: str, bot: str, lease_id: str) -> Tuple[int, dict]:
    d, boot = slot_dir(), boot_id()
    with _locked(d) as had:
        if not had:
            return EX_TEMPFAIL, {"outcome": "refused", "reason": "lock_busy"}
        now = _now()
        rec, _problem = read_record(d)
        if rec is None or (rec["lease"], rec["fleet"], rec["bot"]) != (lease_id, fleet, bot):
            return 0, {"outcome": "not_held", "reason": "not_this_lease"}
        state = judge(rec, now, boot)
        if state != "live":
            return 0, {"outcome": "not_held", "reason": state, "lease": rec}
        rec = {**rec, "released": now, "released_at": _iso(now)}
        _write_record(d, rec)
        return 0, {"outcome": "released", "lease": rec}


def status() -> Tuple[int, dict]:
    d = slot_dir()
    now, boot = _now(), boot_id()
    rec, problem = read_record(d)
    out: dict = {"state": "free", "lease": None, "last": None, "load_line": LOAD_LINE}
    if rec is not None:
        state = judge(rec, now, boot)
        if state == "live":
            out.update(state="held", lease={**rec, "left_s": round(rec["ends"] - now, 1)})
        else:
            out["last"] = {**rec, "ended": state}
    if problem:
        out["record"] = problem
    try:
        out["load1"] = load1()
    except (OSError, ValueError, IndexError) as e:
        out["load1"] = None
        out["load_error"] = str(e) or type(e).__name__
    return 0, out


def _line(verb: str, result: dict) -> str:
    lease = result.get("lease") or {}
    who = f"{lease.get('fleet')}/{lease.get('bot')}"
    if verb == "status":
        load = result.get("load1")
        load_text = "unreadable" if load is None else f"{load:.2f}"
        if result["state"] == "held":
            head = f"held by {who} until {lease.get('ends_at')} (lease {lease.get('lease')})"
        else:
            head = "free"
        return f"compaction slot: {head}; load1 {load_text} (takes refused at {LOAD_LINE:g})"
    outcome, reason = result["outcome"], result.get("reason")
    if outcome == "granted":
        return f"granted: lease {lease['lease']} until {lease['ends_at']}"
    if outcome == "released":
        return f"released: lease {lease['lease']}"
    if reason == "held":
        return f"refused: held by {who} until {lease.get('ends_at')}"
    if reason == "load":
        return f"refused: load1 {result['load1']:.2f} is at or over {LOAD_LINE:g}"
    return f"{outcome}: {reason}"


def _name(text: str) -> str:
    if not _NAME.fullmatch(text):
        raise argparse.ArgumentTypeError("a name: 1-128 characters, no space or slash")
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="compaction-slot.py",
                                     description="One compaction at a time on the host.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb in ("take", "release", "status"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--json", action="store_true", help="print the result as JSON")
        if verb != "status":
            sub.add_argument("--fleet", required=True, type=_name)
            sub.add_argument("--bot", required=True, type=_name)
        if verb == "release":
            sub.add_argument("--lease", required=True, type=_name)
    args = parser.parse_args(argv)
    try:
        if args.verb == "take":
            rc, result = take(args.fleet, args.bot)
        elif args.verb == "release":
            rc, result = release(args.fleet, args.bot, args.lease)
        else:
            rc, result = status()
    except SetupError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True) if args.json else _line(args.verb, result))
    return rc


if __name__ == "__main__":
    sys.exit(main())
