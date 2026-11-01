FLEET_STARTUP_BOOTSTRAP_V1

Run this fleet startup as one turn. First invoke the Skill tool with the exact
name and arguments in this JSON object:

{{RESUME_SKILL_CALL}}

Let that Skill restore context using its own procedure. Its structured resume
result completes the resume portion of this bootstrap, not the enclosing turn.
After it returns, continue with the authored startup instructions below in this
same turn. Do not wait for a second startup message. Preserve unresolved delivery
IDs and refusals from resume; these instructions do not authorize replay.

FLEET_STARTUP_INSTRUCTIONS_BEGIN
{{STARTUP_PROMPT}}
FLEET_STARTUP_INSTRUCTIONS_END
