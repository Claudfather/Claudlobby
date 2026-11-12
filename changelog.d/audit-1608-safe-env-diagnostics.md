### Fixed — rejected environment values stay out of parser diagnostics (#1608)

The restricted `.env` reader now reports the filename, physical line number,
and rejection reason without copying input into startup or job logs. Accepted
input and rejection rules are unchanged. It does not alter existing logs, and
it does not complete the parser-parity work in #1608.
