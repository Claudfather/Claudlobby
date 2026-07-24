"""Path-ownership audit — the compositor's guarantee that fleet wiring carries no
flat, dangling, or otherwise-improper absolute path.

Two layers share this module so their rules can never drift:

* **L1, source-side (deny-by-default)** — :func:`classify_source_value` scans
  *source* values (fleet.yaml, library / MCP fragments) before anything is
  emitted, and denies ANY absolute path that is neither expressed against a
  composer anchor (FLEET_ROOT / BOT_DIR / CLAUDLOBBY_ROOT) nor blessed by an
  ``external_paths`` declaration (:func:`match_external`). A foreign absolute
  never reaches emission.
* **L2, emitted-side** — :func:`improper_fleet_paths` scans *emitted* wiring for
  a fleet-owned absolute that fails to resolve inside the fleet overlay (a flat
  husk, a cross-fleet leak, a foreign-rooted stale).

The generate-time guard (composer) and the freshbox self-containment audit both
call in here. This extends the fresh-box self-containment contract to cover
PATHS, the same shape it already covers for permissions: the compositor
*derives* correct, self-contained wiring rather than trusting hand-written
absolute inputs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import BotConfig, FleetConfig
    from .paths import Paths

# Path anchors the composer exports into bot.conf. A ${VAR} in an MCP fragment,
# or a $VAR in bot.conf, that names one of these resolves — at runtime for
# .mcp.json, at source time for bot.conf — to a composer-derived, migration-safe
# absolute path. They are the blessed way to express an in-fleet absolute path in
# a compose source, so that a raw absolute fleet path stands out as the
# dangling-path smell the guard rejects.
#   CLAUDLOBBY_ROOT — the install root (paths.root)
#   FLEET_ROOT      — the fleet overlay root (paths.fleet_config_dir)
#   BOT_DIR         — this bot's runtime dir (paths.bot_runtime(bot_id))
COMPOSER_PROVIDED_PATH_ANCHORS: tuple[str, ...] = (
    "CLAUDLOBBY_ROOT",
    "FLEET_ROOT",
    "BOT_DIR",
)


@dataclass(frozen=True)
class PathFinding:
    """One improper absolute fleet path in an emitted wiring file."""

    bot_id: str
    file: str  # bot-dir-relative filename
    path: str  # the offending absolute path (anchors already resolved)
    reason: str


# A crude absolute-path token: a run starting with "/" up to whitespace or a
# common delimiter. `<` and `>` delimit too — they cannot occur in a real path,
# so they mark the boundary between an XML tag and a path in a launchd plist
# (``</key><string>/real/path</string>``), keeping the closing tag out of the
# extracted token. Good enough for the machine-generated wiring files scanned
# here (bot.conf, .mcp.json, unit files).
_ABS_TOKEN_RE = re.compile(r"/[^\s'\":;,<>]+")

# Bot-dir-relative wiring files whose absolute paths must resolve for the bot to
# run. Prose (CLAUDE.md) is intentionally excluded — a stale path there does not
# break wiring and the text legitimately carries example paths.
_WIRING_STATIC = ("bot.conf", ".mcp.json", ".claude/settings.local.json")


def _anchor_values(bot: BotConfig, paths: Paths) -> dict[str, str]:
    """Map each composer-provided path anchor to its resolved absolute value."""
    return {
        "CLAUDLOBBY_ROOT": str(paths.root),
        "FLEET_ROOT": str(paths.fleet_config_dir),
        "BOT_DIR": str(paths.bot_runtime(bot.bot_id)),
    }


def _resolve_anchor_tokens(text: str, anchor_values: dict[str, str]) -> str:
    """Expand ``${ANCHOR}`` / ``$ANCHOR`` to the anchor's absolute value, so a path
    written against a blessed anchor is checked at its real resolved location.
    Order-independent: ``${NAME}`` is brace-closed and ``$NAME`` is boundary-
    delimited, so no anchor can partially match inside another."""
    for name, val in anchor_values.items():
        text = text.replace("${" + name + "}", val)
        text = re.sub(r"\$" + re.escape(name) + r"(?![A-Za-z0-9_])", val, text)
    return text


def _fleet_content_roots(paths: Paths) -> list[str]:
    """Trees under which fleet-owned content lives — the ``local/`` overlay and,
    in vault mode, the vault. An absolute path under one of these must resolve
    inside the fleet's own overlay root."""
    roots = [str(paths.root / "local")]
    if paths.vault_root is not None:
        roots.append(str(paths.vault_root))
    return roots


