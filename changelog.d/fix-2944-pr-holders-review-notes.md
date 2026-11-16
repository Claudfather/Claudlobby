### Changed — `pr holders` lists the open PRs that change files under a PATH that names a directory (#2944)

A PATH is a file's path, and a PR holds it only when it changes that file. So a directory named without a trailing slash read as changed by no open PR, even while open PRs changed files under it. Each path's answer now also lists, apart from its holders, each open PR read in full that changes files under PATH and a slash, matched on a file's path or its previous path (`under`). The text answer adds a line that names those PRs and how many of their files are under it. They are not counted as holders, and which PRs hold a path is unchanged: a PR that changes PATH itself is listed as a holder only.

### Changed — `pr holders` reads a renamed or copied file with no previous name as unreadable (#2944)

GraphQL names a renamed or copied file by its new path only, so a PR with one is read again through REST for the old path. A REST row whose status is renamed or copied but which gave no previous name counted as a plain file, and its old path could not match. Such a row now leaves its PR unreadable, so the read exits 6 and names the PR rather than giving a clean answer. No such row has been seen in a real read.
