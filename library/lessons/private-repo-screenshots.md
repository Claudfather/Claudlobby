---
title: Screenshots in private repo issues
description: Use dedicated asset branch + clickable-link for private-repo issue screenshots
---

For private-repo issues with screenshots:

1. Create or reuse an asset branch: `screenshots/<YYYY-MM-DD>-<topic>` off main. Never merged.
2. Commit screenshots under `docs/screenshots/<initiative>/<descriptive-name>.png`.
3. Push the branch. No PR required.
4. In issue body, reference via clickable link: `[alt-text](https://github.com/OWNER/REPO/blob/screenshots/.../path.png)` — plain markdown link, NO leading `!`.

**Why not inline embed:** GitHub rewrites img-tag URLs to `raw.githubusercontent.com`, which doesn't receive `github.com` session cookies — private-repo assets 404 anonymously, showing a broken image icon. The clickable-link path takes viewers through GitHub's auth-aware viewer.

Public repos can use inline `![alt](url)` normally.
