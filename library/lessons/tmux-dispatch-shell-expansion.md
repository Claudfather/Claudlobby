---
title: tmux dispatch shell expansion
description: Disable bash history expansion before tmux send-keys dispatches containing ! tokens
---

Every tmux send-keys dispatch with prompt-like content MUST begin with `set +H;` prefix to disable bash history expansion.

A literal `!` followed by a word (e.g., `!readOnly`, `!isDashboard`, `!foo && bar`) triggers bash/zsh history expansion, which can:
- Silently blank exclamation-adjacent text
- Cause the Enter keystroke to not fire — the prompt lands in the worker's input buffer unsubmitted

**Fixes:**
- **Prefix:** `set +H; <dispatch content>`
- **Large dispatches (>50 lines):** write to a temp file and `tmux send-keys -t <bot> 'cat /tmp/dispatch.txt | claude' Enter`
- **If a dispatch lands but doesn't submit:** send a bare `tmux send-keys -t <bot> Enter`. Dispatches routed through `lib/dispatch.sh` do this for you — `pane_send_verified` polls the input box after the Enter and resends once if the payload is still sitting there, logging a `send_retry` event. Reach for the manual Enter only for a hand-rolled `send-keys` that bypassed the helper.

**The shell mechanics behind this are not tmux facts and are stated once elsewhere:** `!word` history
expansion and backticks-as-command-substitution apply to any generated command carrying text you did not
author — see the `shell-quoting-in-generated-commands` guardrail, which also records the arbitrary-code-
execution instance in `lib/gh-mention-guard.sh`. What stays here is the tmux dispatch application above.
