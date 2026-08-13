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


Snowflake is the data warehouse. You reach it either through a Snowflake domain
skill or agent, or through the `snowsql` CLI.

**What you will need to do with it:**
- Explore schema and run read queries
- Migrate a project's connection to RSA key-pair auth

**How to do it:** use any Snowflake domain skill or agent you have available —
clauDNA ships `/claudna:snowflake-query` and `/claudna:snowflake-cutover`, so
check your own skill list first. If none is installed, use `snowsql` directly.
The read-only rule below binds either way.

**Gotchas:**
- **READ ONLY by default.** Only SELECT queries unless explicitly approved by the human. Never INSERT, UPDATE, DELETE, DROP, TRUNCATE, CREATE, ALTER, or any DDL/DML without approval.
- Query costs are real — avoid `SELECT *` on large tables; use `LIMIT` and filters
- Check the active warehouse and role before querying — wrong role = wrong data access
