#!/usr/bin/env python3
"""How an MCP server's `command` + `args` name a package — in ONE place (#1577).

This grammar was forked three ways, and only one copy knew uvx existed:

  * `claudlobby/commands/core.py`   `warm-cache`, which package to fetch
  * `claudlobby/composer.py`        the npx -> node binary swap, which args to KEEP
  * `lib/check-npx-cache.sh`        an embedded `python3` heredoc, is it cached

Adding the uv probe to the third meant either re-typing the grammar a FOURTH
time or consolidating. Hence this module. It is **stdlib-only and standalone**
(the `dispatch-overdue.py` precedent) because bash must exec it directly on a
host where the package may not be importable by whatever `python3` is on PATH —
a health probe that breaks when an install is half-finished is worse than the
gap it reports.

The two Python consumers reach it through `claudlobby/mcp_grammar.py`, which
REFUSES rather than falling back: a fallback grammar would BE the fourth copy,
and it would be consulted exactly when the two had diverged (`env_tiers.py`'s
rule, same reasoning).

**Two runtimes, two questions, and they are not the same question.** The warm
needs the argv that FETCHES a package; a cache probe needs the bare
distribution NAME to look up. `--from <spec> <entry>` gives different answers to
each, which is why they are separate functions rather than one with a flag.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

#: Package managers that fetch on demand, each with the toolchain an operator
#: installs to get it. A `command` absent from here downloads nothing at boot
#: and has no cache to warm or probe.
WARM_RUNTIMES = {
    "npx": "install Node.js first",
    "uvx": "install uv first",
}

#: npm version/tag suffix on a package spec: `pkg@1.2.3`, `pkg@latest`,
#: `@org/pkg@^2.0.0`. Anchored to the end and required to start with a digit or
#: a range operator, so the `@` of a SCOPED name is never mistaken for one.
_NPM_VERSION_SUFFIX = re.compile(r"@(?:[0-9^~><=*][^/]*|latest|next)$")

#: PEP 440 version specifier on a PyPI spec: `pkg==1.2.3`, `pkg>=2`, `pkg[extra]`.
_PYPI_VERSION_SUFFIX = re.compile(r"(\[[^\]]*\])?\s*(===|==|!=|~=|>=|<=|>|<).*$")

# --- pinning is a DIFFERENT question from where-does-the-suffix-begin ---------
#
# The two patterns above answer "where does the version part start", which is
# what `bare_name` needs, and stripping `@latest` there is CORRECT: the cache is
# keyed by name. They deliberately do NOT answer "is this an EXACT version",
# because `@latest`, `^2.0.0` and `>=2` all have a version part and none of them
# pins anything. Sharing one pattern for both conflated the questions and read a
# floating tag as pinned (vera, PR #1705).

#: An EXACT npm version: full MAJOR.MINOR.PATCH, with optional prerelease and
#: build metadata. Measured rather than assumed: `npm view cowsay@1 version`
#: answers **1.6.0**, not 1.0.0, so a PARTIAL version is a range that resolves
#: to the newest match — only a complete triple pins. `latest` and `next` are
#: dist-tags (npm's own idiom for "give me current") and pin nothing.
_NPM_EXACT_VERSION = re.compile(
    r"@(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$"
)

#: An EXACT PyPI pin: `==` or `===` only. `~=`, `>=`, `<=`, `>`, `<` are ranges;
#: `!=` is an EXCLUSION, which names the one version NOT to run and so pins
#: least of all. A trailing `.*` is PEP 440 prefix matching (`==1.2.*` means any
#: 1.2.x), so it is excluded even though the operator is `==`.
_PYPI_EXACT_VERSION = re.compile(r"(?:\[[^\]]*\])?\s*={2,3}\s*([^=<>!~,\s*]+)$")


def split_npx_args(args: list[str]) -> tuple[str | None, list[str]]:
    """`npx [-y] <pkg> <rest...>` -> (package, rest).

    Both halves are real consumers: `warm-cache` wants the package, the
    composer's binary swap wants the rest. Returning both from one parse is
    what stops them disagreeing about where the boundary is.

    The package is the first token after `-y`. `rest` is everything else with
    `-y <pkg>` removed, preserving order.
    """
    pkg: str | None = None
    rest: list[str] = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            if pkg is None:
                pkg = a
            continue
        if a == "-y":
            skip_next = True
            continue
        rest.append(a)
    return pkg, rest


def warm_prefix(command: str, args: list[str]) -> tuple[str, list[str]] | None:
    """Reduce (command, args) to (display name, the argv that fetches it).

    The warm command is `<command> <prefix> --help`, so the prefix carries
    exactly what identifies the download and nothing else. Dropping the
    server's own flags also keeps unexpanded `${VAR}` placeholders out of the
    subprocess by construction rather than by a filter.

    Returns None when the args name no package this can identify. That is
    deliberately not a guess -- warming the wrong token still exits 0, and a
    server reported as covered while it keeps paying the cold cost is the
    failure the warm exists to prevent.
    """
    if command == "npx":
        pkg, _rest = split_npx_args(args)
        return (pkg, ["-y", pkg]) if pkg else None

    if command == "uvx":
        # `uvx --from <spec> <entry>`: the package to fetch and the console
        # script to run are different tokens and both are needed, because
        # `uvx --from <spec> --help` prints uv's own help and fetches nothing.
        # The entry must sit adjacent to the spec; any other arrangement is a
        # shape this cannot read rather than one it should guess at.
        for i, a in enumerate(args):
            if a == "--from":
                if i + 2 < len(args) and not args[i + 2].startswith("-"):
                    spec, entry = args[i + 1], args[i + 2]
                    return spec, ["--from", spec, entry]
                return None
        # `uvx <pkg> [server flags...]`: only the first token is the package.
        # A leading flag means some other shape -- uv's own value-taking
        # options (--python, --with) would otherwise have their value read as
        # a package name.
        if args and not args[0].startswith("-"):
            return args[0], [args[0]]
        return None

    return None


def normalize_pypi_name(name: str) -> str:
    """PEP 503 normalization: lowercase, runs of `-_.` collapse to `-`.

    uv keys its wheel cache on the normalized name, so a fragment spelling
    `Google_Analytics_MCP` must be looked up as `google-analytics-mcp`.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def bare_name(command: str, spec: str) -> str:
    """Strip a display spec down to the name a CACHE is keyed by.

    Separate from `warm_prefix` because the warm and the probe want different
    answers: the warm should fetch exactly what the server runs, pin included,
    while a cache is keyed by name alone. Taking a spec rather than args lets
    a caller that already holds one avoid parsing twice.
    """
    if command == "npx":
        return _NPM_VERSION_SUFFIX.sub("", spec)
    return normalize_pypi_name(_PYPI_VERSION_SUFFIX.sub("", spec).strip())


