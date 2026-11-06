### Fixed — An unfinished update's notice reaches someone in more cases, and quotes no credential (#2715)

The notice an update's job sends when a run leaves the update unfinished (#2702) gave up when its first recipient refused: a half-written `bot.conf` or a revoked sender token ended it. It now tries each bot with an alert chat in turn, in the order `resolve_alert_target` walks them, until one accepts, and the job's log says how many refused.

It runs the copies of `lib-common.sh`, `cli-context.sh` and `tg-post.sh` that the update retains at quiesce, beside the supervisor scripts it already keeps, not the ones the update has just installed, so a broken script in the new release cannot silence it. An update prepared before this change runs the live scripts, as before. So does one whose retained copies no longer match, and the job's log says so.

A run refused before it reaches the update now sends the notice too: its retained support changed, its native unit differs from the one bound, or its dispatch record is missing. A run that cannot show it is the update's job (no bound job, or membership refused or unknown) still sends nothing, since it may be a hand-run `_run` waiting while the job itself runs.

Text the notice quotes from a failed stage or a refusal is redacted first: a token, a key, a password and similar shapes become `[redacted]`. `host update status` still shows the whole line. A notice stopped at its time limit now gets SIGTERM before SIGKILL, so each bash in it runs its exit trap and removes its scratch files, among them the token-bearing curl config `tg-post.sh` writes. Only a child that ignores SIGTERM, which `curl` does not, can still hold that cleanup past the SIGKILL.

An update job keeps the coordinator it started with. So the job run that installs this change runs without it, and every job run that starts after that run's commit stage has it, a `host update continue` of the same update included. The retained scripts start with the next update's quiesce.
