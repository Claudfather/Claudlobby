"""Path resolution for claudlobby.

Two-layer model:

  1. **Public base** — `library/`, `voices/`, etc., at the claudlobby
     repo root. Open-source, generic content.
  2. **Fleet overlay** — `local/<fleet>/library/`, `local/<fleet>/voices/`,
     etc. User's fleet-specific content. Gitignored.

Library files are looked up in the overlay first, falling back to the
public base. Voices the same. fleet.yaml lives at the overlay root
(`local/<fleet>/fleet.yaml`); runtime output goes to
`local/<fleet>/runtime/bots/`.

If no `--fleet` flag is given, paths default to the repo root —
fleet.yaml at root, runtime/ at root, no overlay. This preserves the
"single fleet at the root" mode used by the public example fleets.
"""

from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass


@dataclass(frozen=True)
class Paths:
    """Path resolution. `root` is the claudlobby repo root.

    `fleet_dir` is None for root-mode, or `local/<fleet>/` for overlay-mode.
    """

    root: Path
    fleet_dir: Path | None = None

    # --- public base (always at repo root) ---

    @property
    def base_library(self) -> Path:
        return self.root / "library"

    @property
    def base_voices(self) -> Path:
        return self.root / "voices"

    # --- overlay (when fleet_dir is set) ---

    @property
    def overlay_library(self) -> Path | None:
        return (self.fleet_dir / "library") if self.fleet_dir else None

    @property
    def overlay_voices(self) -> Path | None:
        return (self.fleet_dir / "voices") if self.fleet_dir else None

    # --- effective paths (overlay-aware lookup) ---
    # These return BOTH dirs (overlay first, base second) for callers
    # that walk-and-merge. For single-file lookup, use `find_library_file`.

    def library_search_dirs(self, kind: str) -> list[Path]:
        """Search dirs for a given library kind, in precedence order.

        kind ∈ {expertise, skills, mcp, integrations, guardrails,
                protocols, resources, lessons, post_actions, permissions}
        """
        out: list[Path] = []
        if self.overlay_library:
            p = self.overlay_library / kind
            if p.is_dir():
                out.append(p)
        out.append(self.base_library / kind)
        return out

    def find_library_file(self, kind: str, stem: str, ext: str = ".md") -> Path | None:
        """Find a single library file by kind + stem. Overlay wins."""
        if ".." in stem:
            raise ValueError(f"path traversal in library file name: {stem!r}")
        for d in self.library_search_dirs(kind):
            p = d / f"{stem}{ext}"
            if p.is_file():
                return p
        return None

    def find_skill_dir(self, name: str) -> Path | None:
        """Skills are directories. Overlay wins."""
        for d in self.library_search_dirs("skills"):
            p = d / name
            if p.is_dir():
                return p
        return None

    def find_voice_file(self, rel_path: str) -> Path | None:
        """Voice file lookup. `rel_path` is relative to voices/ (e.g. 'erlich-bachman.md').

        Accepts either a bare name or 'voices/<name>.md' for backward compat.
        """
        # Strip leading "voices/" if present
        clean = rel_path.removeprefix("voices/")
        candidates = []
        if self.overlay_voices:
            candidates.append(self.overlay_voices / clean)
            # Also support legacy form: full path under fleet_dir
            candidates.append(self.fleet_dir / rel_path)
        candidates.append(self.base_voices / clean)
        candidates.append(self.root / rel_path)  # legacy: voices/<x>.md from repo root
        for c in candidates:
            if c.is_file():
                return c
        return None

    # --- legacy single-dir properties (for code that doesn't yet handle overlay) ---
    # These now refer to the BASE library only. New code should use
    # find_library_file / library_search_dirs / find_skill_dir.

    @property
    def library(self) -> Path:
        return self.base_library

    @property
    def expertise(self) -> Path:
        return self.base_library / "expertise"

    @property
    def skills(self) -> Path:
        return self.base_library / "skills"

    @property
    def mcp(self) -> Path:
        return self.base_library / "mcp"

    @property
    def integrations(self) -> Path:
        return self.base_library / "integrations"

    @property
    def guardrails(self) -> Path:
        return self.base_library / "guardrails"

    @property
    def principles(self) -> Path:
        return self.base_library / "principles"

    @property
    def protocols(self) -> Path:
        return self.base_library / "protocols"

    @property
    def resources(self) -> Path:
        return self.base_library / "resources"

    @property
    def lessons(self) -> Path:
        return self.base_library / "lessons"

    @property
    def post_actions(self) -> Path:
        return self.base_library / "post_actions"

    @property
    def voices(self) -> Path:
        return self.base_voices

    # --- fleet-specific files ---

    @property
    def fleet_yaml(self) -> Path:
        if self.fleet_dir:
            return self.fleet_dir / "fleet.yaml"
        return self.root / "fleet.yaml"

    @property
    def env_file(self) -> Path:
        # .env stays at the repo root (shared across fleets) by default.
        # If a fleet wants its own, it can keep one at fleet_dir/.env.
        if self.fleet_dir and (self.fleet_dir / ".env").is_file():
            return self.fleet_dir / ".env"
        return self.root / ".env"

    @property
    def runtime(self) -> Path:
        if self.fleet_dir:
            return self.fleet_dir / "runtime"
        return self.root / "runtime"

    @property
    def runtime_bots(self) -> Path:
        return self.runtime / "bots"

    @property
    def lib(self) -> Path:
        return self.root / "lib"

    def bot_runtime(self, bot_name: str) -> Path:
        return self.runtime_bots / bot_name

    # --- detection ---

    @classmethod
    def detect(cls, hint: Path | None = None, fleet: str | None = None) -> "Paths":
        """Find the claudlobby root, walking up from `hint` (or CWD).

        Marker: a directory containing both `library/` and `lib/`.

        If `fleet` is given, the fleet overlay path is set to
        `<root>/local/<fleet>/`. The directory must exist.
        """
        start = (hint or Path.cwd()).resolve()
        root = None
        for candidate in [start] + list(start.parents):
            if (candidate / "library").is_dir() and (candidate / "lib").is_dir():
                root = candidate
                break
        if root is None:
            raise FileNotFoundError(
                f"Could not find claudlobby root (looked for library/ + lib/) starting at {start}"
            )

        fleet_dir = None
        if fleet:
            fleet_dir = root / "local" / fleet
            if not fleet_dir.is_dir():
                raise FileNotFoundError(
                    f"Fleet overlay not found: {fleet_dir} (run `claudlobby new-fleet {fleet}` to scaffold)"
                )

        return cls(root=root, fleet_dir=fleet_dir)
