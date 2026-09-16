"""#1517 — every script that can raise a fleet signal is classified, and stays so.

The defect was never one script. `_emit_fleet_signal` is the single resolver;
a HOST-scoped caller has no fleet to pass, so its recipient falls out of a
lexical glob. The fix is in the resolver, but the *set* of host-scoped callers
is what decides who was affected -- and that set is what kept moving during
triage, from four to six to seven.

So the set is a gate rather than a list. Adding a host job that can alert, or
converting a fleet job into a host job, changes SCRIPTS_REACHING_DOOR or
HOST_SCOPED below and turns this red -- which is the moment to decide whether
that job's recipient is declared.

Two enumeration rules, both learned the hard way and both load-bearing:

1. **Follow the transitive closure, not the two direct wrappers.** `notify_currency`
   wraps `emit_fleet_notice`, so grepping only `emit_failure_alert|emit_fleet_notice`
   misses every caller that goes through it -- including `notify-behind.sh`, the
   script whose ten weeks of misrouted notices produced the issue, and
   `update-siblings.sh`. A method that omits the origin instance looks complete.

2. **Classify from system.yaml `host.jobs`, never from the unit name.** The unit for
   `update-claude-code.sh` is `claudlobby-claude-update.service`; a classifier that
   globs for a unit containing the script basename reports "not a host job" for a
   host job, and reports it cleanly.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"

# Wrappers on _emit_fleet_signal, plus the one that wraps a wrapper (rule 1).
DOORS = ("emit_failure_alert", "emit_fleet_notice", "notify_currency")

# Scripts that can reach the signal door. Update deliberately, never to go green.
SCRIPTS_REACHING_DOOR = {
    "disk-monitor", "fleet-memory-check", "host-health-check", "keepalive",
    "migrate-fleet-to-system", "notify-behind", "orphan-browser-reaper",
    "reload-fleet", "rolling-restart", "start-bot", "update-claude-code",
    "update-siblings", "validate-bot-change", "weekly-worker-restart",
}

# Of those, the ones systemd runs with NO fleet TODAY -- the retrofit half.
ACTIVE_MISROUTING = {
    "disk-monitor", "fleet-memory-check", "host-health-check",
    "notify-behind", "orphan-browser-reaper", "update-claude-code",
}

# A third category, not a tiebreak. update-siblings is host-scoped BY DESIGN
# (it walks every framework checkout on the host) but cannot misroute BY CURRENT
# STATE: its units compose, nothing enrols them, and it accepts a fleet
# positionally with a CLAUDLOBBY_FLEET fallback. Armed WITH a fleet it resolves
# correctly; armed as composed -- ExecStart names no fleet -- it misroutes on its
# first run. Collapsing it into either neighbour loses the fact the fix needs:
# it is the only member reachable BEFORE it breaks.
LATENT = {"update-siblings"}

# Pass a fleet, resolve at step 1, and must stay untouched. Over-reaching into
# this set is the realistic failure mode of a fix aimed at the other one.
CORRECT = {
    "keepalive", "migrate-fleet-to-system", "reload-fleet", "rolling-restart",
    "start-bot", "weekly-worker-restart", "validate-bot-change",
}

def _callers() -> set[str]:
    pat = re.compile(r"\b(" + "|".join(DOORS) + r")\s")
    out = set()
    for f in LIB.glob("*.sh"):
        if f.stem == "lib-common":      # defines the doors; not a caller
            continue
        if pat.search(f.read_text(errors="replace")):
            out.add(f.stem)
    return out


def _host_job_scripts() -> set[str]:
    data = yaml.safe_load((REPO_ROOT / "claudlobby" / "system.yaml").read_text()) or {}
    jobs = ((data.get("host") or {}).get("jobs") or {})
    return {Path(str(c.get("script", ""))).stem for c in jobs.values() if c.get("script")}


def test_the_caller_set_has_not_moved() -> None:
    assert _callers() == SCRIPTS_REACHING_DOOR


def test_the_three_categories_partition_the_set() -> None:
    assert ACTIVE_MISROUTING | LATENT | CORRECT == SCRIPTS_REACHING_DOOR
    assert not (ACTIVE_MISROUTING & CORRECT) and not (LATENT & CORRECT)


def test_the_misrouting_half_is_derived_from_system_yaml_not_asserted() -> None:
    """Classify by the declared edge, never by a unit NAME (rule 2)."""
    derived = SCRIPTS_REACHING_DOOR & _host_job_scripts()
    assert derived == ACTIVE_MISROUTING | LATENT, (
        f"host-scoped signal callers changed: {derived ^ (ACTIVE_MISROUTING | LATENT)}. "
        "A new one needs a declared recipient (CLAUDLOBBY_ALERT_MANAGER), "
        "or it inherits the lexical fallback."
    )


def test_the_correct_half_is_left_alone() -> None:
    """Both halves. Over-reach here is the realistic failure mode of the fix."""
    assert CORRECT & _host_job_scripts() == set()


def test_the_latent_member_misroutes_only_if_armed_without_a_fleet() -> None:
    """Why it is fixable before it breaks -- and why it is not 'correct' either."""
    src = (LIB / "update-siblings.sh").read_text(errors="replace")
    assert re.search(r'\*\)\s*\[ -z "\$FLEET" \] && FLEET="\$arg"', src), (
        "update-siblings no longer takes a fleet positionally -- it has moved out "
        "of LATENT into one of the other two categories; re-derive."
    )
    assert 'FLEET="${FLEET:-${CLAUDLOBBY_FLEET:-}}"' in src
    jobs = (yaml.safe_load((REPO_ROOT / "claudlobby" / "system.yaml").read_text()) or {})
    script = ((jobs.get("host") or {}).get("jobs") or {}).get("update-siblings", {}).get("script", "")
    # The declared command carries NO fleet, so arming it as composed misroutes.
    assert script.endswith("update-siblings.sh"), script
    assert " " not in script.strip(), (
        "update-siblings now composes with an argument -- if that argument is a "
        "fleet, this member is fixed by construction and belongs in CORRECT"
    )


def test_notify_currency_is_a_door_or_the_enumeration_is_blind() -> None:
    """Pins rule 1: drop notify_currency and the origin instance vanishes."""
    src = (LIB / "lib-common.sh").read_text(errors="replace")
    body = src[src.index("notify_currency()"):]
    assert "emit_fleet_notice" in body[:2000], (
        "notify_currency no longer reaches emit_fleet_notice -- re-derive DOORS"
    )
    nb = (LIB / "notify-behind.sh").read_text(errors="replace")
    assert not re.search(r"\b(emit_failure_alert|emit_fleet_notice)\s", nb), (
        "notify-behind now calls a wrapper directly; the blindness this guards "
        "against is gone and the comment above should be updated"
    )
    assert re.search(r"\bnotify_currency\s", nb)
