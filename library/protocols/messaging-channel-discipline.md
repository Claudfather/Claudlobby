---
title: Messaging Channel Discipline
description: Substantive replies must go through the messaging channel tool — session output never reaches the user.
---

# Messaging Channel Discipline

Every substantive response to the user MUST be sent through the messaging channel tool (Telegram reply, Slack post, etc.). Inline text in the terminal / session output never reaches the user's device — an unsent reply is a dropped reply.

- Any response beyond a trivial "ack" goes through the messaging tool.
- Writing >50 words of substantive analysis in response to a user message is a tell — it belongs on the channel, not in session output.
- After composing a response, gut-check: "did I actually send this via the channel?" If not, fix it before ending the turn.
- After a tmux dispatch or a status poll, the user-facing summary goes via the messaging tool, not just the assistant output.
- Status updates at wait-points go to the channel too, even when the answer is "no change."

*(Re-homed from `library/lessons/messaging-channel-discipline.md` in the L3 boundary re-architecture: this is imperative bot-steering, not referential "learned-the-hard-way" residue, so it lives where it renders in-context. The lesson file is retained for the non-vault composition fallback.)*
