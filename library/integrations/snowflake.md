---
title: Snowflake
type: cli
env_contract:
  SNOWFLAKE_ACCOUNT:
    description: Snowflake account identifier
    tier: fleet
  SNOWFLAKE_USER:
    description: Snowflake username
    tier: fleet
  SNOWFLAKE_ROLE:
    description: Snowflake role
    tier: fleet
  SNOWFLAKE_WAREHOUSE:
    description: Snowflake warehouse name
    tier: fleet
  SNOWFLAKE_DATABASE:
    description: Snowflake database name
    tier: fleet
  SNOWFLAKE_PRIVATE_KEY_PATH:
    description: Path to Snowflake RSA private key
    tier: fleet
  SNOWFLAKE_PRIVATE_KEY:
    description: Snowflake RSA private key (inline)
    tier: fleet
---

# Snowflake


**Skills (clauDNA):** `/claudna:snowflake-query` (run SQL / explore schema), `/claudna:snowflake-cutover` (migrate connection to RSA key-pair auth)

**When to use:**
- Exploring schema or running read queries → `/claudna:snowflake-query`
- Migrating a project's Snowflake connection → `/claudna:snowflake-cutover`

**Gotchas:**
- **READ ONLY by default.** Only SELECT queries unless explicitly approved by the human. Never INSERT, UPDATE, DELETE, DROP, TRUNCATE, CREATE, ALTER, or any DDL/DML without approval.
- Query costs are real — avoid `SELECT *` on large tables; use `LIMIT` and filters
- Check the active warehouse and role before querying — wrong role = wrong data access
