---
name: review-status
description: "Self-diagnostic for code reviewer bot."
---

# Review Status

Quick health check. Run:

```bash
echo "Session: $(tmux display-message -p '#{session_name}' 2>/dev/null)"
echo "Uptime: $(ps -o etime= -p $(pgrep -f 'claude' | head -1) 2>/dev/null)"
echo "Memory: $(ps -o rss= -p $(pgrep -f 'claude' | head -1) 2>/dev/null | awk '{printf "%.0f MB", $1/1024}')"
```

Report briefly.