def _fleet_layout_needles(paths: Paths) -> list[str]:
    """Slash-bounded fragments that mark a path as this fleet's own content by
    *shape* — what a fleet-owned path keeps even when it is written against a
    foreign install root, which a prefix test against this install's roots
    (``_fleet_content_roots``) cannot recognize.

    A path is fleet-owned by shape when it contains, at segment boundaries:

    * this fleet's overlay dir at its real depth (``…/local/home/tl/…``),
      derived from the fleet config dir so it tracks the layout rather than
      restating it
    * a flat ``local/<fleet>`` overlay — a flat-layout fleet, or a leftover husk
      of one that lives deeper (kept as its own fragment so a flat path is
      caught even when this fleet is nested)
    * a bot runtime tree (``…/runtime/bots/…``)

    Each fragment is gated on this fleet's own directory name or the runtime
    marker, so a bare ``local`` segment (``/usr/local/bin``) or a package token
    never matches; the leading/trailing ``/`` enforce the segment boundary.
    """
    fleet_cfg = paths.fleet_config_dir
    needles = {f"/local/{fleet_cfg.name}/", "/runtime/bots/"}
    if fleet_cfg.is_relative_to(paths.root):
        rel = fleet_cfg.relative_to(paths.root).parts
        if rel:
            needles.add("/" + "/".join(rel) + "/")
    return sorted(needles)


def _traverses_fleet_layout(path: str, needles: list[str]) -> bool:
    """True if *path* contains any fleet-layout fragment at a segment boundary."""
    hay = path + "/"
    return any(n in hay for n in needles)


def improper_fleet_paths(
    text: str, bot: BotConfig, paths: Paths
) -> list[tuple[str, str]]:
    """Return ``[(path, reason)]`` for improper absolute fleet paths in *text*.

    After resolving composer path anchors, an absolute path is improper when it
    is fleet-owned — under a fleet-content root, or shaped like this fleet's own
    overlay / bot-runtime layout at any root — yet does NOT resolve inside
    ``paths.fleet_config_dir``. That covers a flat or dangling husk, a cross-fleet
    leak, and a stale absolute hand-typed against a foreign install root that
    dangles the moment the fleet runs elsewhere. A nested-correct absolute path
    (what the composer itself emits, e.g. FLEET_MISSION_FILE) is fine; the rule
    is correctness, not "no absolutes".
    """
    resolved = _resolve_anchor_tokens(text, _anchor_values(bot, paths))
    content_roots = _fleet_content_roots(paths)
    layout_needles = _fleet_layout_needles(paths)
    fleet_root = str(paths.fleet_config_dir)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _ABS_TOKEN_RE.finditer(resolved):
        p = m.group(0).rstrip("/.,:;\"')}")
        if p in seen:
            continue
        under_content_root = any(
            p == r or p.startswith(r + os.sep) for r in content_roots
        )
        if not (under_content_root or _traverses_fleet_layout(p, layout_needles)):
            continue  # not fleet-owned (system path, $HOME, /tmp, package token, …)
        norm = os.path.normpath(p)
        if norm == fleet_root or norm.startswith(fleet_root + os.sep):
            continue  # resolves inside the fleet's real overlay root — correct
        seen.add(p)
        out.append(
            (
                p,
                f"absolute fleet path that does not resolve inside the fleet "
                f"overlay root {fleet_root} — a flat, dangling, or foreign-rooted "
                "layout, or a cross-fleet leak",
            )
        )
    return out


