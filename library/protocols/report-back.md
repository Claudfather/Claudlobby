---
title: Report-Back Protocol
---

# Report-Back Protocol

Workers report via `{{CLAUDLOBBY_ROOT}}/lib/report-back.sh`, which sends a structured message into the manager's tmux session:

```
[BOTREPORT] <bot> | <status> | <summary> [| pr:<url>] [| issues:<urls>] [| skill:<name>]
```

**Statuses:** `completed` / `progress` / `blocked` / `failed`.

```bash
report-back.sh completed "Added rate-limit middleware" --pr https://github.com/org/repo/pull/123 --task t-1787000000-ab12
report-back.sh blocked "Cannot find SLACK_TOKEN env var in .env" --task t-1787000000-ab12
```

Pass `--task <id>` whenever the dispatch carried one — the watchdog closes your dispatch by that id, and an id-less report does not count for an id'd task (Worker Lifecycle, Step 2). Omit it only for genuinely id-less work.

The manager parses immediately and decides next steps per its decision framework.

## Keep `<summary>` to ~200 characters

The summary is a **routing signal**, not a report. The manager needs enough to decide the next move; everything else belongs where it can be read properly.

A 2,500-character summary technically satisfies "one line" and defeats the purpose — the manager has to parse a wall to find the verdict, and it lands in their context at full cost whether or not they needed the detail.

**Lead with the verdict**, then the one fact that changes what happens next:

```bash
# Good — verdict first, detail addressed
report-back.sh completed "Request Changes on #943: search gate fails 2/3 of its own cases. Evidence in PR comment." --pr https://github.com/org/repo/pull/943 --task t-1787000000-ab12

# Bad — correct format, unreadable payload
report-back.sh completed "Reviewed #943. Ran the exact gh issue list command using naive phrasings of the three frictions, first hit at top of 6 results, second was present but buried at position 8 of 27, third returned 0 results because the actual title says hidden env-var feature switches and the word undiscoverable appears nowhere, therefore ..."
```

**Where the detail goes:** the PR or issue comment, a doc in your `data/` or the fleet's `shared/`, or the branch itself. Put it somewhere addressable *first*, then cite the address. If it has no address yet, that is what to fix — not the wording.

**Never truncate these to fit:** the blocker itself on a `blocked` report, and verbatim error output. If a blocker needs 400 characters to be actionable, use them.
