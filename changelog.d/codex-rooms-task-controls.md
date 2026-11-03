### Added — Open a bot’s recorded conversation from inspection

View conversation in the Rooms bot inspector applies the existing exact-identity recent-thread filter without fetching or sending. Whole team restores the room, and conversation access remains separate from permission to message the bot.

### Added — Filter recorded tasks by status and assignee

Compact Tasks filters use the current recorded snapshot and exact team-qualified assignee identities. Reset restores the full view; refresh keeps missing selections explicit. Attention counts, incomplete-source warnings and truncated-history notices still describe the full snapshot, and task actions retain their existing guards.

### Added — Check missing team action access without losing work

Check action access reads only missing capabilities for the selected team, keeping the current message or task draft, selected task and saved request IDs. Collapsed help distinguishes authorization refusals from unavailable or unsupported access and explains the separate local approvals for messages, nudges and feedback. The check cannot send, approve access or reconnect.
