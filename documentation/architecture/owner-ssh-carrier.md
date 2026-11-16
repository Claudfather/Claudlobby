# Owner operations over a Mac SSH connection

A managed Mac has no admitted HTTPS owner door. The browser reaches it over the
SSH connection the person already authenticated, and that connection carries the
same owner operations the HTTPS door serves. This page is the source contract.
It has been exercised with synthetic fixtures only: real Remote Login behavior,
the source address a real `sshd` reports and the native principal it resolves to
have not been observed on a Mac, and nothing here claims they have.

The Linux and HTTPS paths are unchanged. The private Unix socket verifier, the
grant and workspace schemas and the Tailscale principal namespace are the same
ones every other transport uses.

## Entry

The immutable prelude `claudlobby/preinstall/owner-guide-v1.sh` has three closed
modes: `setup`, `status` and `api`. `api` reads the same one-line installation
admission as the guide, refuses when the session has no `SSH_CONNECTION`, and
then executes the admitted stable launcher as `host owner api --stdio`. The
prelude passes the server's `SSH_CONNECTION` through without reading it for
identity. The browser sends a fixed command with no environment requests.

One `api` process carries exactly one operation and exits after its drain. It
holds the stable launcher's runtime reader for that time only.

## Who is asking

`claudlobby/plane/owner_ssh.py` captures `SSH_CONNECTION` once, before the fixed
profile replaces the environment. The four fields must be canonical, both
addresses must be tailnet addresses, and the destination must be one of the
addresses the native daemon reports for itself. A missing, malformed or
mismatched tuple refuses; `SSH_CLIENT` is never used instead.

The captured client address is resolved through the same bounded policy as the
HTTPS door (official realm, then WhoIs, then official realm again), on every
operation. No frame, header, account name, UID or existing grant selects the
principal. The Unix socket verifier is not reused with a fabricated scope.

This rests on the existing trusted-account boundary. A process running as the
same account, or as root, can rewrite its own environment; that adversary is out
of scope here exactly as it is for the private socket.

## Frames

Frames are canonical sorted ASCII JSON, one per line, with `version`, `type`,
`seq` and `session_id`. Unknown or duplicate fields refuse.

| Direction | Frame | Fields beyond the common four |
|---|---|---|
| server | `hello` | `profile: "owner-api-v1"` |
| client | `start` | `profile`, `expected_uid`, `source_revision`, `binding_sha256`, `operation`, `input`, `token`, `last_event_id` |
| server | `headers` | `status`, `headers` (only `content-type`, `cache-control`, `retry-after`) |
| server | `chunk` | `chunk_id`, `data` (base64, at most 16384 decoded bytes) |
| client | `ack` | `chunk_id` |
| client | `cancel` | none |
| server | `end` | `status`, `bytes_sent`, `cancelled` |
| server | `error` | `code`, `retry: false` |

The server sends one chunk and waits for its `ack`. `end` is sent only after the
policy handler has returned or been cancelled and drained, the application has
shut down and the control reader has joined. A normal `end`, cancelled or not,
exits 0. A refusal sends `error` and exits 4. A cancel that arrives before any
response headers ends with `status: 0, bytes_sent: 0, cancelled: true` and exits
0; the operation never reaches a policy handler if the cancel arrived first.

A channel that closes without a complete `end` and a process exit is unknown to
the caller. Nothing is replayed: a mutation keeps its original request ID and is
recovered by inspecting that original.

The operation names and input fields are a closed set compiled into internal
routes (`operation_request` in `claudlobby/commands/owner_api_stdio.py`). There
is no caller-selected method, path, header, command or environment. Cookies do
not cross this transport.

## Operations this carrier refuses

`setup-signin-begin`, `setup-signin-code` and `setup-signin-cancel` refuse with
`operation_requires_persistent_owner` before any policy code runs. The provider
sign-in attempt lives inside one process: its worker thread, its terminal child,
the sign-in link and the code hand-off are held in memory, and the retained
record admits exactly one original attempt. A one-operation process would start
that attempt and then cancel it in its own mandatory drain, leaving the single
attempt spent. `setup-signin-status` remains available as a read.

Until a reviewed owner for that attempt exists, a setup that was approved with
browser sign-in cannot reach Preview or Launch over this carrier. That is an
open gap in this path, not a supported flow.

## The guide on an SSH-controlled Mac

The typed guide (`host owner setup --stdio`) captures the same server tuple when
its fixed profile is the managed Mac runtime. It then runs without a listener:

- its `prepare_directories` proposal also creates one private settings file,
  the host override, containing only `plane-owner: enroll: false`. The file is
  created only when absent. An existing file is never read for repair,
  replaced or rewritten, and an existing policy that still enrolls the owner
  service stops the guide;
- pairing, workspace approval and setup approval use the same local
  confirmations as every other transport, while the browser's side of each
  arrives as a sibling `api` operation;
- its `pairing_ready`, `workspace_ready`, `preview_launch_ready` and
  `launch_handoff` frames carry `transport: "mac_openssh"`.

No owner service is enrolled to keep the computer reachable. Reopening after the
handoff means a fresh authenticated SSH connection.

## Launch

The updater cannot advance while any process still holds a runtime reader. The
website stops admitting sibling operations, cancels each active one, and waits
for its `end` and process exit before sending `setup-launch`. The launch
operation returns the original receipt and exits. The guide observes that
retained original Launch, returns `launch_handoff` and exits. A lost reply is
unknown; recovery inspects the original tuple and never sends Launch again.

## Saved references

A saved reference may carry a `transport` field.

| `transport` | Meaning |
|---|---|
| absent | A historical reference: HTTPS, as before |
| `"mac_openssh"` | An SSH computer; reopening needs a fresh authenticated connection |
| anything else | Refused |

The `endpoint_origin` stays the logical authority key in both cases. For an SSH
computer it is not a claim that HTTPS is reachable. The Plane UI carries an SSH
reference only through the browser owner transport: without it the workspace
document is refused, and no native fetch to the origin is ever made. A reference
that gains or loses its transport is not the same reference. Passwords, handles
and bearer tokens are never part of a saved reference.
