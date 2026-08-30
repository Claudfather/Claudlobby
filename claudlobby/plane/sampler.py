"""The thumbnail-grid pane sampler (Phase-4 T3; spec §16 F8 + §14).

ONE bounded sampler per pane, cached, never multiplied by browser count —
§14's rule verbatim: browsers read the cache; the sampling cadence is owned
here and is invariant in the number of viewers. The live-pane upgrade path
(ttyd `tmux attach -r`) is a NAMED DEFERRAL to the trust/gaps chunk; this
sampler's focus mode (1s cadence, full height) is the §14-degradable form
that ships first.

READ posture: `tmux capture-pane` reads pane content and mutates nothing —
the view daemon stays observationally read-only. Discovery reads bot.conf
files (BOT_SERVICE = the per-bot tmux socket; the session name is the bot
name — the estate's one-session-per-server convention).

Degradation is typed, never silent (§16): no tmux binary -> the grid surface
reports `unavailable` with remediation; a dead session -> that PANE carries
`alive: false` and stays on the grid (a down bot is a fact, not an absence).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import _iter_fleet_dirs, tmux_socket_for_bot

THUMB_LINES = 14
FOCUS_LINES = 44
THUMB_INTERVAL = 5.0
FOCUS_INTERVAL = 1.0
FOCUS_TTL = 30.0          # focus decays back to thumbnail cadence untouched
DISCOVER_INTERVAL = 60.0  # roster changes are generate-time events


def discover_panes(root: Path) -> list[dict]:
    """Every bot on the host: (fleet, bot, socket). Layout enumeration is
    paths._iter_fleet_dirs (the ONE nested-aware fleet walk) and the socket
    is paths.tmux_socket_for_bot (the ONE bot.conf socket reader — which
    honors TMUX_SOCKET, the composer's single-quote form, and FLEET_NAME
    fail-fast). A grid over a read-only daemon must not fork either SSOT."""
    root = Path(root)
    out = []
    fleet_dirs = list(_iter_fleet_dirs(root / "local"))
    if (root / "runtime" / "bots").is_dir():
        fleet_dirs.append(root)  # root/CLI mode: the fleet label is root.name
    for fleet_dir in fleet_dirs:
        bots = fleet_dir / "runtime" / "bots"
        if not bots.is_dir():
            continue
        for bot_dir in sorted(bots.iterdir()):
            if not (bot_dir / "bot.conf").is_file():
                continue
            try:
                sock = tmux_socket_for_bot(bot_dir)
            except ValueError:
                sock = ""  # FLEET_NAME set but socket empty — skip, disclosed
            if not sock:
                continue
            out.append({"fleet": fleet_dir.name, "bot": bot_dir.name,
                        "socket": sock})
    return out


@dataclass
class PaneSample:
    alive: bool = False
    lines: str = ""
    captured_at: float = 0.0


@dataclass
class PaneSampler:
    """The one sampler. `snapshot()` is what the endpoint serves — pure cache
    read. `focus(bot)` raises that pane's cadence/height for FOCUS_TTL."""

    root: Path
    tmux: str | None = None
    _panes: list[dict] = field(default_factory=list)
    _samples: dict = field(default_factory=dict)
    _focus: tuple[str, float] | None = None
    _task: asyncio.Task | None = None
    _discovered_at: float = 0.0

    def __post_init__(self):
        self.tmux = self.tmux or shutil.which("tmux") or next(
            (c for c in ("/usr/bin/tmux", "/usr/local/bin/tmux",
                         "/opt/homebrew/bin/tmux") if Path(c).exists()), None)

    @property
    def available(self) -> bool:
        return self.tmux is not None

    def start(self) -> None:
        if self._task is None and self.available:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # no loop here (sync thread) — the startup event owns it
            self._task = loop.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def focus(self, bot: str, fleet: str | None = None) -> None:
        # Resolve to a (fleet, bot) key: a bare bot name is ambiguous under a
        # cross-fleet collision (#526). Given fleet wins; else the first
        # discovered pane with that name.
        if fleet is None:
            match = next((p for p in self._panes if p["bot"] == bot), None)
            fleet = match["fleet"] if match else ""
        self._focus = ((fleet, bot), time.monotonic())

    def _focused(self) -> tuple | None:
        if self._focus and time.monotonic() - self._focus[1] < FOCUS_TTL:
            return self._focus[0]
        return None

    def snapshot(self) -> dict:
        focused = self._focused()
        panes = []
        for p in self._panes:
            s = self._samples.get((p["fleet"], p["bot"]))
            panes.append({
                "fleet": p["fleet"], "bot": p["bot"],
                "alive": bool(s and s.alive),
                "lines": s.lines if s else "",
                # age of the last SUCCESSFUL frame — a down pane keeps showing
                # its last good frame with an honestly growing age (§16).
                "captured_ago_s": round(time.monotonic() - s.captured_at, 1)
                                  if s and s.captured_at else None,
                "focused": (p["fleet"], p["bot"]) == focused,
            })
        running = self._task is not None and not self._task.done()
        return {"panes": panes, "sampler_running": running}

    async def _capture(self, pane: dict, lines: int) -> None:
        sess = pane["bot"]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.tmux, "-L", pane["socket"], "capture-pane",
                "-t", sess, "-p", "-e", "-S", f"-{lines}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=4.0)
            alive = proc.returncode == 0
            text = out.decode("utf-8", errors="replace") if alive else ""
        except (OSError, asyncio.TimeoutError):
            # A wedged tmux socket (the Pi SD-stall class) times out every
            # sweep — reap the child so it does not leak per pane per sweep.
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.communicate()
                except (OSError, ProcessLookupError):
                    pass
            alive, text = False, ""
        key = (pane["fleet"], pane["bot"])   # #526: name alone collides
        prev = self._samples.get(key)
        if alive:
            self._samples[key] = PaneSample(
                True, "\n".join(text.splitlines()[-lines:]),
                time.monotonic())
        else:
            # A FAILED capture must not overwrite the last good frame or its
            # timestamp — §16's last-successful-observation. Keep the frame;
            # mark not-alive; do not lie that it is fresh.
            self._samples[key] = PaneSample(
                False, prev.lines if prev else "",
                prev.captured_at if prev else 0.0)

    async def _run(self) -> None:
        try:
            await self._loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the task must fail LOUD,
            # not freeze the grid on stale frames pretending to be live.
            import sys
            print(f"plane-sampler: loop crashed: {exc}", file=sys.stderr)
            raise

    async def _loop(self) -> None:
        last_thumb = 0.0
        while True:
            now = time.monotonic()
            if now - self._discovered_at > DISCOVER_INTERVAL:
                self._panes = await asyncio.to_thread(discover_panes, self.root)
                self._discovered_at = now
            focused = self._focused()
            if focused:
                pane = next((p for p in self._panes
                             if (p["fleet"], p["bot"]) == focused), None)
                if pane:
                    await self._capture(pane, FOCUS_LINES)
            if now - last_thumb >= THUMB_INTERVAL:
                last_thumb = now
                for pane in self._panes:
                    if (pane["fleet"], pane["bot"]) != focused:
                        await self._capture(pane, THUMB_LINES)
            await asyncio.sleep(FOCUS_INTERVAL if focused
                                else THUMB_INTERVAL / 2)