def package_name(command: str, args: list[str]) -> str | None:
    """`bare_name` of whatever `warm_prefix` identifies, or None."""
    target = warm_prefix(command, args)
    return None if target is None else bare_name(command, target[0])


def is_pinned(command: str, spec: str) -> bool:
    """Does *spec* name an EXACT version, rather than whatever the registry
    serves today?

    **Exact, not merely "carries a version part".** `pkg@latest`, `pkg@^2.0.0`
    and `pkg>=2` all have a version part and none of them pins: each resolves to
    whatever the registry answers with at boot, which is precisely the risk the
    unpinned warning describes. An earlier cut keyed this on the same patterns
    `bare_name` strips with, and so read all three as pinned — the two questions
    look alike and are not the same one. `bare_name` asks *where does the
    suffix begin* (and stripping `@latest` is right for a cache key);
    this asks *is that suffix one version*.

    **Deliberately NOT `bare_name(command, spec) != spec`**, which is the
    obvious shortcut and is wrong for PyPI: `bare_name` also applies PEP 503
    normalization, so an unpinned `Google_Analytics_MCP` differs from its bare
    form and would report as pinned. The npm arm would have passed that test
    and the uvx arm would not.

    An unpinned spec is not broken; it is unverified. Nothing here says a
    package is missing — that is a question only the registry can answer.
    """
    pattern = _NPM_EXACT_VERSION if command == "npx" else _PYPI_EXACT_VERSION
    return bool(pattern.search(spec))


