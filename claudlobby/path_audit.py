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
#
# Every composed file that can carry an absolute path belongs here: this list is
# the whole of L2's reach, so an artifact missing from it is not merely unscanned,
# it is invisible — ``freshbox --strict`` reports a fleet self-contained while the
# unlisted file dangles. ``.gitconfig`` (per-org git credential routing) is scanned
# for that reason; its include target and resolved ``gh`` path are external by
# design and are surfaced separately by freshbox's externals report.
_WIRING_STATIC = (
    "bot.conf",
    ".mcp.json",
    ".claude/settings.local.json",
    ".gitconfig",
)


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


def _normalize_rule_path(path: str) -> str:
    """``os.path.normpath`` plus the permission-rule ``//`` prefix (#1312).

    POSIX mandates that a path beginning with EXACTLY two slashes is
    implementation-defined, and ``os.path.normpath`` therefore preserves it —
    ``//tmp/x`` stays ``//tmp/x`` while ``///tmp/x`` collapses to ``/tmp/x``.
    Measured, not assumed.

    That matters here because Claude Code's permission grammar uses a leading
    ``//`` to mean "this path is absolute" (a single slash anchors at the settings
    source). The composer now emits ``Read(//<bot-dir>/**)``, so this auditor sees
    a token whose filesystem meaning is ``/<bot-dir>`` but whose spelling does not
    compare equal to the fleet root — and it flagged the composer's own correct
    output as a foreign-rooted leak.

    Collapsing is the safe direction and that is worth stating rather than
    assuming: it makes ``//<fleet-root>/x`` recognised as inside the fleet
    (correctly unflagged) and leaves ``//<foreign>/x`` flagged exactly as
    ``/<foreign>/x`` already was. No path escapes the audit that did not escape it
    before; one class stops being falsely accused.
    """
    if path.startswith("//") and not path.startswith("///"):
        path = path[1:]
    return os.path.normpath(path)


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
    (what the composer itself emits, e.g. FLEET_MISSION_FILE) is fine; so is the
    fleet's own vault root — bridge-derived (``paths.vault_root``) or bot-declared
    (``claudron_vault_path``, already L1-exempt as declared-by-construction): the
    sanctioned shared parent the fleet belongs to, not a leak. The rule is
    correctness, not "no absolutes".
    """
    resolved = _resolve_anchor_tokens(text, _anchor_values(bot, paths))
    content_roots = _fleet_content_roots(paths)
    layout_needles = _fleet_layout_needles(paths)
    fleet_root = str(paths.fleet_config_dir)
    # The fleet's own sanctioned vault root(s), normalized (fleet_root is not:
    # neither the .claudron bridge string nor the fleet.yaml declaration is
    # guaranteed normalized): bridge-derived (paths.vault_root) and/or
    # bot-declared (claudron_vault_path, L1-exempt as declared-by-construction).
    vault_roots = {
        os.path.normpath(r)
        for r in (paths.vault_root, bot.claudron_vault_path)
        if r
    }
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
        norm = _normalize_rule_path(p)
        if norm == fleet_root or norm.startswith(fleet_root + os.sep):
            continue  # resolves inside the fleet's real overlay root — correct
        if norm in vault_roots:
            # The fleet's own vault root — the sanctioned shared parent it
            # belongs to (bridge-derived or bot-declared; see docstring). Only
            # the root itself is ever blessed, never a subtree.
            continue
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


def _emitted_scan_files(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> list[str]:
    """Bot-dir-relative files whose emitted absolute paths must resolve in-fleet —
    the static wiring plus every rendered ``tools/`` script (F6). A rendered tool is
    compositor-emitted wiring like any other: a fleet-shaped absolute baked into one
    dangles on a fleet move, so it gets the same L2 shape scan (never L1 — a tool
    legitimately runs system binaries like ``/usr/bin/env``)."""
    rels = list(_wiring_files(bot, fleet))
    tools_dir = paths.bot_runtime(bot.bot_id) / "tools"
    if tools_dir.is_dir():
        rels.extend(
            f"tools/{p.name}" for p in sorted(tools_dir.iterdir()) if p.is_file()
        )
    return rels


def audit_bot_paths(
    bot: BotConfig, fleet: FleetConfig, paths: Paths
) -> list[PathFinding]:
    """Scan a bot's emitted wiring files for improper absolute fleet paths."""
    bot_dir = paths.bot_runtime(bot.bot_id)
    findings: list[PathFinding] = []
    for rel in _emitted_scan_files(bot, fleet, paths):
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
    one home of the ``file://`` decision. Used for a URL-headed word and a bare
    word (a ``KEY=``/``--flag=`` rhs recurses the full grammar via
    :func:`_classify_word`)."""
    m = _URL_SCHEME_RE.match(word)
    if m:
        scheme = m.group(0)
        if scheme.lower() == "file://":
            return _classify_atom(word[len(scheme) :])
        return []  # any other scheme is a URL, not a path
    return _classify_atom(word)


def _classify_word(word: str) -> list[str]:
    """Rules 1-5 on a single whitespace-free word: a URL carve-out (1), then a
    ``KEY=``/``--flag=`` assignment re-classifies its rhs by the full grammar (4 →
    1-5, so a ``--flag=name:/abs`` colon list is caught), then a colon list
    classifies each segment by 2-3 (5), else the word itself by 1-3.

    The URL check comes first so a connection string's ``:`` (``postgres://…:port``)
    is never mistaken for a PATH-style colon list."""
    if _URL_SCHEME_RE.match(word):
        return _classify_1_3(word)  # rule 1, incl. the file:// carve-out
    assign = _FLAG_ASSIGN_RE.match(word)
    if assign:
        # rule 4: the rhs is itself a value — recurse through the full grammar so a
        # colon list (rule 5) inside a flag rhs (``--flag=name:/abs/path``) is not
        # a blind spot. The URL check inside keeps ``DATABASE_URL=postgres://…``
        # from colon-splitting.
        return _classify_word(assign.group(1))
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
    ``$BOT_DIR``, …) — a *candidate* for the double-quoted bot.conf emission that
    expands the anchor at source time (R1). This is the loose probe: it scopes the
    emission-safety deny to values that intend an anchor, while the strict
    ``is_safe_anchored_path`` decides whether one is actually safe to emit
    unescaped. A plain env reference (``${GITHUB_PAT}``) is not an anchor."""
    m = re.match(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)", value)
    return bool(m and m.group(1) in COMPOSER_PROVIDED_PATH_ANCHORS)


