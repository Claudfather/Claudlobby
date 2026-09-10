---
name: review-status
description: "Self-diagnostic for code reviewer bot."
---

# Review Status

Quick health check. Run:

```bash
echo "Session: $(tmux display-message -p '#{session_name}' 2>/dev/null)"
"$CLAUDLOBBY_ROOT/lib/session-pid.sh" --summary
```

*Identity comes from the session you are running **inside**, via the shipped
`session-pid.sh` door — never a process-table search. On a host where every bot
runs as the same uid, `pgrep -f 'claude' | head -1` matches every bot on the box
and resolves to the earliest-started match, which is structurally a tmux
**server** rather than a Claude session. It returns the same wrong pid to every
caller, so two bots comparing notes get identical numbers and read that as
corroboration (Claudlobby #1525). The door prints `unknown` and exits 3 when it
cannot resolve; it never guesses.*

Report briefly.
