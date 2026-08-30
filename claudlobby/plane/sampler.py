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

THUMB_LINES = 14
FOCUS_LINES = 44
THUMB_INTERVAL = 5.0
FOCUS_INTERVAL = 1.0
FOCUS_TTL = 30.0          # focus decays back to thumbnail cadence untouched
DISCOVER_INTERVAL = 60.0  # roster changes are generate-time events


def discover_panes(root: Path) -> list[dict]:
    """Every bot on the host: (fleet, bot, socket). Reads the composed
    bot.conf files — the same discovery surface the lifecycle scripts use.
    Flat and nested vault layouts both match local/*/runtime/bots and
    local/*/*/runtime/bots; root-mode matches runtime/bots."""
    out = []
    root = Path(root)
    patterns = ("local/*/runtime/bots/*", "local/*/*/runtime/bots/*",
                "runtime/bots/*")
    seen = set()
    for pat in patterns:
        for bot_dir in sorted(root.glob(pat)):
            conf = bot_dir / "bot.conf"
            if not conf.is_file() or bot_dir in seen:
                continue
            seen.add(bot_dir)
            sock = None
            try:
                for line in conf.read_text().splitlines():
                    line = line.strip().removeprefix("export ").strip()
                    if line.startswith("BOT_SERVICE="):
                        sock = line.split("=", 1)[1].strip().strip('"')
                        break
            except OSError:
                continue
            if not sock:
                continue
            fleet = bot_dir.parent.parent.parent.name  # <fleet>/runtime/bots
            out.append({"fleet": fleet, "bot": bot_dir.name, "socket": sock})
    return out


@dataclass
class PaneSample:
    fleet: str
    bot: str
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
        self.tmux = self.tmux or shutil.which("tmux") \
            or ("/opt/homebrew/bin/tmux"
                if Path("/opt/homebrew/bin/tmux").exists() else None)

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

    def focus(self, bot: str) -> None:
        self._focus = (bot, time.monotonic())

    def _focused(self) -> str | None:
        if self._focus and time.monotonic() - self._focus[1] < FOCUS_TTL:
            return self._focus[0]
        return None

    def snapshot(self) -> dict:
        focused = self._focused()
        panes = []
        for p in self._panes:
            s = self._samples.get(p["bot"])
            panes.append({
                "fleet": p["fleet"], "bot": p["bot"],
                "alive": bool(s and s.alive),
                "lines": s.lines if s else "",
                "captured_ago_s": round(time.monotonic() - s.captured_at, 1)
                                  if s and s.captured_at else None,
                "focused": p["bot"] == focused,
            })
        return {"panes": panes, "sampler_running": self._task is not None}

    async def _capture(self, pane: dict, lines: int) -> None:
        sess = pane["bot"]
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
            alive, text = False, ""
        self._samples[pane["bot"]] = PaneSample(
            pane["fleet"], pane["bot"], alive,
            "\n".join(text.splitlines()[-lines:]), time.monotonic())

    async def _run(self) -> None:
        last_thumb = 0.0
        while True:
            now = time.monotonic()
            if now - self._discovered_at > DISCOVER_INTERVAL:
                self._panes = await asyncio.to_thread(discover_panes, self.root)
                self._discovered_at = now
            focused = self._focused()
            if focused:
                pane = next((p for p in self._panes if p["bot"] == focused),
                            None)
                if pane:
                    await self._capture(pane, FOCUS_LINES)
            if now - last_thumb >= THUMB_INTERVAL:
                last_thumb = now
                for pane in self._panes:
                    if pane["bot"] != focused:
                        await self._capture(pane, THUMB_LINES)
            await asyncio.sleep(FOCUS_INTERVAL if focused
                                else THUMB_INTERVAL / 2)
