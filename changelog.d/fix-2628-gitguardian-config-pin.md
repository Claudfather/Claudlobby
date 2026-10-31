### Fixed — the GitGuardian skill's tests pin the config text its helper gives curl, and the shipped cap on hashes (#2628)

`test.sh` now requires the config that `gitguardian_api.sh` writes to curl's
stdin to be exactly the one header line that carries the token, for every
request. A line more, such as one that sets a method or adds a URL, would
change the call while its arguments stayed the same, and the suite only looked
for the header before. A new test pins the shipped `MAX_HASHES` of 1,500; the
test that lowers the cap shows that the search reads it. Two comments in
`triage.py` now say the stored content looks like a patch, as its top comment
does. The helper itself is unchanged.
