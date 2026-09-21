"""The package-side door onto `lib/mcp-package-grammar.py` (#1577).

**This module owns no grammar.** It asks the INSTALL's `lib/` copy — the same
file `lib/check-npx-cache.sh` execs — so the compositor and the health probe
cannot disagree about which token of an MCP server's args names its package.
That disagreement is not hypothetical: the grammar was forked three ways and
exactly one copy knew `uvx` existed, which is how a uvx MCP server came to be
invisible to both the warm and the probe that gates it.

It **refuses rather than falling back** (`env_tiers.py`'s rule). A local
fallback grammar would BE the fourth copy, and it would be consulted exactly
when the two had diverged — a compositor quietly writing a different answer
from the one the probe reads is the failure mode, not a missing file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .paths import load_lib_module

if TYPE_CHECKING:
    from .paths import Paths

#: The stdlib door. Standalone by design: bash execs the same file on hosts
#: where the package may not be importable by whatever `python3` is on PATH.
GRAMMAR_FILENAME = "mcp-package-grammar.py"


class GrammarUnavailable(RuntimeError):
    """The shared grammar could not be loaded from the install's `lib/`.

    Raised, never swallowed into a default — see the module docstring.
    """


def grammar(paths: "Paths"):
    """The loaded grammar module, or raise.

    `load_lib_module` memoizes, so repeated calls across a generate cost one
    stat apiece rather than a re-exec.
    """
    mod = load_lib_module(paths.lib, GRAMMAR_FILENAME)
    if mod is None:
        raise GrammarUnavailable(
            f"cannot load {paths.lib / GRAMMAR_FILENAME} — the install is incomplete. "
            "This module refuses rather than guessing at the grammar: a second copy "
            "would be consulted exactly when the two had diverged."
        )
    return mod
