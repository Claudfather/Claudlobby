---
title: Git identity — no overrides
description: Always use global git config; never pass --author or set local user.email
---

Always let git pick up the global identity automatically. Do NOT pass `--author=` on `git commit`, and do NOT set a local `user.email` in any repo's `.git/config`.

Bot-specific emails don't exist as real inboxes and don't map to a GitHub account. CI/CD systems (Vercel, etc.) reject commits with unrecognized author emails.

On every commit, just run `git commit -m "..."` with no `--author` flag. If you see an existing local override in a repo, remove it.
