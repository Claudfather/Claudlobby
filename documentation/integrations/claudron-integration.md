# Claudron Integration Guide

How claudlobby connects to [Claudron](https://github.com/Claudfather/Claudron) — the ecosystem's knowledge hub. Claudron owns the long-lived knowledge corpus in a **tenant-owned vault** (a private git repo of plain markdown); claudlobby consumes it. Claudlobby never stores the corpus, never forks Claudron's `SCHEMA.md` (the tri-repo schema SSOT), and never writes `runtime/`, `.env`, or operator PII into a vault.

The full receiving-side plan is `documentation/plans/2026-07-07-claudron-consumption.md` (EPIC #509); Claudron's side is Claudfather/Claudron#14.

## What works today (at the pinned SHA)

- **Vault-based fleet overlays.** A `.claudron` file at the claudlobby root (shell-sourceable, gitignored) points at a vault; `claudlobby --fleet <name>` resolves the fleet overlay from `vault/<name>/` before falling back to `local/<name>/`. Uses claudron's `claudron.vault.detect` / `Vault.fleets` API when the `[vault]` extra is installed, with a manual bridge-file fallback otherwise.
- **Per-bot vault env.** `claudron_vault_path` in fleet.yaml (per-bot or `defaults:`) composes `CLAUDRON_VAULT_PATH` into `bot.conf`.
- **CLI lookup.** `claudron lookup` works over a vault at the pinned SHA — the surface the interim query wedge (plan P1e) builds on.

## Version pin and bump policy

The `[vault]` extra in `pyproject.toml` is **pinned** — to a Claudron main SHA today, moving to the version tag (or a PyPI range like `claudron>=0.2,<0.3`) at Claudron's first tagged release. The extra tracks the **compositor's API consumption** (currently `claudron.vault.detect`); bump it per Claudron release *after* claudlobby's vault-mode tests pass against the new version. Never revert to a bare git URL — `tests/test_claudron_compat.py` enforces the pin.

The MCP *server* install is deliberately **not** coupled to this extra: bots don't run in claudlobby's venv. The server is a host-level install (see Gated surfaces below).

## Compatibility floor

SSOT: `claudlobby/claudron_compat.py` (doctor's claudron check reads it; a unit test keeps this table in sync). Claudron's release numbers are **ordinal** — if their Gate G1 re-orders epics, version numbers follow ship order — so every gate below is the *capability*, with the release number only an annotation.

| Claudlobby surface | Requires (capability) | Slated release |
|---|---|---|
| vault-based fleet overlay resolution (paths.py .claudron bridge) | claudron.vault.detect / Vault.fleets API | pinned SHA |
| interim CLI query wedge (dispatch-task.sh preflight, plan P1e) | claudron lookup CLI | pinned SHA |
| MCP fragment library/mcp/claudron.json (plan P2) | claudron-mcp stdio server (their E3) | 0.3.0 |
| librarian review sweep (plan P5) | claudron review --json (their E5) | 0.5.0 |

## Gated surfaces (not yet available)

These land when the Claudron release shipping their capability tags — not before:

- **MCP fragment + operator quickstart** (plan P2, #511): `library/mcp/claudron.json`, the `setup-system --with-claudron` host-level server install (pipx/uv-tool), doctor's `check_claudron`, and the end-to-end first-hour walkthrough. This section of the guide grows into the full quickstart then.
- **Librarian standing job** (plan P5, #514): `lib/claudron-review-sweep.sh` draining `claudron review --json` weekly into a designated librarian bot.

## References

- Plan: `documentation/plans/2026-07-07-claudron-consumption.md` · EPIC #509 (children #510–#514)
- Claudron roadmap: Claudfather/Claudron#14 · MCP contract: Claudfather/Claudron#17
- Schema SSOT: `SCHEMA.md` at the Claudron repo root (their E1)
