"""Tests for claudlobby/newbot.py — stanza rendering, fleet editing, voice/env helpers."""

from __future__ import annotations

from textwrap import dedent

from claudlobby.newbot import (
    NewBotInputs,
    _yaml_list,
    _yaml_str,
    render_stanza,
    _bot_name_from_stanza,
    insert_bot_stanza,
    _add_to_team,
    maybe_create_voice,
    interactive_collect,
    write_token_to_env,
)
from claudlobby.paths import Paths


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


class TestYamlHelpers:
    def test_yaml_list_simple(self):
        assert _yaml_list(["a", "b", "c"]) == "[a, b, c]"

    def test_yaml_list_single(self):
        assert _yaml_list(["only"]) == "[only]"

    def test_yaml_str_plain(self):
        assert _yaml_str("hello") == "hello"

    def test_yaml_str_colon(self):
        assert _yaml_str("key: value") == '"key: value"'

    def test_yaml_str_special_chars(self):
        for ch in ":#&*!|>'%@`,{}[]":
            result = _yaml_str(f"has{ch}char")
            assert result.startswith('"') and result.endswith('"')

    def test_yaml_str_escapes_backslash_and_quote(self):
        assert _yaml_str('say "hi"!') == '"say \\"hi\\"!"'


# ---------------------------------------------------------------------------
# render_stanza
# ---------------------------------------------------------------------------


class TestRenderStanza:
    def test_minimal(self):
        inp = NewBotInputs(name="bot-1", expertise=["software-engineering"])
        stanza = render_stanza(inp)
        assert "    bot-1:" in stanza
        assert "expertise: [software-engineering]" in stanza

    def test_with_voice_and_model(self):
        inp = NewBotInputs(
            name="eng",
            expertise=["frontend-design"],
            voice="voices/jian-yang.md",
            model="opus",
        )
        stanza = render_stanza(inp)
        assert "voice: voices/jian-yang.md" in stanza
        assert "model: opus" in stanza

    def test_default_account_omitted(self):
        inp = NewBotInputs(name="x", expertise=["a"], account="default")
        assert "account:" not in render_stanza(inp)

    def test_non_default_account_included(self):
        inp = NewBotInputs(name="x", expertise=["a"], account="work")
        assert "account: work" in render_stanza(inp)

    def test_scope_block(self):
        inp = NewBotInputs(
            name="x",
            expertise=["a"],
            scope_org="myorg",
            scope_repos=["repo-a", "repo-b"],
        )
        stanza = render_stanza(inp)
        assert "scope:" in stanza
        assert "org: myorg" in stanza
        assert "repos: [repo-a, repo-b]" in stanza

    def test_telegram_block(self):
        inp = NewBotInputs(
            name="x",
            expertise=["a"],
            telegram_handle="x_bot",
            token_env="TELEGRAM_TOKEN_X",
            require_mention=True,
        )
        stanza = render_stanza(inp)
        assert "telegram:" in stanza
        assert "handle: x_bot" in stanza
        assert "token_env: TELEGRAM_TOKEN_X" in stanza
        assert "require_mention: true" in stanza

    def test_remote_control_false_rendered(self):
        inp = NewBotInputs(name="x", expertise=["a"], remote_control=False)
        assert "remote_control: false" in render_stanza(inp)

    def test_remote_control_none_omitted(self):
        inp = NewBotInputs(name="x", expertise=["a"], remote_control=None)
        assert "remote_control" not in render_stanza(inp)

    def test_dangerously_skip_permissions_true_rendered(self):
        inp = NewBotInputs(name="x", expertise=["a"], dangerously_skip_permissions=True)
        assert "dangerously_skip_permissions: true" in render_stanza(inp)

    def test_dangerously_skip_permissions_none_omitted(self):
        inp = NewBotInputs(name="x", expertise=["a"], dangerously_skip_permissions=None)
        assert "dangerously_skip_permissions" not in render_stanza(inp)

    def test_dangerously_skip_permissions_false_also_omitted(self):
        """False composes to the same safe default as an omitted field, so it
        emits no explicit line — only an explicit opt-in (True) renders."""
        inp = NewBotInputs(
            name="x", expertise=["a"], dangerously_skip_permissions=False
        )
        assert "dangerously_skip_permissions" not in render_stanza(inp)

    def test_list_fields(self):
        inp = NewBotInputs(
            name="x",
            expertise=["a"],
            skills=["dispatch", "prs"],
            guardrails=["no-push-main"],
        )
        stanza = render_stanza(inp)
        assert "skills: [dispatch, prs]" in stanza
        assert "guardrails: [no-push-main]" in stanza


