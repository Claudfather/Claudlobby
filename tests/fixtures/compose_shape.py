#!/usr/bin/env python3
"""tests/fixtures/compose_shape.py — PR4 task 3 byte-identity harness (#1569,
brief Step 4 / controller correction 6, invoked from the Global Constraint
"nothing composes differently on a fleet without a leaf manager").

Composes one of three fleet SHAPES that have no leaf manager at all (``solo``,
``worker-only``, ``coordinator-only`` — see ``_SHAPES``) twice: once with
task 3's two changes neutralised (``--before``, simulating the code as it
stood at the end of task 2) and once with the code exactly as committed
(``--after``). The CLI prints a sorted, deterministic manifest of every file
under the composed fleet's ``runtime/bots/`` tree — content hashes, not paths
that embed a throwaway root — so a plain ``diff`` between a ``--before`` and
an ``--after`` run is a real byte-identity check.

**Scope, stated once (controller correction 6):** the byte-identity claim is
about BOT directories only. Task 3's job gate (``composer.LEAF_MANAGER_GATED_
JOBS``) is a SEPARATE, intended change that legitimately removes the dormant
``manager-checkin`` units from ``runtime/fleet/timers/`` for a fleet with no
leaf manager — that is not a regression to detect, so the CLI's printed
manifest never includes the timers dir. ``compose_shape()`` (the importable
function) returns BOTH manifests, precisely so the pytest test in
``tests/test_composer.py`` can assert the bots tree is identical AND assert,
separately, exactly what the timers dir difference is.

The toggle is two independent monkeypatches, both undone before this
function returns, so a caller in the SAME process (the pytest test) can run
several shapes back to back without leaking state between them:

* ``defaults.REGISTRY["protocols"].roles`` — the leaf-manager role entry
  (task 3 step 3) is popped for ``--before``.
* ``composer.LEAF_MANAGER_GATED_JOBS`` — the job gate (task 3 step 4) is
  emptied for ``--before``, so ``manager-checkin`` composes exactly as it did
  before this task (dormant, unconditionally) rather than being gated.

Neither of the three shapes has a leaf manager, so in practice the registry
toggle is inert for THESE runs (the role never applies to a bot that holds no
role) — what the bots-tree comparison actually proves is that task 3 changed
NOTHING about bot composition on a fleet that cannot use the thing it added.
The job-gate toggle is what makes the timers-dir difference observable at
all: it is the one axis on which the two runs are meant to differ.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

#: The three no-leaf-manager shapes (controller correction 6). Bot ids and
#: the fleet name are obviously fake (the repo is public).
_SHAPES: dict[str, str] = {
    # One bot, no team at all.
    "solo": """\
fleet:
  name: ck4-shape-solo
  service_prefix: com.ck4.solo
  bots:
    solo-bot:
      expertise: [software-engineering]
""",
    # Two bots, neither manages the other — no manager at all.
    "worker-only": """\
fleet:
  name: ck4-shape-worker-only
  service_prefix: com.ck4.wonly
  bots:
    peon-a:
      expertise: [software-engineering]
    peon-b:
      expertise: [software-engineering]
""",
    # coord's only report is midlead, itself a manager -> coord not leaf.
    # midlead's team has NO workers -> midlead not leaf either (no in-fleet
    # report at all). Two managers, zero leaf managers.
    "coordinator-only": """\
fleet:
  name: ck4-shape-coordinator-only
  service_prefix: com.ck4.coordonly
  teams:
    outer:
      manager: topcoord
      workers: [midlead]
    inner:
      manager: midlead
      workers: []
  bots:
    topcoord:
      expertise: [orchestration]
    midlead:
      expertise: [orchestration]
