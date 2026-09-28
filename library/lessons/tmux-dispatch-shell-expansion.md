---
title: tmux dispatch shell expansion
description: Use recorded task and message doors so authored punctuation never becomes shell input
---

Raw `tmux send-keys` task prompts crossed a shell boundary: `!word` could trigger history expansion, and backticks in double-quoted strings could execute command substitution. A failed send could leave text in a pane, making a manual Enter or resend ambiguous.

For fleet work, follow the default `fleet-ops` skill: `claudlobby --json task admit`, then manager-owned `task assign` and `assignment deliver --file FILE`, each with its own retained request UUID. For ordinary communication use `claudlobby --json message send --to BOT --file FILE --request-id UUID`. Authored text stays in the file or one CLI value; do not wrap it in a hand-built tmux command. Admission is not delivery. Inspect the request and message receipt after uncertainty; never send a bare Enter or automatically resend.

`lib/dispatch.sh` and `pane_send_verified` still serve legacy lifecycle and native delivery internals. Their quoting and flush behavior is not a caller recipe for task dispatch. The general shell-quoting mechanism and the `lib/gh-mention-guard.sh` incident remain documented in the `shell-quoting-in-generated-commands` guardrail.
