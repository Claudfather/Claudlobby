"""Layer 0b: shared-config isolation, the deny rules Layer 0 cannot write (#1665).

Layer 0 (``composer.compose_settings_local``) denies each fleet sibling's bot
DIRECTORY. Most cross-bot content does not live there. A session transcript is
written under the shared Claude config dir (``<config>/projects/<slug>/``),
beside the shared prompt history, the OAuth credential and the account config,
and none of those, no ``.env`` tier, and nothing in the install root is named by
any Layer 0 rule. Layer 0b names them, one CLASS per kind of content
(:data:`CLASSES`; rows A to H of the #1665 design).

**THE BOUND, so no reader takes these rules wider than they are.** A deny rule
gates Claude Code's own tool calls and nothing else. It holds on the Read tool,
and on a Bash file command given a LITERAL path (``cat``, ``head``, the target of
a redirection; #1408). It does not stop an interpreter that opens the file
itself (``python3 -c "open(...)"``), a path the matcher cannot resolve
(``$HOME/...``, ``${HOME}/...``), or any script, hook, timer or MCP server, which
are processes rather than tool calls. Every bot runs as one uid. These rules
reduce ACCIDENTAL reads through Claude's own tools; they do not stop a process
running as this uid (#1606 is the structural fix).

**Opt-in, per bot** (``isolation.shared_config``; registered in ``switches.py``).
A composed deny binds on the bot's very next tool call, with no restart between
``generate`` and enforcement, and the nightly ``reload-fleet`` generate would
carry a default-on Layer 0b onto every bot of every fleet with no canary. The
manifest is the only stageable rollout.

**Keyed on the bot's NAME wherever a move changes the path** (row A, and row E's
bot tier). ``move-bot`` gives a bot a new dir and leaves its old transcripts and
a copy of its ``.env`` behind. A path-keyed rule leaves the new path uncovered
until every fleet regenerates, and that regeneration then DROPS the rule that
protected the old path. A name survives the move. Row G is keyed by Telegram
handle, which a move does not change either.

**The host roster** is this fleet (the manifest being composed) plus every fleet
under the install's ``local/`` (the enumeration ``compose_host_bot_handles``
uses). A sibling fleet whose manifest cannot be read contributes NO bots, and
that gap is returned as a note rather than dropped, because for a deny list a
missing fleet fails open.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import BotConfig, FleetConfig
    from .paths import Paths

TRANSCRIPTS = "transcripts"
HISTORY = "history"
CREDENTIALS = "credentials"
ACCOUNT_CONFIG = "account_config"
ENV = "env"
INSTALL_ROOT = "install_root"
TELEGRAM = "telegram"
CONFIG_SURFACES = "config_surfaces"

#: class -> what its rules cover, in the design's row order (A to H). freshbox,
#: validate and the docs render from this, so the three cannot disagree.
CLASSES: dict[str, str] = {
    TRANSCRIPTS: "other bots' session transcripts (Read + Edit)",
    HISTORY: "the shared prompt history, history.jsonl (Read + Edit)",
    CREDENTIALS: "the shared OAuth credential, .credentials.json (Read + Edit)",
    ACCOUNT_CONFIG: "the account config, .config.json* and .claude.json* (Read + Edit)",
    ENV: "every .env tier: host, root, each fleet and each bot, its own included"
    " (Read + Edit)",
    INSTALL_ROOT: "the installed package's code and assets, release store and"
    " retained release targets (Edit only)",
    TELEGRAM: "other bots' Telegram state dirs, token included (Read + Edit)",
    CONFIG_SURFACES: "the shared config that runs or instructs in every session:"
    " settings, CLAUDE.md, hooks/, skills/, plugins/, agents/,"
    " commands/ (Edit only)",
}

#: What ``isolation.exempt`` accepts. An exemption only ever restores a READ,
#: never a write, so no entry here lets a bot change what another bot reads.
EXEMPTIONS: dict[str, str] = {
    ACCOUNT_CONFIG: "Read of the account config, for a platform bot's trust and"
    " flag diagnostics",
    "env_host": "Read of the host-tier ~/.env and ~/.env.*, for a fleet whose own"
    " overlay still tells its bot to source it",
}

CONFIG_SURFACE_FILES = ("settings.json", "settings.local.json", "CLAUDE.md")
CONFIG_SURFACE_DIRS = ("hooks", "skills", "plugins", "agents", "commands")

#: A bot name safe to drop into a glob as a literal path segment.
_NAME_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def transcript_slug(path: object) -> str:
    """The name Claude Code gives a session's transcript dir: the session's cwd
    with every non-alphanumeric character replaced by ``-``.

    The one Python copy. ``claudlobby/_runtime_scripts/transcript-digest.sh`` carries the same rule as
    its fallback (its primary input is the hook payload's ``transcript_path``)
    and cross-references this one. A copy that drifted would aim every row-A
    rule at directories that do not exist, and nothing would say so."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def expand_home(value: str, home: Path) -> Path | None:
    """*value* made absolute against *home*: a leading ``~``, ``$HOME`` or
    ``${HOME}`` becomes *home*. None when the result is still relative, since a
    ``//`` rule needs an absolute path (a relative one anchors at the settings
    source instead, #1312)."""
    for prefix in ("${HOME}", "$HOME", "~"):
        if value == prefix:
            return home
        if value.startswith(prefix + "/"):
            return home / value[len(prefix) + 1 :]
    path = Path(value)
    return path if path.is_absolute() else None


@dataclass(frozen=True)
class HostBot:
    """One bot on the host, as the rules need it."""

    fleet: str
    name: str
    bot_dir: Path
    handle: str | None  #: its Telegram handle, None for a bot with no channel
    config_dir: Path | None  #: its expanded account dir; None if not absolute


@dataclass(frozen=True)
class Roster:
    """Every bot on the host, plus what could not be read."""

    bots: tuple[HostBot, ...]
    fleet_dirs: tuple[Path, ...]  #: every overlay fleet dir (row E's fleet tier)
    unreadable: tuple[str, ...]  #: manifests whose bots are missing from ``bots``


@dataclass(frozen=True)
class Rule:
    cls: str
    tool: str  #: "Read" or "Edit"
    path: str  #: absolute, may carry a glob
    part: str = ""  #: a finer name an exemption can target (row E's host tier)
    subject: str = ""  #: the bot the rule names, where it names one bot

    @property
    def text(self) -> str:
        # `path` is absolute, so this is the `//` form: the only one with a
        # no-rule control behind it here (#1312). The tilde form is unmeasured.
        return f"{self.tool}(/{self.path})"


@dataclass(frozen=True)
class Layer0b:
    rules: tuple[Rule, ...]
    #: every place the rule set is narrower than the roster, or might reach
    #: this bot's own content, said rather than silently dropped
    notes: tuple[str, ...]

    @property
    def deny(self) -> list[str]:
        return [r.text for r in self.rules]


def host_bot(bot: BotConfig, fleet: FleetConfig, paths: Paths, home: Path) -> HostBot:
    from .composer import account_dir, telegram_handle

    return HostBot(
        fleet=fleet.name,
        name=bot.bot_id,
        bot_dir=paths.bot_runtime(bot.bot_id),
        handle=telegram_handle(bot),
        config_dir=expand_home(account_dir(bot, fleet), home),
    )


def host_roster(fleet: FleetConfig, paths: Paths, *, home: Path) -> Roster:
    """This fleet from the config being composed, and every other fleet on the
    host from its own manifest. Nobody is excluded here: excluding self is the
    rule builder's job."""
    import yaml

    from .config import load_fleet
    from .paths import Paths as _Paths
    from .paths import _iter_fleet_dirs

    bots = [host_bot(b, fleet, paths, home) for b in fleet.bots.values()]
    fleet_dirs: list[Path] = [paths.fleet_dir] if paths.fleet_dir else []
    unreadable: list[str] = []

    own = paths.fleet_dir.resolve() if paths.fleet_dir else None
    others: list[tuple[Path | None, Path]] = []
    root_manifest = paths.root / "fleet.yaml"
    if paths.fleet_dir is not None and root_manifest.is_file():
        others.append((None, root_manifest))  # a root-mode fleet beside overlays
    for fleet_dir in _iter_fleet_dirs(paths.root / "local"):
        manifest = fleet_dir / "fleet.yaml"
        if manifest.is_file() and fleet_dir.resolve() != own:
            others.append((fleet_dir, manifest))

    for fleet_dir, manifest in others:
        if fleet_dir is not None:
            fleet_dirs.append(fleet_dir)
        try:
            other, _defaults = load_fleet(manifest)
        # A sibling's broken manifest must not stop this generate, and must not
        # be swallowed either: it is returned, and every reader says it. Narrow
        # to what malformed DATA raises, so a programming error still surfaces
        # (compose_host_bot_handles learned this with a swallowed NameError).
        except (
            OSError,
            yaml.YAMLError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            unreadable.append(str(manifest))
            continue
        other_paths = _Paths(root=paths.root, fleet_dir=fleet_dir, package=paths.package)
        bots.extend(host_bot(b, other, other_paths, home) for b in other.bots.values())
    return Roster(tuple(bots), tuple(fleet_dirs), tuple(unreadable))


def _unique(items: Iterable) -> list:
    return list(dict.fromkeys(items))


def layer0b(
    bot: BotConfig,
    fleet: FleetConfig,
    paths: Paths,
    *,
    home: Path | None = None,
    roster: Roster | None = None,
) -> Layer0b:
    """Every Layer 0b rule for *bot*, and a note wherever the set falls short.

    Computed whatever the switch says: the composer emits it only when the
    bot's ``isolation.shared_config`` is on, and ``freshbox`` compares a bot's
    composed deny list against it."""
    home = home if home is not None else Path.home()
    roster = roster if roster is not None else host_roster(fleet, paths, home=home)
    me = host_bot(bot, fleet, paths, home)
    root = paths.root
    rules: list[Rule] = []
    notes = [
        f"{m} could not be read, so its bots are named by no transcript,"
        " .env or Telegram rule"
        for m in roster.unreadable
    ]

    def both(cls: str, path: str, part: str = "", subject: str = "") -> None:
        rules.append(Rule(cls, "Read", path, part, subject))
        rules.append(Rule(cls, "Edit", path, part, subject))

    my_dir = me.bot_dir.resolve()
    others = [o for o in roster.bots if o.bot_dir.resolve() != my_dir]

    # A. Other bots' transcripts, keyed on the NAME: the `*` absorbs the fleet
    # part of the slug under any layout, so one pair of rules covers the bot's
    # old dir and its new one after a move. Per bot, never a blanket
    # `projects/**`: deny always wins and a `!` carve-out cannot reach a
    # `//`-anchored rule, so "everything but mine" is not expressible.
    root_slug = transcript_slug(root)
    my_slug = transcript_slug(me.name)
    for o in others:
        if o.config_dir is None:
            notes.append(
                f"{o.name}: its account dir is not absolute, so its"
                " transcripts are named by no rule"
            )
            continue
        proj = o.config_dir / "projects"
        o_slug = transcript_slug(o.name)
        if o_slug == my_slug:
            # #526: another fleet's bot has this bot's name (or one that
            # slugs the same). A name-keyed rule would deny this bot its own
            # sessions, so fall back to that bot's PATH, which a move of that
            # bot makes stale. A half-finished move looks exactly like this.
            base = transcript_slug(o.bot_dir)
            notes.append(
                f"{o.fleet}/{o.name} shares this bot's name, so its"
                " transcripts are denied by path, not by name, and a"
                " move of it leaves them uncovered until this fleet"
                " regenerates"
            )
            both(TRANSCRIPTS, f"{proj}/{base}/**", subject=o.name)
            both(TRANSCRIPTS, f"{proj}/{base}-*/**", subject=o.name)
            continue
        base = f"{root_slug}*-runtime-bots-{o_slug}"
        both(TRANSCRIPTS, f"{proj}/{base}/**", subject=o.name)
        if my_slug.startswith(o_slug + "-"):
            notes.append(
                f"no rule for sessions {o.name} starts in a"
                f" subdirectory: that pattern would also match this"
                f" bot's own sessions ('{me.name}' begins with"
                f" '{o.name}-')"
            )
        else:
            both(TRANSCRIPTS, f"{proj}/{base}-*/**", subject=o.name)
        if o_slug.startswith(my_slug + "-"):
            sub = o_slug[len(my_slug) + 1 :]
            notes.append(
                f"'{o.name}' begins with '{me.name}-', so a session this"
                f" bot starts in its own subdirectory '{sub}' (or below"
                f" it) is denied to it as if it were {o.name}'s"
            )

    # B, C, D and H: once per distinct config dir on the host, this bot's own
    # included, since they are shared by every bot that uses the dir.
    config_dirs = _unique(b.config_dir for b in [me, *roster.bots] if b.config_dir)
    for cfg in config_dirs:
        both(HISTORY, f"{cfg}/history.jsonl")
        both(CREDENTIALS, f"{cfg}/.credentials.json")
        # Both layouts claude_config_json() knows: `.config.json` in the dir
        # when CLAUDE_CONFIG_DIR is unset, `.claude.json` when it is set.
        both(ACCOUNT_CONFIG, f"{cfg}/.config.json*")
        both(ACCOUNT_CONFIG, f"{cfg}/.claude.json*")
        for name in CONFIG_SURFACE_FILES:
            rules.append(Rule(CONFIG_SURFACES, "Edit", f"{cfg}/{name}"))
        for name in CONFIG_SURFACE_DIRS:
            rules.append(Rule(CONFIG_SURFACES, "Edit", f"{cfg}/{name}/**"))
    both(ACCOUNT_CONFIG, f"{home}/.claude.json*")  # the legacy store in the home dir

    # E. Every .env tier. The bot's OWN included: its values are already in
    # the session env at boot, and a skill that re-reads the file through the
    # model's tools puts that read in the transcript.
    both(ENV, f"{home}/.env", "env_host")
    both(ENV, f"{home}/.env.*", "env_host")
    # `.env.bak*`, not `.env.*`: the root holds the tracked `.env.example`.
    both(ENV, f"{root}/.env", "env_root")
    both(ENV, f"{root}/.env.bak*", "env_root")
    for fleet_dir in _unique(roster.fleet_dirs):
        both(ENV, f"{fleet_dir}/.env", "env_fleet")
        both(ENV, f"{fleet_dir}/.env.*", "env_fleet")
    root_resolved = root.resolve()
    for name in _unique(b.name for b in [me, *roster.bots]):
        if not _NAME_SAFE.match(name):
            notes.append(
                f"'{name}' is not a plain path segment, so its .env is"
                " named by no rule"
            )
            continue
        both(ENV, f"{root}/**/runtime/bots/{name}/.env", "env_bot", name)
        both(ENV, f"{root}/**/runtime/bots/{name}/.env.*", "env_bot", name)
    for b in [me, *roster.bots]:
        # A bot dir outside the install (a fleet in a vault elsewhere) is
        # beyond the name-keyed pattern, so name its files by path.
        if not b.bot_dir.resolve().is_relative_to(root_resolved):
            both(ENV, f"{b.bot_dir}/.env", "env_bot", b.name)
            both(ENV, f"{b.bot_dir}/.env.*", "env_bot", b.name)

    # F. The selected package and every retained release, including recovery.
    # Share the writable-path guard's owner so a release alias and its resolved
    # target stay protected together; data/source overlays remain writable.
    # Edit only: Read must remain available for platform diagnostics.
    for immutable_root in paths.immutable_roots:
        rules.append(Rule(INSTALL_ROOT, "Edit", f"{immutable_root}/**"))

    # G. Other bots' Telegram state dirs, keyed by handle.
    from .composer import telegram_channel_rel

    for o in others:
        if not o.handle:
            continue
        if o.handle == me.handle:
            notes.append(
                f"{o.fleet}/{o.name} shares this bot's Telegram handle,"
                " so its state dir is named by no rule"
            )
            continue
        both(TELEGRAM, f"{home}/{telegram_channel_rel(o.handle)}/**", subject=o.name)

    exempt = bot.isolation.exempt
    if exempt:
        rules = [
            r
            for r in rules
            if not (r.tool == "Read" and (r.cls in exempt or r.part in exempt))
        ]
    seen: set[str] = set()
    unique_rules = []
    for rule in rules:
        if rule.text not in seen:
            seen.add(rule.text)
            unique_rules.append(rule)
    return Layer0b(tuple(unique_rules), tuple(_unique(notes)))



# ---------------------------------------------------------------------------
# text that tells the model to read ~/.env itself
# ---------------------------------------------------------------------------

#: The home directory in each spelling a shell or a person writes it: `~` (or
#: `~user`), `$HOME` and `${HOME}` (quoted whole, or quoted up to the slash),
#: and the absolute forms under the conventional roots, a placeholder user
#: included (`/home/<user>`). Row E denies the file under every one of them.
_HOME_DIR = (
    r"(?:~[A-Za-z0-9._-]*|\$HOME|\$\{HOME\}"
    r"|/home/[^/\s\"'`]+|/Users/[^/\s\"'`]+|/root)"
)
#: `source <it>` or `. <it>` at a command position: the start of the line or
#: span, or after a separator or a keyword that begins a command.
_COMMAND = r"(?:^|[;&|({]|\b(?:then|do|else|exec)\b)\s*(?:source|\.)\s+[\"']?"
_ENV_FILE = r"[\"']?/\.env(?:\.[A-Za-z0-9._-]+)?[\"']?(?=$|[\s;&|)}])"


@functools.lru_cache(maxsize=8)
def _env_read_re(home: str | None = None) -> re.Pattern:
    """The read, recognising *home* by its absolute path too, since a home
    outside the conventional roots is still the home row E denies."""
    dirs = _HOME_DIR if not home else f"(?:{_HOME_DIR}|{re.escape(home)})"
    return re.compile(_COMMAND + dirs + _ENV_FILE)


ENV_READ = _env_read_re()

_SPAN = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"\s*(`{3,}|~{3,})")
#: Where a clause ends in prose. A negation in another clause of the line
#: ("If X is not set, run `...`", "Run `...` — without it ...") does not
#: reach the span.
_CLAUSE = re.compile(r"[.;:,!?()\[\]—–]|\s-\s")
_WORDS = re.compile(r"[a-z]+(?:'[a-z]+)?")
#: How far back a negation reaches: the words directly before the span.
_NEGATION_REACH = 3
_NEGATION = re.compile(
    r"\b(?:no longer|do not|does not|instead of|rather than|used to|never|"
    r"not|no|don't|dont|cannot|can't|avoid|without)\b"
)
#: A verb that turns a negation back into an instruction: "Do not skip
#: `source ~/.env`" asks for the read.
_REVERSAL = re.compile(
    r"\b(?:skip|skipping|skipped|forget|forgetting|forgotten|omit|omitted|"
    r"miss|missed|neglect|neglected|fail|failed|overlook|overlooked)\b"
)
#: The span as the subject of a negated predicate ("`source ~/.env` is not
#: needed") is a mention too: the one negation that follows the span.
_NEGATED_SUBJECT = re.compile(
    r"\s*(?:(?:is|are|was|does|do|did|will|should|must|need|needs|can)"
    r"\s+(?:not|no longer|never)|isn't|aren't|doesn't|don't|won't|shouldn't|"
    r"mustn't|can't|cannot)\b"
)


def _prose(text: str) -> str:
    """*text* lower-cased, with any code span standing in as one neutral word."""
    return _SPAN.sub(" code ", text).lower().replace("’", "'")


def _negated_before(before: str) -> bool:
    """Whether the words directly before a span negate it: only the span's own
    clause, only its last few words, and a reversal ("Do not skip") undoes it."""
    words = _WORDS.findall(_CLAUSE.split(_prose(before))[-1])[-_NEGATION_REACH:]
    near = " ".join(words)
    last = None
    for last in _NEGATION.finditer(near):
        pass
    return last is not None and not _REVERSAL.search(near[last.end():])


def _negated_after(after: str) -> bool:
    """Whether the span's own clause goes on with a negated verb, making the
    span its subject: `` `X` is not needed``, `` `X` isn't required``. A
    reversal in the rest of the clause undoes it, as it does before the span:
    `` `X` cannot be skipped`` asks for the read."""
    clause = _CLAUSE.split(_prose(after))[0]
    negated = _NEGATED_SUBJECT.match(clause)
    return bool(negated) and not _REVERSAL.search(clause[negated.end():])


def env_reads(text: str, *, home: Path | str | None = None) -> list[tuple[int, str]]:
    """``(line number, line)`` for every line of markdown *text* that tells the
    model to load the home ``.env`` itself, the read row E denies. Pass *home*
    to recognise its absolute spelling wherever the home lives.

    A command, not a mention. Every line of a fenced code block is a command,
    unless the read sits in a comment or inside another command's argument
    (``echo "source ~/.env"``). Outside a fence only an inline code span can
    be one, and it is a mention when the words DIRECTLY before it negate it
    ("Never `source ~/.env`") or it is the subject of a negated predicate
    ("`source ~/.env` is not needed"). A negation elsewhere on the line does not
    count, and a double negative ("Do not skip `source ~/.env`") is an
    instruction. A span holding only the path, or ``source`` as a prose word,
    is a mention. A command that loads the file some other way (``cat``,
    ``export $(...)``) is not recognised."""
    read = _env_read_re(str(home) if home is not None else None)
    hits: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and opener.group(1).startswith(fence):
                fence = None
            elif read.search(line.strip()):
                hits.append((number, line.strip()))
            continue
        if opener:
            fence = opener.group(1)
            continue
        for span in _SPAN.finditer(line):
            if not read.search(span.group(1).strip()):
                continue
            if _negated_before(line[:span.start()]) or _negated_after(line[span.end():]):
                continue
            hits.append((number, line.strip()))
            break
    return hits
