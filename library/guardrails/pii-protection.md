---
title: PII / credential protection
---

Never share in chat, screenshots, or logs:

- API tokens, OAuth secrets, private keys
- Customer PII (names, emails, addresses, phone, payment info)
- Internal user IDs tied to real people
- `.env` / `.env.shared` contents

If debugging requires it: redact tokens to last 4 chars; use anonymized substitutes for PII (`user_123`, `example@example.com`). Files matching `*.env*`, `*credentials*`, `*secrets*`, `*.pem`, `*.key` are never staged for commit.
