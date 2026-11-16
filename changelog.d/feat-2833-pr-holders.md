### Added — `pr holders`: one read names the open PRs that change given paths (#2833)

`claudlobby --json pr holders PATH... --repo OWNER/REPO` lists, for each PATH, every open PR of the repository that changes it, with the PR's number, draft state, base branch and last update. A file matches on its name and on its previous name (a rename or a copy in the PR), and each match says which. Separately, it lists each open PR that changes a file with the same file name at another path, without counting it as a holder: a PR written before a directory move changes the file at its old path. The result also says how many open PRs it read, and how many files.

It fails closed. The listing counts as complete only when the PRs read match GitHub's count of open PRs. A PR's file list counts as complete only when the files read match GitHub's count of its changed files, each file once. When the listing, or any PR's file list, cannot be read in full, the read exits 6 (`unavailable`), and the error names each such PR. REST lists at most 3,000 files, so a PR with more is unreadable.

It reads only GitHub, through `gh`. It makes one GraphQL request per 50 open PRs, plus a REST read of every page for a PR that changes more than 100 files, or that renames or copies a file (GraphQL does not give the old name).

A syntax error under `pr holders` is still argparse's usage refusal: exit 2, and nothing on stdout, even with `--json`. Every other answer is one schema-1 result. No composed permission grants the command to bots yet; that is a separate change.
