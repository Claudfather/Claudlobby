# Owner-managed backup and restore

Claudlobby can export its accepted operational database and selected configuration
to a private directory. You choose when to back up and where to keep an off-host
copy. Nothing is uploaded, scheduled or retained by the website.

Run from an operator shell, using the managed installation's stable CLI (`INSTALL/bin/claudlobby`, see [Getting started](getting-started.md)). Backup creation requires a managed Git installation; a host still on the earlier sealed-release model cannot create one until its reviewed conversion exists (not yet implemented). Bundles made earlier remain inspectable.

```bash
claudlobby --root /absolute/host host backup create /absolute/private-backups/snapshot
claudlobby host backup inspect /absolute/private-backups/snapshot
```

The destination must not exist and must be outside the host root. Copy the whole
directory with permissions preserved. Treat it as private: work descriptions,
messages and configuration may contain confidential material. Inspection checks
file hashes and layout, not authenticity against a malicious archive author.

The SQLite backup API captures a consistent database transaction while the
runtime-use lock keeps the committed configuration stable. Ordinary database
writers can continue. The bundle includes the host identity and authored
fleet/project manifests, local library/voice/template overlays and host override
as they exist at backup time (`authored_at_backup`). Those files can include
pending edits. The committed configuration and current update record are retained
separately as recovery evidence; neither is a runnable restored configuration.
Concurrent changes during export refuse the backup. Review these distinct inputs
before using a restored root.

This initial path supports configuration inside the data root (root fleets and
`local/` fleets); externally located fleet declarations and linked overlays refuse
export rather than producing an incomplete recovery bundle. Keep a separate copy
of those external sources. Do not put secret values in authored configuration:
this exporter excludes credential facilities, but is not a secret scanner.

The bundle does **not** include:

- Owner grants, browser sessions, workspace bindings or managed GitHub credentials.
- `.env` files, provider logins, OS credential stores or private keys kept there.
- Bot memory/data, repository working copies, logs, mounted files or referenced artifacts.
- Claudron knowledge or other external storage.
- Uncommitted ingest queues or separate delivery receipt files.
- The managed installation (`INSTALL/src`, `INSTALL/env`), generated units or native service state. The current update record is copied only as inert recovery evidence.

Back up needed artifacts, bot-owned files and Claudron data through their own
owners. A database snapshot is not a full machine image and cannot recover a
website account or its permissions. Events accepted after the snapshot are absent.
An external action may already have happened even if its receipt is absent: inspect
provider state and reconcile uncertain work before allowing a manager to continue.

## Restore a replacement host

Retire the original deployment first. The explicit flag below records your
attestation; it does not shut down or fence another machine. Never run both hosts
with the retained host identity. This is replacement recovery, not fleet cloning.

```bash
claudlobby --root /absolute/replacement host backup restore \
  /absolute/private-backups/snapshot --replacing-retired-host
claudlobby --root /absolute/replacement host status
```

The replacement root must not exist. Restore verifies the entire bundle before
publishing the new root and starts no services. An interrupted final publication
may leave an empty reserved destination; inspect it explicitly before retrying.

The restore starts nothing and leaves an inactive root. Reinstall the **recorded Git
revision** and its locked dependencies into a **new** managed installation by following
[Getting started](getting-started.md); do not copy an installation from another host or
root. The code must match the database schema recorded in the bundle, and cross-platform
recovery is not yet supported. State restored under `state/restored-backup*` is inert
evidence: do not promote it or resume any saved update record from it.

Review restored manifests for old absolute paths, mounts and repository locations.
Re-provision provider credentials and reconcile external work.

If the saved configuration enrolled the owner service, set
`host.jobs.plane-owner.enroll: false` in the restored `host-system.yaml` for the first
initialization. Owner authority and the recorder binding are deliberately absent on the
replacement; the private service cannot start until you initialize and approve them again.
If the bundle contains `host-system.yaml`, select it explicitly with
`CLAUDLOBBY_HOST_SYSTEM_YAML` while reviewing.

**Restored-root startup is not yet supported by the Git updater.** Fresh initialization
refuses an existing database or restore marker. Keep the restored root inactive;
do not remove those markers or replay the archived update to make startup pass.
A separate reviewed adoption step is required before this replacement can run. The database preserves historical actors and work
identities. It does not automatically replay an old browser request or restore an old
permission grant.

After the replacement is admitted, initialize owner access, re-attest the recorder with
`host owner bind-source`, and pair/approve the workspace through the existing owner
commands. Re-enroll managed GitHub access and prepare repository working copies through
their existing owners. Review and revoke the replaced host's old website/GitHub
authorizations as part of replacement; importing a backup cannot change those remote
services. Application readiness and actual worker progress still require observation on the
replacement host. Once local owner access is prepared, re-enable `host.jobs.plane-owner`,
apply that reviewed change through the update guide, and verify the private service
responds before connecting the website to it.
