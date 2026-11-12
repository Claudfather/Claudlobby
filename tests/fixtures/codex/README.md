# Native Codex hook fixtures

Captured from installed Codex 0.162.0-alpha.17.2 on 2026-10-16 in the explicitly authorized private P0 canary. These objects were transformed directly from native hook stdin; producer source was not used. `provenance.json` links the exact sanitized bytes to original stdin hashes and byte counts. The unmodified shape-only batch harness hash identifies the parallel C1 evidence; the additional native fixture hook performed the redaction.

The in-memory transform recursively retains dictionary keys, list order and scalar types. It retains only event/source/reason/trigger/tool/permission/model enums; replaces session/turn/tool IDs with obvious fake constants; replaces cwd/transcript paths with fake `/tmp/rnc-*` paths; replaces nonempty content strings with `<redacted-FIELD>`; and preserves empty strings. It hashes original stdin before redaction and writes only the sanitized object plus shape/provenance metadata. No raw native payload, prompt, command, output or auth is stored here.

The tests substitute deterministic test prompts/trailers into the captured UserPromptSubmit envelope to prove the existing receipt path. This substitution is test input, not a claim that a native Codex dispatch occurred. Pre/PostToolUse objects preserve the observed Bash command shape and empty-string response. These are the three events this Lobby phase consumes; the full P0 batch also measured lifecycle/compaction in its separate evidence.

The fixed source fixtures do not activate Codex bots, change hook trust, prove native supervision, or retire legacy tool_call rows. Those boundaries remain in the epic and companions.
