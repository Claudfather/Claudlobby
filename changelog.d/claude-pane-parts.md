### Added — the workspace shows a started computer's parts (#3082)

A connected session on a started computer can ask for its parts with one read
that needs no setup approval and writes nothing: `state` on the setup door,
`setup-state` on the SSH route and in the browser carrier's list. Claude counts
as linked exactly when a sign-in line with a value is saved on the computer,
never from an approval or an attempt's record, and the line's value is never
read out; the team count is the saved configuration's, shown as "1 team set
up" for one team and never as running.

The pane draws the answer as a parts view (this computer, Claude, bots, and the
helper only when the embedding page names it) with no working control: the whole
view with no team, a strip above the dashboard with one. It is read once for each
new connection and again when the window regains focus; a renewal keeps it
shown. On a bridged page the five-step panel stays the view whenever the read
has no answer or says not started, and, with no parts read sent, while it
follows a Launch this page sent or a hand-off it followed, or the read says a
dispatched Launch has not finished cleanly (so a reloaded page keeps Check
launch status, and shows no "nothing runs yet" line or unapproved step for it
before reading anything); a computer in
first-team setup is never sent the read, and one whose core predates it shows
what it does today. The connected document may carry `part_names` for the
provider, bots and helper (never a computer), checked strictly and dropped whole
when malformed. Its sentences are the owner's approved wording (2026-11-23),
and nothing is visible until the website's pin moves.
