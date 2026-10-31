### Fixed — the GitGuardian skill's test compares the config curl reads for the EU host too (#2632)

The check that sets `GITGUARDIAN_API_URL` to the EU host in `test.sh` now calls
`token_kept`, as 23 other checks do, so the config that `gitguardian_api.sh`
writes to curl's stdin for that host must be exactly the one header line that
carries the token. A line written only for that host, such as one that sets a
method, passed the suite before and now fails that check. The helper itself is
unchanged.
