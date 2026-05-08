---
title: Snowflake — read-only
---

# Snowflake — read-only

Without explicit human confirmation, only `SELECT`. Never:

- DML: `INSERT` / `UPDATE` / `DELETE` / `MERGE`
- DDL: `CREATE` / `DROP` / `ALTER` / `TRUNCATE` / `RENAME`
- Access: `GRANT` / `REVOKE`
- `CALL <procedure>` (procedures may mutate)

If a task seems to require DML/DDL, propose the SQL to the human first. Always `LIMIT` exploratory queries.
