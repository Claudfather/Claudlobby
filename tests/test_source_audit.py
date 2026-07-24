"""Tests for the L1 source-side deny-by-default guard (#702) in path_audit.py.

The L1 guard classifies *source* values (fleet.yaml, library / MCP fragments)
before any output is written: an absolute path in a compose source is denied
unless it is expressed against a composer anchor (FLEET_ROOT / BOT_DIR /
CLAUDLOBBY_ROOT) or blessed by an ``external_paths`` declaration. This is the
deny-by-default complement to #690's L2 emitted-path predicate, whose tests live
in test_path_audit.py and stay untouched.

Every row of the plan's grammar truth-table is a test here. The grammar is
head-anchored per value (rules 1-5): it classifies what a *single word* starts
with — it never substring-scans free text. Only the three word-split fields
(rule 6) split on whitespace first, then classify each token by 1-5.
"""

from __future__ import annotations

import pytest

from claudlobby.config import (
    AutonomousRunnerConfig,
    BotConfig,
    FleetConfig,
    TelegramConfig,
)
from claudlobby.path_audit import (
    ExternalDecl,
    SourceFinding,
    assert_bot_sources,
    audit_bot_sources,
    classify_grant_paths,
    classify_source_value,
    denied_source_paths,
    is_anchor_headed,
    match_external,
    parse_external_decls,
)


class TestClassifyBaseRows:
    """Base truth-table rows — one value, classified by rules 1-5."""

    def test_foreign_absolute_is_denied(self):
        # rule 3: absolute head → path-classified → deny
        assert classify_source_value("/Users/x/random.json") == ["/Users/x/random.json"]

    def test_fleet_root_anchor_passes(self):
        # rule 2: ${ANCHOR} head → pass
        assert classify_source_value("${FLEET_ROOT}/mcp/x.py") == []

    def test_home_tilde_is_denied(self):
        # rule 3: ~ head → path-classified → deny
        assert classify_source_value("~/creds.json") == ["~/creds.json"]

    def test_flag_form_absolute_rhs_is_denied(self):
        # rule 4: --flag=rhs → classify rhs (absolute) by 1-3 → deny
        assert classify_source_value("--config=/Users/x/f.json") == ["/Users/x/f.json"]

    def test_colon_list_denies_absolute_segment(self):
        # rule 5: colon list → each segment by 2-3; the absolute one denies
        assert classify_source_value("lib:/opt/x") == ["/opt/x"]

    def test_npm_scope_token_passes(self):
        # not a path — no scheme, no '=', no ':', no '/'|'~' head
        assert classify_source_value("@notionhq/notion-mcp-server@2.2.1") == []

    def test_https_url_passes(self):
        # rule 1: scheme:// → pass
        assert classify_source_value("https://api.example.com/v1") == []

    def test_postgres_url_passes(self):
        # rule 1: a connection URL is a URL, not a path
        assert classify_source_value("postgresql://u@h/db") == []

    def test_file_url_carve_out_denies_path(self):
        # rule 1 carve-out: file:// → classify the path part as absolute → deny
        assert classify_source_value("file:///Users/x") == ["/Users/x"]

    def test_env_var_reference_passes(self):
        # rule 2: any ${VAR} head passes (a runtime env ref, not a path)
        assert classify_source_value("${GITHUB_PAT}") == []

    def test_short_flag_passes(self):
        assert classify_source_value("-y") == []

    def test_model_id_passes(self):
        assert classify_source_value("claude-fable-5") == []


class TestClassifyAnchorsAndVars:
    def test_bare_dollar_anchor_passes(self):
        assert classify_source_value("$FLEET_ROOT/mcp/x.py") == []

    def test_bot_dir_anchor_passes(self):
        assert classify_source_value("${BOT_DIR}/data/index.js") == []

    def test_claudlobby_root_anchor_passes(self):
        assert classify_source_value("${CLAUDLOBBY_ROOT}/state/x.json") == []

    def test_anchor_segment_in_colon_list_passes_foreign_denied(self):
        # a PATH-style list: the anchor segment passes, the foreign absolute denies
        assert classify_source_value("${FLEET_ROOT}/bin:/opt/x") == ["/opt/x"]

    def test_flag_rhs_url_passes(self):
        # DATABASE_URL=postgres://… — flag rhs is a URL (rule 4 → rule 1) → pass
        assert classify_source_value("DATABASE_URL=postgresql://u@h/db") == []

    def test_flag_rhs_anchor_passes(self):
        assert classify_source_value("--config=${FLEET_ROOT}/x.json") == []


