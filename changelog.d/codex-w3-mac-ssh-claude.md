### Added — Owner operations over a Mac SSH connection (source only)

A managed Mac has no admitted HTTPS owner door, so the browser can now carry
owner operations over the SSH connection the person already authenticated. The
guide prelude gains a closed `api` mode that runs `host owner api --stdio`: one
typed operation per process, with acknowledged chunks and an `end` frame sent
only after the operation has drained. Cancelling before any response ends
normally with exit 0. The caller's identity comes from the server's own
`SSH_CONNECTION`, captured before the environment is replaced and resolved
through the same Tailscale realm and WhoIs checks as the HTTPS door; no frame or
header selects it.

On such a Mac the typed guide runs without a listener or an owner service. Its
directory approval also creates one private settings file that leaves the owner
connection service unenrolled; an existing file is never replaced or repaired.

Saved computer references may carry `transport`. An absent field is the
historical HTTPS reference, `mac_openssh` is an SSH computer, and any other
value is refused. The Plane UI never fetches an SSH computer over HTTPS.

Provider sign-in has its own profile, `owner-signin-v1`: one process keeps one
policy app and one attempt for a bounded conversation. Begin is the first call;
status, code and cancel follow as numbered `call` frames, each checked afresh by
the same policy route and answered by one `reply` holding the whole response. A
reply is a receipt for one call only. `end` still means the complete drain,
including the joined sign-in worker, and is sent once. A conversation lasts at
most 330 seconds, with 20 seconds observed per call and 45 seconds idle; an
expired bound starts the drain and is not proof that anything stopped. Ordinary
one-operation processes still refuse begin, code and cancel, and still read
status. This is checked with synthetic fixtures only: a real sign-in over SSH,
real Remote Login, source-address and principal behavior on a Mac have not been
observed. Linux and HTTPS behavior is unchanged.

The `api` prelude mode admits the exact current installed source after an
ordinary source advance without a finish tuple, keeping the original preparation
binding and source verification. `end` now waits for successful lifespan
shutdown, and a returned handler without a complete final body (or an explicit
cancel) is refused as `response_incomplete` instead of ending normally. An explicit caller cancel stays
cancelled even after the final body, and a component shutdown failure neither
replaces an earlier failure (an incomplete response included) nor skips the
remaining drains or the control-reader join.

A call is now admitted only after the final byte of the previous reply is
written and committed, so a call read while a reply is blocked or partly written
is refused as `invalid_request` and never promoted, whatever the timing (input
reads and the final write share one short-step I/O owner; no lock-time threshold).
Begin waits for the renewal actually in flight, the guided panel survives the
confirmed pre-begin renewal, and provider-required Preview/Launch need a matching
connected attempt. The setup panel follows the original
sign-in attempt with one serialized status check about every two seconds, holds
Preview and Launch while it is open, and the connected client holds session
renewal (renewing first only when the session would end inside the attempt)
until the attempt reaches a terminal result or the connection ends. Test-owned
gates and threads in the SSH API tests are now released and joined on failure.

Under output backpressure the sign-in reply writer uses nonblocking steps, so a
peer that stops reading cannot hold the carrier's I/O owner. A cancel or lost
channel halts the reply write, starts the full drain and joins the writer; a
truncated replies never produce a successful `end`; cancellation during delivery is incomplete and earlier carrier errors remain preserved.
