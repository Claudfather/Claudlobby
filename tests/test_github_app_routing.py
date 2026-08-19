"""App-auth P3 (#1273): github_app config parsing + composed gitconfig routing.

Compose-text pins in the house pin-per-property style (a reorder breaks
routing SILENTLY — git takes the first helper that answers), plus behavioral
runs through REAL `git credential fill` with an on-disk counting stub helper
(no crypto, no network: the stub stands where `lib/git-credential-github-app`
is baked, exactly as a fleet checkout would place it).
"""

import subprocess
from pathlib import Path

import pytest

import claudlobby.composer as comp
from claudlobby.config import (
    BotConfig,
    FleetConfig,
    GithubAppConfig,
    _merge_github_app,
    _parse_github_app,
)
from claudlobby.paths import Paths

from tests.conftest import _write_exec


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


def _app_bot(creds=None, **app_kwargs):
    return BotConfig(
        bot_id="worker",
        name="worker",
        expertise=["eng"],
        git_credentials=creds or {},
        github_app=GithubAppConfig(**app_kwargs),
    )


class TestGithubAppParsing:
    def test_bot_tier_merges_over_fleet_defaults_per_field(self):
        merged = _merge_github_app(
            {"slug": "fleet-app", "bot_user_id": 11, "orgs": ["OrgA"]},
            {"bot_user_id": 22},
            bot_name="w",
        )
        assert (merged.slug, merged.bot_user_id, merged.orgs) == (
            "fleet-app",
            22,
            ["OrgA"],
        )

    def test_enabled_false_at_bot_tier_disables_a_fleet_default(self):
        assert (
            _merge_github_app({"slug": "fleet-app"}, {"enabled": False}, bot_name="w")
            is None
        )

    def test_enabled_requires_a_real_yaml_boolean(self):
        with pytest.raises(ValueError, match="arming knob"):
            _parse_github_app({"enabled": "tue"}, where="bot 'w'")

    def test_bot_user_id_must_be_a_positive_int(self):
        with pytest.raises(ValueError, match="BOT USER id"):
            _parse_github_app({"bot_user_id": True}, where="bot 'w'")
        with pytest.raises(ValueError, match="BOT USER id"):
            _parse_github_app({"bot_user_id": "123"}, where="bot 'w'")

    def test_org_with_slash_is_rejected(self):
        with pytest.raises(ValueError, match="org-scoped"):
            _parse_github_app({"orgs": ["Org/repo"]}, where="fleet defaults")

    def test_unknown_key_and_bad_slug_are_rejected(self):
        with pytest.raises(ValueError, match="unknown key"):
            _parse_github_app({"slugg": "x"}, where="bot 'w'")
        with pytest.raises(ValueError, match="URL slug"):
            _parse_github_app({"slug": "My App"}, where="bot 'w'")

    def test_absent_is_none_not_an_error(self):
        assert _merge_github_app(None, None, bot_name="w") is None


