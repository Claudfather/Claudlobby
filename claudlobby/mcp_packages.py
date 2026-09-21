"""Does an MCP fragment name a package that actually exists? (#1058)

A fragment composes `npx -y <pkg>`. If `<pkg>` is not in the registry the
server is **dead, not degraded** — and it fails silently at both layers, because
an erroring MCP fires `PostToolUseFailure` while `bot-vitals.sh` hooks
`PostToolUse`, so no `mcp_error` event is ever produced. Nothing on the host
says anything. The fleets exposed are the ones that followed the documentation;
the one fleet using the fragment in production carries a local override pointing
at a fork, so the shared fragment is exercised nowhere.

**Two signals, and the cheap one is the better one.**

*Pinning* (offline, unconditional, zero network): does the declaration name a
version? This is PREDICTIVE rather than current. Measured on the shared library
twice, 46 days apart: every npx declaration carrying a version resolves, and
both that do not are dead. An unpinned declaration is one nobody verified
against a real install, which is a statement about the DECLARATION and needs no
registry to make.

*Resolution* (network, opt-in, bounded): ask the real package manager. This has
been hand-rolled twice independently, and the second time it had a bug — a
scoped name carrying no version stripped to the empty string, the registry root
answered 200, and a dead fragment read healthy. That is why nothing here
composes a registry URL: the probe runs the EXACT argv the fragment would run,
via `mcp-package-grammar.warm_argv`, and lets npm do its own name encoding.
npm's own URL for the spotify fragment is
`registry.npmjs.org/@modelcontextprotocol%2fserver-spotify`; the percent-encoded
slash is precisely what a hand-composed URL gets wrong.

**This module is the INSTRUMENT, never the remedy.** It reports; which
fragments survive is an operator decision. Nothing here edits, marks, or ranks a
fragment.

**Everything it produces is a WARNING.** A fleet must never fail to compose
because npm is unreachable, slow, or having an outage.

**Unreachable is not "resolves"** — `source_state.py`'s rule. A probe that could
not answer yields an UNCHECKED finding carrying its reason, which is disclosed
exactly like the others. It is never rounded into a pass, and a nonzero exit is
never by itself read as "missing": a real package whose `--help` is unsupported
would exit nonzero too, so only an unambiguous registry not-found signature
condemns a declaration. That distinction is not theoretical — the first live run
of this probe had `uvx workspace-mcp --help` exceed 120s on a real host, which a
rc-only rule would have reported as a dead package.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .config import FleetConfig
    from .paths import Paths

#: Finding kinds. `UNPINNED` is offline and predictive; `MISSING` is a measured
#: registry answer; `UNCHECKED` is the honest third state.
UNPINNED = "unpinned"
MISSING = "missing"
UNCHECKED = "unchecked"

#: Per-package bound on the network probe. A probe that cannot answer inside it
#: yields UNCHECKED, never MISSING. Generous rather than tight: the argv is the
#: warm command, so a first probe of a real package pays a real download, and a
#: timeout that routinely fired would fill the report with UNCHECKED noise and
#: teach operators to ignore it.
PROBE_TIMEOUT_S = int(os.environ.get("CLAUDLOBBY_MCP_PROBE_TIMEOUT_S") or 60)

#: The flag that arms the network signal. Registered in `switches.py`; a
#: `*_ENABLED` name in one of our namespaces that no switch claims is reported
#: dead by the validator, so the row there is not optional.
PROBE_FLAG = "CLAUDLOBBY_MCP_PROBE_ENABLED"

#: Runtimes whose "not in the registry" answer we have actually SEEN, with the
#: signatures that say it. npm's E404 is measured (#1058, and again here).
#:
#: uvx is deliberately absent, and that absence is load-bearing rather than an
#: oversight: no uv not-found signature has been observed here, and inventing
#: one would manufacture exactly the confident-wrong verdict this module exists
#: to prevent. A uvx declaration is therefore reported UNCHECKED by
#: construction and says so. Its pinning is still checked — that signal needs no
#: registry and covers every runtime.
NOT_FOUND_SIGNATURES: dict[str, tuple[str, ...]] = {
    "npx": ("E404", "is not in this registry", "404 Not Found"),
}


@dataclass(frozen=True)
class Finding:
    """One thing worth saying about one declaration."""

    kind: str
    fragment: str  #: basename, which is what an operator would open
    server: str
    runtime: str
    spec: str
    detail: str = ""

    def message(self) -> str:
        where = f"mcp/{self.fragment} ({self.server})"
        if self.kind == UNPINNED:
            return (
                f"{where} declares '{self.runtime} {self.spec}' with NO VERSION — "
                "whatever the registry serves at boot is what runs, and nothing "
                "has verified that this resolves at all. Every shared fragment "
                "found dead so far was unpinned (#1058)"
            )
        if self.kind == MISSING:
            return (
                f"{where} declares '{self.spec}', which the registry does not "
                f"have — a fleet declaring this gets a DEAD server, not a "
                f"degraded one, and the failure is silent ({self.detail})"
            )
        return f"{where}: could NOT check whether '{self.spec}' resolves — {self.detail}"


def declared_fragments(fleet: "FleetConfig", paths: "Paths") -> list[str]:
    """The fragment files THIS FLEET declares — resolved WITHOUT the grammar.

    Separate from `fleet_declarations` so a caller can ask "is there anything
    here to check?" before reaching for anything that can fail. A fleet that
    declares no MCP server has nothing to check, and a rung that announced it
    could not check a thing that does not exist would be noise on every such
    fleet — including every minimal test fixture in this repo.

    Scoped to declared fragments rather than the whole library on purpose: the
    warning exists for the fleet that would actually get the dead server, and a
    fleet is not served by warnings about a fragment it never names. The library
    as a whole is the operator's question, which `claudlobby doctor` answers.
    """
    wanted: list[str] = []
    for bot in fleet.bots.values():
        for entry in bot.mcp:
            frag = paths.find_library_file("mcp", entry.name, ".json")
            if frag is not None and str(frag) not in wanted:
                wanted.append(str(frag))
    return sorted(wanted)


def fleet_declarations(fleet: "FleetConfig", paths: "Paths", grammar) -> list[tuple]:
    """The grammar's rows for the fragments this fleet declares."""
    return grammar.declared_packages(declared_fragments(fleet, paths))


