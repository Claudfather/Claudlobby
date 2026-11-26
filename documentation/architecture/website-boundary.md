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

The guided setup's team card (`allow_setup`) can carry a messages mark. Then it
also lets the person message that one team from the website after a healthy
Launch, under the name chosen on the card. Who the person is stays the bound
website account; the name is only a label the team reads. The approval is
still local and revocable (`host owner revoke-setup` before the finish,
`host owner revoke-messages` after it), and the website signs nothing new.
One message, the setup goal, is sent once by the computer's own operator on the team card's local approval, without a session; it is recorded as such.
Its receipt carries the provenance `{"carrier": "setup_finish"}`, never a
connected session's.

The guide's cards travel over the computer's Tailscale connection, not in a
website session, and carry no website-signed statement. Core cannot tell which
website account a setup page is signed into: whoever can act as the computer's
Tailscale user already reaches the guide's cards, so the team card's name is an
accident boundary, like the operator terminal. The website's name-card change
adds the page-side rule: the page fills the card's name only for the account the
computer is bound to. Today's page has no such rule and sends no name, so core
refuses its answer to the marked team card (`invalid_request`). Core refuses the finish only when the binding the approval pinned has
changed (a re-pair or re-bind); it cannot refuse a different account on the
same binding.

The shared UI retains host-qualified target identities and original request
receipts across navigation and uncertain outcomes. Inspecting a receipt does
not resend a request. Recorded, delivered and completed work remain different
outcomes. A host losing admission must not move a draft or action to another
host; another independently admitted host can remain usable.

