"""tests/test_shared_documentation_vault_library.py — the vault form of the
shared-documentation protocol keeps every rule in the text each vault-wired bot
composes. #2758 moved the reasons into `fleet-ops/vault-reference.md` and kept
the rules; its body maps 25 guards to the lines that hold them, and its review
checked each one. One `in` check per kept rule sentence: a later trim that drops
one fails here, and a rule reworded on purpose changes its line here in the same
PR (#2761). tests/test_shared_docs_default.py composes stand-in copies, so it
pins none of this text. The reasons the cut moved out are checked the other way:
none of their sentences comes back into the protocol."""

from __future__ import annotations

import re
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


# The five spans #2758 moved out to `fleet-ops/vault-reference.md`, under the names its
# table gives them, one sentence at a time: a sentence that comes back into the protocol
# puts its tokens back on every call of a vault-wired bot.
MOVED_OUT = (
    # Why `INDEX.md` is off limits
    "This is the one rule that distinguishes a vault-wired fleet from a raw-tree one, and it is not a style preference: the fleet's `shared/` tree is *inside* `$CLAUDRON_VAULT_PATH`, so those INDEX files are vault files.",
    "Opening them by hand is the thing the vault door exists to replace, and it silently misses everything in the vault's other tiers.",
    # The Claudron #143 evidence for short queries
    "The brevity is not style: **`recall` ranks correctly on short queries and collapses on prose** (Claudron #143 — score saturates, and the result set becomes a function of the vault rather than of your query).",
    "Measured, same door, same subject, only length differing: a three-word query returned the exact note first; a one-sentence description of the *same idea* returned five notes from an unrelated domain.",
    # What a degraded recall looks like
    "Under #143 what comes back is a topically self-consistent cluster from an adjacent domain — it reads exactly like a real relevance-ranked answer, which is why it gets accepted instead of retried.",
    "Obvious garbage would be safer.",
    # The maintainer note for when #143 is fixed
    "**This section is a MITIGATION, not the fix.**",
    "The defect is Claudron #143; this protocol only points people at the door.",
    "Written assuming #143 is **unfixed**.",
    "If it lands such that prose ranks correctly, revisit both rungs above rather than leaving them to rot — the short-query rule stays harmless, but the reasoning attached to it would no longer be true.",
    # What `capture` does
    "The capture verb types and dedups the note and puts it in the right tier.",
    "Hand-creating a file in the tree bypasses both and is how near-duplicate notes accumulate.",
)


def _prose(text: str) -> str:
    """Whitespace-collapsed with blockquote markers dropped: the maintainer note was a
    quoted block, and a quoted sentence that wraps carries a `>` inside it."""
    return _flat(re.sub(r"(?m)^\s*>", " ", text))


def test_the_reasons_the_cut_moved_out_stay_out():
    body = _prose(parse_frontmatter(PROTOCOL.read_text())[1])
    back = [s for s in MOVED_OUT if _prose(s) in body]
    assert not back, back
