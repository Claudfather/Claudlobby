### Added — a Skill call's `tool_call` event names the skill (#3156)

On a Skill tool call, the `tool_call` event that `bot-vitals.sh` records carries `skill`: the name in the hook payload's `tool_input.skill`, as the call gives it (for example `claudna:session`), so `claudlobby event list` can tell which library skills run, and how often, and composition can be measured. The call's arguments are never read. A Skill call whose `tool_input.skill` is missing, empty or not a string records the event without `skill`, and every other tool's event is unchanged.
