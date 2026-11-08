"""tests/test_shared_documentation_vault_library.py — the vault form of the
shared-documentation protocol keeps every rule in the text each vault-wired bot
composes. #2758 moved the reasons into `fleet-ops/vault-reference.md` and kept
the rules; its body maps 25 guards to the lines that hold them, and its review
checked each one. One `in` check per kept rule sentence: a later trim that drops
one fails here, and a rule reworded on purpose changes its line here in the same
PR (#2761). tests/test_shared_docs_default.py composes stand-in copies, so it
pins none of this text."""

from __future__ import annotations

from pathlib import Path

from claudlobby.loader import parse_frontmatter

PROTOCOL = Path(__file__).resolve().parent.parent / "library" / "protocols" / "shared-documentation-vault.md"


def _flat(text: str) -> str:
    """Whitespace-collapsed: the recall rules wrap mid-sentence."""
    return " ".join(text.split())


# In the protocol's order, each under the number #2758's guard table gives it.
KEPT = (
    # 1: the Claudron door
    "Your fleet's shared docs live **inside the vault**.",
    "Reach them through the Claudron door — `recall` to find, `lookup` to fetch, `capture` to write.",
    # 2: never hand-scan `INDEX.md` or walk the tree
    "**Do not hand-scan `INDEX.md` files, and do not walk the doc tree looking for prior work.**",
    # 3-7: recall first with a short query; look up; at most 5 notes
    "**Recall first — with a SHORT query. Two to four keywords, never a sentence.**",
    "This is the step that replaces scanning a tree.",
    "Name the subject, not the situation — `cross-fleet consent`, not `when an incident on one fleet breaks something on another fleet…`.",
    "**Look up** directly when you already know the title or tag you want.",
    "Read only what the results justify.",
    "**Cap: never open more than 5 notes before starting work.**",
    # 8: plans surface through the same query; flag a conflicting plan
    "**Recall covers plans, not just knowledge.**",
    "An active plan for the same repo or area surfaces through the same query — you do not need a second, manual pass over `planning/active/`.",
    "If a returned plan conflicts with your task, flag it to the manager before proceeding.",
    # 9, 10: an empty result is a result; retry shorter
    "**A query that returns nothing is a result, not a failure** — but retry **SHORTER** before you conclude the fleet knows nothing.",
    "Recall is relevance-ranked, so vocabulary mismatch reads exactly like absence; re-wording at the same length does not help and **re-wording a long query stays broken**, which spends the one retry you were willing to spend.",
    # 11-13: a degraded result; confirm a negative with a short lookup
    "**And a degraded result does not look degraded.**",
    "**If the results are coherent but off-subject, suspect the query shape before concluding the vault is empty**, and confirm a negative with a short `lookup` before capturing a new note: two long recalls reading empty is not evidence of absence.",
    # 14-16: the writing convention
    "**Capture, don't hand-place.**",
    "**One note per topic.** If recall surfaces an existing note on the subject, update that note rather than adding a second.",
    "**Do not hardcode tier paths.** Which tier a note belongs in is Claudron's contract, not a path you compose yourself.",
    # 17-20: the lifecycle
    "**Creating:** `capture` the note. Claudron types and places it; there is no INDEX to regenerate.",
    "**Updating:** edit the note in place and let `capture` reconcile it, or edit a working document directly.",
    "**Completing:** set the terminal status in frontmatter (`superseded`; plans and audits may close `completed`).",
    "The note stays in the vault — recall ranks by relevance, so a closed doc stops surfacing without being moved.",
    "**Stale docs:** knowledge defaults to a 90-day `expires:` TTL.",
    "Surfacing expired notes is a frontmatter scan until `claudron review --json` is wired, at which point a weekly librarian job takes it over.",
    # 21, 22: working documents; one writer
    "Plans, decisions, and runbooks are *working* documents: they change while work is in flight, and they are edited directly rather than captured.",
    "Reach them through the door like anything else, and edit them in place once recall has told you where they are.",
    "**Single-writer still applies** — do not edit a document another bot is actively writing.",
    # 23-25: the promotion ladder; human-gated promotion; building blocks go by PR
    "Knowledge promotes by audience, and the vault owns the rungs:",
    "`memory/` — single bot (preferences, feedback). Outside the vault; yours alone.",
    "Your fleet's tier — fleet-wide. Where `capture` puts a finding by default.",
    "The shared hub — visible to every fleet on the vault.",
    "Promotion between tiers is a curation verb, human-gated; propose it, do not perform it.",
    "**Reusable building blocks** (skills, protocols, guardrails, expertise) are not knowledge-tier content and do not promote through the vault.",
    "When a fleet-local pattern proves useful to any claudlobby deployment, promote it to `library/` via a branch + PR to the open-source repo — a separate track, not a rung.",
)


def test_the_protocol_keeps_its_rule_sentences():
    fm, body = parse_frontmatter(PROTOCOL.read_text())
    assert fm["title"] == "Shared Documentation"
    missing = [s for s in KEPT if _flat(s) not in _flat(body)]
    assert not missing, missing