class TestClassifyWordSplitRule6:
    """Adversarial rows — only the three word-split fields split first (rule 6).
    An embedded absolute invisible to rules 1-5 on the whole value is caught only
    when the field word-splits."""

    def test_hook_command_embedded_absolute_denied(self):
        assert classify_source_value("python3 /Users/x/hook.py", word_split=True) == [
            "/Users/x/hook.py"
        ]

    def test_dash_headed_equalless_flag_absolute_denied(self):
        # --mcp-config /Users/x/f.json — '-'-headed, '='-less; invisible to 1-5 on
        # the whole value, caught only by word-split
        assert classify_source_value(
            "--mcp-config /Users/x/f.json", word_split=True
        ) == ["/Users/x/f.json"]

    def test_word_split_colon_mount_denies_both_segments(self):
        # -v /Users/x:/data → token 2 colon-splits (rule 6 → 5)
        assert classify_source_value("-v /Users/x:/data", word_split=True) == [
            "/Users/x",
            "/data",
        ]

    def test_embedded_absolute_not_split_when_not_word_split(self):
        # same value on a NON-word-split field (e.g. an env option-string): the
        # embedded path is the accepted residual — L2 catches fleet-shaped ones
        assert classify_source_value("python3 /Users/x/hook.py") == []


class TestClassifySlashRefPostureBoundary:
    """A slash-command ref would false-positive if classified — this pins the raw
    grammar behavior that makes the autonomous_runner.skill posture-exemption
    load-bearing (the field is never classified; see _FIELD_POSTURES)."""

    def test_slash_command_ref_classifies_as_absolute(self):
        # /claudna:implement-plan → colon-split → /claudna is absolute-headed
        assert classify_source_value("/claudna:implement-plan") == ["/claudna"]


class TestClassifyTildeRemediation:
    def test_tilde_denied_anywhere(self):
        # remediation is to declare the EXPANDED absolute; decls never carry '~'
        assert classify_source_value("~/models") == ["~/models"]


class TestExternalDeclParsing:
    def test_valid_decl_parses(self):
        decls = parse_external_decls(
            [{"path": "/var/lib/printify/data", "purpose": "printify mount root"}]
        )
        assert decls == [
            ExternalDecl(path="/var/lib/printify/data", purpose="printify mount root")
        ]

    def test_missing_purpose_rejected(self):
        # purpose is a required, verifiable schema field (YAML comments are invisible)
        with pytest.raises(ValueError, match="purpose"):
            parse_external_decls([{"path": "/var/lib/printify/data"}])

    def test_empty_purpose_rejected(self):
        with pytest.raises(ValueError, match="purpose"):
            parse_external_decls([{"path": "/var/lib/printify/data", "purpose": ""}])

    def test_relative_path_rejected(self):
        with pytest.raises(ValueError, match="absolute"):
            parse_external_decls([{"path": "var/lib/x", "purpose": "p"}])

    def test_tilde_path_rejected(self):
        # '~' is not absolute — declare the expanded form
        with pytest.raises(ValueError, match="absolute"):
            parse_external_decls([{"path": "~/models", "purpose": "p"}])

    def test_parent_escape_rejected(self):
        with pytest.raises(ValueError, match=r"\.\."):
            parse_external_decls([{"path": "/var/lib/../etc", "purpose": "p"}])

    def test_non_tail_glob_rejected(self):
        with pytest.raises(ValueError, match=r"\*\*"):
            parse_external_decls([{"path": "/var/**/data", "purpose": "p"}])

    def test_embedded_star_rejected(self):
        with pytest.raises(ValueError, match=r"\*"):
            parse_external_decls([{"path": "/var/li*b/**", "purpose": "p"}])

    def test_shallow_glob_rejected_by_breadth_floor(self):
        # /opt/** is one segment before the glob — too broad
        with pytest.raises(ValueError, match="broad"):
            parse_external_decls([{"path": "/opt/**", "purpose": "p"}])

    def test_root_glob_rejected_by_breadth_floor(self):
        with pytest.raises(ValueError, match="broad"):
            parse_external_decls([{"path": "/**", "purpose": "p"}])

    def test_two_segment_glob_allowed(self):
        decls = parse_external_decls(
            [{"path": "/var/lib/printify/**", "purpose": "printify tree"}]
        )
        assert decls[0].path == "/var/lib/printify/**"

    def test_non_mapping_entry_rejected(self):
        with pytest.raises(ValueError, match="mapping|path"):
            parse_external_decls(["/var/lib/printify/data"])

    def test_empty_list_parses_empty(self):
        assert parse_external_decls([]) == []