class TestGithubAppComposedText:
    def _render(self, tmp_path, monkeypatch, bot):
        monkeypatch.setattr(comp, "_resolve_gh_executable", lambda: "/usr/bin/gh")
        monkeypatch.setattr(
            comp, "_operator_gitconfig", lambda: tmp_path / "user.gitconfig"
        )
        return comp.compose_bot_gitconfig(bot, _make_paths(tmp_path))

    def test_pat_org_section_precedes_the_app_block(self, tmp_path, monkeypatch):
        out = self._render(
            tmp_path, monkeypatch, _app_bot(creds={"OrgA": "ORG_A_PAT"})
        )
        assert out.index('github.com/OrgA"') < out.index("git-credential-github-app")

    def test_cache_layer_precedes_the_app_helper(self, tmp_path, monkeypatch):
        out = self._render(tmp_path, monkeypatch, _app_bot())
        assert out.index("cache --timeout=3000") < out.index(
            "git-credential-github-app"
        )

    def test_app_helper_is_a_baked_absolute_path(self, tmp_path, monkeypatch):
        out = self._render(tmp_path, monkeypatch, _app_bot())
        assert f"{tmp_path}/lib/git-credential-github-app" in out
        assert "$CLAUDLOBBY_ROOT" not in out, "gitconfig files do not expand env vars"

    def test_app_block_scoped_per_org_when_orgs_declared(self, tmp_path, monkeypatch):
        out = self._render(tmp_path, monkeypatch, _app_bot(orgs=["OrgB", "OrgA"]))
        assert '[credential "https://github.com/OrgA"]' in out
        assert '[credential "https://github.com/OrgB"]' in out
        # No host-generic App section when scoped.
        assert out.count("git-credential-github-app") == 2

    def test_host_generic_app_suppresses_the_gh_fallback(self, tmp_path, monkeypatch):
        out = self._render(tmp_path, monkeypatch, _app_bot())
        assert "auth git-credential" not in out, (
            "for a host-generic App, gh could only answer after an App-helper "
            "failure — a silent operator-identity substitution"
        )

    def test_org_scoped_app_keeps_the_gh_fallback(self, tmp_path, monkeypatch):
        out = self._render(tmp_path, monkeypatch, _app_bot(orgs=["OrgA"]))
        assert "auth git-credential" in out

    def test_app_identity_composes_iff_both_fields(self, tmp_path, monkeypatch):
        both = self._render(
            tmp_path, monkeypatch, _app_bot(slug="my-app", bot_user_id=42)
        )
        assert "name = my-app[bot]" in both
        assert "email = 42+my-app[bot]@users.noreply.github.com" in both
        # AFTER the include, so these keys win (later-wins).
        assert both.index("[include]") < both.index("[user]")
        for partial in (dict(slug="my-app"), dict(bot_user_id=42)):
            out = self._render(tmp_path, monkeypatch, _app_bot(**partial))
            assert "[user]" not in out, partial

    def test_ssh_rewrite_scoping(self, tmp_path, monkeypatch):
        scoped = self._render(tmp_path, monkeypatch, _app_bot(orgs=["OrgA"]))
        assert "insteadOf = git@github.com:OrgA/" in scoped
        assert "insteadOf = git@github.com:\n" not in scoped
        generic = self._render(tmp_path, monkeypatch, _app_bot())
        assert "insteadOf = git@github.com:\n" in generic

    def test_bot_conf_gate_and_path_prepend(self, tmp_path, monkeypatch):
        from tests.conftest import install_real_template

        install_real_template(tmp_path)
        bot = _app_bot()
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        conf = comp.compose_bot_conf(bot, fleet, _make_paths(tmp_path))
        assert "GIT_CONFIG_GLOBAL" in conf
        assert 'export PATH="$BOT_DIR/tools:$PATH"' in conf
        bare = BotConfig(bot_id="b", name="b", expertise=["eng"])
        conf2 = comp.compose_bot_conf(bare, fleet, _make_paths(tmp_path))
        assert "GIT_CONFIG_GLOBAL" not in conf2
        assert "$BOT_DIR/tools:$PATH" not in conf2

    def test_non_declaring_bot_composes_nothing_new(self, tmp_path, monkeypatch):
        # The dormancy pin: without github_app, the composed gitconfig for a
        # git_credentials bot carries not one App-flavored byte.
        out = self._render(tmp_path, monkeypatch,
            BotConfig(
                bot_id="w",
                name="w",
                expertise=["eng"],
                git_credentials={"OrgA": "ORG_A_PAT"},
            ),
        )
        for token in ("github-app", "cache --timeout", "insteadOf", "[user]"):
            assert token not in out, token
        bare = BotConfig(bot_id="b", name="b", expertise=["eng"])
        assert comp.compose_bot_gitconfig(bare, _make_paths(tmp_path)) is None


