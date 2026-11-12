#!/usr/bin/env python3
"""memory-thrash.py -- swap thrash, read from the per-minute host probe's swap-out counter (#1645).

    memory-thrash.py check --state-dir DIR (--out PAGES | --skip WHY) [--boot-id ID]
    memory-thrash.py delivered --state-dir DIR (--episode ID | --refused SINCE)
    memory-thrash.py bots [--bots-dir DIR ...] [--top N] [--proc DIR]
    memory-thrash.py replay --samples FILE --lines FILE [--minutes N]
    memory-thrash.py status --state-dir DIR

The rule. plane-host-probe.sh reads the kernel's swap-out counter (`pswpout` in
/proc/vmstat, pages swapped out since boot) once a minute and hands it to `check`,
which compares it with the last reading, kept in DIR/state.json. The rate is the
pages swapped out between the two readings, per minute of the time between them.
A rate over the line (strictly over: a rate equal to it is not) starts or extends
a run. A run of `minutes` readings in a row over the line is an episode, and an
episode raises one alert; the first reading under the line ends it, so the next
run that reaches `minutes` is a second episode with an alert of its own. A new
boot (another boot ID) or a counter that went back starts again from that reading.

The line is the host's own, read from DIR/lines on every check, in `name = value`
lines (`#` starts a comment): `line_pages` (pages a minute), `minutes`, and the
bounds they keep, `line_pages_max` and `minutes_max`, so that a typo cannot raise
the line past every episode the host has had. There is no default: with no file
the check does not run. The one off is the line `alert = off`; no number turns it
off. A file with a name it does not know, a number given twice, a value that is
not a positive number (a whole one for the minutes), a number missing or a bound
broken is refused: the check does not run, and the probe pages the refusal once,
so a bad edit is never a quiet off. read_lines is the one reader of the host's
config, and the Rule it returns is all the rest takes, so moving the config
elsewhere changes read_lines alone.

`check` prints one tab-separated verdict on stdout, for the probe to act on:
`skipped WHY`, `refused SINCE WHY` (the lines file is refused, since SINCE: the
probe pages WHY), `baseline WHY`, `under RATE`, `over RATE RUN`, `alert EPISODE
TEXT` (the probe raises the alert, TEXT saying the line and the run's rates),
`alerted EPISODE RATE` (this episode's alert went out) or `ended EPISODE READINGS
PEAK WHY` (a reading under the line, a new boot or a counter that went back ended
it). It says a skip (a refusal is one), a reading over the line, an alert and an
episode's end on stderr, the probe's journal: a skip only when its reason is new,
so a host with no counter (macOS) or no line says so once, not every minute. An
alert stays unsent until the probe calls `delivered` with its episode, once a
channel took it; until then each later reading of the episode returns `alert`
again. A refusal is the same, with `delivered --refused SINCE`, and is a skip
after it; a file that gives a rule, goes or says alert = off ends the refusal, so
the next bad edit pages again. A refusal wins over a missing counter, so a gap in
the counter does not page it again.

`bots` names the largest bots by memory: each process of this user counts for the
bot whose unit holds it or, walking up by parent link, an ancestor of it
(browser-scan.py's process table, imported from beside this file), at its
proportional resident and swapped size (smaps_rollup Pss and SwapPss, else status
VmRSS and VmSwap). The bots are those of each --bots-dir (the probe passes
lib-common.sh's host_bots_dirs) whose bot.conf names a unit (BOT_SERVICE, read as
bot_conf_get reads it; the unit is LABEL.service), each named FLEET_NAME/BOT. It
prints one line: the --top bots and what no bot holds. With no /proc (macOS) it
prints nothing.

`replay` runs the same rule, from a lines file and with --minutes in place of
its minutes if given, over the samples `claudlobby --json plane samples
host.swap_pages` printed, and prints each alert the rule would have raised (the
reading's time and the rates of its run), then a count. `status` prints
DIR/state.json and the rule DIR/lines gives. Exit 0, except 2 for usage. Test
seams: MEMORY_THRASH_NOW (the epoch `check` reads) and --proc (another directory
to read as /proc, the probe's HOST_PROBE_PROC). Stdlib, Python 3.9.
"""

import argparse
import fcntl
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RATES_KEPT = 10
STATE_FILE = "state.json"
LINES_FILE = "lines"
LOCK_FILE = "lock"
NUMBERS = ("line_pages", "minutes", "line_pages_max", "minutes_max")
WHOLE = ("minutes", "minutes_max")
# bot_conf_get cuts an unquoted value at its first [[:space:]].
UNQUOTED = re.compile(r"[^ \t\n\v\f\r]*")


class Rule:
    """The line in pages a minute, and how many readings in a row make an episode."""

    def __init__(self, line: float, minutes: int):
        self.line = line
        self.minutes = minutes