""",
}


def _write_shape(fleet_dir: Path, shape: str) -> Path:
    fleet_dir.mkdir(parents=True, exist_ok=True)
    fleet_yaml = fleet_dir / "fleet.yaml"
    fleet_yaml.write_text(_SHAPES[shape])
    return fleet_yaml


@contextmanager
def _toggled(before: bool):
    """No-op for ``--after``. For ``--before``, neutralises task 3's two
    changes for the duration of the block and restores both on exit."""
    if not before:
        yield
        return

    from claudlobby import composer, defaults

    protocols = defaults.REGISTRY["protocols"]
    had_role = defaults.ROLE_LEAF_MANAGER in protocols.roles
    saved_role = protocols.roles.pop(defaults.ROLE_LEAF_MANAGER, None)
    saved_gate = composer.LEAF_MANAGER_GATED_JOBS
    composer.LEAF_MANAGER_GATED_JOBS = frozenset()
    try:
        yield
    finally:
        if had_role:
            protocols.roles[defaults.ROLE_LEAF_MANAGER] = saved_role
        composer.LEAF_MANAGER_GATED_JOBS = saved_gate


def _manifest(base: Path) -> list[str]:
    """Sorted ``<relpath>\\t<sha256-or-target>`` lines for every file under
    *base*. Content hashes rather than paths, so two composes into different
    throwaway roots compare equal when their content does. A directory
    symlink (a skill link) fails the ``is_file()`` probe below and would
    otherwise be invisible to the manifest entirely — recorded by its actual
    ``os.readlink`` target (fix round 1, item 5b), not the literal string
    ``SYMLINK``: recording the same constant for every link makes a link
    whose TARGET changed (a skill re-pointed at a different library entry,
    same name) invisible — the path matches, the recorded value never
    varies, so nothing distinguishes it from an unchanged link. Both arms
    compose into the same fleet dir against the same library (module
    docstring), so a target string is exactly as stable across arms as a
    content hash is."""
    if not base.is_dir():
        return []
    lines: list[str] = []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if p.is_symlink():
            lines.append(f"{rel}\t{os.readlink(p)}")
        elif p.is_file():
            lines.append(f"{rel}\t{hashlib.sha256(p.read_bytes()).hexdigest()}")
    return lines


def _text_map(base: Path) -> dict[str, str]:
    """``{relpath: content}`` for every regular file under *base* — the raw
    counterpart to :func:`_manifest`'s hashes, for a caller that needs to
    inspect a small file's actual content (the DORMANT manifest's own text)
    rather than just detect that it changed."""
    if not base.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(base.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(base))] = p.read_text()
    return out


def compose_shape(
    root: Path, fleet_dir: Path, shape: str, *, before: bool
) -> dict[str, list[str] | dict[str, str]]:
    """Compose *shape* against *root* (a claudlobby checkout supplying the
    real ``library/``/``templates/``/``lib/``) into *fleet_dir* (an overlay
    this call owns exclusively), toggling task 3's changes per *before*.

    Returns ``{"bots": [...], "timers": [...], "timers_text": {...}}`` —
    hash manifests scoped to ``runtime/bots/`` and ``runtime/fleet/timers/``
    respectively (see the module docstring for why the split matters), plus
    the timers dir's raw text content for a caller that needs to read a
    specific file (e.g. the DORMANT manifest) rather than just diff it.

    BETWEEN ARMS, ``runtime/bots`` is removed before composing (fix round 1,
    item 5a) — both arms compose into the SAME *fleet_dir*, so a file the
    SECOND arm stops writing would otherwise survive on disk from the FIRST
    arm and read identical: the byte-identity comparison could only ever see
    an addition, never a removal. ``runtime/fleet/timers`` is deliberately
    LEFT ALONE here: the timers-dir test's whole point is that the second
    arm UNLINKS what the first arm composed (the job-gate prune itself), and
    clearing it pre-emptively would turn that assertion into a restatement
    of what this function already did rather than a proof of what the
    COMPOSER did.
    """
    from claudlobby.composer import compose_fleet, compose_fleet_timers
    from claudlobby.config import load_fleet
    from claudlobby.paths import Paths

    fleet_yaml = _write_shape(fleet_dir, shape)
    paths = Paths(root=root, fleet_dir=fleet_dir)
    if paths.runtime_bots.is_dir():
        shutil.rmtree(paths.runtime_bots)
    with _toggled(before):
        fleet, merged = load_fleet(fleet_yaml)
        compose_fleet(fleet, paths, log=lambda _m: None)
        compose_fleet_timers(fleet, paths, merged)
    timers_dir = paths.runtime_fleet / "timers"
    return {
        "bots": _manifest(paths.runtime_bots),
        "timers": _manifest(timers_dir),
        "timers_text": _text_map(timers_dir),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path, help="exported claudlobby checkout")
    ap.add_argument("shape", choices=sorted(_SHAPES))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--before", action="store_true")
    g.add_argument("--after", action="store_true")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    fleet_dir = root / "local" / f"ck4-shape-{args.shape}"
    manifests = compose_shape(root, fleet_dir, args.shape, before=args.before)
    # Scoped to the bots tree ONLY (module docstring) — the timers-dir
    # difference is real, intended, and this CLI's diff must stay clean.
    for line in manifests["bots"]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