def pinning_findings(rows: list[tuple]) -> list[Finding]:
    """The offline signal. No network, no subprocess, no conditions."""
    out = []
    for frag, server, runtime, spec, _bare, pinned, _argv in rows:
        if not pinned:
            out.append(Finding(UNPINNED, Path(frag).name, server, runtime, spec))
    return out


def _probe(argv: list[str]) -> tuple[str, str]:
    """Run *argv* bounded. Returns (kind, detail) — never raises.

    Only an unambiguous registry not-found condemns. Everything else that is
    not a clean exit is UNCHECKED with its reason, because the alternatives all
    fail in the expensive direction: a wrong "this package is missing" sends an
    operator to replace a fragment that was fine.
    """
    runtime = argv[0]
    signatures = NOT_FOUND_SIGNATURES.get(runtime)
    if signatures is None:
        return UNCHECKED, f"no not-found signature is established for {runtime}"
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return UNCHECKED, f"{runtime} is not installed on this host"
    except subprocess.TimeoutExpired:
        return UNCHECKED, f"{runtime} did not answer within {PROBE_TIMEOUT_S}s"
    except OSError as e:  # pragma: no cover — defensive
        return UNCHECKED, f"{runtime} could not be run: {e}"

    if proc.returncode == 0:
        return "", ""
    blob = f"{proc.stdout}\n{proc.stderr}"
    if any(sig in blob for sig in signatures):
        return MISSING, f"{runtime} reported it is not in the registry"
    return (
        UNCHECKED,
        f"{runtime} exited {proc.returncode} without saying the package is "
        "absent — this is not evidence it is missing",
    )


def resolution_findings(rows: list[tuple]) -> list[Finding]:
    """The network signal. Callers arm it; this function never decides to run.

    The argv comes from the row, which the grammar built from the same parse
    that produced the row — never from a second read of the fragment, which
    could disagree with the declaration the finding names.
    """
    out = []
    for frag, server, runtime, spec, _bare, _pinned, argv in rows:
        kind, detail = _probe(argv)
        if kind:
            out.append(Finding(kind, Path(frag).name, server, runtime, spec, detail))
    return out
