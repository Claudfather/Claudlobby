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


def _read_claudron_config(path: Path) -> dict[str, str]:
    """Read shell-sourceable ``.claudron`` config file at *path*.

    Returns parsed key=value pairs. Returns empty dict if file missing.
    """
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip()
    return result


@dataclass(frozen=True)
class Paths:
    """Path resolution. `root` is the claudlobby repo root.

    `fleet_dir` is None for root-mode, or `local/<fleet>/` for overlay-mode.
    `seed` is True when operating on the built-in seed fleet (fleet.yaml.seed).
    `vault_root` is set when a ``.claudron`` config points to a vault.
    """

    root: Path
    fleet_dir: Path | None = None
    seed: bool = False
    vault_root: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            object.__setattr__(self, "root", Path(self.root))

    # --- public base (always at repo root) ---
    # Use these for callers, such as new-bot scaffolding, that intentionally
    # want public-base-only lookup instead of overlay-merged content.

    @property
    def base_library(self) -> Path:
        return self.root / "library"

    @property
    def base_expertise(self) -> Path:
        return self.base_library / "expertise"

    @property
    def base_skills(self) -> Path:
        return self.base_library / "skills"

    @property
    def base_mcp(self) -> Path:
        return self.base_library / "mcp"

    @property
    def base_integrations(self) -> Path:
        return self.base_library / "integrations"

    @property
    def base_guardrails(self) -> Path:
        return self.base_library / "guardrails"

    @property
    def base_protocols(self) -> Path:
        return self.base_library / "protocols"

    @property
    def base_resources(self) -> Path:
        return self.base_library / "resources"

    @property
    def base_lessons(self) -> Path:
        return self.base_library / "lessons"

    @property
    def base_post_actions(self) -> Path:
        return self.base_library / "post_actions"

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

    # --- shared documentation ---

    @property
    def shared_docs(self) -> Path | None:
        """Fleet-level shared documentation directory."""
        if self.fleet_dir:
            return self.fleet_dir / "shared"
        return None

    # --- fleet-specific files ---

    @property
    def fleet_yaml(self) -> Path:
        if self.seed:
            return self.root / "fleet.yaml.seed"
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
        if self.seed:
            return self.root / "runtime" / "seed"
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

        If `fleet` is given, first check ``.claudron`` config at claudlobby root
        for a vault path. If the vault contains a fleet overlay for *fleet*,
        use that. Otherwise fall back to ``<root>/local/<fleet>/``.
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
        vault_root = None

        if fleet:
            # Check .claudron config for vault-based fleet resolution
            config = _read_claudron_config(root / ".claudron")
            vault_str = config.get("vault")
            if vault_str:
                vault_path = Path(vault_str)
                vault_fleet = vault_path / fleet
                if (vault_fleet / "fleet.yaml").is_file():
                    fleet_dir = vault_fleet
                    vault_root = vault_path

            # Fall back to local/<fleet>/
            if fleet_dir is None:
                fleet_dir = root / "local" / fleet
                if not fleet_dir.is_dir():
                    raise FileNotFoundError(
                        f"Fleet overlay not found: {fleet_dir} (run `claudlobby new-fleet {fleet}` to scaffold)"
                    )

        return cls(root=root, fleet_dir=fleet_dir, vault_root=vault_root)