class Refused(ValueError):
    """The host's lines file is there and gives no rule; the message says why."""


def read_lines(f: Path):
    """(the rule, None) from the host's lines file f, or (None, why there is none)
    when there is no file or it says alert = off. Refused for any other file that
    gives no rule."""
    try:
        text = f.read_text()
    except FileNotFoundError:
        return None, f"no line for this host: {f} does not exist"
    except (OSError, UnicodeDecodeError) as e:
        raise Refused(f"{f} is unreadable: {getattr(e, 'strerror', None) or e}") from None
    values, off = {}, False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, eq, value = (part.strip() for part in line.partition("="))
        if eq and name == "alert":
            if value != "off":
                raise Refused(f"{f} line {number}: alert takes one value, off")
            off = True
            continue
        if not eq or name not in NUMBERS:
            raise Refused(f"{f} line {number} names no line (the names: "
                          f"{', '.join(NUMBERS)}, alert)")
        if name in values:
            raise Refused(f"{f} line {number}: {name} is given twice")
        try:
            number_value = float(value)
        except ValueError:
            number_value = -1.0
        whole = name in WHOLE
        if not 0 < number_value < float("inf") or (whole and number_value != int(number_value)):
            raise Refused(f"{f} line {number}: {name} needs a positive "
                          f"{'whole ' if whole else ''}number")
        values[name] = number_value
    if off:
        return None, f"the alert is off: {f} says alert = off"
    missing = [name for name in NUMBERS if name not in values]
    if missing:
        raise Refused(f"{f} sets no {', '.join(missing)}")
    for name, bound in (("line_pages", "line_pages_max"), ("minutes", "minutes_max")):
        if values[name] > values[bound]:
            raise Refused(f"{f}: {name} {values[name]:g} is over {bound} {values[bound]:g}")
    return Rule(values["line_pages"], int(values["minutes"])), None


def _rule(f: Path):
    """read_lines, a refusal read as its why: (the rule, why there is none, the refusal)."""
    try:
        rule, why = read_lines(f)
    except Refused as e:
        return None, f"refused: {e}", str(e)
    return rule, why, None