# The characters a path segment may hold to be emitted, UNESCAPED, inside a
# double-quoted bot.conf value: alphanumerics and the handful of punctuation a
# real fleet path uses — never a shell-active character ($ ` " \), whitespace, or
# a glob/redirection metacharacter. An anchored fleet path is built only from
# these; anything else after the anchor is not a well-formed path and is denied
# rather than expanded at source time (#731).
_PATH_SEG = r"[A-Za-z0-9_.@:+=,%-]+"
_SAFE_PATH_TAIL_RE = re.compile(rf"^(?:/{_PATH_SEG})*/?$")
_SAFE_REL_PATH_RE = re.compile(rf"^{_PATH_SEG}(?:/{_PATH_SEG})*$")
# A single composer-anchor head — braced (``${FLEET_ROOT}``) or bare
# (``$BOT_DIR``). Unlike the loose ``is_anchor_headed`` probe, the braced form
# must close, so a truncated ``${FLEET_ROOT`` never reads as a safe anchor.
_ANCHOR_HEAD_RE = re.compile(
    r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def is_safe_anchored_path(value: str) -> bool:
    """True if *value* is a well-formed anchored fleet path: a single composer
    anchor (``${FLEET_ROOT}`` / ``$BOT_DIR`` / ``${CLAUDLOBBY_ROOT}``) at the head
    followed only by path-safe segments. Such a value — and only such a value — is
    safe to emit DOUBLE-quoted in bot.conf: the anchor expands and nothing after
    it is shell-active. An anchor head trailed by a command substitution, a quote,
    a backslash, or a second ``$`` (a non-composer var or a further anchor) is NOT
    well-formed; it is denied at the source guard and never reaches the
    double-quoted sink (#731). Multi-anchor values are a deliberate residual —
    fail loud, extend the grammar if a real need appears."""
    m = _ANCHOR_HEAD_RE.match(value)
    if not m:
        return False
    name = m.group(1) or m.group(2)
    if name not in COMPOSER_PROVIDED_PATH_ANCHORS:
        return False
    return bool(_SAFE_PATH_TAIL_RE.match(value[m.end() :]))


def is_safe_relative_subpath(subpath: str) -> bool:
    """True if *subpath* is a fleet-relative path of path-safe segments only — the
    shape a secret-file path must have to be anchored on ``$FLEET_ROOT`` and
    emitted double-quoted without opening a shell-injection surface. Rejects a
    shell metacharacter; the caller still rejects an absolute/``~`` head and
    ``..`` traversal separately (#731)."""
    return bool(_SAFE_REL_PATH_RE.match(subpath))


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
    "hooks.type": Posture.EXEMPT,  # hook event kind (e.g. "command"), not a path
    "hooks.matcher": Posture.EXEMPT,  # tool-name matcher, not a path
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
        # Both structured fields refine by terminal key and default to CHECK, so a
        # NEW hook/runner sub-field is deny-by-default covered (not silently
        # exempt). The known non-path keys (hooks.type/matcher,
        # autonomous_runner.skill) are the recorded exemptions.
        return _FIELD_POSTURES.get(f"{top}.{segments[-1]}", Posture.CHECK)
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


_SOURCE_DENY_REASON = "denied absolute path"

# A value emitted DOUBLE-quoted into bot.conf (an anchored bot.env value's tail, a
# secret-file path anchored on $FLEET_ROOT) expands at source time — so a shell
# metacharacter in it would execute on every bot start. Findings tagged with this
# reason are that emission-safety class (#731), distinct from a foreign absolute
# (a path-ownership finding).
_EMISSION_METACHAR_REASON = (
    "shell metacharacter in a value emitted double-quoted into bot.conf "
    "(sourcing it would execute the metacharacter)"
)


def _checked_config_leaves(bot: BotConfig):
    """Yield ``(provenance, value, word_split)`` for every CHECKED (non-EXEMPT)
    string leaf of a ``BotConfig`` — the posture-walk SSOT shared by the deny guard
    (:func:`audit_bot_sources`) and the pre-bless lens
    (:func:`classified_source_paths`), so the posture / word-split decision lives in
    one place and the two can never disagree on which leaves get classified."""
    for segments, display, value in _walk_source_leaves(bot):
        posture = _posture_for(segments)
        if posture is Posture.EXEMPT:
            continue
        yield f"bots.{bot.bot_id}.{display}", value, posture is Posture.CHECK_WORDS


def _checked_fragment_leaves(fragments: dict[str, object]):
    """Yield ``(provenance, value)`` for every leaf of the loaded MCP fragments —
    the fragment-walk SSOT shared by the same two callers. Fragment leaves carry no
    posture (all CHECK, never word-split); a ``url`` leaf is a URL by grammar rule 1
    (``file://`` still denies its path part)."""
    for name, fragment in fragments.items():
        for _segments, display, value in _walk_source_leaves(fragment):
            yield f"library/mcp/{name}.json {display}", value


def _mcp_fragment_findings(
    bot_id: str, fragments: dict[str, object], decls: list[ExternalDecl]
) -> list[SourceFinding]:
    """Classify the leaves of loaded MCP fragments (choke-site 1). The composer loads
    the fragments and passes them in — path_audit never reads the library layout
    itself."""
    return [
        SourceFinding(
            bot_id=bot_id,
            source=source,
            value=value,
            path=denied,
            reason=_SOURCE_DENY_REASON,
        )
        for source, value in _checked_fragment_leaves(fragments)
        for denied in denied_source_paths(value, decls)
    ]


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
    for source, value, word_split in _checked_config_leaves(bot):
        for denied in denied_source_paths(value, decls, word_split=word_split):
            findings.append(
                SourceFinding(
                    bot_id=bot.bot_id,
                    source=source,
                    value=value,
                    path=denied,
                    reason=_SOURCE_DENY_REASON,
                )
            )
    # bot.conf emission safety (#731): the two source values that emit DOUBLE-quoted
    # into bot.conf — a bot.env value led by a composer anchor, and a secret-file
    # path anchored on $FLEET_ROOT — expand at source time, so a shell metacharacter
    # in either would execute on every bot start. Deny both here, in the shared
    # audit, so validate ≡ generate catch them (the composer sink is
    # defense-in-depth). Env is scoped to anchor-headed values: the general grammar
    # (above) still passes every ${VAR}-headed value, so an MCP/tool leaf that CC,
    # not the shell, expands is never swept up.
    for key, value in bot.env.items():
        if is_anchor_headed(value) and not is_safe_anchored_path(value):
            findings.append(
                SourceFinding(
                    bot_id=bot.bot_id,
                    source=f"bots.{bot.bot_id}.env.{key}",
                    value=value,
                    path=value,
                    reason=_EMISSION_METACHAR_REASON,
                )
            )
    for key, subpath in bot.secret_files.items():
        # a relative subpath with a shell metacharacter — the walk above already
        # denies an absolute/~ subpath; '..' traversal stays the emission guard's job
        if not subpath.startswith(("/", "~")) and not is_safe_relative_subpath(subpath):
            findings.append(
                SourceFinding(
                    bot_id=bot.bot_id,
                    source=f"bots.{bot.bot_id}.secret_files.{key}",
                    value=subpath,
                    path=subpath,
                    reason=_EMISSION_METACHAR_REASON,
                )
            )
    if isinstance(fragments, dict):
        findings.extend(_mcp_fragment_findings(bot.bot_id, fragments, decls))
    return findings


def classified_source_paths(
    bot: BotConfig, fragments: object | None = None
) -> list[tuple[str, str]]:
    """Every ``(provenance, absolute_path)`` a bot's CHECKED source values classify
    to, before ``external_paths`` blessing — the pre-bless companion to
    :func:`denied_source_paths`. The freshbox externals report consumes it to tell
    which declaration blesses a live source value (a declaration that blesses none
    is rot). Shares the walk + posture + grammar primitives with
    :func:`audit_bot_sources`; that guard filters this to the denied set and adds
    the emission-safety checks."""
    out: list[tuple[str, str]] = [
        (source, path)
        for source, value, word_split in _checked_config_leaves(bot)
        for path in classify_source_value(value, word_split=word_split)
    ]
    if isinstance(fragments, dict):
        out.extend(
            (source, path)
            for source, value in _checked_fragment_leaves(fragments)
            for path in classify_source_value(value)
        )
    return out


def source_findings_error(bot_id: str, findings: list[SourceFinding]) -> ValueError:
    """Build the field-precise failure for a set of source findings — the shared
    failure-UX for every choke-site (the dataclass walk and the composer's
    fragment / grant / tool / timer sites), so the remediation reads the same
    wherever a denied path is found."""
    detail = "\n".join(
        f"  fleet.yaml {f.source} = {f.value!r}\n      {f.reason}: {f.path}"
        for f in findings
    )
    tips = [
        "  - anchor it: rewrite against FLEET_ROOT / BOT_DIR / CLAUDLOBBY_ROOT "
        "(e.g. ${FLEET_ROOT}/sub/path) so the composer derives the real location;",
        "  - declare it: add an external_paths entry {path, purpose} blessing the "
        "absolute path, for a genuine dependency outside the fleet overlay.",
    ]
    if any(f.reason == _EMISSION_METACHAR_REASON for f in findings):
        tips.append(
            "  - de-fang it: a value emitted into bot.conf (an anchored env value's "
            "tail, a secret-file path) must be only path segments — drop the shell "
            "metacharacter (a fleet path has no $(...), backtick, or quote)."
        )
    return ValueError(
        f"bot {bot_id!r}: denied source value(s):\n{detail}\n\n"
        "Fix — pick one:\n" + "\n".join(tips) + "\n"
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


def timer_script_findings(jobs: object) -> list[SourceFinding]:
    """Denied absolute paths in fleet timer scripts (``jobs.<name>.script``) — the
    census companion to the composer's per-unit timer guard (compose_fleet_timers),
    so ``validate`` surfaces the same denied script ``generate`` rejects. A timer's
    script is fleet/install code, never an external dependency, so it is classified
    with no ``external_paths`` blessing (mirroring the composer choke's empty
    decls): it must be ``$CLAUDLOBBY_ROOT``-anchored or it is denied."""
    findings: list[SourceFinding] = []
    if not isinstance(jobs, dict):
        return findings
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict):
            continue
        script = cfg.get("script", "")
        if not isinstance(script, str):
            continue
        findings.extend(
            SourceFinding(
                bot_id=f"timer {name}",
                source=f"jobs.{name}.script",
                value=script,
                path=denied,
                reason="timer script path",
            )
            for denied in denied_source_paths(script, [])
        )
    return findings
