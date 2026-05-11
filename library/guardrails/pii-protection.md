---
title: PII / credential protection
description: Never leak PII, secrets, or fleet-specific details into shared code, library components, chat, or logs
---

# PII / credential protection

**No PII in shared/committed assets.** Library components (`library/`, `templates/`, `voices/`, `lib/`), compositor source (`claudlobby/`), and documentation (`documentation/`) must never contain:

- Real API tokens, OAuth secrets, private keys, bot tokens
- Real Telegram chat IDs, user IDs, or bot token strings
- Real database UUIDs, org IDs, project IDs, or Notion database IDs
- Real email addresses, phone numbers, physical addresses
- Real names tied to personal details (author names in pyproject.toml are fine)
- Real IP addresses (localhost/examples are fine)
- Financial account numbers or identifiers
- Real store domains, customer data, or order IDs

Use obviously fake placeholders in examples and documentation: `ghp_xxxxxxxxxxxxxxxxxxxx`, `"-1001234567890"`, `ntn_XXXXXXXXXXXXXXXXXXXX`, `8888888:AAAAAAAAAAAAAAAAAAAA`.

Fleet-specific values (real tokens, IDs, paths) belong exclusively in `local/<fleet>/` directories and `.env` files — both gitignored.

**No PII in runtime output.** Never share in chat, screenshots, or logs:

- API tokens, OAuth secrets, private keys
- Customer PII (names, emails, addresses, phone, payment info)
- Internal user IDs tied to real people
- `.env` / `.env.shared` contents

If debugging requires it: redact tokens to last 4 chars; use anonymized substitutes for PII (`user_123`, `example@example.com`). Files matching `*.env*`, `*credentials*`, `*secrets*`, `*.pem`, `*.key` are never staged for commit.

**Before committing any change to shared directories**, verify: `git diff --cached` shows no secrets, no fleet-specific UUIDs, no hardcoded paths pointing to real infrastructure.
