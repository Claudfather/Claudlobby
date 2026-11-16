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

Provider sign-in operations are refused over this carrier, because one sign-in
attempt cannot outlive a one-operation process. A setup approved with browser
sign-in therefore cannot reach Launch this way yet. This is checked with
synthetic fixtures only: real Remote Login, source-address and principal
behavior on a Mac have not been observed. Linux and HTTPS behavior is unchanged.

The `api` prelude mode admits the exact current installed source after an
ordinary source advance without a finish tuple, keeping the original preparation
binding and source verification. `end` now waits for successful lifespan
shutdown, and a returned handler without a complete final body (or an explicit
cancel) is refused as `response_incomplete` instead of ending normally.
