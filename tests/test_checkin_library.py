# tests/test_checkin_library.py
"""The /checkin skill (spec §6) is whole, is coupled to its doors by name and to
the contract by key, and its grants MATCH the command lines it writes (permissions-model.md:48-52: a space is a word boundary;
a pipeline is matched per subcommand) and contain no forbidden wildcard
(claudron-integration.md:29; boundary Invariant 5)."""

import fnmatch
import importlib.util
import json
import re
import shutil
from pathlib import Path

from claudlobby.composer import compose_claude_md
from claudlobby.config import load_fleet
from claudlobby.loader import parse_frontmatter
from claudlobby.paths import Paths
from claudlobby.status import BotStatus, format_json
from tests.conftest import install_real_template

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"
SKILL = LIB / "skills" / "checkin" / "SKILL.md"


def _flat(text: str) -> str:
    """Whitespace-collapsed: prose and code spans wrap at ~85 columns, and a
    needle that straddles a wrap must still be found (cycle-3 B5, R7)."""
    return " ".join(text.split())


DOORS = ['claudlobby --fleet "$FLEET_NAME" checkins --bot $BOT_ID --last --json',
         'claudlobby --fleet "$FLEET_NAME" checkins --bot $BOT_ID --since 7d --raised --json',
         'claudlobby --fleet "$FLEET_NAME" brief --bot $BOT_ID --json',
         'claudlobby --fleet "$FLEET_NAME" status --json', "claudron lookup --limit 5", "gh issue list",
         'bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<\'EOF\'', 'ck=$(bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" <<\'EOF\'', '--checkin "$ck"',
         'ck=$(bash "$CLAUDLOBBY_ROOT/lib/checkin-record.sh" --dry-run <<\'EOF\'', ') && claudlobby --fleet "$FLEET_NAME" checkins --bot $BOT_ID --last --json',
         "lib/dispatch-task.sh", "--checkin", "--project", "lib/tg-post.sh", "## Projects", "## Fleet Mission",
         "pane_state", "issues_seen", "DRY-RUN", "checkins[0]", "unavailable"]
