FLEET_STARTUP_BOOTSTRAP_V1

Run this fleet startup as one turn. First resume using the Skill named here:

{{RESUME_SLASH_NAME}}

Invoke the Skill tool with the exact name and arguments in this JSON object:

{{RESUME_SKILL_CALL}}

Let that Skill restore context using its own procedure. If it is unavailable,
requests permission, is refused, or fails to establish restored context, stop and report
the reason. Do not continue to startup, and do not reproduce the Skill by another route.
Actual restored context completes the resume portion of this bootstrap, not
the enclosing turn. Preserve and report nonfatal repository-scan caveats without
treating them as failed restore. A partial result without restored context does
not authorize startup. After context is restored, continue with the authored
startup instructions below in this same turn. Do not wait for a second startup message. Preserve unresolved delivery
IDs and refusals from resume; these instructions do not authorize replay.

FLEET_STARTUP_INSTRUCTIONS_BEGIN
{{STARTUP_PROMPT}}
FLEET_STARTUP_INSTRUCTIONS_END
