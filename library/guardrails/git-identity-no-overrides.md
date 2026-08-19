---
title: Git identity — no overrides
description: Always use global git config; never pass --author or set local user.email
---

# Git identity — no overrides

Always let git pick up the global identity automatically. Do NOT pass `--author=` on `git commit`, and do NOT set a local `user.email` in any repo's `.git/config`.

The behavioral rule is about WHO sets identity, not which identity: global config (for a bot, the composed `GIT_CONFIG_GLOBAL` file) is the one owner, and agent-side overrides fork it. On a shared-PAT fleet the global identity is the operator's. On an App-mode fleet (`github_app:` with `slug` + `bot_user_id`), the composed gitconfig itself sets `<slug>[bot]` + the numeric noreply address — which DOES map to a real GitHub account (the App's bot user), so the old rationale "bot-specific emails don't map to an account" is scoped to hand-invented emails, not to the composed App identity. Either way: the composed config decides; the agent never does.

On every commit, just run `git commit -m "..."` with no `--author` flag. If you see an existing local override in a repo, remove it.
