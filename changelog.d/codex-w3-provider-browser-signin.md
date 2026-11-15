### Added — Locally approved browser-mediated Claude sign-in

The connected first-team guide can run one explicitly approved, exact-artifact
Claude setup-token attempt on the selected host. It exposes a validated provider
link and private code entry, retains the original attempt without automatic
retry, and stores the token only in the host's private environment file.
Cancellation and interrupted attempts retain uncertainty. Existing private-token
setup and separate local message approval remain available.

After original Launch, the same guide releases its old runtime readers, waits
read-only for that exact update and reenters current source before optional local
messages-only approval. Skipping creates no new approval; interrupted or refused
effects retain their original recovery identity without automatic replay.

Exact supported public Claude artifacts installed with hard links are accepted
through held-descriptor hash and identity verification. Private credentials and
per-attempt executable copies retain their single-link requirement.
### Fixed — Local workspace approval recovery

The first-team guide identifies rejected workspace approval codes without asking
users to pair the owner again. It keeps local serving available for an explicit
RETRY with a new browser proposal, or CANCEL/EOF to stop. Retries preserve the
original owner and workspace binding, show the verified proposal again, and
recheck its validity in the authority transaction when BIND-WORKSPACE is entered.
No proposal, binding, provider attempt or launch is retried automatically.

### Changed — Reusable core workspace approval

Workspace binding now uses a shared read-only preview and explicit presenter
approval, with terminal rendering kept separate. Hidden codes, approval wording,
fresh-code retry and transaction checks remain intact. Owner or binding changes
during code entry now produce a specific conflict. This adds no command,
endpoint or authorization channel; a presenter does not authenticate its caller.

### Added — Passive preinstall host inventory

A standalone, package-owned POSIX `host-readiness-v1` payload reports bounded
OS/account facts and prerequisite executable presence without executing those
prerequisites. Python features, provider sign-in, Tailscale access and native
user-manager readiness remain explicitly unknown. It lives under `preinstall/`
because initial inventory cannot depend on installed runtime helpers; no SSH
transport, setup action or bot-runtime wiring is added.
