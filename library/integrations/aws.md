---
title: AWS Secrets Manager
type: cli
env_contract:
  AWS_ACCESS_KEY_ID:
    description: AWS access key for the fleetwide IAM user (read-only Secrets Manager)
    tier: fleet
  AWS_SECRET_ACCESS_KEY:
    description: Paired secret for the same IAM user
    tier: fleet
  AWS_DEFAULT_REGION:
    description: Required because bots have no ~/.aws/config — set to the region where secrets live
    tier: fleet
---

# AWS Secrets Manager

Fleetwide AWS credentials so bots can pull secrets from AWS Secrets Manager at runtime (e.g., per-service tokens that rotate independently of fleet-wide `.env` files).

**Scope:** one read-only IAM user shared across every bot on the host. No per-bot CloudTrail attribution. Matches the existing fleetwide-secret pattern (GitHub PAT, Notion token, Snowflake credentials).

## Setup

1. Create an IAM user (suggested name: `<fleet>-fleet-secrets-ro`) with programmatic access only — no console login.
2. Attach a scoped inline policy. Keep `GetSecretValue` narrow to a resource ARN pattern; let `kms:Decrypt` ride on a `ViaService` condition so the bot can't use the KMS key for anything outside Secrets Manager:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
         "Resource": "arn:aws:secretsmanager:<region>:<account>:secret:<fleet>/*"
       },
       {
         "Effect": "Allow",
         "Action": "kms:Decrypt",
         "Resource": "*",
         "Condition": {
           "StringEquals": { "kms:ViaService": "secretsmanager.<region>.amazonaws.com" }
         }
       },
       {
         "Effect": "Allow",
         "Action": "sts:GetCallerIdentity",
         "Resource": "*"
       }
     ]
   }
   ```

3. Generate an access key for the user and put `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + `AWS_DEFAULT_REGION` in your fleet-tier `.env` (`local/<fleet>/.env`).

4. Make sure `awscli` is installed on the host (see [`runbooks/mac-mini-setup-guide.md`](../runbooks/mac-mini-setup-guide.md), Phase 5).

`lib/creds-check.sh` will validate the keys via `sts:GetCallerIdentity` on every tick — rotated or revoked keys page Telegram automatically. `sts:GetCallerIdentity` does **not** verify the Secrets Manager policy is correctly scoped; that surfaces the first time a bot calls `GetSecretValue` against an out-of-scope ARN.

## Why fleetwide vs per-bot

Per-bot keys mean per-bot CloudTrail attribution and tighter blast radius — at the cost of N times the IAM users to rotate when an operator leaves. The fleetwide pattern trades attribution for ergonomics. Bots that need broader AWS access (e.g., S3 writes from one specific worker) should land per-bot keys in **bot-tier** `.env`, not widen the fleetwide IAM policy.

## Gotchas

- `AWS_DEFAULT_REGION` is required — bots run under launchd/systemd, which doesn't load `~/.aws/config`, so the SDK has no fallback for region.
- Skip silently when missing — `lib/creds-check.sh:check_aws_secrets` records `skip` (no alert) when `AWS_ACCESS_KEY_ID` is unset or the `aws` CLI is absent. Fleets that don't use AWS aren't paged.
- Never put AWS keys in shared `library/` or commit them anywhere. Fleet-tier `.env` is gitignored — keep it that way.