def _wiring_files(bot: BotConfig, fleet: FleetConfig) -> list[str]:
    return [
        *_WIRING_STATIC,
        f"{fleet.service_prefix}.{bot.bot_id}.service",
        f"{fleet.service_prefix}.{bot.bot_id}.plist",
    ]


def audit_bot_paths(
    bot: BotConfig, fleet: FleetConfig, paths: Paths
) -> list[PathFinding]:
    """Scan a bot's emitted wiring files for improper absolute fleet paths."""
    bot_dir = paths.bot_runtime(bot.bot_id)
    findings: list[PathFinding] = []
    for rel in _wiring_files(bot, fleet):
        try:
            text = (bot_dir / rel).read_text()
        except (OSError, UnicodeDecodeError):
            continue  # file absent (e.g. no .mcp.json) or binary — nothing to scan
        for path, reason in improper_fleet_paths(text, bot, paths):
            findings.append(PathFinding(bot.bot_id, rel, path, reason))
    return findings


def assert_bot_paths(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> None:
    """Fail loudly if any emitted wiring file carries an improper fleet path.

    The generate-time half of the path-ownership guarantee: a hand-typed flat or
    dangling absolute fleet path in any compose source surfaces here as a hard
    error, never a silent dangle.
    """
    findings = audit_bot_paths(bot, fleet, paths)
    if not findings:
        return
    detail = "\n".join(f"  {f.file}: {f.path}\n      {f.reason}" for f in findings)
    raise ValueError(
        f"bot {bot.bot_id!r}: improper absolute fleet path(s) in composed wiring — "
        "derive the path from a composer anchor (FLEET_ROOT / BOT_DIR / "
        "CLAUDLOBBY_ROOT), never hand-type it:\n" + detail
    )


# ─────────────────────────────────────────────────────────────────────────────
# L1 — source-side deny-by-default guard (#702)
#
# The L2 predicate above scans *emitted* wiring for a fleet-owned path that fails
# to resolve. L1 fires earlier and stricter: it classifies *source* values and
# denies EVERY absolute path that is not either expressed against a composer
# anchor or blessed by an ``external_paths`` declaration — so a foreign absolute
# is stopped at the source, before it can be emitted.
#
# The grammar is head-anchored per value: it classifies what a single word
# *starts* with and never substring-mines free text. That keeps a prose-capable
# value (a startup prompt, an env option-string) from being scraped for the
# incidental path it mentions — only a value that leads with a path, or one of
# the three whitespace-split fields, is denied.
# ─────────────────────────────────────────────────────────────────────────────

# A URL scheme head (``https://``, ``postgresql://``, ``file://``): a value that
# leads with one is a URL, not a path — except ``file://``, whose path part is
# still classified so ``file:///Users/x`` cannot smuggle an absolute past L1.
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

# A ``${VAR}`` / ``$VAR`` head. Any env-var reference at the head of a value
# passes the grammar: a composer anchor (FLEET_ROOT / BOT_DIR / CLAUDLOBBY_ROOT)
# resolves to an in-fleet path, and a plain env ref (``${GITHUB_PAT}``) is not a
# path at all. Whether the *surface* actually expands the anchor at the right
# time — anchor-liveness — is an audit-layer concern, not this pure grammar's.
_VAR_HEAD_RE = re.compile(r"^\$\{?[A-Za-z_]")

# A ``KEY=rhs`` / ``--flag=rhs`` head: an assignment whose right-hand side is the
# thing to classify (an env-var name or a CLI flag on the left).
_FLAG_ASSIGN_RE = re.compile(
    r"^(?:--?[A-Za-z0-9][\w.\-]*|[A-Za-z_]\w*)=(.*)$", re.DOTALL
)


def _classify_atom(token: str) -> list[str]:
    """Rules 2-3 on a single indivisible token: a ``${VAR}`` / ``$VAR`` head
    passes; a ``/`` or ``~`` head is an absolute path (denied unless later
    blessed); anything else — a relative token, a bare flag, a package name —
    passes."""
    if _VAR_HEAD_RE.match(token):
        return []
    if token[:1] in ("/", "~"):
        return [token]
    return []


def _classify_1_3(word: str) -> list[str]:
    """Rules 1-3 — the URL-scheme carve-out over :func:`_classify_atom`, and the
    one home of the ``file://`` decision. Used for a URL-headed word, a bare
    word, and a ``KEY=``/``--flag=`` right-hand side (rule 4)."""
    m = _URL_SCHEME_RE.match(word)
    if m:
        scheme = m.group(0)
        if scheme.lower() == "file://":
            return _classify_atom(word[len(scheme) :])
        return []  # any other scheme is a URL, not a path
    return _classify_atom(word)


def _classify_word(word: str) -> list[str]:
    """Rules 1-5 on a single whitespace-free word: a URL carve-out (1), then a
    ``KEY=``/``--flag=`` assignment classifies its rhs by 1-3 (4), then a colon
    list classifies each segment by 2-3 (5), else the word itself by 1-3.

    The URL check comes first so a connection string's ``:`` (``postgres://…:port``)
    is never mistaken for a PATH-style colon list."""
    if _URL_SCHEME_RE.match(word):
        return _classify_1_3(word)  # rule 1, incl. the file:// carve-out
    assign = _FLAG_ASSIGN_RE.match(word)
    if assign:
        return _classify_1_3(assign.group(1))
    if ":" in word:
        denied: list[str] = []
        for segment in word.split(":"):
            denied.extend(_classify_atom(segment))
        return denied
    return _classify_atom(word)


def classify_source_value(value: str, *, word_split: bool = False) -> list[str]:
    """Return the absolute paths in a compose *source* value that are neither
    anchored nor URL-excluded — the candidates the audit layer denies unless an
    ``external_paths`` declaration blesses them. An empty list means the value is
    clean.

    Rules 1-5 (see the helpers) classify a value by its head and never
    substring-scan. Only the three word-split fields — ``hooks[].command``,
    ``extra_flags`` entries, ``autonomous_runner.args`` — pass
    ``word_split=True`` to split on whitespace first, then classify each token by
    1-5 (rule 6). The residual — an embedded path in a non-split value — is
    deliberate; the L2 emitted-path guard catches the fleet-shaped ones.
    """
    words = value.split() if word_split else [value]
    denied: list[str] = []
    for word in words:
        denied.extend(_classify_word(word))
    return denied


@dataclass(frozen=True)
class ExternalDecl:
    """One blessed external path outside the fleet overlay — a genuine dependency
    (a mount source, a system tool tree) a compose source may reference by
    absolute path. ``purpose`` is required and verifiable: a YAML comment is
    invisible to the guard, so the justification lives in the schema itself."""

    path: str
    purpose: str


def parse_external_decls(raw: object) -> list[ExternalDecl]:
    """Validate and build the ``external_paths`` declarations, rejecting the
    shapes that would turn a declaration into a silent over-grant.

    Each entry is a mapping with an absolute ``path`` and a non-empty
    ``purpose``. A trailing ``/**`` blesses a whole subtree, but only on a
    segment boundary and only below a breadth floor of two leading path segments
    — never ``/**``, ``/opt/**``, or another root-adjacent width. ``~``, relative
    paths, ``..``, and a non-tail ``*`` are all rejected.
    """
    if not isinstance(raw, list):
        raise ValueError(
            "external_paths must be a list of {path, purpose} mappings, got "
            f"{type(raw).__name__}"
        )
    decls: list[ExternalDecl] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(
                "external_paths entry must be a mapping with 'path' and 'purpose', "
                f"got {entry!r}"
            )
        path = entry.get("path")
        purpose = entry.get("purpose")
        if not isinstance(path, str) or not path:
            raise ValueError(
                f"external_paths entry needs a non-empty 'path': {entry!r}"
            )
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError(
                f"external_paths[{path!r}] needs a non-empty 'purpose' — the "
                "justification is a verifiable schema field, not a YAML comment"
            )
        if not path.startswith("/"):
            raise ValueError(
                f"external_paths[{path!r}] must be an absolute path — declare the "
                "expanded form (a '~' or relative path is not accepted)"
            )
        is_glob = path.endswith("/**")
        core = path[:-3] if is_glob else path
        if "*" in core:
            raise ValueError(
                f"external_paths[{path!r}]: '**' is only allowed as a single "
                "trailing '/**' segment"
            )
        segments = [s for s in core.split("/") if s]
        if ".." in segments:
            raise ValueError(f"external_paths[{path!r}] must not contain '..'")
        if is_glob and len(segments) < 2:
            raise ValueError(
                f"external_paths[{path!r}] is too broad — a '/**' subtree needs at "
                "least two leading path segments (never '/**' or '/<top>/**')"
            )
        decl = ExternalDecl(path=path, purpose=purpose)
        if decl not in decls:
            decls.append(decl)
    return decls


def match_external(path: str, decls: list[ExternalDecl]) -> bool:
    """True if *path* is blessed by a declaration: an exact match, or — for a
    ``/**`` declaration — the declared prefix itself or any path below it on a
    segment boundary. ``/var/lib/printify/**`` blesses ``/var/lib/printify`` and
    ``/var/lib/printify/data/x`` but never the sibling ``/var/lib/printify-secret``.
    A non-glob declaration blesses only its exact path. No general glob."""
    for decl in decls:
        if decl.path.endswith("/**"):
            prefix = decl.path[:-3]
            if path == prefix or path.startswith(prefix + "/"):
                return True
        elif path == decl.path:
            return True
    return False


def denied_source_paths(
    value: str, decls: list[ExternalDecl], *, word_split: bool = False
) -> list[str]:
    """The absolute paths in *value* that :func:`classify_source_value` surfaces
    and no declaration blesses — the deny set for one source value. The single
    call every choke-site funnels through, so ``classify`` + ``match_external``
    are the sole grammar SSOT."""
    return [
        p
        for p in classify_source_value(value, word_split=word_split)
        if not match_external(p, decls)
    ]


# A ``Tool(spec)`` permission grant — a leading tool name and a parenthesised
# argument, e.g. ``Read(/abs)``, ``Bash(python3 /abs/x)``, ``Edit(${FLEET_ROOT}/x)``.
# A bare tool (``Edit``) or an mcp glob (``mcp__github__*``) has no ``(spec)`` and
# is never a path.
_GRANT_SPEC_RE = re.compile(r"^[A-Z][A-Za-z0-9]*\((.*)\)$", re.DOTALL)


def classify_grant_paths(grant: str, decls: list[ExternalDecl]) -> list[str]:
    """Denied absolute paths inside a ``Tool(spec)`` grant's argument — the ``/abs``
    in ``Read(/abs)`` or the ``/abs/x`` in ``Bash(python3 /abs/x)``. The spec is
    scanned word-split (a Bash grant is a command line); a bare tool or an mcp
    glob has no spec and is clean. Path-headed grants must anchor or declare like
    any other source value."""
    m = _GRANT_SPEC_RE.match(grant)
    if not m:
        return []
    return denied_source_paths(m.group(1), decls, word_split=True)


def is_anchor_headed(value: str) -> bool:
    """True if *value* leads with a composer path anchor (``${FLEET_ROOT}``,
    ``$BOT_DIR``, …). bot.conf must emit such a value DOUBLE-quoted so the shell
    expands the anchor at source time (R1); the default single-quoted ``_shq``
    sink would freeze it as a literal, leaving the anchor un-expanded and the
    'anchor it' remediation inert. A plain env reference (``${GITHUB_PAT}``) is
    not an anchor and keeps its existing frozen emission."""
    m = re.match(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)", value)
    return bool(m and m.group(1) in COMPOSER_PROVIDED_PATH_ANCHORS)


# ── The posture registry + the dataclass walk ───────────────────────────────
#
# Every string leaf reachable from a BotConfig is CHECKED by default — a new
# field is covered the moment it is added, with no registry edit. _FIELD_POSTURES
# records only the deviations from that deny-by-default baseline, keyed by the
# leaf's top-level BotConfig field (refined by the terminal segment for the two
# structured fields, ``hooks`` and ``autonomous_runner``).


class Posture(Enum):
    """How a source-value leaf is classified."""

    CHECK = "check"  # rules 1-5 — deny an unanchored, undeclared absolute
    CHECK_WORDS = "check_words"  # rule 6 — whitespace-split first, then 1-5
    EXEMPT = "exempt"  # never classified


_FIELD_POSTURES: dict[str, Posture] = {
    # exempt — prose bodies, declared-by-construction paths, and grant strings
    # (the last classified at the settings.local.json choke, not by this walk):
    "startup_prompt": Posture.EXEMPT,  # prose (F7)
    "mounts": Posture.EXEMPT,  # host mount targets — resolve+escape-gated elsewhere
    "external_paths": Posture.EXEMPT,  # the declarations themselves (absolute by design)
    "claudron_vault_path": Posture.EXEMPT,  # declared-by-construction vault path
    "permissions": Posture.EXEMPT,  # Tool(spec) grants — classified at the grant choke
    "tool_permissions": Posture.EXEMPT,  # Tool(spec) grants — classified at the grant choke
    "autonomous_runner.skill": Posture.EXEMPT,  # a slash-command ref, not a path
    # word-split (rule 6, F3=b) — the only fields whose value is scanned token by
    # token; everywhere else the grammar stays head-anchored:
    "extra_flags": Posture.CHECK_WORDS,
    "hooks.command": Posture.CHECK_WORDS,
    "autonomous_runner.args": Posture.CHECK_WORDS,
}


def _posture_for(segments: tuple[str, ...]) -> Posture:
    """The posture for a leaf, from its walk *segments* (dataclass field names and
    dict keys, list indices dropped). The two structured fields refine by their
    terminal segment; every unlisted field is CHECK (deny-by-default coverage)."""
    if not segments:
        return Posture.CHECK
    top = segments[0]
    if top in ("hooks", "autonomous_runner"):
        # a hook entry's non-command keys (type, matcher) are not shell commands
        default = Posture.EXEMPT if top == "hooks" else Posture.CHECK
        return _FIELD_POSTURES.get(f"{top}.{segments[-1]}", default)
    return _FIELD_POSTURES.get(top, Posture.CHECK)


def _walk_source_leaves(obj: object, segments: tuple[str, ...] = (), display: str = ""):
    """Yield ``(segments, display, value)`` for every ``str`` leaf reachable from
    *obj*, recursing through dataclasses, dicts, and lists. ``segments`` keys the
    posture (list indices omitted); ``display`` is the human path for the finding
    (indices included). Non-str scalars (bool/int/None) are ignored."""
    if is_dataclass(obj) and not isinstance(obj, type):
        for f in fields(obj):
            child = f.name if not display else f"{display}.{f.name}"
            yield from _walk_source_leaves(
                getattr(obj, f.name), segments + (f.name,), child
            )
    elif isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k)
            child = key if not display else f"{display}.{key}"
            yield from _walk_source_leaves(v, segments + (key,), child)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_source_leaves(v, segments, f"{display}[{i}]")
    elif isinstance(obj, str):
        yield segments, display, obj


