# tests/test_eval_protocols_library.py
"""The eval protocols bring their procedure with them (#717): `eval-workflow` and
`ab-gating-rollout` each require the `run-eval` skill, so a bot that composes either
one carries the skill without declaring it. Pinned as the check-in protocol's
requirement is (tests/test_checkin_library.py). Each also keeps its rules in its own
text, one `in` check per kept rule sentence, and the skill keeps the rules the cut
moved into it, checked the same way (#2761)."""

import shutil
from pathlib import Path

import pytest

from claudlobby.composer import resolve_effective_skills
from claudlobby.config import load_fleet
from claudlobby.loader import parse_frontmatter
from claudlobby.paths import Paths
from tests.package_fixtures import source_package

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"
EVAL_PROTOCOLS = ("eval-workflow", "ab-gating-rollout")


@pytest.mark.parametrize("name", EVAL_PROTOCOLS)
def test_the_protocol_requires_the_run_eval_skill(name):
    fm, _body = parse_frontmatter((LIB / "protocols" / f"{name}.md").read_text())
    assert fm["requires"] == {"skills": ["run-eval"]}


@pytest.mark.parametrize("name", EVAL_PROTOCOLS)
def test_a_bot_that_declares_only_the_protocol_carries_the_skill(fleet_dir, name):
    shutil.copy(LIB / "protocols" / f"{name}.md", fleet_dir / "library" / "protocols" / f"{name}.md")
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    worker-1:\n", f"    worker-1:\n      protocols: [{name}]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    bot = fleet.bots["worker-1"]
    assert name in bot.protocols and "run-eval" not in bot.skills  # required, never declared
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package())
    is_manager = bot.bot_id in fleet.manager_bots()
    assert "run-eval" in resolve_effective_skills(bot, fleet, paths, is_manager=is_manager)


def _flat(text: str) -> str:
    """Whitespace-collapsed, as tests/test_checkin_library.py reads a body."""
    return " ".join(text.split())


# The rules each protocol keeps in its own text, word for word: #2753's guard table
# places rows 1, 2 and 10 in `eval-workflow` and rows 2, 10, 13, 15, 18 and 26-30 in
# `ab-gating-rollout`, and its review checked each against the text. A later trim that
# drops one fails here; a rule reworded on purpose changes its line here in the same PR.
KEPT = {
    "eval-workflow": (
        "An eval exists to change a decision: if no result would change what you ship, do not run one.",
        "When one is due, invoke the `run-eval` skill **before the first run**.",
        "It starts by writing down what would convince you, which is worth nothing once you have seen results, and it holds every other rule of the eval.",
        "**INCONCLUSIVE is a real result and is never rounded up to PASS.**",
    ),
    "ab-gating-rollout": (
        "A change whose justification is a measurable effect lands on evidence, not argument.",
        'The gate fires when the PR body states a number, a percentage or a comparative ("cheaper", "faster", "fewer tokens"), when a protocol, prompt, model or library change exists to change behaviour, or when landing it would change what the fleet spends.',
        "It does not fire for correctness fixes, refactors that claim no effect, or docs.",
        "An effect you cannot state so that an eval could refute it is no claim: drop it from the PR body and land the change on its other merits.",
        "When the gate fires, the PR needs a verdict from the `run-eval` skill, which also says what each outcome does to the PR.",
        "A verdict counts only if its statistic was registered before the first run, no axis or threshold changed after the results, and its pins (model, artifact, battery and harness versions) still match the running system.",
        "**INCONCLUSIVE never passes the gate.**",
    ),
}


@pytest.mark.parametrize("name", EVAL_PROTOCOLS)
def test_the_protocol_keeps_its_rule_sentences(name):
    _fm, body = parse_frontmatter((LIB / "protocols" / f"{name}.md").read_text())
    missing = [s for s in KEPT[name] if _flat(s) not in _flat(body)]
    assert not missing, missing


