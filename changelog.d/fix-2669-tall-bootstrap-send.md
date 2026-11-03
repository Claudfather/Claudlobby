### Fixed — A startup prompt taller than the pane is submitted (#2669)

The one-prompt bootstrap is taller than the 80x24 pane a bot session starts in. Claude Code's input box then loses its prompt line off the top of the pane, and the send looked for the payload's end only below that line, so it withheld the Enter: the bootstrap stayed in the box unsubmitted, with `BOOTSTRAP — NOT SUBMITTED` in startup.log. With no prompt line in view, the send now reads the bottom of the box: the payload's end must be the last text above the rule drawn under it. The check after the Enter reads the box the same way, so an Enter the box did not take is retried and then reported, never counted as a submit, and a send types nothing into a tall box that already holds text.

### Added — `STARTUP_BOOTSTRAP_ENABLED=0` turns the one-prompt start off (#2669)

Set in the host or root `.env`, it keeps the literal start at each bot's next start: the resume command, then the startup prompt, as two sends, with no plan or activation. The startup.log RESUME line names the switch. On by default.
