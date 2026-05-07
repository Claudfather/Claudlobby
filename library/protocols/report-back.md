---
title: Report-Back Protocol
---

Workers report via `{{CLAUDLOBBY_ROOT}}/lib/report-back.sh`, which sends a structured message into the manager's tmux session:

```
[BOTREPORT] <bot> | <status> | <summary> [| pr:<url>] [| issues:<urls>] [| skill:<name>]
```

**Statuses:** `completed` / `progress` / `blocked` / `failed`.

```bash
report-back.sh completed "Added rate-limit middleware" --pr https://github.com/org/repo/pull/123
report-back.sh blocked "Cannot find SLACK_TOKEN env var in .env"
```

The manager parses immediately and decides next steps per its decision framework.