class TestMatchExternal:
    def _decls(self, *paths):
        return [ExternalDecl(path=p, purpose="p") for p in paths]

    def test_exact_match(self):
        decls = self._decls("/var/lib/printify/data/x.json")
        assert match_external("/var/lib/printify/data/x.json", decls) is True

    def test_unmatched_path_is_false(self):
        decls = self._decls("/var/lib/printify/data")
        assert match_external("/etc/passwd", decls) is False

    def test_glob_matches_on_segment_boundary(self):
        decls = self._decls("/var/lib/printify/**")
        assert match_external("/var/lib/printify/data/x", decls) is True

    def test_glob_does_not_match_sibling_prefix(self):
        # /var/lib/printify/** must NOT match /var/lib/printify-secret/x
        decls = self._decls("/var/lib/printify/**")
        assert match_external("/var/lib/printify-secret/x", decls) is False

    def test_glob_matches_the_prefix_dir_itself(self):
        # the tree includes its own root
        decls = self._decls("/var/lib/printify/**")
        assert match_external("/var/lib/printify", decls) is True

    def test_exact_decl_does_not_prefix_match(self):
        # a non-glob decl blesses only the exact path, not its children
        decls = self._decls("/var/lib/printify")
        assert match_external("/var/lib/printify/data", decls) is False

    def test_empty_decls_never_match(self):
        assert match_external("/var/lib/printify/data", []) is False

    def test_value_scoped_blessing_is_surface_agnostic(self):
        # one declared prefix blesses a matching value regardless of which field
        # carried it — match_external takes only (path, decls)
        decls = self._decls("/var/lib/printify/**")
        assert match_external("/var/lib/printify/dist/index.js", decls) is True


def _bot(**overrides):
    base = dict(
        bot_id="kev",
        name="kev",
        expertise=["eng"],
        telegram=TelegramConfig(handle="kev_bot"),
    )
    base.update(overrides)
    return BotConfig(**base)


def _fleet():
    return FleetConfig(name="tl", service_prefix="com.crog.tl")


