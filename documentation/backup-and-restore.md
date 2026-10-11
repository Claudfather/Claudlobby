# Owner-managed backup and restore

Claudlobby can export its accepted operational database and selected configuration
to a private directory. You choose when to back up and where to keep an off-host
copy. Nothing is uploaded, scheduled or retained by the website.

Run from an operator shell, using the selected release's CLI:

```bash
claudlobby --root /absolute/host host backup create /absolute/private-backups/snapshot
claudlobby host backup inspect /absolute/private-backups/snapshot
```

The destination must not exist and must be outside the host root. Copy the whole
directory with permissions preserved. Treat it as private: work descriptions,
messages and configuration may contain confidential material. Inspection checks
file hashes and layout, not authenticity against a malicious archive author.

The SQLite backup API captures a consistent database transaction while the
activation lock keeps the selected configuration stable. Ordinary database writers
can continue. The bundle includes the host identity, the selected fleet/project
manifests, unchanged local library/voice/template overlays and the host's system
override. Unactivated edits to fleet/project manifests are not substituted for the
selected versions. Changed overlays refuse export because their old authored bytes
are not retained by older plans. Activate reviewed edits before trying again.

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
- Installed releases, generated units, activation journals or native service state.

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

Install the **same release ID** recorded in the bundle through the normal release
preparation/installer path. This requires the same platform/interpreter assembly;
cross-platform recovery is not yet supported. Do not copy a sealed release from
another root: its installation seal binds its location. Once recovery is active,
upgrade using the normal activation path.

Review restored manifests for old absolute paths, mounts and repository locations.
Re-provision provider credentials and reconcile external work. If the bundle
contains `host-system.yaml`, use it explicitly while reviewing and planning:

```bash
export CLAUDLOBBY_HOST_SYSTEM_YAML=/absolute/replacement/host-system.yaml
claudlobby --root /absolute/replacement config validate
claudlobby --root /absolute/replacement config plan --release RELEASE_ID
claudlobby --root /absolute/replacement config diff PLAN_ID
claudlobby --root /absolute/replacement host activate PLAN_ID --install-directory /absolute/native-user-units
```

Use the printed IDs and the appropriate existing native user-unit directory.
Activation accepts the untouched restored database only with its matching restore
record, host identity and release. It reuses normal migration, task audit, native
enrollment and startup; it never overwrites an active root. The database preserves
historical actors and work identities. It does not automatically replay an old
browser request or restore an old permission grant.

After activation, initialize owner access, re-attest the recorder with
`host owner bind-source`, and pair/approve the workspace through the existing
owner commands. Re-enroll managed GitHub access and prepare repository working
copies through their existing owners. Review and revoke the replaced host's old
website/GitHub authorizations as part of replacement; importing a backup cannot
change those remote services. Application readiness and actual worker progress
still require observation on the replacement host.