# The rules the cut moved into `run-eval`, word for word: each rule sentence on the
# skill lines #2753's guard table names as carrying a guard, among them the four its
# review named (the Verdict row, the PASS and FAIL lines, and the paragraph that gates
# on every cost). A rule is pinned with its reason clause; where a sentence also holds
# an example list or a pointer, only the rule's part is. Headings, the sizing formula,
# the report example and sentences that only give a reason are not pinned.
RUN_EVAL = (
    # The opening and When to use
    "**Default is `--dry-run`.**",
    "Real runs cost real money and are opt-in",
    "A PR claims a measurable effect",
    "and needs the pre-land gate.",
    "**When not to use:** correctness fixes, refactors claiming no effect, docs.",
    "If no result would change what you ship, do not run this — you are buying a number, not an answer.",
    "Say so and move on.",
    "An effect you cannot state so that an eval could refute it is no claim either: drop it from the PR body and land the change on its other merits.",
    # Step 1: Pre-register
    "Write these down first, every row: an eval missing one cannot produce a verdict anyone should act on, and one registered after the results are known is not evidence.",
    "| **Target** | One property, stated so a result could contradict it. Name what it is *not*. |",
    "| **Battery** | Tasks chosen now, representative of the real workload. Verdicts are **per task type**, never pooled. |",
    "| **Baseline** | Control arm identical in everything except the target. |",
    "| **Reps** | Paired reps per task; stopping rule is the CI rule below, not your judgement. |",
    "| **Verdict** | An axis for every cost the change moves, a threshold per axis, CI method/width/seed. |",
    "| **Pins** | Model version, artifact hash, battery version, harness version. |",
    "If a threshold is not yet ratified, the run emits **INCONCLUSIVE by construction**.",
    "Do not substitute a permissive default to get a result — a gate that can green itself before its bar exists is worse than no gate.",
    # Step 2: Size the matrix
    "Estimate tokens per cell and multiply.",
    "Check the total against the fleet's budget ceiling **now**.",
    "If the number is uncomfortable, cut reps or battery *before* running and record that you did — a narrowed battery is a scope statement the verdict has to carry.",
    "If the eval would cost more than the change is worth, that is itself a finding: say so, and land the change without its effect claim rather than run a matrix nobody wanted.",
    # Step 3: Run
    "Run control and treatment **paired** — compare within a task and rep, then aggregate the paired deltas.",
    # Step 4: Verdict
    "Apply the pre-registered rule; do not add an axis or move a threshold after seeing data.",
    "If the pre-registration was wrong, say so and re-register publicly, as a new run, never as a quiet edit.",
    "**PASS** — on every co-primary axis, the confidence interval clears its bar.",
    "**FAIL** — on any axis, the interval lies entirely below its bar.",
    "**STRADDLE** — CI spans the bar; extend reps up to `--reps-max`.",
    "**INCONCLUSIVE** — still straddling at `--reps-max`.",
    "**INCONCLUSIVE is never rounded up to PASS.**",
    "Reps extend on a straddling interval, never on a disappointing one.",
    "Where a change has more than one cost, gate on all of them: *all* must clear — a win on one and a regression on another is a FAIL, not a trade to argue in prose.",
    "If such a trade is worth making, a human decides and records it; the gate never absorbs it.",
    # Step 5: Report
    "Report the effect size **and** its interval, with pins:",
    "Also report **what was not measured** — every eval has a scope boundary the number cannot show.",
    "Report INCONCLUSIVE as plainly as PASS.",
    "Attach the verdict to the PR when this is running as the `ab-gating-rollout` gate.",
    # Step 6: What the verdict does to the PR
    "| **PASS** | The gate is satisfied: review and merge go on their normal terms. |",
    "| **FAIL** | It does not land on this effect claim. Fix the change, or withdraw the claim. |",
    "| **INCONCLUSIVE** | It does not pass the gate. It may land only on a different, stated justification",
    "with the effect claim taken out of its body; never on the original claim with the eval called noisy. |",
    '"Directionally right" and "no evidence of harm" are not the bar: a change that cannot show its effect has not earned a landing on it.',
    "The gate decides before merge whether a change may land on its claim; `canary-rollout` decides after merge how a landed change rolls out.",
    "Neither covers the other: a change that claims an effect and ships fleet-wide passes both.",
    # Modes
    "| `--dry-run` (**default**) | Zero model calls. Synthesized inputs driven through the **real** measurement and verdict path, so the wiring stays honest between real runs. CI-safe, and the only mode CI runs. |",
    "| `--real` | Requires `AB_EVAL_REAL=1` in the environment as well as the flag.",
    "Real mode is deliberately two-key: a flag alone cannot trip it, and the env gate cannot trip it without intent.",
    "Never set `AB_EVAL_REAL` on a bot's standing config.",
    # Honesty rules
    "Report the verdict you got, including when it is not the one hoped for.",
    "Never re-run to a better number.",
    "Never narrow or otherwise change the battery after seeing results: a battery edited in response to results is no longer evidence.",
    "A verdict whose pins no longer match the running system is **expired**, not weak evidence.",
    "Re-run or drop the claim.",
)


def test_run_eval_keeps_the_rules_the_cut_moved_into_it():
    body = _flat(parse_frontmatter((LIB / "skills" / "run-eval" / "SKILL.md").read_text())[1])
    missing = [s for s in RUN_EVAL if _flat(s) not in body]
    assert not missing, missing
