"""Lossless-restart age gate + manager detection (lib-common.sh helpers).

These back Mechanism 2 of the fleet update lifecycle:

- ``iso_to_epoch`` / ``session_md_handoff_epoch`` / ``should_resume_session`` —
  the F6 age gate that decides whether ``start-bot.sh`` resumes from a
  ``session.md`` handoff or clean-starts a stale one. It keys off the doc-level
  ``last_updated`` ISO-8601 UTC frontmatter field (robust to file touches the
  way mtime is not), falling back to mtime only for legacy artifacts.
- ``bot_is_manager`` — the F5 guard ``weekly-worker-restart.sh`` uses to skip
  managers (and ``update-claude-code.sh`` to address failure notifications).
  Managers carry ``MANAGER_TMUX == BOT_ID`` plus an inline comment that
  ``bot_conf_get`` does not strip; getting this wrong bounces a manager.

CI runs pytest only, so these bash helpers are exercised here via subprocess —
without this wrapper the logic would be untested in CI.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_COMMON = REPO_ROOT / "lib" / "lib-common.sh"


def _run(snippet: str, *args: str) -> tuple[str, int]:
    """Source lib-common.sh, run a snippet with positional args $2.., return (stdout, rc)."""
    proc = subprocess.run(
        ["bash", "-c", f'. "$1"; {snippet}', "_", str(LIB_COMMON), *args],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip(), proc.returncode


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_md(tmp_path: Path, name: str, last_updated: str | None) -> Path:
    f = tmp_path / name
    if last_updated is None:
        f.write_text("## State\nbranch: x\n")  # legacy: no frontmatter
    else:
        f.write_text(
            f"---\ncwd: /x\nlast_updated: {last_updated}\nschema_version: 2\n---\n"
        )
    return f


class TestIsoToEpoch:
    def test_known_value(self):
        out, rc = _run('iso_to_epoch "$2"', "2026-05-15T14:30:00Z")
        assert rc == 0
        expected = int(datetime(2026, 5, 15, 14, 30, tzinfo=timezone.utc).timestamp())
        assert out == str(expected)

    def test_empty_is_error(self):
        out, rc = _run('iso_to_epoch "$2"', "")
        assert rc != 0
        assert out == ""


class TestShouldResumeSession:
    """F6 age gate: resume from a fresh checkpoint, clean-start a stale one."""

    def test_fresh_resumes(self, tmp_path):
        f = _session_md(
            tmp_path, "fresh.md", _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        )
        _, rc = _run('should_resume_session "$2" "$3"', str(f), "86400")
        assert rc == 0  # resume

    def test_stale_skips(self, tmp_path):
        f = _session_md(
            tmp_path, "stale.md", _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        )
        _, rc = _run('should_resume_session "$2" "$3"', str(f), "86400")
        assert rc == 1  # skip -> clean start

    def test_ancient_skips(self, tmp_path):
        f = _session_md(tmp_path, "ancient.md", "2020-01-01T00:00:00Z")
        _, rc = _run('should_resume_session "$2" "$3"', str(f), "86400")
        assert rc == 1

    def test_missing_skips(self, tmp_path):
        _, rc = _run(
            'should_resume_session "$2" "$3"', str(tmp_path / "nope.md"), "86400"
        )
        assert rc == 1

    def test_legacy_no_field_uses_mtime(self, tmp_path):
        # A freshly written legacy file (no last_updated) falls back to mtime,
        # which is "now" -> fresh -> resume.
        f = _session_md(tmp_path, "legacy.md", None)
        _, rc = _run('should_resume_session "$2" "$3"', str(f), "86400")
        assert rc == 0


class TestHandoffEpoch:
    def test_prefers_last_updated_over_mtime(self, tmp_path):
        # last_updated is 2020; the file mtime is "now" — the field must win.
        f = _session_md(tmp_path, "s.md", "2020-01-01T00:00:00Z")
        out, rc = _run('session_md_handoff_epoch "$2"', str(f))
        assert rc == 0
        assert out == str(int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()))


class TestBotIsManager:
    """F5 guard: MANAGER_TMUX == BOT_ID marks a manager (never auto-restarted)."""

    def _bot(self, tmp_path, name, conf):
        d = tmp_path / name
        d.mkdir()
        (d / "bot.conf").write_text(conf)
        return d

    def test_manager_with_inline_comment(self, tmp_path):
        d = self._bot(
            tmp_path,
            "ari",
            "export BOT_ID=ari\nexport MANAGER_TMUX=ari  # this bot is a manager\n",
        )
        _, rc = _run('bot_is_manager "$2"', str(d))
        assert rc == 0

    def test_worker(self, tmp_path):
        d = self._bot(
            tmp_path, "astrid", "export BOT_ID=astrid\nexport MANAGER_TMUX=ari\n"
        )
        _, rc = _run('bot_is_manager "$2"', str(d))
        assert rc == 1

    def test_worker_double_quoted(self, tmp_path):
        d = self._bot(tmp_path, "val", 'BOT_ID="valbot"\nMANAGER_TMUX="valmgr"\n')
        _, rc = _run('bot_is_manager "$2"', str(d))
        assert rc == 1


class TestSessionResumeCapability:
    """#1163 — the boot path must not inject a command that cannot resolve.

    ``start-bot.sh`` sent a hardcoded, plugin-qualified resume command on EVERY
    start of EVERY bot. Under ``plugins.include_defaults: false`` — a supported
    configuration — that plugin is never installed, so the keystroke was
    unresolvable on every boot, including on bots that never equipped
    ``restart``. There is no agent reading anything at that instant, so nothing
    can exercise judgement; the command resolves or it does not.

    The asymmetry below is the design: only a POSITIVE finding of absence
    suppresses the send. Injecting a command that does not resolve costs one
    visible wasted keystroke; failing to inject when we should have costs the
    session its context silently, which is the failure this closes.
    """

    def _status(self, cmd: str, bot_dir: str = "", config_dir: str | None = None):
        pre = f'export CLAUDE_CONFIG_DIR="{config_dir}"; ' if config_dir else ""
        return _run(f'{pre}session_command_status "$2" "$3"', cmd, bot_dir)

    def _plugin_home(self, tmp_path: Path, plugin: str | None) -> str:
        home = tmp_path / "cfg"
        (home / "plugins" / "cache" / "SomeMarketplace").mkdir(parents=True)
        if plugin:
            (home / "plugins" / "cache" / "SomeMarketplace" / plugin).mkdir()
        return str(home)

    def test_installed_provider_injects(self, tmp_path):
        cfg = self._plugin_home(tmp_path, "someplugin")
        out, rc = self._status("/someplugin:session resume --auto", config_dir=cfg)
        assert (out, rc) == ("available", 0)

    def test_absent_provider_skips_and_names_it(self, tmp_path):
        # The #1163 configuration: the command's plugin is simply not installed.
        cfg = self._plugin_home(tmp_path, None)
        out, rc = self._status("/someplugin:session resume --auto", config_dir=cfg)
        assert rc == 1, "an unresolvable command must not be injected"
        assert out == "provider-absent:someplugin", "the skip must name the reason"

    def test_empty_command_disables_injection(self, tmp_path):
        cfg = self._plugin_home(tmp_path, "someplugin")
        out, rc = self._status("", config_dir=cfg)
        assert (out, rc) == ("no-command", 1)

    def test_undeterminable_command_still_injects(self, tmp_path):
        # Fail OPEN: a bare (non-plugin-qualified) command cannot be checked
        # against a plugin, and a silent non-injection is the worse error.
        cfg = self._plugin_home(tmp_path, "someplugin")
        out, rc = self._status("/somebareskill --auto", config_dir=cfg)
        assert (out, rc) == ("unverifiable", 0)

    def test_a_bot_equipped_skill_resolves_without_a_plugin(self, tmp_path):
        cfg = self._plugin_home(tmp_path, None)
        bot = tmp_path / "bot"
        (bot / ".claude" / "skills" / "mysession").mkdir(parents=True)
        out, rc = self._status("/mysession", str(bot), config_dir=cfg)
        assert (out, rc) == ("available", 0)

    def test_a_colon_in_a_later_argument_is_not_read_as_a_plugin(self, tmp_path):
        # Only the FIRST token can carry a plugin qualifier. Scanning the whole
        # string would read `note:` here as a plugin and skip a valid command.
        cfg = self._plugin_home(tmp_path, None)
        out, rc = self._status("/somebareskill --note foo:bar", config_dir=cfg)
        assert (out, rc) == ("unverifiable", 0)

    def test_both_session_verbs_route_through_one_predicate(self, tmp_path):
        """resume (boot) and handoff (shutdown) share the helper, not a copy.

        The shutdown call site matters more than the boot one: it runs on the
        systemd ExecStop path, so an unresolvable command there is fired at the
        one moment the handoff is all that stands between a restart and lost
        context. A second mechanism for the same question is how the two drift.
        """
        cfg = self._plugin_home(tmp_path, None)
        for const in ("_SESSION_RESUME_COMMAND_DEFAULT", "_SESSION_HANDOFF_COMMAND_DEFAULT"):
            cmd, rc = _run(f'printf "%s" "${const}"')
            assert rc == 0 and cmd, f"{const} is not defined"
            out, rc = self._status(cmd, config_dir=cfg)
            assert (out, rc) == ("provider-absent:claudna", 1), (
                f"{const} does not gate through the shared predicate"
            )


class TestBareVersusPluginAsymmetryIsDeliberate:
    """The two branches of ``session_command_status`` treat a MISS differently.

    They look inconsistent side by side, and a reviewer nearly filed the bare
    branch as a bug on exactly that reading. It is not a bug, and these tests
    exist so that anyone who reaches the same conclusion is stopped by a failing
    test rather than by whether they happened to read a comment.

    ============================================================
      command shape        skill/plugin missing   ->  behaviour
    ------------------------------------------------------------
      /plugin:verb         plugin not in cache    ->  SUPPRESS (rc 1)
      /bareword            no file under skills/  ->  INJECT   (rc 0)
    ============================================================

    The difference is *what a negative means*, not a lapse in consistency:

    - ``/plugin:verb`` names a plugin. A plugin absent from the cache is a
      POSITIVE finding of absence — we looked in the one place it could be and
      it was not there.
    - ``/bareword`` names no plugin, and the bare namespace includes Claude
      Code's own NATIVE commands (``/compact``, ``/clear`` and friends). Those
      have no filesystem representation under ``.claude/skills`` at ALL, so a
      miss is AMBIGUOUS — it cannot distinguish "no such command" from "a
      native command, which never has a file". Suppressing on it would silently
      refuse to send every native command a fleet ever configured.

    Hence the lookup is still worth doing (a hit is a real positive and upgrades
    ``unverifiable`` to ``available``) while a miss falls back to the helper's
    fail-open rule: only a positive finding of absence suppresses a send.
    """

    def _status(self, cmd: str, bot_dir: str = "", config_dir: str | None = None):
        pre = f'export CLAUDE_CONFIG_DIR="{config_dir}"; ' if config_dir else ""
        return _run(f'{pre}session_command_status "$2" "$3"', cmd, bot_dir)

    def _empty_plugin_cache(self, tmp_path: Path) -> str:
        home = tmp_path / "cfg"
        (home / "plugins" / "cache" / "SomeMarketplace").mkdir(parents=True)
        return str(home)

    # -- the protected branch -------------------------------------------------

    def test_bare_command_missing_from_skills_dir_still_injects(self, tmp_path):
        """A bare command whose name is absent under ``.claude/skills`` INJECTS.

        This is the case the guarding comment protects and the one no other test
        reaches: a bot_dir IS supplied (so the lookup actually runs) and the
        skill is NOT there (so the negative is available to act on). The helper
        deliberately does not act on it.
        """
        cfg = self._empty_plugin_cache(tmp_path)
        bot = tmp_path / "bot"
        (bot / ".claude" / "skills").mkdir(parents=True)  # exists, but empty

        out, rc = self._status("/compact", str(bot), config_dir=cfg)

        assert (out, rc) == ("unverifiable", 0), (
            "\n"
            "A bare /command that is ABSENT from .claude/skills must still be injected\n"
            "(status 'unverifiable', rc 0). This test got "
            f"{(out, rc)!r} instead.\n"
            "\n"
            "If you just made this branch suppress on a miss so it matches the\n"
            "plugin-qualified branch above it: that symmetry is the bug, not the fix.\n"
            "\n"
            "  A bare /word does NOT name a plugin, and the bare namespace includes\n"
            "  Claude Code's own NATIVE commands -- /compact, /clear and friends --\n"
            "  which have NO filesystem representation under .claude/skills at all.\n"
            "  So a miss there cannot tell 'no such command' apart from 'a native\n"
            "  command, which never has a file'. It is an AMBIGUOUS negative, not a\n"
            "  finding of absence.\n"
            "\n"
            "  Suppressing on it would silently stop sending every native command a\n"
            "  fleet ever configured -- and silence is exactly the failure mode\n"
            "  #1163 was opened to close. Only a POSITIVE finding of absence may\n"
            "  suppress a send; 'I could not tell' sends and says so.\n"
            "\n"
            "The plugin branch may suppress because a plugin missing from the cache\n"
            "IS a positive finding. See the contrasting test in this class.\n"
        )

    def test_the_lookup_is_still_worth_doing_a_hit_upgrades_the_status(self, tmp_path):
        """The miss is ignored, but a HIT is not — otherwise drop the lookup.

        This is what keeps the branch from collapsing into an unconditional
        'unverifiable': a present skill is a real positive and must report
        'available'. If someone deletes the lookup as dead code because the miss
        is ignored, this fails.
        """
        cfg = self._empty_plugin_cache(tmp_path)
        bot = tmp_path / "bot"
        (bot / ".claude" / "skills" / "mysession").mkdir(parents=True)

        out, rc = self._status("/mysession", str(bot), config_dir=cfg)

        assert (out, rc) == ("available", 0), (
            "A bare /command that IS present under .claude/skills must report "
            "'available', not 'unverifiable'. The skills lookup on this branch is "
            "not dead code: the miss is ignored, but the HIT is load-bearing. "
            "Removing the lookup would lose a real positive."
        )

    # -- the contrasting branch, so the asymmetry reads as a pair -------------

    def test_plugin_command_missing_from_cache_suppresses(self, tmp_path):
        """The SAME shape of miss on the plugin branch DOES suppress.

        Deliberately adjacent to the bare-command test above: the two are only
        coherent as a pair. Read alone, either one looks like the other's bug.
        """
        cfg = self._empty_plugin_cache(tmp_path)
        bot = tmp_path / "bot"
        (bot / ".claude" / "skills").mkdir(parents=True)

        out, rc = self._status("/someplugin:session resume", str(bot), config_dir=cfg)

        assert (out, rc) == ("provider-absent:someplugin", 1), (
            "\n"
            "A plugin-qualified command whose plugin is NOT in the cache must be\n"
            f"suppressed (rc 1) and must name the plugin. Got {(out, rc)!r}.\n"
            "\n"
            "If you just made this branch fail open so it matches the bare-command\n"
            "branch: that symmetry is also wrong, in the opposite direction.\n"
            "\n"
            "  A plugin-qualified command NAMES the plugin it needs. The cache is\n"
            "  the one place that plugin could be, so absent-from-cache is a\n"
            "  POSITIVE finding of absence -- unlike a bare /word, where a miss is\n"
            "  ambiguous because native commands have no files at all.\n"
            "\n"
            "  Injecting here would fire a keystroke that provably cannot resolve,\n"
            "  on every boot of every bot, under a SUPPORTED configuration\n"
            "  (plugins.include_defaults: false). That is #1163.\n"
        )

    def test_the_two_branches_disagree_on_the_same_input_shape(self, tmp_path):
        """Pin the asymmetry itself, so symmetrizing EITHER side fails here too.

        The per-branch tests above can each be made to pass by 'fixing' the
        other branch. This one cannot: it asserts the two branches return
        DIFFERENT rcs for the same miss, which is the actual invariant.
        """
        cfg = self._empty_plugin_cache(tmp_path)
        bot = tmp_path / "bot"
        (bot / ".claude" / "skills").mkdir(parents=True)

        _, bare_rc = self._status("/notinstalled", str(bot), config_dir=cfg)
        _, plugin_rc = self._status("/notinstalled:verb", str(bot), config_dir=cfg)

        assert (bare_rc, plugin_rc) == (0, 1), (
            "\n"
            "The two branches of session_command_status MUST disagree here, and "
            f"got bare={bare_rc} plugin={plugin_rc}.\n"
            "\n"
            "Same shape of input -- a name that resolves to nothing on disk -- and\n"
            "the correct answers are opposite:\n"
            "\n"
            "  bare   /notinstalled       -> rc 0, INJECT   (ambiguous negative:\n"
            "                                native commands have no files)\n"
            "  plugin /notinstalled:verb  -> rc 1, SUPPRESS (positive finding:\n"
            "                                the cache is the only place it lives)\n"
            "\n"
            "If both are 0 you removed a real guard and re-opened #1163. If both\n"
            "are 1 you will silently stop sending native commands. The asymmetry\n"
            "is the design.\n"
        )
