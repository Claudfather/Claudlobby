# Changelog fragments

Each PR adds its changelog entry here, as a file of its own, instead of editing
`CHANGELOG.md`. A PR that edits `CHANGELOG.md` conflicts with every other open PR
that does; a new file conflicts with nothing. Once a week, a roll-up moves the
files into `CHANGELOG.md` and deletes them.

## Your fragment

- **Name:** `changelog.d/<head branch>.md`, with each `/` in the branch name written
  as `-`. The branch `fix/1234-short-name` adds `changelog.d/fix-1234-short-name.md`.
- **Text:** the entry exactly as it should read in `CHANGELOG.md`, a `### ` heading
  that names one category, then what changed and why:

  ```markdown
  ### Fixed — what a reader would notice, in one line (#1234)

  What changed and why, in a few sentences.
  ```

- **Categories:** Added, Changed, Deprecated, Removed, Fixed, Security, Docs.
- One file can hold more than one entry, each under its own `### ` heading.
- A PR that needs no entry says why in its description, on a line of its own:
  `Changelog: none — <why>`.

## The check

The `Changelog fragment` check (`.github/workflows/changelog.yml`) passes a PR that
adds an entry to its fragment, or whose description says why it needs none. It
refuses a fragment under any other name, a heading outside the categories, and a
new entry written straight into `CHANGELOG.md`. It runs the default branch's copy
of `bin/changelog.py`, so a PR cannot change the rules it is checked by.

## The weekly roll-up

On a branch off fresh `main`, run:

```bash
python3 bin/changelog.py assemble
```

It moves every fragment into `CHANGELOG.md`, directly under `## [Unreleased]` and
newest merge first, and deletes the fragments. Commit the result unchanged and open
it as an ordinary PR with `N/A: docs-only` under its `## Rollout check`. The check
recomputes the roll-up from the fragments it deletes and refuses one that drops,
adds or changes anything. With `--version X`, the entries go under a new
`## [X] - <date>` heading instead.
