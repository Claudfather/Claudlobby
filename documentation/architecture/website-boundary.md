# Claudlobby and the website

Claudlobby runs the team on the owner's computer. Claudfather.ai supplies the
public website, account and workspace navigation. The same Claudlobby package
owns the operational UI and behavior used through the website, direct Plane and
the supported CLI. Installing or using a standalone host does not require a
Claudfather.ai account.

## Ownership

| Capability | Owner |
| --- | --- |
| Product pages, Google login, account sessions and workspace membership | Website |
| Saved computer names and exact HTTPS addresses | Website navigation metadata; saving an address does not connect or authorize a host |
| Fleet configuration, identities, project wiring, permissions and supervision | Claudlobby on the host |
| Tasks, assignments, conversations, results and delivery receipts | The host's canonical Plane records and existing command owners |
| Operational renderer, styles and interaction controllers | `claudlobby/plane/ui/`, shared by direct and website-connected views |
| Local owner pairing, workspace approval, host sessions and action grants | Claudlobby's host authority |
| Installation, release selection, configuration staging and activation | Claudlobby's existing setup and lifecycle owners |
| Runtime provider credentials and native provider sign-in | The host and provider; never an account cookie or a field in a website task store |

The website repository has two deployment roots: the account/public site and a
separate-origin Plane publisher. The publisher downloads a pinned, hash-checked
core UI graph. It does not implement another renderer or execute agents on
Vercel. Both publishers share repository write authority; separate origins do
not create separate maintainers.

## Connecting and acting

The website issues purpose-specific statements about current account/workspace
context. The isolated Plane browser document reads fresh account metadata and
connects directly to each saved private host. Website servers never fetch a
saved private address or proxy operational records.

Each host independently verifies the native owner and its local workspace
binding. Local approval cannot be replaced by Google login, a signed website
statement, a saved address or network reachability. Messages, task nudges and
feedback each require their existing local grant in addition to a valid host
session. Reads alone confer no action permission.

The shared UI retains host-qualified target identities and original request
receipts across navigation and uncertain outcomes. Inspecting a receipt does
not resend a request. Recorded, delivered and completed work remain different
outcomes. A host losing admission must not move a draft or action to another
host; another independently admitted host can remain usable.

Detailed contracts live in [owner access and actions](observable-plane.md),
[local workspace approval](owner-workspace.md) and
[connected sessions and the canonical browser entry](owner-connected-read.md).
The reciprocal [website and host contract](https://github.com/Claudfather/claudfather.ai/blob/main/documentation/architecture/host-contract.md)
describes the account and document handoff side.

## Setup, availability and current limits

[Getting started](../getting-started.md) owns the installed-release path.
The website must consume supported core setup/readiness operations as guided
team creation is completed. It must not introduce its own installer, activation
queue, credential fallback or operational completion flag. Core release assembly
does not select a release or activate a fleet; configuration activation remains
a separate consequential operation.

A website outage does not stop host supervision or erase host records. Direct
private Plane access uses its own host authorization and the shared core UI.
Removing a saved website address changes navigation only; revoke host authority
through the host's local controls when that is intended.

The published connected UI supports approved existing-host reads and separately
granted actions. Publication is not proof of a working account-to-host journey.
Same-site distinct HTTPS website/Plane origins, real Google configuration,
Tailscale/browser permissions and actual host acceptance remain deployment
requirements. Fresh-host installation, website-guided creation and launch of a
new team, and its verified GitHub connection remain unfinished product work.
The separately labelled public demo uses synthetic data and cannot prove any
of those capabilities.