CONTRACT = REPO / "lib" / "checkin-contract.py"
_spec = importlib.util.spec_from_file_location("checkin_contract", CONTRACT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def test_the_record_template_carries_every_contract_key():
    # the here-doc is the model's only example of the contract; a dropped key would refuse
    # every real check-in at rc 2 and the fallback would keep writing stub rows (cycle-8 devex)
    text = SKILL.read_text()
    record = re.search(r"```bash\nck=\$\(bash \"\$CLAUDLOBBY_ROOT/lib/checkin-record.sh\" <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)
    for key in (*cc.INPUTS_COUNTS, *cc.INPUTS_LISTS, *cc.DELTA_COUNTS, "prev_checkin_id", "project_key", "rationale", "decided", "reason", "held"):
        assert f'"{key}"' in record, f"the RECORD template lost {key}"


def test_the_roster_door_still_types_an_unobserved_worker_as_null():
    # the skill's dispatch gate reads pane_state null as UNOBSERVED; status.py writes
    # `bs.pane_state or None` over a dataclass default of "" -- pin it (cycle-8 engineering)
    bots = json.loads(format_json([BotStatus(name="w1")], "f"))["bots"]
    assert bots[0]["pane_state"] is None and bots[0]["plane_unreachable"] is None


def test_the_skill_file_is_whole_and_its_three_bash_blocks_are_closed():
    # cycle-3 B6: a nested fence in the plan truncated the deliverable at the inner
    # closer and every test still passed on the stump; pin the tail and the fences
    # (three blocks since cycle 9: the plain record, the record-and-dispatch call, the dry run)
    text = SKILL.read_text()
    assert "## Not in this chunk" in text and "## Rules" in text
    assert text.count("```") == 6 and len(re.findall(r"```bash\n.*?```", text, re.S)) == 3


def test_the_skill_is_coupled_to_its_doors():
    text = _flat(SKILL.read_text())
    for d in DOORS:
        assert d in text, d
    for action in ("dispatch", "ask", "nothing"):
        assert f"**{action}**" in text, action
    assert "RECORD before ACT" in text and "considered" in text and "could not measure" in text
    assert "follow-up check-in" in text                               # a failed ACT is recorded, never retried blind
    assert "names the chosen project and its tier" in text            # the rigor bar was weighed, not only what was picked
    assert "minimal valid `nothing` row" in text                       # the re-record is bounded: a turn never ends without a row
    assert "UNOBSERVED" in text and "plane_unreachable" in text          # a roster answer with no observation is not an idle worker
    assert "propose" not in text.split("## Not in this chunk")[0]   # the enum the contract accepts
    assert "cat <<" not in text                                      # a pipeline is matched per subcommand


def test_the_degraded_rule_is_keyed_on_mode_omitted_per_field():
    # brief's degraded[] is NEVER empty on a real fleet (captured live, cycle 4: a
    # fleet with a plane carries alerts:labeled #903 and dispatches.orphaned:labeled
    # #1014 on every call); only mode "omitted" means a field is absent (#1467)
    rule = _flat(SKILL.read_text().split("## DECIDE")[0])
    assert "`mode` is `omitted`" in rule and "unavailable" in rule
    assert "`mode` is `labeled`" in rule and "present" in rule
    assert "utilization" in rule and "not an input" in rule
    assert "plane-known" in _flat(SKILL.read_text())


# --- the grants match the command lines the skill itself writes --------------------

FORBIDDEN = ("Bash", "Bash(*)", "Bash(bash *)", "Bash(cat *)", "Bash(claudron *)", "Bash(sh *)", "Bash(gh *)", "Bash(claudlobby *)")


def _bash_grant_matches(grant: str, command: str) -> bool:
    """permissions-model.md:48-51 — the pattern inside Bash(...) is a glob over
    the command line; a trailing ' *' requires a space (a word boundary)."""
    assert grant.startswith("Bash(") and grant.endswith(")")
    return fnmatch.fnmatchcase(command, grant[5:-1])


def _skill_command_lines() -> list[str]:
    """Every `bash …`, `claudlobby …`, `claudron …`, `gh …` span — inline code,
    which MAY wrap across lines — or fenced-bash line of SKILL.md, whitespace
    collapsed; first pipeline stage only (the skill must not use pipelines)."""
    text = SKILL.read_text()
    cmds = re.findall(r"`((?:bash|claudlobby|claudron|gh) [^`]+)`", text)
    for block in re.findall(r"```bash\n(.*?)```", text, re.S):
        for line in block.splitlines():
            for piece in line.split(" && "):                        # a compound command is matched per subcommand
                piece = re.sub(r"^[a-z_]+=\$\(", "", piece.strip())   # `ck=$(bash …` is the bash subcommand
                piece = re.sub(r"^\)\s*", "", piece)                    # `) && claudlobby …` closes the substitution
                if piece.startswith(("bash ", "claudlobby ", "claudron ", "gh ")):
                    cmds.append(piece)
    return [_flat(c) for c in cmds]


def test_the_skill_grants_cover_its_own_commands_and_nothing_forbidden():
    fm, _ = parse_frontmatter(SKILL.read_text())
    grants = fm["tool_grants"]
    assert not [g for g in grants if g in FORBIDDEN], grants
    assert "mcp__plugin_telegram_telegram__reply" in grants           # ask posts through the reply tool
    bash_grants = [g for g in grants if g.startswith("Bash(")]
    for g in bash_grants:                                              # a CLI grant names ONE literal verb, never a prefix
        if g.startswith("Bash(claudlobby"):
            assert re.fullmatch(r"Bash\(claudlobby --fleet \* [a-z][a-z-]+ \*\)", g), g
    cmds = _skill_command_lines()
    assert cmds, "no command lines found in the skill"
    assert any(c.startswith("gh issue list ") for c in cmds)          # the wrapped span IS collected (cycle-3 R7)
    assert any("--dry-run" in c for c in cmds)                          # the dry run has a runnable invocation
    assert any(c.startswith('bash "$CLAUDLOBBY_ROOT/lib/dispatch-task.sh"') and '"$ck"' in c for c in cmds)   # the act rides the record call (cycle-7 R1)
    for block in re.findall(r"```bash\n(.*?)```", SKILL.read_text(), re.S):                                # …and none hides in a fenced block
        for line in block.splitlines():
            for piece in line.split(" && "):
                piece = re.sub(r"^[a-z_]+=\$\(", "", re.sub(r"^\)\s*", "", piece.strip()))
                assert not re.match(r"(echo|printf|env|cat|export)\b", piece), f"ungranted builtin: {line!r}"
    for c in cmds:
        assert any(_bash_grant_matches(g, c) for g in bash_grants), f"ungranted: {c!r}"
    # necessity: every grant is the ONLY match for some command line, so a grant
    # widened to a wildcard (which still "covers") shows up as a sibling made idle
    for g in bash_grants:
        others = [o for o in bash_grants if o != g]
        assert any(not any(_bash_grant_matches(o, c) for o in others)
                   for c in cmds if _bash_grant_matches(g, c)), f"grant {g} covers nothing on its own"


def test_the_composer_resolves_the_script_grants_through_tool_grants(fleet_dir):
    # restart/SKILL.md declares Bash(*spin-up-bot.sh*) under allowed-tools, a key
    # the compositor never reads: this is the first star-bounded script grant that
    # must ride the tool_grants path (loader.iter_skill_grants -> composer._resolve_skill_grants)
    from claudlobby.composer import _resolve_skill_grants
    install_real_template(fleet_dir)
    dst = fleet_dir / "library" / "skills" / "checkin"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(SKILL, dst / "SKILL.md")
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n", "    lead:\n      skills: [checkin]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    grants = _resolve_skill_grants(
        fleet.bots["lead"].skills, Paths(root=fleet_dir, fleet_dir=fleet_dir)
    )
    for g in ("Bash(*checkin-record.sh*)", "Bash(*dispatch-task.sh*)", "Bash(*tg-post.sh*)",
              "Bash(claudlobby --fleet * checkins *)", "Bash(claudlobby --fleet * brief *)",
              "Bash(claudlobby --fleet * status *)"):
        assert g in grants, grants
    assert "Bash(claudlobby *)" not in grants
    for g in ("Bash(claudlobby checkins *)", "Bash(claudlobby brief *)", "Bash(claudlobby status *)"):
        assert g not in grants, g


# --- library/protocols/checkin.md: the check-in protocol (PR2 Task 3) -------


def test_the_protocol_requires_its_skill_and_still_has_no_self_fire():
    text = (LIB / "protocols" / "checkin.md").read_text()
    fm, body = parse_frontmatter(text)
    # chunk 4: the grant union — the protocol brings its skill along (§10)
    assert fm["title"] == "Check-in" and fm["requires"] == {"skills": ["checkin"]}
    assert "natural idle point" not in body                        # the trigger owns the beat, with its throttles
    assert "governs where it composes beside" in _flat(body.split("## Manager")[0])


def test_the_protocol_names_the_same_bounded_ask_read_as_the_skill():
    # without --raised every check-in counts as an ask and the manager falls silent
    _fm, body = parse_frontmatter((LIB / "protocols" / "checkin.md").read_text())
    assert 'claudlobby --fleet "$FLEET_NAME" checkins --bot $BOT_ID --since 7d --raised' in _flat(body)


def test_both_sections_compose_for_a_hand_equipped_manager(fleet_dir):
    install_real_template(fleet_dir)
    shutil.copy(LIB / "protocols" / "checkin.md", fleet_dir / "library" / "protocols" / "checkin.md")
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n", "    lead:\n      protocols: [checkin]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    md = compose_claude_md(fleet.bots["lead"], fleet, Paths(root=fleet_dir, fleet_dir=fleet_dir))
    assert "### Manager" in md and "### Worker" in md
    assert "Silence is the default" in md


def test_the_manager_section_fixes_the_post_shape_and_the_one_post_budget():
    _fm, body = parse_frontmatter((LIB / "protocols" / "checkin.md").read_text())
    m = _flat(body.split("## Manager")[1].split("## Worker")[0])
    for needle in ("one status line", "one ask", "one pointer",
                   "At most one post per check-in", "never one per project",
                   "point to it"):
        assert needle in m, needle


def test_the_worker_section_keeps_the_start_ack_off_telegram():
    # start agrees with worker-lifecycle's "No Telegram ack" (line 87); only
    # done/blocked are Telegram-eligible, per the checkin/worker-lifecycle ruling
    _fm, body = parse_frontmatter((LIB / "protocols" / "checkin.md").read_text())
    w = _flat(body.split("## Worker")[1])
    assert "Start is plane-only" in w
    assert "Done and blocked" in w and "Telegram where the worker is configured for it" in w


def test_the_cadence_rules_are_untouched_in_this_chunk():
    # chunk 4 retires them with a grep-derived sweep; this chunk composes beside them
    assert "Idle silence is a bug" in (LIB / "protocols" / "proactivity-discipline.md").read_text()
    assert re.search(r"2.3 min", (LIB / "protocols" / "worker-lifecycle.md").read_text())


# --- library/protocols/dispatch.md: the project: envelope field (PR2 Task 4) -----


def test_the_dispatch_envelope_documents_the_project_field():
    # chunk 1 shipped `dispatch-task.sh --project KEY`; the composed protocol
    # text catches up here so a manager reading it sees the field it stamps
    text = (LIB / "protocols" / "dispatch.md").read_text()
    row = next((ln for ln in text.splitlines() if "`project:<key>`" in ln), None)
    assert row, "no `project:<key>` row in the envelope key-value table"
    flat = _flat(row)
    assert "`projects.yaml` slug" in flat
    assert "adds it to the envelope" in flat
    assert "stamps `project_key`" in flat
    assert "plane work item" in flat


def test_the_tracked_dispatch_recipe_shows_the_project_flag():
    text = (LIB / "protocols" / "dispatch.md").read_text()
    line = next((ln for ln in text.splitlines()
                 if "dispatch-task.sh" in ln and "--workstream <ws-id>" in ln), None)
    assert line, "tracked-dispatch recipe line not found"
    assert "--project <key>" in line


# --- every CLI door names the fleet (PR4 task 5b) ---------------------------


def test_no_checkin_door_runs_the_cli_without_a_fleet():
    # a fleet-less call runs the CLI in root mode, which an overlay install does
    # not have (#1570) -- a future edit that drops the flag must fail HERE, not
    # on a running bot
    bare = re.compile(r"claudlobby\s+(checkins|brief|status)\b")
    skill_flat = _flat(SKILL.read_text())
    protocol_flat = _flat((LIB / "protocols" / "checkin.md").read_text())
    for flat in (skill_flat, protocol_flat):
        assert not bare.findall(flat), bare.findall(flat)
    assert skill_flat.count('claudlobby --fleet "$FLEET_NAME" ') >= 6
