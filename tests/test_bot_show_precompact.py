"""`bot show` names the PreCompact hooks a bot's compose gives it (#1053, build PR 3b).

The compaction executor is to read the same facts from `bot.conf`, so each test
reads both: the files a real render writes, and the CLI's `precompact` block.
The orientation tests' file is held by another open PR, so these tests reuse its
fixture and CLI helper from here instead of adding to it.
"""

import hashlib
import json

import pytest
import yaml

from claudlobby import composer, context
from claudlobby.__main__ import main
from claudlobby.composer import compose_bot_conf, compose_settings_local
from claudlobby.config import BotConfig, FleetConfig
from tests.test_claudron_loop import _paths
from tests.test_config_staging import _write
from tests.test_config_staging import staging_case as source_staging_case  # noqa: F401 (fixture)
from tests.test_orientation_commands import _call, _commit, staging_case  # noqa: F401 (fixture)

WORKER = "primary-worker"
SETTINGS = ".claude/settings.local.json"


def _declare(case, **keys):
    """Set (or, with None, remove) keys on the worker's fleet.yaml entry."""
    doc = yaml.safe_load(case.paths.fleet_yaml.read_text())
    worker = doc["fleet"]["bots"][WORKER]
    for key, value in keys.items():
        if value is None:
            worker.pop(key, None)
        else:
            worker[key] = value
    case.paths.fleet_yaml.write_text(yaml.safe_dump(doc))


def _compose(case):
    """The real renderer's bot.conf and settings, written where `bot show` reads them."""
    loaded = context.load_context(case.paths)
    rendered = composer.render_bot_files(loaded.fleet.bots[WORKER], loaded.fleet, case.paths,
                                         boot_delay_s=0, cascade={})
    directory = case.paths.bot_runtime(WORKER)
    for name in ("bot.conf", SETTINGS):
        _write(directory / name, rendered[name].content)
    return directory


def _show(case, capsys):
    _commit(case)
    return _call(capsys, case.root, "bot", "show", WORKER)["bot"]["effective_configuration"]["precompact"]


def _text(case, capsys):
    assert main(["--root", str(case.root), "bot", "show", WORKER]) == 0
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("PreCompact:")]


def _engine_commands(settings):
    """The engine's PreCompact commands, by its `hook pre-compact` suffix, written out here
    rather than read from the composer, so a wrong predicate cannot pass its own test."""
    return [hook["command"] for group in settings.get("hooks", {}).get("PreCompact", [])
            for hook in group["hooks"] if hook["command"].endswith("hook pre-compact")]


def _digest(command):
    return hashlib.sha256(command.encode()).hexdigest()


@pytest.mark.parametrize("keys, loop", [
    ({"claudron_vault_path": "vault"}, True),
    ({"claudron_vault_path": "vault", "claudron_session_loop": False}, False),
    ({}, False),
    ({"claudron_session_loop": True}, False),
], ids=["vault-loop-unset", "vault-loop-off", "no-vault", "loop-on-no-vault"])
def test_bot_show_names_the_engine_hook_exactly_where_the_compose_adds_it(
        staging_case, monkeypatch, capsys, keys, loop):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    _declare(case, **keys)
    directory = _compose(case)
    engine = _engine_commands(json.loads((directory / SETTINGS).read_text()))
    assert len(engine) == (1 if loop else 0)
    assert f"\nexport CLAUDRON_SESSION_LOOP={1 if loop else 0}\n" in (directory / "bot.conf").read_text()

    block = _show(case, capsys)
    assert block == {
        "claudron_session_loop": loop, "claudna_precompact_reflect": False,
        "composed": {"state": "present", "source": str(directory / SETTINGS),
                     "commands": [{"hook": "claudron", "sha256": _digest(command)} for command in engine],
                     "matches_declaration": True}}
    assert _text(case, capsys) == [
        f"PreCompact: Claudron session loop {'on' if loop else 'off'}; clauDNA prompt off; "
        f"composed settings present, commands: {'claudron' if loop else 'none'}."]


@pytest.mark.parametrize("armed", [True, False])
def test_bot_show_reads_the_clauDNA_prompt_as_bot_conf_composes_it(staging_case, monkeypatch, capsys, armed):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    _declare(case, claudna={"precompact_reflect": armed})
    directory = _compose(case)
    assert f"\nexport CLAUDNA_PRECOMPACT_REFLECT={1 if armed else 0}\n" in (directory / "bot.conf").read_text()
    assert _show(case, capsys)["claudna_precompact_reflect"] is armed


