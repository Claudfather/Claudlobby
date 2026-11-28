"""The name a team sees: core's rule against the shared name list (row 16), copied from the plan."""
import json
from pathlib import Path

import pytest

from claudlobby.commands.host_owner_setup import numbered_name
from claudlobby.operation_context import (RESERVED_SENDER_NAMES, _valid_human_alias, sender_alias_refusal,
                                          sender_name_form)
from claudlobby.plane.owner_setup import messages_shape

FIXTURE = Path(__file__).parent / "fixtures/sender_names_2026-11-23.jsonl"
LINES = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]


def test_the_shared_list_is_the_whole_copy():
    assert len(LINES) == 85 and len({line["id"] for line in LINES}) == 85
    assert {line["kind"] for line in LINES} == {"form", "check", "number"}


@pytest.mark.parametrize("line", [line for line in LINES if line["kind"] == "check"], ids=lambda line: line["id"])
def test_check_lines(line):
    assert (sender_alias_refusal(line["value"]) is None) is (line["answer"] == "accept")


@pytest.mark.parametrize("line", [line for line in LINES if line["kind"] == "number"], ids=lambda line: line["id"])
def test_number_lines(line):
    answer = numbered_name(line["base"], set(line["taken"]))
    assert (answer or "name_taken") == line["answer"]
    if answer is not None:
        assert sender_alias_refusal("human:" + answer) is None


@pytest.mark.parametrize("line", [line for line in LINES if line["kind"] == "form"], ids=lambda line: line["id"])
def test_every_formed_field_passes_the_check(line):
    # Core forms no name: it keeps the form lines and checks only what the card's field holds.
    assert sender_alias_refusal("human:" + line["field"]) is None


def test_reserved_names_are_one_constant_and_default_user_is_ordinary():
    assert RESERVED_SENDER_NAMES == ("owner", "lead", "manager", "engineer", "reviewer", "system", "admin",
                                     "root", "claude", "bot", "human", "team", "everyone")
    for name in RESERVED_SENDER_NAMES:
        assert sender_alias_refusal("human:" + name) == "sender_name_reserved"
        assert sender_name_form("human:" + name), "a reserved word still has the form; it is refused for its meaning"
    assert "default-user" not in RESERVED_SENDER_NAMES
    for name in ("human:default-user", "human:default-user-2"):
        assert sender_alias_refusal(name) is None and _valid_human_alias(name)


@pytest.mark.parametrize("value", ["human:ada", "human:ada-lovelace", "human:jean-luc-picard", "human:j-r-r-tolkien",
                                   "human:" + "b" * 40, "human:default-user", "human:default-user-2"])
def test_plan_accepts(value):
    assert sender_alias_refusal(value) is None


@pytest.mark.parametrize("value", ["human:a", "human:Ada", "human:42", "human:-x", "human:ada-", "human:ada--b",
                                   "human:owner", "human:lead", "human:ada lovelace", "human:ada:lovelace",
                                   "human:ada/lovelace", "human:mary-o'brien", "human:jos\u00e9", "human:" + "b" * 41,
                                   "ada-lovelace", "bot:ada-lovelace", "human:", None, 42])
def test_plan_refuses(value):
    assert sender_alias_refusal(value) == ("sender_name_reserved" if value in ("human:owner", "human:lead")
                                           else "sender_name_form")


def test_a_forty_character_name_numbers_within_the_rule():
    base = "c" * 40
    numbered = numbered_name(base, {base})
    assert numbered == "c" * 38 + "-2" and len(numbered) == 40 and sender_alias_refusal("human:" + numbered) is None


@pytest.mark.parametrize("value,accepted", [
    ({"alias": "human:ada-lovelace"}, True), ({"alias": "human:owner"}, True),
    ({"alias": "human:Ada"}, False), ({"alias": "human:ada", "sender": "card"}, False), ({"sender": "card"}, False),
    ({}, False), ("human:ada", False), (None, False)])
def test_stored_mark_shape(value, accepted):
    # A stored name keeps loading if the reserved list later grows: only its form is checked on load.
    assert bool(messages_shape(value)) is accepted
