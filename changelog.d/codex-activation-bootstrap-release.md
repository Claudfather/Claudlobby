### Fixed — keep callable Skill resume and startup in one turn

Automatic bot startup invokes a positively resolved model-callable resume Skill and the unchanged authored startup instructions in one fleet bootstrap prompt. Explicit-empty resume settings and literal native or unproven custom commands keep their existing semantics. Submission remains distinct from completion; activation-held release and host pacing are separate work.