@dataclass(frozen=True)
class SourceFinding:
    """One denied absolute path in a compose source value (mirrors PathFinding)."""

    bot_id: str
    source: str  # dotted provenance, e.g. "bots.kev.env.GA4_KEY"
    value: str  # the raw source value that carried it
    path: str  # the specific denied absolute path within value
    reason: str


_SOURCE_DENY_REASON = (
    "absolute path in a compose source that is neither anchored on a composer "
    "path nor blessed by an external_paths declaration"
)


def _mcp_fragment_findings(
    bot_id: str, fragments: dict[str, object], decls: list[ExternalDecl]
) -> list[SourceFinding]:
    """Classify the leaves of loaded MCP fragments (choke-site 1). A fragment's
    command / args / env / headers / url values are walked like any nested source
    structure; a ``url`` leaf is a URL by rule 1 (``file://`` still denies its path
    part). The composer loads the fragments and passes them in — path_audit never
    reads the library layout itself."""
    findings: list[SourceFinding] = []
    for name, fragment in fragments.items():
        for _segments, display, value in _walk_source_leaves(fragment):
            for denied in denied_source_paths(value, decls):
                findings.append(
                    SourceFinding(
                        bot_id=bot_id,
                        source=f"library/mcp/{name}.json {display}",
                        value=value,
                        path=denied,
                        reason=_SOURCE_DENY_REASON,
                    )
                )
    return findings


