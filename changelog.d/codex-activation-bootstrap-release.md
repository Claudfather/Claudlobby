### Fixed — keep callable Skill resume and startup in one turn

Automatic bot startup invokes a positively resolved model-callable resume Skill and the unchanged authored startup instructions in one fleet bootstrap prompt. The exact slash Skill is mentioned separately for message-scoped permission; invocation and restore refusal stop the bootstrap without bypassing native permissions. Explicit-empty resume settings and literal native or unproven custom commands keep their existing semantics. Submission remains distinct from completion; activation-held release and host pacing are separate work.

Configured session Skills receive an exact whole-Skill invocation allow through the existing permission composer; deny/ask rules and underlying-tool permissions stay intact. The composed resume default preserves inherited and .env overrides, including explicit empty values.

The fresh-box permission audit traces the configured resume Skill through the same composition resolver, avoiding a false orphan-grant failure. Startup and briefing tests follow the shared capability and injection owners.
