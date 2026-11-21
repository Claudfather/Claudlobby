### Added — the workspace shows a started computer's parts (#3082)

A connected session on a started computer can ask for its parts with one read
that needs no setup approval and writes nothing: `state` on the setup door,
`setup-state` on the SSH route and in the browser carrier's list. Claude counts
as linked exactly when a sign-in line is saved on the computer, never from an
approval or an attempt's record, and the line's value is never read out.

The pane draws the answer as a parts view (this computer, Claude, bots, and the
helper only when the embedding page names it) with no working control: the whole
view with no team, a strip above the dashboard with one. It is read once for each
new connection and again when the window regains focus. On a bridged page the
five-step panel stays the view whenever the read has no answer or says not
started, and while it follows a launch this browser asked for; a computer in
first-team setup is never sent the read, and one whose core predates it shows
what it does today. The connected document may carry `part_names`, checked
strictly and dropped whole when malformed. Its sentences are drafts awaiting
approval, and nothing is visible until the website's pin moves.
