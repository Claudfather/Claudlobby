---
title: Design Evidence for Private Repos
description: Screenshot crawls in private repos need an asset branch + clickable links — not inline embeds.
---

# Design Evidence for Private Repos

Designer bots audit visual changes by crawling a preview deployment, capturing screenshots, and citing file:line in their reports. For **private** repos, the standard "embed the screenshot inline in the PR comment" pattern fails: GitHub's image proxy needs a public source.

**Pattern:**

1. **Asset branch per crawl.** Create a branch named `design-crawl/<YYYY-MM-DD>-<slug>` and commit screenshots into a `crawl-assets/` directory.
2. **Clickable links in the report.** In the PR comment / issue body, link each screenshot as `[<page>](https://github.com/<org>/<repo>/blob/<branch>/crawl-assets/<file>.png)`. The target opens in GitHub's authenticated viewer.
3. **One asset branch per crawl.** Don't reuse — old crawls drift, links break, reviewers get confused.

**Why not inline embeds:**

- Markdown image syntax (`![alt](url)`) requires an image proxy that won't authenticate.
- Pasting screenshots via GitHub's drag-and-drop uploads to `user-images.githubusercontent.com`, which is public — leaks visual state of pre-release UIs.

**Why not a CDN:**

- Adds infra. Adds rotation. Adds a thing to forget.

**Why a branch:**

- Already authenticated. Already in version control. Already deletable.

Designers (minimalist + maximalist alike) follow this pattern uniformly so reports look the same regardless of who authored them.
