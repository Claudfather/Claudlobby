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

Vault integration:

  When claudron is installed (``pip install claudlobby[vault]``), vault-aware
  path resolution uses ``claudron.vault.detect()`` for proper fleet discovery.
  When claudron is not installed, a built-in ``.claudron`` bridge file parser
  provides basic vault support. Both paths produce identical results for the
  common case; claudron adds richer vault health and index features.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .env_tiers import EnvTier, Resolution

try:
    from claudron.vault import detect as _claudron_detect

    _HAS_CLAUDRON = True
except ImportError:
    _HAS_CLAUDRON = False


def _read_claudron_config(path: Path) -> dict[str, str]:
    """Read shell-sourceable ``.claudron`` config file at *path*.

    Returns parsed key=value pairs. Returns empty dict if file missing.
    Fallback for when claudron is not installed.
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


def tmux_socket_for_bot(bot_dir: Path) -> str:
    """Resolve a bot's per-bot tmux server socket name from its ``bot.conf``.

    Mirrors the bash SSOT ``tmux_socket_for_bot`` (lib/lib-common.sh): prefer the
    explicit ``TMUX_SOCKET`` field, fall back to ``BOT_SERVICE`` (equal by
    construction). When neither resolves (an un-regenerated or missing
    ``bot.conf``):

      * ``FLEET_NAME`` set → raise ``ValueError`` (fail-fast). An empty socket in
        a real fleet is a misconfigured bot; silently resolving a bare socket
        would collide across fleets and reintroduce the shared-server SPOF. This
        is the twin of the bash guard's ``return 1`` + 'refusing' stderr, so
        ``doctor``/``status`` error identically on misconfig instead of quietly
        resolving a wrong socket.
      * ``FLEET_NAME`` unset (CLI / pre-fleet) → return "" and let the caller
        decide its own fallback.

    The single Python reader for the socket field, so ``doctor``/``status``/
    ``move_bot`` don't each fork their own.
    """
    conf = bot_dir / "bot.conf"
    found: dict[str, str] = {}
    if conf.is_file():
        try:
            for line in conf.read_text().splitlines():
                line = line.strip().removeprefix("export ").strip()
                for key in ("TMUX_SOCKET", "BOT_SERVICE"):
                    if line.startswith(f"{key}="):
                        found[key] = line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
    socket = found.get("TMUX_SOCKET") or found.get("BOT_SERVICE") or ""
    if socket:
        return socket
    if os.environ.get("FLEET_NAME"):
        raise ValueError(
            f"tmux_socket_for_bot: empty BOT_SERVICE for '{bot_dir}' while "
            "FLEET_NAME is set — refusing a bare socket name that would collide "
            "across fleets and reintroduce the shared-server SPOF"
        )
    return ""


def vault_api_available() -> bool:
    """True when the ``[vault]`` extra's claudron import seam resolves.

    The one place other modules ask "is the imported Claudron API here?" —
    they must not test the import themselves (the ``[vault]`` extra is the
    single sanctioned import seam and it lives behind this module).
    """
    return _HAS_CLAUDRON


def detect_vault(path: Path) -> Path | None:
    """Resolve *path* to a vault root, or ``None`` when no vault is addressed.

    The sanctioned detection seam for callers outside this module (twin of
    :func:`_resolve_vault_fleet`): nothing else in claudlobby imports
    ``claudron.*``. Uses claudron's own ``vault.detect`` when the ``[vault]``
    extra is installed — that is authoritative. Without it, falls back to the
    documented marker walk-up (a ``_shared/`` or ``shared/`` directory, the way
    git ascends for ``.git/``; Claudron ``VAULT-STRUCTURE.md``).

    The fallback is deliberately *coarser* than the engine's: it does not
    re-implement the overlay/system-container guards, so it can bind a fleet
    overlay where the engine would keep walking to the true root. Every caller
    is a warn-level diagnostic, and the coarse direction under-warns rather
    than crying wolf on a vault that is fine.
    """
    try:
        start = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return None

    if _HAS_CLAUDRON:
        vault = _claudron_detect(start)
        return vault.root if vault else None

    for candidate in [start, *start.parents]:
        for marker in ("_shared", "shared"):
            if (candidate / marker).is_dir():
                return candidate
    return None


def _resolve_vault_fleet(root: Path, fleet: str) -> tuple[Path | None, Path | None]:
    """Resolve a fleet overlay through a vault.

    Tries claudron's ``Vault`` API first (proper fleet discovery via
    ``Vault.fleets``). Falls back to manual ``.claudron`` bridge file
    parsing when claudron is not installed.

    Returns (fleet_dir, vault_root) or (None, None) if no vault match.
    """
    config = _read_claudron_config(root / ".claudron")
    vault_str = config.get("vault")
    if not vault_str:
        return None, None

    vault_path = Path(vault_str)

    if _HAS_CLAUDRON:
        vault = _claudron_detect(vault_path)
        if vault and fleet in vault.fleets:
            return vault.fleets[fleet], vault.root
        return None, None

    # Fallback: manual fleet.yaml existence check
    vault_fleet = vault_path / fleet
    if (vault_fleet / "fleet.yaml").is_file():
        return vault_fleet, vault_path
    return None, None


# --- nested-containment fleet enumeration (Claudlobby#602 P2) -----------------
#
# A vault may nest fleets one level under a ``<system>/`` container: flat
# ``local/<fleet>/`` OR nested ``local/<system>/<fleet>/``. The rule is
# marker-agnostic — Claudlobby needs no knowledge of the system marker: a
# *fleet* is a directory holding ``fleet.yaml``; a *container* is a depth-1 dir
# with no ``fleet.yaml`` of its own. Both helpers below are the single place
# that nested-awareness lives, so the four resolution sites (Paths.detect, the
# --root CLI twin, the validator collision scan, move-bot) stay DRY.


def _iter_fleet_dirs(local_dir: Path) -> Iterator[Path]:
    """Yield candidate fleet overlay dirs under *local_dir*, flat AND nested.

    Enumerates depth 1 (``local/<fleet>/``) and depth 2
    (``local/<system>/<fleet>/`` — one level under a system container). Only
    *containers* (depth-1 dirs with no ``fleet.yaml`` of their own) are
    descended into; never past two levels.

    Depth-1 dirs are always yielded, with no ``fleet.yaml`` gate — the
    pre-nesting enumeration did not require one, and callers (the collision
    scan, move-bot autodetect) already filter to real fleets via their own
    ``runtime/bots`` / ``fleet.yaml`` checks. This keeps the flat case
    byte-identical while making nested siblings visible.
    """
    if not local_dir.is_dir():
        return
    for entry in sorted(local_dir.iterdir()):
        if not entry.is_dir():
            continue
        yield entry
        if not (entry / "fleet.yaml").is_file():
            for child in sorted(entry.iterdir()):
                if child.is_dir():
                    yield child


def _find_fleet_dir(local_dir: Path, fleet: str) -> Path | None:
    """Resolve a fleet *name* to its overlay dir, flat OR nested.

    Flat (``local/<fleet>/``) is tried first and wins — byte-identical to the
    pre-nesting behavior: a bare dir resolves even without a ``fleet.yaml``
    (scaffolding and the existing suite rely on this). Otherwise the unique
    ``local/<system>/<fleet>/`` that carries a ``fleet.yaml`` (one level under
    a container) resolves. Returns None when the name is present at neither
    depth.

    Raises ``ValueError`` on an F5 global-unique-name violation: the same name
    present as both a flat *fleet* (a dir carrying ``fleet.yaml``) and a nested
    fleet, or under two systems — never silently pick one. A bare flat *husk*
    (a dir with no ``fleet.yaml`` — e.g. the gitignored ``local/<fleet>/runtime/``
    a flat→nested ``git mv`` leaves behind) is NOT a real fleet: it yields to
    the nested fleet rather than falsely colliding.
    """
    flat = local_dir / fleet
    flat_is_fleet = (flat / "fleet.yaml").is_file()
    flat_match = flat if flat.is_dir() else None

    nested_matches: list[Path] = []
    if local_dir.is_dir():
        for entry in sorted(local_dir.iterdir()):
            # Containers only: a dir with a fleet.yaml is itself a flat fleet
            # (handled by flat_match), not a system container to descend.
            if not entry.is_dir() or (entry / "fleet.yaml").is_file():
                continue
            candidate = entry / fleet
            if (candidate / "fleet.yaml").is_file():
                nested_matches.append(candidate)

    # A genuine F5 both-depths collision requires the flat arm to be a REAL
    # fleet. A bare husk (flat dir without fleet.yaml) must NOT trigger it —
    # otherwise a leftover gitignored runtime/ husk bricks nested resolution.
    if flat_is_fleet and nested_matches:
        raise ValueError(
            f"fleet '{fleet}' resolves at two depths — flat ({flat_match}) and "
            f"nested ({nested_matches[0]}); fleet names must be globally unique "
            f"across the vault"
        )
    if len(nested_matches) > 1:
        joined = ", ".join(str(m) for m in nested_matches)
        raise ValueError(
            f"fleet '{fleet}' resolves under multiple systems ({joined}); "
            f"fleet names must be globally unique across the vault"
        )
    if nested_matches and not flat_is_fleet:
        # Flat is a husk (or absent) → the real nested fleet wins.
        return nested_matches[0]
    if flat_match:
        # A real flat fleet with no nested sibling, OR a bare flat dir ALONE
        # (the byte-identical scaffolding corner: a marker-less overlay still
        # resolves when nothing collides).
        return flat_match
    return None


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
                protocols, resources, lessons, post_actions, permissions,
                tools}
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

    def find_library_dir(self, kind: str, name: str) -> Path | None:
        """Find a dir-form library item (skills, tools). Overlay wins."""
        if ".." in name:
            raise ValueError(f"path traversal in library dir name: {name!r}")
        for d in self.library_search_dirs(kind):
            p = d / name
            if p.is_dir():
                return p
        return None

    def library_dir_names(self, kind: str, sentinel: str) -> dict[str, bool]:
        """Names of dir-form library items (flat scan), name → is-overlay.

        Only dirs containing the category's sentinel file count (e.g.
        tools/tool.yaml). Overlay wins on collisions.
        """
        out: dict[str, bool] = {}
        for d in self.library_search_dirs(kind):
            if not d.is_dir():
                continue
            is_overlay = bool(self.overlay_library and d == self.overlay_library / kind)
            for sub in sorted(d.iterdir()):
                if sub.is_dir() and (sub / sentinel).is_file() and sub.name not in out:
                    out[sub.name] = is_overlay
        return out

    def expand_library_folder(self, kind: str, dir_name: str) -> dict[str, Path]:
        """Expand a ``dir/`` entry into ``{rel_key: Path}`` for every ``.md`` file.

        Walks overlay → base, deduplicates by relative path (sans extension),
        skips README files, returns a sorted dict. Overlay wins on collisions.
        """
        collected: dict[str, Path] = {}
        for search_dir in self.library_search_dirs(kind):
            target = search_dir / dir_name if dir_name else search_dir
            if not target.is_dir():
                continue
            for path in sorted(target.rglob("*.md")):
                if path.stem.lower().startswith("readme"):
                    continue
                rel_key = str(path.relative_to(target).with_suffix(""))
                if rel_key not in collected:  # overlay (first) wins
                    collected[rel_key] = path
        return dict(sorted(collected.items()))

    def expand_skill_folder(self, dir_name: str) -> dict[str, Path]:
        """Expand a ``dir/`` entry into ``{leaf_name: Path}`` for every skill dir.

        A skill dir is any directory containing a ``SKILL.md`` marker file.
        Walks overlay → base, deduplicates by leaf directory name, returns a
        sorted dict. Overlay wins on collisions.
        """
        collected: dict[str, Path] = {}
        for search_dir in self.library_search_dirs("skills"):
            target = search_dir / dir_name if dir_name else search_dir
            if not target.is_dir():
                continue
            for sub in sorted(target.rglob("*")):
                if not sub.is_dir():
                    continue
                if not (sub / "SKILL.md").is_file():
                    continue
                leaf = sub.name
                if leaf not in collected:
                    collected[leaf] = sub
        return dict(sorted(collected.items()))

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
    def projects_yaml(self) -> Path:
        """projects.yaml sits beside fleet.yaml — the one home for that
        co-location rule (load_fleet derives it from the raw fleet.yaml
        path; every Paths-aware consumer should use this property)."""
        return self.fleet_config_dir / "projects.yaml"

    @property
    def fleet_config_dir(self) -> Path:
        """The directory holding fleet.yaml — the base every fleet-relative
        config path (mission_file, projects.yaml) resolves against, across
        root/overlay/seed/vault modes."""
        return self.fleet_yaml.parent

    @property
    def env_file(self) -> Path:
        """The tier a NEW operator-supplied value should be WRITTEN to.

        One file, deliberately: a write has to land somewhere, and the fleet
        tier (falling back to root) is where a fleet-scoped secret belongs.

        **This is not where a value is read from, and reading it as such was
        the #1226 defect.** The runtime cascades four tiers; this property knows
        two and picks one, so a credential placed at the host or bot tier
        resolved fine at boot and read as missing to every tool that came here
        for the answer. For reads use :meth:`env_resolved` — it asks the runtime
        rather than guessing, and it can tell you which tier won.
        """
        if self.fleet_dir and (self.fleet_dir / ".env").is_file():
            return self.fleet_dir / ".env"
        return self.root / ".env"

    def env_tiers(self, bot_name: str | None = None) -> list["EnvTier"]:
        """The four ``.env`` tiers in runtime sourcing order, least specific first.

        Answered by ``lib/env-tiers.sh`` — the runtime's own resolver — not by a
        copy of its order kept here. Pass *bot_name* to include that bot's tier;
        without it the bot tier reports ``unresolved`` rather than guessing.
        """
        from .env_tiers import read_tiers

        return read_tiers(self, bot_name=bot_name)

    def env_resolved(self, bot_name: str | None = None) -> dict[str, "Resolution"]:
        """Every env var the runtime would see, with the tier that decided it.

        The read door :attr:`env_file` was mistaken for. Values are merged the
        way the shell merges them — most specific ASSIGNMENT wins, including an
        assignment to the empty string.
        """
        from .env_tiers import resolve

        return resolve(self, bot_name=bot_name)

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
    def runtime_fleet(self) -> Path:
        return self.runtime / "fleet"

    @property
    def fleet_state(self) -> Path:
        """Directory for fleet-scoped runtime state (report-back.jsonl,
        workstreams.json): the fleet's own ``runtime/`` in overlay mode, the
        shared ``runtime/fleet/`` in root mode. The single home for that
        overlay-vs-root rule on the Python side (bash twin: ``fleet_runtime_dir``
        in lib-common.sh)."""
        return self.runtime if self.fleet_dir else self.runtime_fleet

    @property
    def lib(self) -> Path:
        return self.root / "lib"

    def bot_runtime(self, bot_name: str) -> Path:
        return self.runtime_bots / bot_name

    # --- detection ---

    @classmethod
    def detect(cls, hint: Path | None = None, fleet: str | None = None) -> "Paths":
        """Find the claudlobby root, walking up from the detection start.

        Start precedence: explicit `hint` > ``CLAUDLOBBY_ROOT`` env var > CWD.
        Supervised contexts (timer units, bot sessions) run from an arbitrary
        cwd but export CLAUDLOBBY_ROOT, so the env var beats the cwd walk-up.

        Marker: a directory containing both `library/` and `lib/`.

        If `fleet` is given, first check ``.claudron`` config at claudlobby root
        for a vault path. If the vault contains a fleet overlay for *fleet*,
        use that. Otherwise fall back to ``local/`` — resolving the fleet at
        flat (``<root>/local/<fleet>/``) OR nested
        (``<root>/local/<system>/<fleet>/``) depth.
        """
        start = Path(hint or os.environ.get("CLAUDLOBBY_ROOT") or Path.cwd()).resolve()
        root = None
        for candidate in [start] + list(start.parents):
            if (candidate / "library").is_dir() and (candidate / "lib").is_dir():
                root = candidate
                break
        if root is None:
            source = (
                "hint"
                if hint
                else "CLAUDLOBBY_ROOT env"
                if os.environ.get("CLAUDLOBBY_ROOT")
                else "cwd"
            )
            raise FileNotFoundError(
                f"Could not find claudlobby root (looked for library/ + lib/) "
                f"starting at {start} (from {source})"
            )

        fleet_dir = None
        vault_root = None

        if fleet:
            # Try vault-based fleet resolution (.claudron bridge → claudron API)
            fleet_dir, vault_root = _resolve_vault_fleet(root, fleet)

            # Fall back to local/ — flat OR nested (one level under a container).
            if fleet_dir is None:
                fleet_dir = _find_fleet_dir(root / "local", fleet)
                if fleet_dir is None:
                    flat = root / "local" / fleet
                    raise FileNotFoundError(
                        f"Fleet overlay not found: {flat} (run `claudlobby new-fleet {fleet}` to scaffold)"
                    )

        return cls(root=root, fleet_dir=fleet_dir, vault_root=vault_root)
