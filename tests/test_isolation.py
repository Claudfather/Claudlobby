"""Layer 0b, shared-config isolation (#1665): the rules, the switch, the doors.

What these tests can and cannot show. They prove what the composer EMITS and
which paths those rules name under gitignore-style matching (the documented
semantics of a permission rule's path). They do not prove Claude Code enforces
any of it: that is the live canary's job (P1-P16 / N1-N10 in the #1665 design,
run on one bot after merge). The offline P/N cells below are the half of that
table a matcher simulation can answer, and only that half.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claudlobby import isolation as iso
from claudlobby import switches as sw
from claudlobby.composer import compose_settings_local
from claudlobby.config import BotConfig, FleetConfig, IsolationConfig, load_fleet
from claudlobby.paths import Paths

# ---------------------------------------------------------------------------
# a synthetic host: two fleets under one install, one home
# ---------------------------------------------------------------------------

ALPHA = """\
fleet:
  name: alpha
  service_prefix: com.alpha
  bots:
    ravi:
      expertise: [eng]
      isolation: {shared_config: true}
    otis:
      expertise: [eng]
"""

BETA = """\
fleet:
  name: beta
  service_prefix: com.beta
  bots:
    clog:
      expertise: [eng]
      telegram: {handle: clog_bot}
    craig:
      expertise: [eng]
      channels: []
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    """(root, home, alpha fleet, alpha paths). HOME is redirected so no rule,
    and no read, can reach the real operator's home."""
    root = tmp_path / "claudlobby"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name, text in (("alpha", ALPHA), ("beta", BETA)):
        d = root / "local" / name
        d.mkdir(parents=True)
        (d / "fleet.yaml").write_text(text)
    alpha_dir = root / "local" / "alpha"
    fleet, _ = load_fleet(alpha_dir / "fleet.yaml")
    return root, home, fleet, Paths(root=root, fleet_dir=alpha_dir)


def _deny(bot, fleet, paths) -> list[str]:
    return (
        compose_settings_local(bot, fleet, paths).get("permissions", {}).get("deny", [])
    )