def audit_bot_sources(
    bot: BotConfig,
    fleet: FleetConfig,
    paths: Paths | None = None,
    fragments: object | None = None,
) -> list[SourceFinding]:
    """Scan a bot's *source* values (the deny-by-default L1 guard). Walks every
    string leaf of the BotConfig, applies its posture, and denies any absolute
    path that :func:`classify_source_value` surfaces and no ``external_paths``
    declaration blesses. ``paths``/``fragments`` feed the non-dataclass
    choke-sites (MCP fragments, grants, tool renders, timers).

    Maintenance contract: this guard only protects the sources routed to it. A
    new raw parse/read of a fleet-controlled file (a new ``json.loads`` /
    ``yaml.safe_load``) is a new source surface — wire it here or bless it, or
    the guard-the-guard tripwire (``tests/test_source_guard_tripwire.py``) fails."""
    decls = list(bot.external_paths)
    findings: list[SourceFinding] = []
    for segments, display, value in _walk_source_leaves(bot):
        posture = _posture_for(segments)
        if posture is Posture.EXEMPT:
            continue
        for denied in denied_source_paths(
            value, decls, word_split=posture is Posture.CHECK_WORDS
        ):
            findings.append(
                SourceFinding(
                    bot_id=bot.bot_id,
                    source=f"bots.{bot.bot_id}.{display}",
                    value=value,
                    path=denied,
                    reason=_SOURCE_DENY_REASON,
                )
            )
    if isinstance(fragments, dict):
        findings.extend(_mcp_fragment_findings(bot.bot_id, fragments, decls))
    return findings


