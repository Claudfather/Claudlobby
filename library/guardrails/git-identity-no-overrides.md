---
title: Git identity — no overrides
description: Always use global git config; never pass --author or set local user.email
---

# Git identity — no overrides

Always let git pick up the global identity automatically. Do NOT pass `--author=` on `git commit`, and do NOT set a local `user.email` in any repo's `.git/config`.

The rule is about WHO sets identity, not which one: the composed global config may carry the operator identity or the App's `<slug>[bot]` noreply address (which maps to a real GitHub account — only HAND-INVENTED emails do not). Either way the composed config decides; the agent never does.

On every commit, just run `git commit -m "..."` with no `--author` flag. If you see an existing local override in a repo, remove it.