def declared_packages(
    paths: list[str],
) -> list[tuple[str, str, str, str, str, bool, list[str] | None]]:
    """Every warmable server declaration, KEYED BY THE FRAGMENT THAT MADE IT.

    Rows are (fragment path, server key, runtime, display spec, bare name,
    pinned, probe argv). `probe_targets` answers "what must the cache hold", so
    it dedupes to a package set; a reader reporting a finding has to name the
    FILE an operator would edit, which a deduped set cannot do — two fragments
    sharing `mcp-remote@0.1.38` are one warm and two declarations.

    Both are the same walk, so this is the walk and `probe_targets` narrows it.

    The argv rides along so a caller never re-reads the fragment to get it: the
    command a probe runs then provably comes from the same parse as the row it
    is reported against, rather than from a second read that could disagree.
    """
    out: list[tuple[str, str, str, str, str, bool, list[str] | None]] = []
    for p in paths:
        path = Path(p)
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for frag_path in files:
            try:
                frag = json.loads(frag_path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(frag, dict):
                continue
            for key, server in servers_in(frag):
                runtime = server.get("command")
                if runtime not in WARM_RUNTIMES or "args" not in server:
                    continue
                target = warm_prefix(runtime, server["args"])
                if target is None:
                    continue
                spec = target[0]
                out.append(
                    (
                        str(frag_path),
                        key,
                        runtime,
                        spec,
                        bare_name(runtime, spec),
                        is_pinned(runtime, spec),
                        _probe_argv(runtime, target),
                    )
                )
    return sorted(out)


def _probe_argv(command: str, target: tuple[str, list[str]]) -> list[str]:
    """`<command> <warm prefix> --help` — the argv that FETCHES the package.

    One definition because two callers build it: `declared_packages`, which
    already holds the target, and `warm_argv`, which parses one from args.
    Typed twice it would be a fork waiting to happen, in the file whose whole
    thesis is that this grammar has exactly one copy.
    """
    return [command, *target[1], "--help"]


def warm_argv(command: str, args: list[str]) -> list[str] | None:
    """The EXACT argv that fetches this server's package, command included.

    `<command> <warm_prefix> --help` is what `warm-cache` runs, so a probe
    built from this asks the real package manager to resolve the real spec.
    That matters more than it sounds: every hand-rolled version of this
    question has instead composed a registry URL from a name it parsed itself,
    and the second one shipped with a scoped name stripping to the empty
    string — the registry root answered 200 and a dead fragment read healthy.
    """
    target = warm_prefix(command, args)
    return None if target is None else _probe_argv(command, target)


def servers_in(fragment: dict) -> list[tuple[str, dict]]:
    """The real server entries of a fragment — `_`-prefixed keys are contracts
    (`_env_contract`, `_permissions_contract`), never servers.

    Consolidated from `warm-cache` and `check-npx-cache.sh`. TWO copies still
    stand and are named rather than quietly left: `composer.py`'s
    `compose_mcp_json` (which `break`s at the first server where this returns
    all — latent only because every shipped fragment holds exactly one) and
    `doctor.py`'s fragment walk. Routing those through here widens the
    refusal in `mcp_grammar` from the binary swap to ALL composition, which
    is a rollout decision rather than a tidy-up, so it is a follow-up."""

    return [
        (k, v) for k, v in fragment.items() if not k.startswith("_") and isinstance(v, dict)
    ]


def probe_targets(paths: list[str]) -> list[tuple[str, str, str]]:
    """Every warmable server across the given fragment files or directories, as
    (runtime, display spec, bare name), deduped and sorted.

    One call answers for a whole library, which is why `check-npx-cache.sh`
    spawns this ONCE where it used to spawn `python3` per fragment.
    """
    out: dict[tuple[str, str], tuple[str, str, str]] = {}
    for _frag, _key, runtime, spec, bare, _pinned, _argv in declared_packages(paths):
        out[(runtime, spec)] = (runtime, spec, bare)
    return sorted(out.values())


def main(argv: list[str]) -> int:
    """`--probe-targets <path>...` prints one TAB-separated row per server:
    `<runtime>\\t<display spec>\\t<bare name>`. Machine-read by bash, so the
    rows carry no prose and nothing else goes to stdout."""
    if len(argv) < 2 or argv[1] != "--probe-targets":
        sys.stderr.write("usage: mcp-package-grammar.py --probe-targets <path>...\n")
        return 2
    for runtime, spec, bare in probe_targets(argv[2:]):
        sys.stdout.write(f"{runtime}\t{spec}\t{bare}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