# ---------------------------------------------------------------------------
# bot name extraction
# ---------------------------------------------------------------------------


class TestBotNameFromStanza:
    def test_simple(self):
        stanza = "    eng-1:\n      expertise: [a]\n"
        assert _bot_name_from_stanza(stanza) == "eng-1"

    def test_underscore(self):
        stanza = "    my_bot:\n      expertise: [a]\n"
        assert _bot_name_from_stanza(stanza) == "my_bot"


# ---------------------------------------------------------------------------
# insert_bot_stanza
# ---------------------------------------------------------------------------


FLEET_WITH_BOTS = dedent("""\
    fleet:
      name: test
      service_prefix: com.test

      bots:
        existing:
          expertise: [orchestration]
""")


class TestInsertBotStanza:
    def test_appends_to_bots_block(self, tmp_path):
        fleet_yaml = tmp_path / "fleet.yaml"
        fleet_yaml.write_text(FLEET_WITH_BOTS)
        stanza = "    new-bot:\n      expertise: [software-engineering]\n"
        result = insert_bot_stanza(fleet_yaml, stanza)
        assert "existing:" in result
        assert "new-bot:" in result
        # new bot should come after existing
        assert result.index("existing:") < result.index("new-bot:")

    def test_preserves_comments(self, tmp_path):
        text = dedent("""\
            fleet:
              name: test
              # important comment
              bots:
                existing:
                  expertise: [a]
        """)
        fleet_yaml = tmp_path / "fleet.yaml"
        fleet_yaml.write_text(text)
        stanza = "    added:\n      expertise: [b]\n"
        result = insert_bot_stanza(fleet_yaml, stanza)
        assert "# important comment" in result

    def test_with_team_registration(self, tmp_path):
        text = dedent("""\
            fleet:
              name: test
              teams:
                eng:
                  manager: lead
                  workers: [worker-1]
              bots:
                lead:
                  expertise: [orchestration]
                worker-1:
                  expertise: [software-engineering]
        """)
        fleet_yaml = tmp_path / "fleet.yaml"
        fleet_yaml.write_text(text)
        stanza = "    worker-2:\n      expertise: [software-engineering]\n"
        result = insert_bot_stanza(fleet_yaml, stanza, team="eng")
        assert "worker-2" in result
        assert "workers: [worker-1, worker-2]" in result


# ---------------------------------------------------------------------------
# _add_to_team
# ---------------------------------------------------------------------------


class TestAddToTeam:
    def test_appends_worker(self):
        text = dedent("""\
            fleet:
              teams:
                eng:
                  manager: lead
                  workers: [a, b]
              bots:
                lead:
                  expertise: [x]
        """)
        result = _add_to_team(text, "eng", "c")
        assert "workers: [a, b, c]" in result

    def test_no_duplicate(self):
        text = dedent("""\
            fleet:
              teams:
                eng:
                  manager: lead
                  workers: [a, b]
              bots:
                x:
                  expertise: [y]
        """)
        result = _add_to_team(text, "eng", "a")
        assert result.count("workers:") == 1
        # 'a' should appear only once in the workers list
        import re

        m = re.search(r"workers: \[([^\]]+)\]", result)
        workers = [w.strip() for w in m.group(1).split(",")]
        assert workers.count("a") == 1


# ---------------------------------------------------------------------------
# Voice creation
# ---------------------------------------------------------------------------


class TestMaybeCreateVoice:
    def test_explicit_path_passthrough(self, tmp_path):
        paths = Paths(root=tmp_path)
        result = maybe_create_voice(paths, "bot", "voices/custom.md", None)
        assert result == "voices/custom.md"

    def test_inline_text_creates_file(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "voices").mkdir()
        paths = Paths(root=root)
        result = maybe_create_voice(paths, "jian", None, "Terse and blunt.")
        assert result == "voices/jian.md"
        voice_file = root / "voices" / "jian.md"
        assert voice_file.is_file()
        content = voice_file.read_text()
        assert "name: Jian" in content
        assert "Terse and blunt." in content

    def test_none_when_no_voice(self, tmp_path):
        paths = Paths(root=tmp_path)
        assert maybe_create_voice(paths, "bot", None, None) is None