Detailed contracts live in [owner access and actions](observable-plane.md),
[local workspace approval](owner-workspace.md) and
[connected sessions and the canonical browser entry](owner-connected-read.md).
A managed Mac is reached over the person's SSH connection instead of HTTPS; its
source contract, including the saved-reference `transport` field, is in
[owner operations over a Mac SSH connection](owner-ssh-carrier.md).
The reciprocal [website and host contract](https://github.com/Claudfather/claudfather.ai/blob/main/documentation/architecture/host-contract.md)
describes the account and document handoff side.

### The computer run's link intent

The computer run (`owner-guide-computer-v1`, prelude word `computer`) can be
asked one thing more on a started computer: a link approval for Claude sign-ins
([owner-connected-read](owner-connected-read.md#a-link-approval-for-claude-sign-ins)).
The client asks only by sending `link_claude: true`, on the prelude's first line
and on the start; without it both are exactly as before, and the run can ask no
Claude card. The key is only ever `true`, and only the computer run takes it;
anything else is `invalid_request`. A link run sends only the hello, the cards
`install_provider` (only when Claude is missing) and `link_provider`, the state
`provider_installing` (`state`, `platform`, `provider_version`), the result
`link_ready` (`state` and the approval's `generation`, a lowercase UUID) and
error frames with `code`, `state` and `retry` (and `next: inspect_status` for an
install that did not finish). It never sends a pairing or workspace state,
`computer_ready`, `computer_starting` or `start_failed`. The `link_provider`
card carries details only: the sign-in scope, the active source revision and the
approval's two windows in seconds; the words a person reads are the website's.

Before any card the run refuses, in this order: the prelude's own
`revision_differs` (against the active source); the run's own admission
(`installation_changed`, `use_full_guide`); then `not_here` (not Linux for now),
`not_started`, `already_linked`, `signin_live` or `signin_unconfirmed` (the
sign-in lease, probed from the run's own process), `busy` (maintenance or an
update in the way), `credential_present` (an empty sign-in line in `.env`) and
`environment_format` (a `.env` the sign-in cannot read). When two hold at once
the first in this order is the answer. Then the started
computer's operator check, the install card and its own refusals
(`provider_install_*`), and the sign-in scope's own refusal. A no or no answer
on the first card is `declined` or `expired` and changes nothing. A yes writes
the approval, whose writer checks the computer again (its refusals end the run
with nothing written), and answers `link_ready`.

Every ending after the install card installed Claude in this run that writes no
approval is that ending's word prefixed `installed_`, so the page can say Claude
is installed but not linked: `installed_declined` and `installed_expired` (the
link card), `installed_` with the scope's refusal (`unsupported_provider`,
`profile_changed`, `file_binding_changed`, `private_path_changed`), with
`scope_changed`, with a writer refusal (`setup_denied`, `use_full_guide`,
`not_started`, `signin_live`, `signin_unconfirmed`, `setup_source_changed`,
`workspace_binding_required`, `workspace_native_owner_required`),
with the channel's own endings (`cancelled`, `disconnected`, `invalid_request`,
`session_closed`, `output_unavailable`, `output_limit`), and
`installed_cancelled` (a signal), `installed_unavailable` and
`installed_internal_error` (any other failure). Without an install in this run
the same endings keep their plain words.

Two writer refusals are decided inside the write, after the person's yes:
`workspace_binding_required` (no active workspace link remains, for example it
was removed or changed after the run read it: a rare race) and
`workspace_native_owner_required` (the caller is not one of the computer's own
local accounts; not expected from the setup page, which always runs as one). Each ends the run with nothing written, as an error frame with
`retry: false`: final for this run, and a new run starts again. After an
install they end as `installed_workspace_binding_required` and
`installed_workspace_native_owner_required`.

### The Claude part and the two run messages

A started computer's parts view draws one Claude part in its provider row. It
reads only the setup state's `provider` object (`link`, `signin`); a value it
does not know makes the whole answer unusable. It never starts a run itself.
Two messages cross between the view and the setup page that embeds it, each
with exactly its keys, a closed set of values, and only between the view's
parent window and the shell origin:

- View to page, `{type: "claudfather.workspace.request-run", version: 1, run}`,
  with `run` `link` or `update`, sent only from a click on the part's button.
- Page to view, `{type: "claudfather.workspace.run-ended", version: 1, run,
  outcome, reason}`. `outcome` is `approved`, `declined`, `timed_out`,
  `refused` or `stopped`; `reason` is `null` except with `refused`, where it is
  one of `revision_differs`, `installation_changed`, `not_started`,
  `already_linked`, `signin_live`, `signin_unconfirmed`, `busy`,
  `credential_present`, `environment_format`, `install_failed`,
  `use_full_guide` or `other`. An `update` run ends only as `stopped` with no
  reason or `refused` with `other`. On `refused` the view hides
  "Go to the update", since the page has no update offer to show, until a
  read with a link marker or a new connection.

Any other shape is ignored. On `approved` the view reads the parts once and
begins the sign-in by itself when a sign-in is offered; on any other outcome it
reads once and returns the part to its button. That read is a fresh one: a
read already in flight was sent before the answer, so one new read follows it.
Only a refused begin (core refuses with 403), or a begin that never reached the
computer, ends at "could not start yet"; a begin whose answer is lost or fails
may have started, so its attempt is kept and Check sign-in progress offered.
When core then refuses that check, which also stops that attempt in this
browser, the step ends at the closing line and the browser releases the session
hold it kept for that begin. A hold never refuses a begin without asking
core once: a begin asked while any earlier begin holds the session first checks
the held attempt, with its own ID and approval. An ending (unknown included) or
a refused check releases the hold and the begin is sent; a live answer
(starting or waiting), or none, keeps it, so two begins are never in flight for
a live attempt. A parts read naming the held attempt, under its approval, as
ended (terminal, or its sign-in lease free) also releases the hold, and so does
one that shows Claude linked (core then refuses every begin), as does a status
naming the held attempt unknown, as after a restart; a read naming
no attempt or another one, or sent under an earlier hold, releases nothing.
"Stop waiting" appears only after ten seconds of waiting. The sign-in step is the module `claude-signin.js`, with no
storage.

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