class TestAuditBotSourcesWalk:
    """The dataclass walk applies _FIELD_POSTURES to every string leaf, denies
    unanchored/undeclared absolutes, and blesses declared ones."""

    def test_foreign_absolute_env_value_is_a_finding(self):
        findings = audit_bot_sources(
            _bot(env={"GA4_KEY": "/Users/x/ga4.json"}), _fleet()
        )
        assert len(findings) == 1
        assert isinstance(findings[0], SourceFinding)
        assert findings[0].path == "/Users/x/ga4.json"
        assert "env.GA4_KEY" in findings[0].source

    def test_anchored_env_value_is_clean(self):
        assert (
            audit_bot_sources(_bot(env={"P": "${FLEET_ROOT}/mcp/x.py"}), _fleet()) == []
        )

    def test_declared_external_path_is_clean(self):
        bot = _bot(
            env={"P": "/var/lib/printify/data/index.js"},
            external_paths=[ExternalDecl(path="/var/lib/printify/**", purpose="mount")],
        )
        assert audit_bot_sources(bot, _fleet()) == []

    def test_external_paths_own_absolute_is_never_denied(self):
        bot = _bot(
            external_paths=[
                ExternalDecl(path="/var/lib/printify/data", purpose="mount")
            ]
        )
        assert audit_bot_sources(bot, _fleet()) == []

    def test_startup_prompt_is_exempt_prose(self):
        bot = _bot(startup_prompt="/Users/x is just an example path in prose")
        assert audit_bot_sources(bot, _fleet()) == []

    def test_mounts_are_declared_by_construction_exempt(self):
        assert (
            audit_bot_sources(_bot(mounts={"data": "/mnt/host/data"}), _fleet()) == []
        )

    def test_tilde_env_value_is_a_finding(self):
        findings = audit_bot_sources(_bot(env={"H": "~/creds.json"}), _fleet())
        assert [f.path for f in findings] == ["~/creds.json"]

    def test_secret_files_absolute_is_a_finding(self):
        findings = audit_bot_sources(_bot(secret_files={"K": "/etc/secret"}), _fleet())
        assert [f.path for f in findings] == ["/etc/secret"]

    def test_secret_files_relative_is_clean(self):
        assert (
            audit_bot_sources(_bot(secret_files={"K": ".secrets/k.json"}), _fleet())
            == []
        )

    def test_hook_command_embedded_absolute_is_a_finding(self):
        bot = _bot(
            hooks={
                "PreToolUse": [{"type": "command", "command": "python3 /Users/x/h.py"}]
            }
        )
        assert [f.path for f in audit_bot_sources(bot, _fleet())] == ["/Users/x/h.py"]

    def test_hook_non_command_key_is_not_scanned(self):
        # a matcher/type is not a shell command — exempt from the source scan
        bot = _bot(
            hooks={
                "PreToolUse": [
                    {"type": "command", "command": "true", "matcher": "/Users/x"}
                ]
            }
        )
        assert audit_bot_sources(bot, _fleet()) == []

    def test_extra_flags_word_split_finding(self):
        bot = _bot(extra_flags=["--mcp-config", "/Users/x/f.json"])
        assert [f.path for f in audit_bot_sources(bot, _fleet())] == ["/Users/x/f.json"]

    def test_autonomous_runner_skill_is_exempt(self):
        bot = _bot(
            autonomous_runner=AutonomousRunnerConfig(
                skill="/claudna:implement-plan", cadence="daily", target_repo="org/repo"
            )
        )
        assert audit_bot_sources(bot, _fleet()) == []

    def test_autonomous_runner_args_word_split_finding(self):
        bot = _bot(
            autonomous_runner=AutonomousRunnerConfig(
                skill="/x",
                cadence="daily",
                target_repo="o/r",
                args="--config /Users/x/f.json",
            )
        )
        # skill exempt; args word-split catches the embedded absolute
        assert [f.path for f in audit_bot_sources(bot, _fleet())] == ["/Users/x/f.json"]

    def test_new_string_field_is_checked_by_default(self):
        # deny-by-default coverage: a plain scalar path field (mission) is checked
        bot = _bot(mission="/Users/x/charter.md")
        assert [f.path for f in audit_bot_sources(bot, _fleet())] == [
            "/Users/x/charter.md"
        ]

    def test_non_path_scalars_pass(self):
        bot = _bot(
            model="claude-fable-5", channels=["plugin:telegram@claude-plugins-official"]
        )
        assert audit_bot_sources(bot, _fleet()) == []


class TestAssertBotSources:
    def test_clean_bot_does_not_raise(self):
        assert_bot_sources(_bot(env={"P": "${FLEET_ROOT}/x"}), _fleet())

    def test_finding_raises_field_precise_with_both_fixes(self):
        bot = _bot(env={"GA4_KEY": "/Users/x/ga4.json"})
        with pytest.raises(ValueError) as ei:
            assert_bot_sources(bot, _fleet())
        msg = str(ei.value)
        assert "env.GA4_KEY" in msg  # field-precise provenance
        assert "/Users/x/ga4.json" in msg  # the denied path
        assert "FLEET_ROOT" in msg  # fix 1: anchor
        assert "external_paths" in msg  # fix 2: declare
        assert "mounts" in msg  # triage line