def step(state: dict, at: float, boot: str, out: int, rule: Rule):
    """(the next state, the verdict) for one reading of the swap-out counter.

    state holds the last reading and the current run: its first reading over the
    line (the episode's ID), how many readings it holds, its last rates, its peak
    and whether its alert went out."""
    last, run = state.get("last"), state.get("run")
    nxt = {"last": {"at": at, "boot": boot, "out": out}}
    why = None
    if last is None:
        why = "no reading before this one"
    elif boot and last.get("boot") and boot != last["boot"]:
        why = "a new boot"
    elif out < last["out"]:
        why = "the counter went back"
    elif at <= last["at"]:
        why = "no time since the last reading"
    if why is None:
        per_minute = (out - last["out"]) * 60.0 / (at - last["at"])
        rate = round(per_minute)
        if per_minute > rule.line:
            if run is None:
                run = {"from": at, "count": 0, "rates": [], "peak": 0, "alerted": False}
            run = dict(run, count=run["count"] + 1, rates=(run["rates"] + [rate])[-RATES_KEPT:],
                       peak=max(run["peak"], rate))
            nxt["run"] = run
            if run["count"] < rule.minutes:
                return nxt, ("over", rate, run["count"])
            if run["alerted"]:
                return nxt, ("alerted", _iso(run["from"]), rate)
            text = (f"swap-out over {rule.line:g} pages a minute for {run['count']} readings in a "
                    f"row ({', '.join(str(r) for r in run['rates'])})")
            return nxt, ("alert", _iso(run["from"]), text)
        why, quiet = "a reading under the line", ("under", rate)
    else:
        quiet = ("baseline", why)
    if run is not None and run["count"] >= rule.minutes:
        return nxt, ("ended", _iso(run["from"]), run["count"], run["peak"], why)
    return nxt, quiet


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_state(d: Path) -> dict:
    try:
        state = json.loads((d / STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(d: Path, state: dict) -> None:
    tmp = d / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n")
    os.replace(tmp, d / STATE_FILE)


def _locked(d: Path):
    """The state dir's lock, held by this process, or None while another holds it."""
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _say(text: str) -> None:
    print(f"memory-thrash: {text}", file=sys.stderr)


def _print(*fields) -> None:
    print("\t".join(str(f) for f in fields))


def check(args) -> int:
    d = Path(args.state_dir)
    try:
        return _check(d, args)
    except OSError as e:
        why = f"cannot keep the state in {d}: {getattr(e, 'strerror', None) or e}"
        _say(f"skipped: {why}")
        _print("skipped", why)
        return 0


def _check(d: Path, args) -> int:
    fd = _locked(d)
    if fd is None:
        _print("skipped", "another check holds the lock")
        return 0
    try:
        state = _read_state(d)
        at = float(os.environ.get("MEMORY_THRASH_NOW") or time.time())
        rule, why, refused = _rule(d / LINES_FILE)
        if args.skip is not None and refused is None:
            why = args.skip
        if why is not None:
            return _quiet(d, state, at, why, refused)
        nxt, verdict = step(state, at, args.boot_id, args.out, rule)
        _write_state(d, nxt)
    finally:
        os.close(fd)
    kind = verdict[0]
    if kind == "over":
        _say(f"swap-out {verdict[1]} pages a minute, over the line {rule.line:g} "
             f"({verdict[2]} of {rule.minutes} readings)")
    elif kind == "alert":
        _say(f"episode {verdict[1]}: {verdict[2]}: alert")
    elif kind == "ended":
        _say(f"episode {verdict[1]} ended after {verdict[2]} readings over the line, "
             f"peak {verdict[3]} pages a minute: {verdict[4]}")
    _print(*verdict)
    return 0


def _quiet(d: Path, state: dict, at: float, why: str, refused) -> int:
    """A reading the rule does not run on, said and kept when its reason is new. A
    refusal (refused, its why) returns `refused` until the probe has paged it."""
    seen = state.get("skip")
    if (not isinstance(seen, dict) or seen.get("why") != why
            or not isinstance(seen.get("since"), (int, float))):
        _say(f"skipped: {why}")
        seen = {"why": why, "since": at, "paged": refused is None}
        _write_state(d, dict(state, skip=seen))
    if refused is not None and not seen.get("paged"):
        _print("refused", _iso(seen["since"]), refused)
    else:
        _print("skipped", why)
    return 0


def delivered(args) -> int:
    d = Path(args.state_dir)
    fd = _locked(d)
    if fd is None:
        return 1
    try:
        state = _read_state(d)
        if args.refused is not None:
            seen = state.get("skip")
            if (not isinstance(seen, dict) or not isinstance(seen.get("since"), (int, float))
                    or _iso(seen["since"]) != args.refused):
                return 1
            _write_state(d, dict(state, skip=dict(seen, paged=True)))
        else:
            run = state.get("run")
            if not run or _iso(run["from"]) != args.episode:
                return 1
            _write_state(d, dict(state, run=dict(run, alerted=True)))
    finally:
        os.close(fd)
    return 0


_SCAN = None


def _scan():
    """browser-scan.py, imported once from beside this file."""
    global _SCAN
    if _SCAN is None:
        spec = importlib.util.spec_from_file_location("browser_scan", HERE / "browser-scan.py")
        _SCAN = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SCAN)
    return _SCAN


def _footprint(d: Path):
    """(proportional resident KiB, proportional swapped KiB) of one process:
    smaps_rollup's Pss and SwapPss, else status's VmRSS and VmSwap, else (0, 0)."""
    for name, keys in (("smaps_rollup", ("Pss:", "SwapPss:")), ("status", ("VmRSS:", "VmSwap:"))):
        found = {}
        for line in (_scan()._read(d / name) or "").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] in keys and fields[1].isdigit():
                found[fields[0]] = int(fields[1])
        if keys[0] in found:
            return found[keys[0]], found.get(keys[1], 0)
    return 0, 0


def bot_sizes(proc: Path, units: dict) -> dict:
    """{owner: [resident KiB, swapped KiB]}, with None for what no bot's unit holds."""
    scan = _scan()
    table = scan.processes(proc)
    unit_owner = {label + ".service": owner for label, owner in units.items()}

    def owner_of(pid):
        for _ in range(scan.MAX_DEPTH):
            entry = table.get(pid)
            if entry is None:
                return None
            for part in entry[3].split("/"):
                if part in unit_owner:
                    return unit_owner[part]
            pid = entry[0]
        return None

    sizes = {}
    for pid in table:
        resident, swapped = _footprint(proc / str(pid))
        total = sizes.setdefault(owner_of(pid), [0, 0])
        total[0] += resident
        total[1] += swapped
    return sizes


def conf_values(bot_dir: Path, keys) -> dict:
    """The keys' values in a bot's bot.conf, each read as lib-common.sh's bot_conf_get
    reads one: its first `KEY=` or `export KEY=` line, one surrounding quote pair
    taken off, an unquoted value cut at its first space. A key with no line is left
    out."""
    try:
        lines = (bot_dir / "bot.conf").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    found = {}
    for line in lines:
        name, eq, value = (line[len("export "):] if line.startswith("export ") else line).partition("=")
        if not eq or name not in keys or name in found:
            continue
        if len(value) > 1 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        elif value[:1] not in ("\"", "'"):
            value = UNQUOTED.match(value).group(0)
        found[name] = value
    return found


