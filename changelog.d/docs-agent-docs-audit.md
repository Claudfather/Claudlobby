### Docs — the agent instruction files agree with the code on main

- `claudlobby/_runtime_scripts/CLAUDE.md` says that a script without a row in its table documents itself in its header, as 9 of the 89 files beside it do. The root `CLAUDE.md` points to it as the script reference.
- Its `tail-fleet.sh` row names `claudlobby event list --bot <bot> --limit N`, the flag that command takes.
- `harness/CLAUDE.md` says that the `ab-coverage-verdict.py` row there is the whole of its entry: `documentation/harness-script-reference.md` has none for it.
- `PROJECT_MISSION.md` says that workers report back through the `claudlobby` CLI, which records each report in the plane, and that Telegram, tmux and the CLI are the management surface.

Each `AGENTS.md` stays a byte copy of the `CLAUDE.md` beside it.
