# clauDNA export capture

Captured from actual Claude Code 2.1.263 native hook dispatch against released
clauDNA v0.28.0 (fe665ea6ce6cdaba95bc8692de09b1f1f6b87b77), 2026-10-15.
The ok session ran the real detached clauDNA summarizer and native CLI with a
fixed, schema-valid loopback provider response; the other session had summaries
disabled. No model inference or provider account/auth was used. CLI protocol and
hook/store behavior were observed; provider/model quality and supervised fleet
rollout were not. The reported producer cost is the native CLI estimate.

Session/job identifiers, PIDs, transcript hashes and local paths are scrubbed placeholders. This is
a shape capture, not a producer-source or producer-test-generated fixture. Only
store session artifacts are retained: no transcript, credential or entrypoint.
`envelopes.json` is the real export door over this scrubbed store. Regenerate it
when `claudlobby/contracts/claudna.ref` moves; export copies the store first so
acks never mutate this fixture.