def bot_units(bots_dirs) -> dict:
    """{unit label: FLEET/BOT} for each bot of the bots dirs whose bot.conf names its
    unit (BOT_SERVICE); a bot with none is left out."""
    units = {}
    for bots_dir in bots_dirs:
        try:
            bot_dirs = sorted(p for p in Path(bots_dir).iterdir()
                              if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
        for bot_dir in bot_dirs:
            conf = conf_values(bot_dir, ("BOT_SERVICE", "FLEET_NAME"))
            if conf.get("BOT_SERVICE"):
                fleet = conf.get("FLEET_NAME")
                units[conf["BOT_SERVICE"]] = f"{fleet}/{bot_dir.name}" if fleet else bot_dir.name
    return units


def _mib(kib: int) -> str:
    return f"{(kib + 512) // 1024:,} MiB"


def bots(args) -> int:
    proc = Path(args.proc)
    if not (proc / "uptime").is_file():
        return 0
    sizes = bot_sizes(proc, bot_units(args.bots_dir))
    outside = sizes.pop(None, [0, 0])
    ranked = sorted(sizes.items(), key=lambda item: (-sum(item[1]), item[0]))[:args.top]
    named = ", ".join(f"{owner} {_mib(r + s)} ({_mib(s)} swapped)" for owner, (r, s) in ranked)
    print(f"Largest bots by memory: {named or 'none found'}; outside every bot "
          f"{_mib(sum(outside))} ({_mib(outside[1])} swapped).")
    return 0


def _samples(path: Path):
    """(epoch, out) per sample, oldest first, from `claudlobby --json plane samples
    host.swap_pages` output; a sample with no whole `out` is left out."""
    rows = []
    for sample in ((json.loads(path.read_text()).get("data") or {}).get("samples") or []):
        value = sample.get("value")
        out = value.get("out") if isinstance(value, dict) else None
        if isinstance(out, int) and not isinstance(out, bool) and out >= 0:
            at = datetime.fromisoformat(sample["occurred_at"].replace("Z", "+00:00")).timestamp()
            rows.append((at, out))
    rows.sort()
    return rows


def replay_rule(rows, rule: Rule):
    """The alerts the rule raises over rows of (epoch, out), as (epoch, rates)."""
    state, alerts = {}, []
    for at, out in rows:
        state, verdict = step(state, at, "", out, rule)
        if verdict[0] == "alert":
            alerts.append((at, list(state["run"]["rates"])))
            state["run"]["alerted"] = True
    return alerts


def replay(args) -> int:
    rule, why, _ = _rule(Path(args.lines))
    if rule is None:
        print(f"memory-thrash.py: no rule to replay: {why}", file=sys.stderr)
        return 2
    if args.minutes is not None:
        rule = Rule(rule.line, args.minutes)
    rows = _samples(Path(args.samples))
    alerts = replay_rule(rows, rule)
    for at, rates in alerts:
        _print("alert", _iso(at), *rates)
    first, last = (_iso(rows[0][0]), _iso(rows[-1][0])) if rows else ("-", "-")
    print(f"{len(alerts)} alerts over {len(rows)} samples, {first} to {last}, line {rule.line:g} "
          f"pages a minute for {rule.minutes} readings")
    return 0


def status(args) -> int:
    d = Path(args.state_dir)
    rule, why, _ = _rule(d / LINES_FILE)
    print(json.dumps({"rule": {"line_pages": rule.line, "minutes": rule.minutes} if rule else why,
                      "state": _read_state(d)}, sort_keys=True))
    return 0


def _whole(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("needs a whole number of at least 1")
    return value


def _counter(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("needs a whole number")
    return value


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="memory-thrash.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--state-dir", required=True)
    given = c.add_mutually_exclusive_group(required=True)
    given.add_argument("--out", type=_counter)
    given.add_argument("--skip")
    c.add_argument("--boot-id", default="")
    m = sub.add_parser("delivered")
    m.add_argument("--state-dir", required=True)
    marked = m.add_mutually_exclusive_group(required=True)
    marked.add_argument("--episode")
    marked.add_argument("--refused", metavar="SINCE")
    b = sub.add_parser("bots")
    b.add_argument("--bots-dir", action="append", default=[], metavar="DIR")
    b.add_argument("--top", type=_whole, default=3)
    b.add_argument("--proc", default="/proc", help=argparse.SUPPRESS)
    r = sub.add_parser("replay")
    r.add_argument("--samples", required=True)
    r.add_argument("--lines", required=True)
    r.add_argument("--minutes", type=_whole)
    s = sub.add_parser("status")
    s.add_argument("--state-dir", required=True)
    args = ap.parse_args(argv)
    return {"check": check, "delivered": delivered, "bots": bots, "replay": replay,
            "status": status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
