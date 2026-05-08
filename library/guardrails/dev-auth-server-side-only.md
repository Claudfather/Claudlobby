---
title: Dev-Auth Server-Side Only
description: Auth bypass flags must never appear in `NEXT_PUBLIC_*` (or any client-bundled) env. Server-side only.
---

# Dev-Auth Server-Side Only

Dev-auth bypass mechanisms (e.g., `DEV_AUTH_BYPASS=true`) exist to skip login during development and preview crawls. They must never reach the browser bundle.

**The rule:**

- Server-side env name only: `DEV_AUTH_BYPASS`, `BYPASS_AUTH`, etc.
- **Never** `NEXT_PUBLIC_DEV_AUTH`, `NEXT_PUBLIC_BYPASS_AUTH`, or any `NEXT_PUBLIC_*` form.
- **Never** read the bypass flag from `process.env.NEXT_PUBLIC_*` in client components.
- Gate the bypass behind a server-side check (middleware, getServerSideProps, route handler) that the client never sees.

**Why:** any `NEXT_PUBLIC_*` env is bundled into the JavaScript shipped to users' browsers. A `NEXT_PUBLIC_DEV_AUTH=true` flag in prod doesn't just enable bypass on the preview deployment — it bypasses auth for every user who loads the bundled JS. This has happened. It will happen again unless the rule is absolute.

**Reviewers:** flag any `NEXT_PUBLIC_*` env name with "auth", "bypass", "dev", "skip" in the substring as **request changes**, no exceptions.