class TestGhShim:
    def test_shim_composed_for_app_bots_and_absent_otherwise(self, tmp_path):
        bot = _app_bot()
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        outputs = comp.compose_tool_outputs(bot, fleet, _make_paths(tmp_path), tmp_path)
        assert "gh" in outputs
        assert "mint-github-token.sh" in outputs["gh"]
        assert "exec" in outputs["gh"]
        bare = BotConfig(bot_id="b", name="b", expertise=["eng"])
        assert comp.compose_tool_outputs(bare, fleet, _make_paths(tmp_path), tmp_path) == {}

    def test_shim_mints_and_execs_the_real_gh(self, tmp_path):
        # Behavioral: run the composed shim with a stub mint CLI (via
        # CLAUDLOBBY_ROOT) and a fake real-gh further down PATH.
        bot = _app_bot()
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        shim_dir = tmp_path / "tools"
        shim_dir.mkdir()
        outputs = comp.compose_tool_outputs(bot, fleet, _make_paths(tmp_path), tmp_path)
        _write_exec(shim_dir / "gh", outputs["gh"])
        (tmp_path / "lib").mkdir()
        _write_exec(
            tmp_path / "lib" / "mint-github-token.sh",
            "#!/bin/bash\nprintf 'ghs_SHIMMINT'\n",
        )
        real_dir = tmp_path / "real-bin"
        real_dir.mkdir()
        _write_exec(
            real_dir / "gh",
            '#!/bin/bash\nprintf "%s|%s" "$GH_TOKEN" "$*"\n',
        )
        import os

        r = subprocess.run(
            [str(shim_dir / "gh"), "pr", "list"],
            env={
                "PATH": f"{shim_dir}:{real_dir}:{os.environ['PATH']}",
                "CLAUDLOBBY_ROOT": str(tmp_path),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout == "ghs_SHIMMINT|pr list"

    def test_shim_is_loud_when_mint_fails(self, tmp_path):
        bot = _app_bot()
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        shim_dir = tmp_path / "tools"
        shim_dir.mkdir()
        outputs = comp.compose_tool_outputs(bot, fleet, _make_paths(tmp_path), tmp_path)
        _write_exec(shim_dir / "gh", outputs["gh"])
        (tmp_path / "lib").mkdir()
        _write_exec(tmp_path / "lib" / "mint-github-token.sh", "#!/bin/bash\nexit 1\n")
        import os

        r = subprocess.run(
            [str(shim_dir / "gh"), "pr", "list"],
            env={
                "PATH": f"{shim_dir}:{os.environ['PATH']}",
                "CLAUDLOBBY_ROOT": str(tmp_path),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 1
        assert "refusing to fall through" in r.stderr


class TestGithubAppRoutingResolvesForReal:
    """Real `git credential fill` against the composed file; the App helper is
    an on-disk COUNTING stub standing exactly where the composer bakes it."""

    def _composed(self, tmp_path, monkeypatch, bot, app_body=None):
        lib = tmp_path / "lib"
        lib.mkdir(exist_ok=True)
        _write_exec(
            lib / "git-credential-github-app",
            app_body
            or (
                "#!/bin/bash\n"
                'printf "x" >> "$APP_STUB_LOG"\n'
                "printf 'username=x-access-token\\npassword=ghs_APPSTUB\\n'\n"
            ),
        )
        gh_stub = tmp_path / "gh-stub"
        _write_exec(
            gh_stub,
            "#!/bin/bash\n"
            '[ "$1" = "auth" ] || exit 0\n'
            'printf "x" >> "$GH_STUB_LOG"\n'
            "printf 'username=u\\npassword=HOST_DEFAULT\\n'\n",
        )
        user_cfg = tmp_path / "user.gitconfig"
        user_cfg.write_text("[user]\n\temail = operator@example.com\n")
        monkeypatch.setattr(comp, "_resolve_gh_executable", lambda: str(gh_stub))
        monkeypatch.setattr(comp, "_operator_gitconfig", lambda: user_cfg)
        cfg = tmp_path / "composed.gitconfig"
        cfg.write_text(comp.compose_bot_gitconfig(bot, _make_paths(tmp_path)))
        return cfg

    def _env(self, tmp_path, cfg, extra=None):
        import os

        return {
            **os.environ,
            **(extra or {}),
            "GIT_CONFIG_GLOBAL": str(cfg),
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "APP_STUB_LOG": str(tmp_path / "app-calls.log"),
            "GH_STUB_LOG": str(tmp_path / "gh-calls.log"),
        }

    def _fill(self, tmp_path, cfg, path, extra=None):
        return subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost=github.com\npath={path}\n\n",
            env=self._env(tmp_path, cfg, extra),
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _password(r):
        for line in r.stdout.splitlines():
            if line.startswith("password="):
                return line.removeprefix("password=")
        return None

    def test_pat_org_wins_and_the_app_helper_is_never_consulted(
        self, tmp_path, monkeypatch
    ):
        cfg = self._composed(
            tmp_path,
            monkeypatch,
            _app_bot(creds={"OrgA": "ORG_A_PAT"}, orgs=["OrgA", "OrgB"]),
        )
        r = self._fill(tmp_path, cfg, "OrgA/repo.git", {"ORG_A_PAT": "PAT_SENTINEL"})
        assert self._password(r) == "PAT_SENTINEL"
        assert not (tmp_path / "app-calls.log").exists(), (
            "PAT-beats-App is by ORDER: the App helper must not even run"
        )

    def test_app_org_resolves_via_the_app_helper(self, tmp_path, monkeypatch):
        cfg = self._composed(
            tmp_path, monkeypatch, _app_bot(creds={"OrgA": "ORG_A_PAT"}, orgs=["OrgB"])
        )
        r = self._fill(tmp_path, cfg, "OrgB/repo.git", {"ORG_A_PAT": "PAT_SENTINEL"})
        assert self._password(r) == "ghs_APPSTUB"

    def test_host_generic_app_serves_everything(self, tmp_path, monkeypatch):
        cfg = self._composed(tmp_path, monkeypatch, _app_bot())
        r = self._fill(tmp_path, cfg, "AnyOrg/repo.git")
        assert self._password(r) == "ghs_APPSTUB"
        assert not (tmp_path / "gh-calls.log").exists()

    def test_app_identity_wins_over_the_operator_include(self, tmp_path, monkeypatch):
        cfg = self._composed(
            tmp_path, monkeypatch, _app_bot(slug="my-app", bot_user_id=42)
        )
        r = subprocess.run(
            ["git", "config", "--get", "user.email"],
            env=self._env(tmp_path, cfg),
            capture_output=True,
            text=True,
        )
        assert r.stdout.strip() == "42+my-app[bot]@users.noreply.github.com"

    def test_identity_less_app_keeps_the_operator_identity(self, tmp_path, monkeypatch):
        cfg = self._composed(tmp_path, monkeypatch, _app_bot())
        r = subprocess.run(
            ["git", "config", "--get", "user.email"],
            env=self._env(tmp_path, cfg),
            capture_output=True,
            text=True,
        )
        assert r.stdout.strip() == "operator@example.com"

    def test_insteadof_rewrites_ssh_remotes(self, tmp_path, monkeypatch):
        cfg = self._composed(tmp_path, monkeypatch, _app_bot(orgs=["OrgB"]))
        r = subprocess.run(
            ["git", "ls-remote", "--get-url", "git@github.com:OrgB/x.git"],
            env=self._env(tmp_path, cfg),
            capture_output=True,
            text=True,
        )
        assert r.stdout.strip() == "https://github.com/OrgB/x.git"

    def test_failing_app_helper_stops_the_chain_loudly(self, tmp_path, monkeypatch):
        # The B2/D11 pin at the composed layer: quit=1 from the helper must
        # stop git dead — never a fall-through to the gh fallback.
        cfg = self._composed(
            tmp_path,
            monkeypatch,
            _app_bot(orgs=["OrgB"]),
            app_body=(
                "#!/bin/bash\n"
                'printf "x" >> "$APP_STUB_LOG"\n'
                "printf 'quit=1\\n'\n"
                "exit 1\n"
            ),
        )
        r = self._fill(tmp_path, cfg, "OrgB/repo.git")
        assert r.returncode != 0
        assert self._password(r) is None
        assert not (tmp_path / "gh-calls.log").exists(), (
            "git consulted the gh fallback after quit=1 — the silent "
            "operator-identity substitution D11 exists to stop"
        )