class TestClassifyGrantPaths:
    """Tool(spec) grants — the path portion is classified after the wrapper is
    stripped, since classify_source_value is head-anchored and 'Read(' is not."""

    def _decls(self, *paths):
        return [ExternalDecl(path=p, purpose="p") for p in paths]

    def test_read_grant_absolute_denied(self):
        assert classify_grant_paths("Read(/Users/x/f)", []) == ["/Users/x/f"]

    def test_bash_grant_embedded_absolute_denied(self):
        assert classify_grant_paths("Bash(python3 /Users/x/h.py)", []) == [
            "/Users/x/h.py"
        ]

    def test_bash_glob_grant_clean(self):
        assert classify_grant_paths("Bash(ls *)", []) == []

    def test_anchored_grant_clean(self):
        assert classify_grant_paths("Read(${FLEET_ROOT}/x)", []) == []

    def test_declared_grant_clean(self):
        assert (
            classify_grant_paths("Bash(/opt/tool/bin *)", self._decls("/opt/tool/**"))
            == []
        )

    def test_bare_tool_grant_clean(self):
        assert classify_grant_paths("Edit", []) == []

    def test_mcp_glob_grant_clean(self):
        assert classify_grant_paths("mcp__github__*", []) == []


class TestDeniedSourcePaths:
    def test_filters_declared(self):
        decls = [ExternalDecl(path="/var/lib/printify/**", purpose="p")]
        assert denied_source_paths("/var/lib/printify/x", decls) == []
        assert denied_source_paths("/Users/x", decls) == ["/Users/x"]

    def test_word_split(self):
        assert denied_source_paths("a /x", [], word_split=True) == ["/x"]


class TestAuditBotSourcesFragments:
    """Choke-site 1 — loaded MCP fragment leaves (command/args/env/headers/url)."""

    def test_fragment_absolute_arg_is_a_finding(self):
        frag = {
            "mcpServers": {
                "printify": {"command": "node", "args": ["/Users/x/dist/index.js"]}
            }
        }
        findings = audit_bot_sources(_bot(), _fleet(), fragments={"printify": frag})
        assert [f.path for f in findings] == ["/Users/x/dist/index.js"]
        assert "library/mcp/printify.json" in findings[0].source

    def test_fragment_anchored_arg_is_clean(self):
        frag = {
            "mcpServers": {
                "printify": {"args": ["${FLEET_ROOT}/runtime/bots/kev/data/x.js"]}
            }
        }
        assert audit_bot_sources(_bot(), _fleet(), fragments={"printify": frag}) == []

    def test_fragment_env_placeholder_and_npx_are_clean(self):
        frag = {
            "mcpServers": {
                "gh": {
                    "command": "npx",
                    "args": ["-y", "@org/server-github@2025.4.8"],
                    "env": {"TOKEN": "${GITHUB_PAT}"},
                }
            }
        }
        assert audit_bot_sources(_bot(), _fleet(), fragments={"gh": frag}) == []

    def test_fragment_file_url_is_denied(self):
        frag = {"mcpServers": {"x": {"url": "file:///Users/x/sock"}}}
        findings = audit_bot_sources(_bot(), _fleet(), fragments={"x": frag})
        assert [f.path for f in findings] == ["/Users/x/sock"]

    def test_fragment_declared_arg_is_clean(self):
        frag = {"mcpServers": {"p": {"args": ["/var/lib/printify/dist/index.js"]}}}
        bot = _bot(
            external_paths=[ExternalDecl(path="/var/lib/printify/**", purpose="mount")]
        )
        assert audit_bot_sources(bot, _fleet(), fragments={"p": frag}) == []


class TestIsAnchorHeaded:
    """R1 — which bot.conf values must emit double-quoted so anchors expand live."""

    def test_fleet_root_braced_is_anchor(self):
        assert is_anchor_headed("${FLEET_ROOT}/x") is True

    def test_bot_dir_bare_is_anchor(self):
        assert is_anchor_headed("$BOT_DIR/x") is True

    def test_claudlobby_root_is_anchor(self):
        assert is_anchor_headed("${CLAUDLOBBY_ROOT}/state") is True

    def test_plain_env_var_is_not_anchor(self):
        assert is_anchor_headed("${GITHUB_PAT}") is False

    def test_absolute_is_not_anchor(self):
        assert is_anchor_headed("/Users/x") is False
