"""The ``.env`` tier cascade, as the compositor sees it.

**This module owns no order.** It asks ``lib/env-tiers.sh`` — which is
``env_tier_rows`` in ``lib-common.sh``, the same function ``start-bot.sh``
sources at boot — and reports what the runtime says. That is the whole point
of #1226: ``Paths.env_file`` used to pick ONE tier (fleet if present, else
root) while the runtime cascaded FOUR, so a credential placed at the bot or
host tier resolved correctly at boot and read as missing to every tool.

A Python reimplementation of the order would have been cheaper and is exactly
the failure this repo keeps having — a predicate fixed centrally while a
consumer keeps its own copy (#892, #1143). The subprocess costs ~240ms once per
generate. A silent disagreement about where a secret comes from costs more.

Precedence, which is shell assignment semantics and nothing cleverer:

    The MOST SPECIFIC tier that ASSIGNS a key decides its value.

Assigns, not "supplies a value". ``export FOO=`` at the bot tier beats a real
secret at the fleet tier and resolves to the empty string, because ``.`` is
assignment. Measured, both bash and zsh. So an empty value is a WIN, not a
miss, and :attr:`Resolution.empty` exists to make that visible rather than to
paper over it — on this estate ``GITHUB_PAT`` is present-but-empty at the fleet
tier of two of four fleets, which is why the GitHub MCP has been wired to ``""``
there without anyone noticing (#1213).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from . import dotenv

if TYPE_CHECKING:
    from .paths import Paths

_log = logging.getLogger(__name__)

#: Seconds to wait for the resolver. Generous: it sources lib-common.sh, which
#: is ~3300 lines, and the measured cost on a Raspberry Pi 5 is ~240ms.
_RESOLVER_TIMEOUT_S = 30


class ResolverUnavailable(RuntimeError):
    """The runtime's resolver could not be reached.

    Raised, never swallowed into a default. A fallback ordering here would BE
    the second copy this module exists to avoid, and it would be consulted
    exactly when the two are most likely to disagree.
    """


class EnvTier(NamedTuple):
    """One tier row as the runtime reports it."""

    tier: str  # "host" | "root" | "fleet" | "bot"
    path: Path | None  # None only when state == "unresolved"
    state: str  # "present" | "absent" | "unresolved"

    @property
    def exists(self) -> bool:
        return self.state == "present"

    @property
    def applies(self) -> bool:
        """False when the tier does not apply at all (no fleet, no bot).

        Distinct from :attr:`exists`: a fleet tier that applies and holds no
        file is a fleet nobody has given a ``.env`` yet, which is a different
        fact from a query that named no fleet.
        """
        return self.state != "unresolved"


class Resolution(NamedTuple):
    """Where one env var actually comes from, and what it lost on the way."""

    name: str
    value: str
    tier: str  #: winning tier — the most specific one that ASSIGNED the key
    path: Path | None  #: the file that won
    assigned_by: tuple[str, ...]  #: every tier that assigned it, cascade order
    origin_hint: str = ""  #: filled by the register; not known here

    @property
    def empty(self) -> bool:
        """The winning assignment is the empty string.

        Deliberately not folded into "missing". An empty win means a tier
        actively blanked the key; a missing key means no tier mentioned it.
        Same observable to a naive check, opposite remedies: one is "delete the
        stub", the other is "add the secret".
        """
        return self.value == ""

    @property
    def shadowed(self) -> tuple[str, ...]:
        """Tiers whose assignment the winner overrode."""
        return self.assigned_by[:-1]


def resolver_path(paths: Paths) -> Path:
    """Where ``env-tiers.sh`` lives for this root."""
    return paths.lib / "env-tiers.sh"


def read_tiers(
    paths: Paths, bot_name: str | None = None, fleet_name: str | None = None
) -> list[EnvTier]:
    """The four tiers in runtime sourcing order, least specific first.

    The child's environment is built explicitly rather than inherited. Every
    var the resolver reads — ``HOME``, ``CLAUDLOBBY_ROOT``, ``FLEET_NAME``,
    ``BOT_DIR`` — is also a var an ambient bot session exports, so an inherited
    environment would let the caller's own session silently redirect the answer.
    That has already happened here once, to a ledger assertion, and it read as
    a clean pass on every arm at once.
    """
    script = resolver_path(paths)
    if not script.is_file():
        raise ResolverUnavailable(
            f"env tier resolver not found at {script} — the compositor reads the "
            f"tier order from the runtime and will not substitute a copy of it"
        )

    fleet = fleet_name if fleet_name is not None else _fleet_name_for(paths)
    bot_dir = str(paths.bot_runtime(bot_name)) if bot_name else ""

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "CLAUDLOBBY_ROOT": str(paths.root),
        "FLEET_NAME": fleet,
        "BOT_DIR": bot_dir,
    }
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["bash", str(script), bot_dir, fleet],
            capture_output=True,
            text=True,
            env=env,
            timeout=_RESOLVER_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResolverUnavailable(f"could not run {script}: {exc}") from exc
    if proc.returncode != 0:
        raise ResolverUnavailable(
            f"{script} exited {proc.returncode}: {proc.stderr.strip()[:400]}"
        )

    rows: list[EnvTier] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise ResolverUnavailable(f"malformed row from {script}: {line!r}")
        tier, raw_path, state = parts
        rows.append(EnvTier(tier, Path(raw_path) if raw_path else None, state))
    if not rows:
        raise ResolverUnavailable(f"{script} returned no tiers")
    return rows


def _fleet_name_for(paths: Paths) -> str:
    """The fleet name the runtime would use, or "" in root mode.

    ``Paths`` carries the fleet DIR, not the name; the runtime is given the
    name and resolves the dir itself (flat or nested). Handing it the directory
    leaf is what start-bot.sh is handed, so the resolution path is the same one.
    """
    return paths.fleet_dir.name if paths.fleet_dir else ""


def cascade(tiers: list[EnvTier]) -> dict[str, Resolution]:
    """Merge the tiers the way the shell does: later assignment wins.

    Empty values are assignments and they win. See the module docstring.
    """
    winner: dict[str, Resolution] = {}
    for tier in tiers:
        if not tier.exists or tier.path is None:
            continue
        try:
            parsed = dotenv.read(tier.path)
        except (OSError, UnicodeDecodeError):
            # One undecodable byte in one tier must not abort a whole generate;
            # skipping is the same direction _upstream_env_names already takes.
            _log.warning("could not read env tier %s, skipping", tier.path)
            continue
        for name, value in parsed.items():
            prior = winner.get(name)
            winner[name] = Resolution(
                name=name,
                value=value,
                tier=tier.tier,
                path=tier.path,
                assigned_by=(prior.assigned_by if prior else ()) + (tier.tier,),
            )
    return winner


def resolve(
    paths: Paths, bot_name: str | None = None, fleet_name: str | None = None
) -> dict[str, Resolution]:
    """Every var the runtime would see for *bot_name*, and where it came from."""
    return cascade(read_tiers(paths, bot_name=bot_name, fleet_name=fleet_name))