def source_findings_error(bot_id: str, findings: list[SourceFinding]) -> ValueError:
    """Build the field-precise failure for a set of source findings — the shared
    failure-UX for every choke-site (the dataclass walk and the composer's
    fragment / grant / tool / timer sites), so the remediation reads the same
    wherever a denied path is found."""
    detail = "\n".join(
        f"  fleet.yaml {f.source} = {f.value!r}\n      denied absolute path: {f.path}"
        for f in findings
    )
    return ValueError(
        f"bot {bot_id!r}: source value(s) carry an absolute path that is "
        f"neither anchored on a composer path nor declared:\n{detail}\n\n"
        "Fix — pick one:\n"
        "  - anchor it: rewrite against FLEET_ROOT / BOT_DIR / CLAUDLOBBY_ROOT "
        "(e.g. ${FLEET_ROOT}/sub/path) so the composer derives the real location;\n"
        "  - declare it: add an external_paths entry {path, purpose} blessing the "
        "absolute path, for a genuine dependency outside the fleet overlay.\n"
        "Triage: anchor first (in-fleet paths), declare last (true externals); "
        "for a host mount use the bot's mounts: map, not a raw path."
    )


def assert_bot_sources(
    bot: BotConfig,
    fleet: FleetConfig,
    paths: Paths | None = None,
    fragments: object | None = None,
) -> None:
    """Fail loudly if any source value carries an unanchored, undeclared absolute
    path — the source-side half of the guard, run before any output is written so
    a failing bot leaves no partial wiring behind."""
    findings = audit_bot_sources(bot, fleet, paths, fragments)
    if findings:
        raise source_findings_error(bot.bot_id, findings)
