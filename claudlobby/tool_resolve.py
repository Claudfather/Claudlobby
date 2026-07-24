"""Library tools resolution — manifest, template, and param handling.

Shared by composer (render + write) and validator (pre-flight checks), the
same split as mcp_resolve. A tool is a directory ``library/tools/<name>/``
(fleet overlay wins) containing:

- ``tool.yaml`` — manifest: ``type`` (required, one of KNOWN_TOOL_TYPES),
  optional ``template`` (filename; defaults to the directory's sole ``*.j2``),
  optional ``params`` (``name: {default, required, description}``), optional
  ``env`` (runtime env var names the rendered tool reads via ``os.environ``).
- the Jinja template(s). The emitted filename is the template name minus
  ``.j2``.

Params are compose-time structure (paths, cadence) baked into the rendered
file. Secrets NEVER pass through params/Jinja — rendered tools are 0755
world-readable; secrets stay runtime env reads declared under ``env:``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .known_values import KNOWN_TOOL_TYPES

# Context names the composer always provides to tool templates. A manifest
# may not declare params that shadow them. Kept in lockstep with
# tool_context() below — its keyword-only signature is the same set, and
# test_tools asserts the two never drift.
RESERVED_TOOL_CONTEXT = frozenset(
    {"bot_id", "bot_name", "fleet_name", "bot_dir", "data_dir"}
)


def tool_context(
    params: dict[str, Any],
    *,
    bot_id: str,
    bot_name: str,
    fleet_name: str,
    bot_dir: str,
    data_dir: str,
) -> dict[str, Any]:
    """The full Jinja context for a tool render.

    Base context is spread last, so even a param that slipped past the
    reserved-name check could never shadow it.
    """
    return {
        **params,
        "bot_id": bot_id,
        "bot_name": bot_name,
        "fleet_name": fleet_name,
        "bot_dir": bot_dir,
        "data_dir": data_dir,
    }


_TEMPLATE_SUFFIX = ".j2"


def load_tool_manifest(tool_dir: Path) -> dict[str, Any]:
    """Load + shape-check ``tool.yaml``. Raises ValueError on any defect."""
    manifest_path = tool_dir / "tool.yaml"
    if not manifest_path.is_file():
        raise ValueError(f"tool '{tool_dir.name}': missing tool.yaml manifest")
    data = yaml.safe_load(manifest_path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"tool '{tool_dir.name}': tool.yaml must be a mapping")
    ttype = data.get("type")
    if ttype not in KNOWN_TOOL_TYPES:
        raise ValueError(
            f"tool '{tool_dir.name}': unknown type {ttype!r} — "
            f"known types: {sorted(KNOWN_TOOL_TYPES)}"
        )
    declared = data.get("params") or {}
    if not isinstance(declared, dict):
        raise ValueError(f"tool '{tool_dir.name}': params must be a mapping")
    shadowed = RESERVED_TOOL_CONTEXT & set(declared)
    if shadowed:
        raise ValueError(
            f"tool '{tool_dir.name}': params shadow reserved context names "
            f"{sorted(shadowed)}"
        )
    return data


def tool_template_path(tool_dir: Path, manifest: dict[str, Any]) -> Path:
    """The template to render: explicit ``template:`` or the sole ``*.j2``."""
    declared = manifest.get("template")
    if declared:
        p = tool_dir / declared
        if ".." in declared or not p.is_file():
            raise ValueError(f"tool '{tool_dir.name}': template '{declared}' not found")
        return p
    candidates = sorted(tool_dir.glob(f"*{_TEMPLATE_SUFFIX}"))
    if len(candidates) != 1:
        raise ValueError(
            f"tool '{tool_dir.name}': expected exactly one *{_TEMPLATE_SUFFIX} "
            f"template (or an explicit 'template:' in tool.yaml), "
            f"found {len(candidates)}"
        )
    return candidates[0]


def tool_target_name(template_path: Path) -> str:
    """Emitted filename: the template name minus the ``.j2`` suffix."""
    return template_path.name.removesuffix(_TEMPLATE_SUFFIX)


def resolve_tool_params(
    tool_name: str,
    manifest: dict[str, Any],
    overrides: dict[str, Any],
    decls: list | None = None,
) -> dict[str, Any]:
    """Merge manifest defaults with per-bot overrides.

    Unknown override names are a hard error — a typo'd param that silently
    no-ops is the same latent-breakage class as a silently-empty Jinja var.
    Missing required params are a hard error; params that are neither set,
    defaulted, nor required are simply absent (StrictUndefined catches any
    template use).

    ``decls`` is the equipping bot's ``external_paths`` (the composer threads it
    in; ``None`` → no path guard). With it, the L1 source guard (#702) denies any
    resolved string param — a manifest ``default:`` or a fleet.yaml override —
    that carries an absolute path neither anchored on a composer path nor
    declared, so a tool default cannot smuggle a foreign absolute into a rendered
    script.
    """
    declared = manifest.get("params") or {}
    unknown = set(overrides) - set(declared)
    if unknown:
        raise ValueError(
            f"tool '{tool_name}': unknown param(s) {sorted(unknown)} — "
            f"declared: {sorted(declared) or 'none'}"
        )
    resolved: dict[str, Any] = {}
    for pname, spec in declared.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ValueError(
                f"tool '{tool_name}': param '{pname}' spec must be a mapping"
            )
        if pname in overrides:
            resolved[pname] = overrides[pname]
        elif "default" in spec:
            resolved[pname] = spec["default"]
        elif spec.get("required"):
            raise ValueError(f"tool '{tool_name}': missing required param '{pname}'")

    from .path_audit import SourceFinding, denied_source_paths, source_findings_error

    tool_id = f"tool {tool_name}"
    findings = [
        SourceFinding(
            bot_id=tool_id,
            source=f"tools.{tool_name}.params.{pname}",
            value=value,
            path=denied,
            reason="tool param path",
        )
        for pname, value in resolved.items()
        if isinstance(value, str)
        for denied in denied_source_paths(value, decls or [])
    ]
    if findings:
        raise source_findings_error(tool_id, findings)
    return resolved
