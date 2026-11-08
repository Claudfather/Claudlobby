### Removed — the two SCHEMA 2 naked-bot baselines the gate can no longer compare

`documentation/baselines/naked-bot-2026-08-11.json` and `naked-bot-2026-08-12.json` are deleted. `harness/naked-bot-observe.py` refuses to diff records across schema versions, and both remaining records are SCHEMA 3, so nothing current could be compared with them; git history keeps both. `documentation/naked-bot-observation-gate.md` now names one diff target, `naked-bot-2026-09-30.json`, in its table and its example command, and says how to read the SCHEMA 2 records from git.