def _glob(pattern: str) -> re.Pattern:
    """A permission-rule path glob as gitignore reads it: `*` stays inside one
    segment, `**` spans any number (zero included)."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("/**/", i):
            out.append("(?:/.*)?/")
            i += 4
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("/.*")
            i += 3
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _denied(rules: list[str], tool: str, path: Path | str) -> bool:
    for rule in rules:
        head = f"{tool}(/"
        if rule.startswith(head) and rule.endswith(")"):
            if _glob(rule[len(head) : -1]).match(str(path)):
                return True
    return False


def _slug_dir(home: Path, bot_dir: Path, sub: str = "") -> Path:
    cwd = bot_dir / sub if sub else bot_dir
    return home / ".claude" / "projects" / iso.transcript_slug(cwd) / "s.jsonl"


# ---------------------------------------------------------------------------
# the switch: every class when on, nothing when off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", list(iso.CLASSES))
def test_each_class_emits_when_the_switch_is_on(host, cls):
    root, home, fleet, paths = host
    deny = _deny(fleet.bots["ravi"], fleet, paths)
    rules = [
        r for r in iso.layer0b(fleet.bots["ravi"], fleet, paths).rules if r.cls == cls
    ]
    assert rules, f"class {cls} composed no rule"
    assert all(r.text in deny for r in rules)


@pytest.mark.parametrize("cls", list(iso.CLASSES))
def test_each_class_emits_nothing_when_the_switch_is_off(host, cls):
    root, home, fleet, paths = host
    otis = fleet.bots["otis"]
    assert otis.isolation.shared_config is False  # the shipped default
    deny = _deny(otis, fleet, paths)
    rules = [r for r in iso.layer0b(otis, fleet, paths).rules if r.cls == cls]
    assert rules  # the positive control: there WAS something to leave out
    assert not [r.text for r in rules if r.text in deny]
    # and Layer 0 is untouched: exactly the sibling pair, as before #1665
    ravi_dir = paths.bot_runtime("ravi")
    assert deny == [f"Read(/{ravi_dir}/**)", f"Edit(/{ravi_dir}/**)"]


def test_every_rule_is_double_slash_anchored(host):
    root, home, fleet, paths = host
    for rule in iso.layer0b(fleet.bots["ravi"], fleet, paths).rules:
        assert rule.text.startswith((f"{rule.tool}(//",)), rule.text


def test_the_install_root_is_edit_only(host):
    root, home, fleet, paths = host
    rules = iso.layer0b(fleet.bots["ravi"], fleet, paths).rules
    install = [r for r in rules if r.cls == iso.INSTALL_ROOT]
    assert {r.tool for r in install} == {"Edit"}
    assert not [
        r for r in rules if r.tool == "Read" and r.path.startswith(f"{root}/lib")
    ]
    assert not _denied([r.text for r in rules], "Read", root / "lib" / "lib-common.sh")


# ---------------------------------------------------------------------------
# the offline P/N cells: which paths the rules name
# ---------------------------------------------------------------------------


def test_the_offline_positive_cells_are_named(host):
    root, home, fleet, paths = host
    deny = _deny(fleet.bots["ravi"], fleet, paths)
    otis_dir = paths.bot_runtime("otis")
    clog_dir = root / "local" / "beta" / "runtime" / "bots" / "clog"
    cells = {
        "P1 sibling base-slug transcript": ("Read", _slug_dir(home, otis_dir)),
        "P2 sibling variant-slug transcript": (
            "Read",
            _slug_dir(home, otis_dir, "projects/Claudlobby"),
        ),
        "P3 another fleet's transcript": ("Read", _slug_dir(home, clog_dir)),
        "P4 history.jsonl": ("Read", home / ".claude" / "history.jsonl"),
        "P5 .credentials.json": ("Read", home / ".claude" / ".credentials.json"),
        "P6 host .env": ("Read", home / ".env"),
        "P6 root .env": ("Read", root / ".env"),
        "P6 fleet .env": ("Read", root / "local" / "beta" / ".env"),
        "P6 own bot .env": ("Read", paths.bot_runtime("ravi") / ".env"),
        "P6 own bot .env backup": ("Read", paths.bot_runtime("ravi") / ".env.bak-1"),
        "P6 other fleet bot .env": ("Read", clog_dir / ".env"),
        "P7 another bot's Telegram .env": (
            "Read",
            home / ".claude" / "channels" / "telegram-clog_bot" / ".env",
        ),
        "P8 Edit on the install's lib/": ("Edit", root / "lib" / "lib-common.sh"),
        "P8 Edit on the shared settings.json": (
            "Edit",
            home / ".claude" / "settings.json",
        ),
        "P8 Edit on a shared hook": ("Edit", home / ".claude" / "hooks" / "x.sh"),
        "account config": ("Read", home / ".claude" / ".config.json"),
        "legacy account config": ("Read", home / ".claude.json"),
    }
    missed = [
        name for name, (tool, path) in cells.items() if not _denied(deny, tool, path)
    ]
    assert missed == []


def test_the_offline_negative_cells_stay_open(host):
    root, home, fleet, paths = host
    deny = _deny(fleet.bots["ravi"], fleet, paths)
    ravi_dir = paths.bot_runtime("ravi")
    cells = {
        "N1 own base-slug transcript": ("Read", _slug_dir(home, ravi_dir)),
        "N2 own variant-slug transcript": (
            "Read",
            _slug_dir(home, ravi_dir, "projects/Claudlobby"),
        ),
        "N9 Read on the install's lib/": ("Read", root / "lib" / "lib-common.sh"),
        "own Telegram state dir": (
            "Read",
            home / ".claude" / "channels" / "telegram-ravi" / "access.json",
        ),
        "the tracked .env.example": ("Read", root / ".env.example"),
        "Read of the shared settings.json": (
            "Read",
            home / ".claude" / "settings.json",
        ),
        "own memory": ("Edit", ravi_dir / "memory" / "MEMORY.md"),
    }
    hit = [name for name, (tool, path) in cells.items() if _denied(deny, tool, path)]
    assert hit == []


def test_a_moved_bot_stays_covered_at_both_homes(host):
    """The addendum's reason for NAME keys: move-bot changes the path, and a
    path-keyed rule leaves the new one open until every fleet regenerates."""
    root, home, fleet, paths = host
    deny = _deny(fleet.bots["ravi"], fleet, paths)
    old = root / "local" / "beta" / "runtime" / "bots" / "clog"
    new = root / "local" / "gamma" / "runtime" / "bots" / "clog"
    for bot_dir in (old, new):
        assert _denied(deny, "Read", _slug_dir(home, bot_dir))
        assert _denied(deny, "Read", _slug_dir(home, bot_dir, "work"))
        assert _denied(deny, "Read", bot_dir / ".env")


# ---------------------------------------------------------------------------
# name-glob collisions over a synthetic roster
# ---------------------------------------------------------------------------

ROSTER_NAMES = ("ravi", "ravi-x", "worker", "worker-1", "worker-1-b", "a", "ab", "otis")


def _synthetic(tmp_path: Path, names=ROSTER_NAMES, *, dup: str | None = None):
    root, home = tmp_path / "r", tmp_path / "h"
    bots = []
    for i, name in enumerate(names):
        fleet = f"f{i % 3}"
        bots.append(
            iso.HostBot(
                fleet,
                name,
                root / "local" / fleet / "runtime" / "bots" / name,
                f"{name}_bot",
                home / ".claude",
            )
        )
    if dup:
        bots.append(
            iso.HostBot(
                "other",
                dup,
                root / "local" / "other" / "runtime" / "bots" / dup,
                f"{dup}_dup_bot",
                home / ".claude",
            )
        )
    return root, home, iso.Roster(tuple(bots), (), ())


def _self(root: Path, me: iso.HostBot):
    bot = BotConfig(
        bot_id=me.name,
        name=me.name,
        expertise=["eng"],
        isolation=IsolationConfig(shared_config=True),
    )
    fleet = FleetConfig(name=me.fleet, service_prefix="p", bots={me.name: bot})
    return bot, fleet, Paths(root=root, fleet_dir=root / "local" / me.fleet)


@pytest.mark.parametrize("name", ROSTER_NAMES)
def test_no_name_glob_denies_a_bot_its_own_sessions(tmp_path, name):
    root, home, roster = _synthetic(tmp_path)
    me = next(b for b in roster.bots if b.name == name)
    bot, fleet, paths = _self(root, me)
    result = iso.layer0b(bot, fleet, paths, home=home, roster=roster)
    deny = result.deny
    assert not _denied(deny, "Read", _slug_dir(home, me.bot_dir))
    # A subdirectory session is denied only in the one case a note names.
    for sub in ("projects/Claudlobby", "b", "x"):
        if _denied(deny, "Read", _slug_dir(home, me.bot_dir, sub)):
            assert any(
                f"subdirectory '{sub.split('/')[0]}'" in n for n in result.notes
            ), (name, sub, result.notes)


@pytest.mark.parametrize("name", ROSTER_NAMES)
def test_every_other_bot_is_covered_or_the_gap_is_said(tmp_path, name):
    root, home, roster = _synthetic(tmp_path)
    me = next(b for b in roster.bots if b.name == name)
    bot, fleet, paths = _self(root, me)
    result = iso.layer0b(bot, fleet, paths, home=home, roster=roster)
    for other in roster.bots:
        if other.name == name:
            continue
        assert _denied(result.deny, "Read", _slug_dir(home, other.bot_dir)), other.name
        if not _denied(result.deny, "Read", _slug_dir(home, other.bot_dir, "work")):
            assert any(f"sessions {other.name} starts" in n for n in result.notes)


def test_the_prefix_guards_fire_where_the_roster_says_they_should(tmp_path):
    root, home, roster = _synthetic(tmp_path)
    me = next(b for b in roster.bots if b.name == "ravi-x")
    bot, fleet, paths = _self(root, me)
    notes = iso.layer0b(bot, fleet, paths, home=home, roster=roster).notes
    assert any("sessions ravi starts" in n for n in notes)  # skip, forward
    me = next(b for b in roster.bots if b.name == "ravi")
    bot, fleet, paths = _self(root, me)
    notes = iso.layer0b(bot, fleet, paths, home=home, roster=roster).notes
    assert any("'ravi-x' begins with 'ravi-'" in n for n in notes)  # said, reverse
    me = next(b for b in roster.bots if b.name == "otis")
    bot, fleet, paths = _self(root, me)
    assert iso.layer0b(bot, fleet, paths, home=home, roster=roster).notes == ()


def test_a_shared_name_falls_back_to_the_path_and_says_so(tmp_path):
    """#526: another fleet holds this bot's name. A name-keyed rule for it would
    deny this bot its own sessions."""
    root, home, roster = _synthetic(tmp_path, dup="ravi")
    me = next(b for b in roster.bots if b.name == "ravi" and b.fleet != "other")
    twin = next(b for b in roster.bots if b.fleet == "other")
    bot, fleet, paths = _self(root, me)
    result = iso.layer0b(bot, fleet, paths, home=home, roster=roster)
    assert not _denied(result.deny, "Read", _slug_dir(home, me.bot_dir))
    assert _denied(result.deny, "Read", _slug_dir(home, twin.bot_dir))
    assert any("shares this bot's name" in n for n in result.notes)


def test_an_unreadable_sibling_manifest_is_said_not_swallowed(host, caplog):
    root, home, fleet, paths = host
    (root / "local" / "beta" / "fleet.yaml").write_text("fleet: [not, a, mapping")
    result = iso.layer0b(fleet.bots["ravi"], fleet, paths)
    assert any("beta" in n and "could not be read" in n for n in result.notes)
    # the fleet tier is still named: the DIR is known even when its bots are not
    assert _denied(result.deny, "Read", root / "local" / "beta" / ".env")
    with caplog.at_level("WARNING"):
        _deny(fleet.bots["ravi"], fleet, paths)
    assert "could not be read" in caplog.text


# ---------------------------------------------------------------------------
# exemptions restore a READ, never a write
# ---------------------------------------------------------------------------


def test_exemptions_restore_reads_and_keep_writes(host):
    root, home, fleet, paths = host
    ravi = fleet.bots["ravi"]
    ravi.isolation = IsolationConfig(
        shared_config=True, exempt=frozenset({"account_config", "env_host"})
    )
    deny = _deny(ravi, fleet, paths)
    assert not _denied(deny, "Read", home / ".claude" / ".config.json")
    assert _denied(deny, "Edit", home / ".claude" / ".config.json")
    assert not _denied(deny, "Read", home / ".env")
    assert _denied(deny, "Edit", home / ".env")
    assert _denied(deny, "Read", root / "local" / "beta" / ".env")  # other tiers hold


# ---------------------------------------------------------------------------
# config: strict, because it arms deny rules
# ---------------------------------------------------------------------------


def _load(tmp_path, bot_stanza: str, defaults: str = "") -> FleetConfig:
    d = tmp_path / "f"
    d.mkdir(exist_ok=True)
    (d / "fleet.yaml").write_text(
        "fleet:\n  name: f\n  service_prefix: p\n"
        + (f"  defaults:\n{defaults}" if defaults else "")
        + f"  bots:\n    b:\n      expertise: [eng]\n{bot_stanza}"
    )
    return load_fleet(d / "fleet.yaml")[0]


def test_the_knob_resolves_per_bot_over_defaults(tmp_path):
    fleet = _load(
        tmp_path,
        "      isolation: {shared_config: false, exempt: [env_host]}\n",
        "    isolation: {shared_config: true, exempt: [account_config]}\n",
    )
    iso_cfg = fleet.bots["b"].isolation
    assert iso_cfg.shared_config is False
    assert iso_cfg.exempt == {"account_config", "env_host"}
    assert _load(tmp_path, "").bots["b"].isolation == IsolationConfig()


@pytest.mark.parametrize(
    "stanza, needle",
    [
        ("      isolation: {shared_config: 'yes'}\n", "YAML boolean"),
        ("      isolation: {shared_confg: true}\n", "unknown key"),
        ("      isolation: {exempt: [credentials]}\n", "unknown exemption"),
        ("      isolation: {exempt: [{a: 1}]}\n", "unknown exemption"),
        ("      isolation: true\n", "must be a mapping"),
    ],
)
def test_a_typo_is_an_error_not_a_silent_switch(tmp_path, stanza, needle):
    with pytest.raises(ValueError, match=needle):
        _load(tmp_path, stanza)


# ---------------------------------------------------------------------------
# the ~/.env read: a command, not a mention
# ---------------------------------------------------------------------------

READS = [
    "```bash\nsource ~/.env\npython3 x.py\n```",
    "```bash\nset -a; . ~/.env; set +a          # or wherever you keep secrets\n```",
    "   ```bash\n   source ~/.env\n   ```",
    '```\n. "$HOME/.env" && run\n```',
    "```\nsource ${HOME}/.env.local\n```",
    "```\nif true; then source ~/.env; fi\n```",
    "1. Always `source ~/.env` before running any finance commands",
    "Load it first: `set -a; . ~/.env; set +a`.",
    "~~~\nsource ~/.env\n~~~",
    # #1919, from otis's review of #1918: a negation elsewhere on the line hid
    # each of these, because the first version tested the WHOLE line.
    "If `SIMPLEFIN_ACCESS_URL` is not set, run `source ~/.env` first.",
    "Run `source ~/.env` — without it the scripts fail.",
    "Do not skip `source ~/.env`.",
    # a reversal after the negation is still an instruction
    "Don't forget to `source ~/.env` before the scripts.",
    # #1919: every spelling row E denies, not only `~`
    "```\nsource $HOME/.env\n```",
    '```\n. "$HOME"/.env && run\n```',
    "```\nsource ~alice/.env\n```",
    "```bash\nsource /home/<user>/.env\n```",
    "Run `. /home/alice/.env` before the script.",
    "```\nsource /Users/alice/.env.local\n```",
    "```\nsource /root/.env\n```",
]

MENTIONS = [
    "Crons source `~/.env` via `. <USER_HOME>/.env`.",
    "1. Never `source ~/.env`: the variable is already in the session",
    "**Why not `source ~/.env`.** It loads nothing the session does not hold.",
    "So never read `~/.env` yourself.",
    "Put the key in `~/.env` and restart.",
    '```bash\n: "${SIMPLEFIN_ACCESS_URL:?not in the session env - restart}"\n```',
    '```bash\n# source ~/.env if you must\necho "source ~/.env"\n```',
    "```\nsource .env\nsource ~/.envrc\ncat ~/.env.example\n```",
    "Do not `source ~/.env` here.",
    # the path inside another command's argument, fenced or inline
    '```bash\ngrep -rn "source ~/.env" library/\n```',
    "Find them with `grep -rn \"source ~/.env\" library/`.",
    "```\ngrep source ~/.env\n```",
    # #1919: a negation directly before the span, or the span as the subject
    # of a negated predicate, still makes the line a mention
    "Never run `source ~/.env`; the variables are already in the session.",
    "There is no need to `source ~/.env` any more.",
    "Use the guard instead of `source ~/.env`.",
    "This skill used to `source ~/.env`.",
    "`source ~/.env` is not needed: the variables are already there.",
    "`source ~/.env` isn't required any more.",
    "`. ~/.env` is no longer needed.",
]


@pytest.mark.parametrize("line", [
    "If `SIMPLEFIN_ACCESS_URL` is not set, run `source ~/.env` first.",
    "Run `source ~/.env` — without it the scripts fail.",
    "Do not skip `source ~/.env`.",
])
def test_otis_three_lines_from_the_1918_review_fire(line):
    """#1919: pinned verbatim. Each held a negation word, and the first
    version suppressed a match on ANY negation anywhere on the line."""
    assert iso.env_reads(line) == [(1, line)]


def test_the_composers_own_home_is_recognised_by_its_absolute_path():
    text = "```\nsource /srv/people/someone/.env\n```"
    assert iso.env_reads(text) == []  # not a conventional root...
    assert iso.env_reads(text, home="/srv/people/someone") == [(2, "source /srv/people/someone/.env")]
    assert iso.env_reads("```\nsource /srv/people/other/.env\n```",
                         home="/srv/people/someone") == []


@pytest.mark.parametrize("text", READS)
def test_a_command_that_reads_the_home_env_is_found(text):
    assert iso.env_reads(text), text


@pytest.mark.parametrize("text", MENTIONS)
def test_a_mention_of_the_file_is_not_a_read(text):
    assert iso.env_reads(text) == [], text


def test_the_finding_names_the_line():
    text = "# T\n\nprose\n\n```bash\nsource ~/.env\n```\n"
    assert iso.env_reads(text) == [(6, "source ~/.env")]


# ---------------------------------------------------------------------------
# the doors: validate, freshbox, the switch table
# ---------------------------------------------------------------------------


def _overlay_resource(paths: Paths, fleet: FleetConfig, body: str) -> Path:
    res = paths.fleet_dir / "library" / "resources" / "cli-tools.md"
    res.parent.mkdir(parents=True)
    res.write_text(
        f"---\ntitle: CLI tools\ndescription: d\n---\n\n# CLI tools\n\n{body}\n"
    )
    for bot in fleet.bots.values():
        bot.resources = ["cli-tools"]
    return res


def test_validate_names_the_file_that_reads_env_only_when_the_switch_is_on(host):
    from claudlobby.validator import validate

    root, home, fleet, paths = host
    res = _overlay_resource(paths, fleet, "```bash\nsource ~/.env\npython3 x.py\n```")
    hits = [
        w for k, w in validate(fleet, paths).categorized() if k == "isolation-env-read"
    ]
    assert len(hits) == 1 and f"{res}:9 " in hits[0] and "ravi" in hits[0]
    assert "otis" not in hits[0]  # off for otis, so nothing to break there
    fleet.bots["ravi"].isolation = IsolationConfig()
    assert not [
        k for k, _ in validate(fleet, paths).categorized() if k.startswith("isolation")
    ]


def test_freshbox_reports_each_missing_class_by_name(host):
    from claudlobby.freshbox import _isolation_findings

    root, home, fleet, paths = host
    ravi = fleet.bots["ravi"]
    target = paths.bot_runtime("ravi") / ".claude" / "settings.local.json"
    target.parent.mkdir(parents=True)

    def write(deny):
        target.write_text(json.dumps({"permissions": {"deny": deny}}))

    write(_deny(ravi, fleet, paths))  # composed by this install: nothing missing
    assert [
        f
        for f in _isolation_findings(ravi, fleet, paths)
        if f.kind == "isolation_missing"
    ] == []

    ravi_off = BotConfig(bot_id="ravi", name="ravi", expertise=["eng"])
    write(_deny(ravi_off, fleet, paths))  # a stale install: Layer 0 only
    missing = [
        f
        for f in _isolation_findings(ravi, fleet, paths)
        if f.kind == "isolation_missing"
    ]
    assert sorted(f.detail.split(" ")[0] for f in missing) == sorted(iso.CLASSES)
    assert all(f.severity == "warn" for f in missing)

    full = _deny(ravi, fleet, paths)
    write([r for r in full if not r.endswith("history.jsonl)")])  # one class stripped
    missing = [
        f
        for f in _isolation_findings(ravi, fleet, paths)
        if f.kind == "isolation_missing"
    ]
    assert [f.detail.split(" ")[0] for f in missing] == [iso.HISTORY]


def test_freshbox_names_a_bot_new_to_the_host(host):
    from claudlobby.freshbox import _isolation_findings

    root, home, fleet, paths = host
    ravi = fleet.bots["ravi"]
    target = paths.bot_runtime("ravi") / ".claude" / "settings.local.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"permissions": {"deny": _deny(ravi, fleet, paths)}}))
    beta = root / "local" / "beta" / "fleet.yaml"
    beta.write_text(BETA + "    newbie:\n      expertise: [eng]\n")
    details = [
        f.detail
        for f in _isolation_findings(ravi, fleet, paths)
        if f.kind == "isolation_missing"
    ]
    assert any(
        d.startswith("transcripts") and "no rule yet for newbie" in d for d in details
    )


def test_freshbox_says_the_switch_is_off_once_per_fleet(host):
    from claudlobby.freshbox import _isolation_off_finding

    root, home, fleet, paths = host
    (finding,) = _isolation_off_finding(fleet)
    assert finding.severity == "info" and "1 of 2 bot(s) (otis)" in finding.detail
    for bot in fleet.bots.values():
        bot.isolation = IsolationConfig(shared_config=True)
    assert _isolation_off_finding(fleet) == []


def test_the_switch_table_reads_each_bot(host):
    root, home, fleet, paths = host
    states = {s.switch.key: s for s in sw.resolve(paths, fleet, cascade={})}
    st = states["shared-config-isolation"]
    assert st.on and "1 of 2 bot(s): ravi" in st.source
    fleet.bots["ravi"].isolation = IsolationConfig()
    st = {s.switch.key: s for s in sw.resolve(paths, fleet, cascade={})}[
        "shared-config-isolation"
    ]
    assert not st.on and st.source == "default" and st.label == "off (opt-in)"


def test_the_path_audit_leaves_deny_rules_alone_and_still_catches_wiring(host):
    """Found by the throwaway-root rehearsal, not by a unit test: the L2 wiring
    audit read every absolute path in settings.local.json, so a generate of an
    armed bot died on its own deny rules (they name other fleets on purpose).
    A deny is a restriction, never wiring; the same path anywhere else in the
    file must still be caught."""
    from claudlobby.path_audit import audit_bot_paths

    root, home, fleet, paths = host
    ravi = fleet.bots["ravi"]
    target = paths.bot_runtime("ravi") / ".claude" / "settings.local.json"
    target.parent.mkdir(parents=True)
    settings = compose_settings_local(ravi, fleet, paths)
    target.write_text(json.dumps(settings))
    assert audit_bot_paths(ravi, fleet, paths) == []

    leak = str(root / "local" / "beta" / "runtime" / "bots" / "clog" / "x")
    settings["permissions"]["allow"] = [f"Read(/{leak})"]
    target.write_text(json.dumps(settings))
    assert [f.path for f in audit_bot_paths(ravi, fleet, paths)] == [f"/{leak}"]