def test_interactive_collect_lists_public_base_expertise_only(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "repo"
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "base-engineering.md").write_text("base")
    (root / "local" / "fleet-a" / "library" / "expertise").mkdir(parents=True)
    (
        root / "local" / "fleet-a" / "library" / "expertise" / "overlay-only.md"
    ).write_text("overlay")
    paths = Paths(root=root, fleet_dir=root / "local" / "fleet-a")
    # Same wizard answer sequence as the opt-in test — one source of truth for the
    # positional prompt order (blank at idx 14 = default the dangerous prompt).
    answers = iter(_wizard_answers(""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    result = interactive_collect(paths)

    output = capsys.readouterr().out
    assert "base-engineering" in output
    assert "overlay-only" not in output
    assert result.expertise == ["base-engineering"]
    # Opt-in: a blank answer to the --dangerously-skip-permissions prompt stays
    # SAFE — the field is omitted, composing to the acceptEdits default.
    assert result.dangerously_skip_permissions is None


def _wizard_answers(dangerous: str) -> list[str]:
    """interactive_collect's answer sequence; index 14 is the
    --dangerously-skip-permissions prompt (parameterized here)."""
    return [
        "bot-a",
        "1",
        "3",
        "",
        "",
        "",
        "",
        "none",
        "none",
        "none",
        "none",
        "none",
        "none",
        "",  # remote_control (blank = default)
        dangerous,  # --dangerously-skip-permissions?
        "",
        "",
        "",
        "",
        "",
        "",
        "n",
        "",
    ]


def test_interactive_collect_dangerous_is_explicit_opt_in(tmp_path, monkeypatch):
    """The wizard defaults to SAFE: only an explicit 'y' sets the flag, which then
    renders into the stanza. Previously a 'yes' emitted nothing and the bot
    silently got acceptEdits — the UX lied."""
    root = tmp_path / "repo"
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "base-engineering.md").write_text("base")
    paths = Paths(root=root)

    answers = iter(_wizard_answers("y"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    result = interactive_collect(paths)
    assert result.dangerously_skip_permissions is True
    assert "dangerously_skip_permissions: true" in render_stanza(result)


def test_cli_dangerous_is_opt_in_and_old_flag_removed(tmp_path, capsys):
    """End-to-end non-interactive CLI: --dangerously-skip-permissions is a positive
    opt-in that renders the flag; omitted, the stanza omits it (safe acceptEdits).
    The old --no- opt-out of the removed dangerous default no longer parses."""
    import pytest

    from claudlobby.__main__ import main

    base = [
        "--root",
        str(tmp_path),
        "new-bot",
        "--name",
        "x",
        "--expertise",
        "software-engineering",
        "--dry-run",
        "--yes",
    ]

    assert main(base + ["--dangerously-skip-permissions"]) == 0
    assert "dangerously_skip_permissions: true" in capsys.readouterr().out

    assert main(base) == 0
    assert "dangerously_skip_permissions" not in capsys.readouterr().out

    with pytest.raises(SystemExit):  # cut clean: the old opt-out is gone
        main(base + ["--no-dangerously-skip-permissions"])


# ---------------------------------------------------------------------------
# .env token management
# ---------------------------------------------------------------------------


class TestWriteTokenToEnv:
    def test_creates_new_env(self, tmp_path):
        env = tmp_path / ".env"
        write_token_to_env(env, "MY_TOKEN", "secret123")
        assert env.is_file()
        assert "MY_TOKEN=secret123" in env.read_text()
        assert (env.stat().st_mode & 0o777) == 0o600

    def test_appends_to_existing(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("EXISTING=value\n")
        write_token_to_env(env, "NEW_TOKEN", "abc")
        text = env.read_text()
        assert "EXISTING=value" in text
        assert "NEW_TOKEN=abc" in text

    def test_updates_existing_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("MY_TOKEN=old\nOTHER=keep\n")
        write_token_to_env(env, "MY_TOKEN", "new")
        text = env.read_text()
        assert "MY_TOKEN=new" in text
        assert "MY_TOKEN=old" not in text
        assert "OTHER=keep" in text
