### Removed — `claudlobby pr holders` and the `pr` command group (#2833)

`claudlobby pr holders`, the read that listed the open PRs of a repository that change given paths, is removed (it was added in #2942 and changed in #2951), and so is the `pr` command group, which held only it. `claudlobby pr ...` is now refused as an unknown command: argparse's usage error, exit status 2, with nothing on stdout, even under `--json`. Nothing else in the repository called the command, so no other command, script, skill or guide changes.