def test_a_fleet_hook_is_listed_as_other_by_its_digest_and_never_printed(staging_case, monkeypatch, capsys):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    # `_call` asserts that no "SECRET-orientation" text reaches the output.
    own = "echo SECRET-orientation-precompact"
    _declare(case, claudron_vault_path="vault", hooks={"PreCompact": [{"command": own}]})
    directory = _compose(case)
    (engine,) = _engine_commands(json.loads((directory / SETTINGS).read_text()))

    composed = _show(case, capsys)["composed"]
    assert composed["commands"] == [{"hook": "other", "sha256": _digest(own)},
                                    {"hook": "claudron", "sha256": _digest(engine)}]
    assert composed["matches_declaration"] is True
    lines = _text(case, capsys)
    assert lines == ["PreCompact: Claudron session loop on; clauDNA prompt off; "
                     "composed settings present, commands: other, claudron."]


@pytest.mark.parametrize("composed_with, declared", [
    ({"claudron_vault_path": "vault"}, {"claudron_session_loop": False}),
    ({"claudron_vault_path": "vault", "claudron_session_loop": False}, {"claudron_session_loop": None}),
], ids=["hook-composed-loop-now-off", "no-hook-composed-loop-now-on"])
def test_a_composition_older_than_its_declaration_reads_as_a_mismatch(
        staging_case, monkeypatch, capsys, composed_with, declared):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    _declare(case, **composed_with)
    directory = _compose(case)
    hooks = [item["hook"] for item in _show(case, capsys)["composed"]["commands"]]
    _declare(case, **declared)  # not composed again

    block = _show(case, capsys)
    assert block["claudron_session_loop"] is ("claudron" not in hooks)
    assert [item["hook"] for item in block["composed"]["commands"]] == hooks
    assert block["composed"]["matches_declaration"] is False
    assert f"export CLAUDRON_SESSION_LOOP={1 if hooks else 0}\n" in (directory / "bot.conf").read_text()
    (line,) = _text(case, capsys)
    assert line.endswith("; they do not match the declaration.")


@pytest.mark.parametrize("content", [
    "{not json", "[]", b"\xff\xfe", '{"hooks": []}', '{"hooks": {"PreCompact": "x"}}',
    '{"hooks": {"PreCompact": null}}', '{"hooks": {"PreCompact": [{"hooks": ["x"]}]}}',
    '{"hooks": {"PreCompact": [{"hooks": 3}]}}', None,
], ids=["not-json", "a-list", "not-utf8", "hooks-a-list", "event-a-string", "event-null",
        "handler-a-string", "hooks-a-number", "a-directory"])
def test_an_unreadable_settings_file_reads_unreadable_never_an_empty_list(
        staging_case, monkeypatch, capsys, content):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    _declare(case, claudron_vault_path="vault")
    path = _compose(case) / SETTINGS
    path.unlink()
    if content is None:
        path.mkdir()
    elif isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    block = _show(case, capsys)
    assert block["claudron_session_loop"] is True
    assert block["composed"] == {"state": "unreadable"}
    assert _text(case, capsys) == [
        "PreCompact: Claudron session loop on; clauDNA prompt off; composed settings unreadable."]


def test_an_absent_or_hookless_settings_file_reads_as_such(staging_case, monkeypatch, capsys):
    case = staging_case
    monkeypatch.setattr(context, "get_resources", lambda: case.package)
    path = _compose(case) / SETTINGS
    path.unlink()
    assert _show(case, capsys)["composed"] == {"state": "absent"}
    path.write_text("{}")
    assert _show(case, capsys)["composed"] == {"state": "present", "source": str(path), "commands": [],
                                               "matches_declaration": True}


@pytest.mark.parametrize("vault", [None, "vault"])
@pytest.mark.parametrize("loop", [None, True, False])
def test_one_compose_writes_the_line_exactly_where_it_adds_the_engine_hook(tmp_path, vault, loop):
    """`CLAUDRON_SESSION_LOOP=1` in bot.conf exactly where the settings carry the engine's
    PreCompact hook, so the two cannot disagree; a loop with no vault composes neither."""
    bot = BotConfig(bot_id="b1", name="b1", expertise=["eng"],
                    claudron_vault_path=vault, claudron_session_loop=loop)
    fleet = FleetConfig(manager="b1", name="t", service_prefix="p", bots={"b1": bot})
    paths = _paths(tmp_path)
    engine = _engine_commands(compose_settings_local(bot, fleet, paths))
    lines = [line for line in compose_bot_conf(bot, fleet, paths).splitlines()
             if "CLAUDRON_SESSION_LOOP" in line]
    assert len(engine) == (1 if vault is not None and loop is not False else 0)
    assert lines == [f"export CLAUDRON_SESSION_LOOP={len(engine)}"]
