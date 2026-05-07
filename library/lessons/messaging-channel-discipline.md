---
title: Messaging channel discipline
description: Substantive replies must go via the messaging channel tool — session output never reaches the user
---

Every substantive response to the user must be sent through the messaging channel tool (Telegram reply, Slack post, etc.). Inline text in the terminal/session output never reaches the user's device.

- Any response beyond a trivial "ack" goes through the messaging tool.
- If you're writing >50 words of substantive analysis in response to a user message, that's a tell — it belongs on the channel, not in session output.
- After composing a response, gut-check: "did I actually send this via the channel?" If not, fix it before ending the turn.
- After a tmux dispatch or a status poll, the user-facing summary goes via the messaging tool, not just the assistant output.
- Status updates at wait-points go to the channel too, even when the answer is "no change."
