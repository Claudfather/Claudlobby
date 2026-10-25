#!/usr/bin/env bash
# vercel_preview_gate — the Ignored Build Step behind the preview rule in
# library/integrations/vercel.md: a pull request gets a Vercel preview only when
# it changes what the site renders. Production deployments build as before.
#
# Vercel runs the command in the project's Root Directory before each build and
# reads its exit status: 1 builds, 0 skips (the deployment ends CANCELED, and
# still counts toward the deployment quota and the build slots). Every case this
# script cannot judge builds: no environment, a branch's first deployment, a
# history it cannot read, any error (Vercel builds on every status but 0).
#
# Apply it to a site repo in one PR; no Vercel token is needed:
# 1. Copy this file unchanged to scripts/vercel_preview_gate.sh under the
#    project's Root Directory. A bot copies it from
#    "$CLAUDLOBBY_LIBRARY_DIR/integrations/" and names its sha256 in the PR.
# 2. Merge these keys into the project's vercel.json (or vercel.ts, vercel.toml):
#
#      {
#        "git": { "deploymentEnabled": { "dependabot/**": false } },
#        "ignoreCommand": "bash scripts/vercel_preview_gate.sh '*.md' 'docs/**'"
#      }
#
#    deploymentEnabled keeps Dependabot branches from deploying at all. Its
#    dependabot/** matches the names Dependabot gives its branches by default,
#    dependabot/<package-manager>/<dependency>. A repo whose dependabot.yml sets
#    pull-request-branch-name (another separator, prefix or template) needs a
#    pattern for its own names.
#    Each quoted argument names more paths that never reach the build, as a git
#    pathspec glob from the top of the repository: * stays inside one folder and
#    ** crosses folders (unlike the rollout check's docs-paths, where * crosses).
#    Name the repo's docs, and leave out any path the site renders. The globs
#    count from the top in a subfolder project too: a project whose Root
#    Directory is apps/site names its docs 'apps/site/docs/**', not 'docs/**'.
# 3. Before the PR merges, read which checks the repo's branch protection and
#    rulesets require. If a Vercel check is among them, a Dependabot pull
#    request may wait on it for good: its branch makes no deployment, so
#    presumably no Vercel status ever reports (not seen on a project). What a
#    skipped preview reports there is not verified either. A repo that requires
#    one should settle both first.
# 4. After the merge, confirm it: on a branch whose preview has built, push a
#    commit that changes only docs. Its deployment should end CANCELED.
#
# What only the Vercel project's settings can do (a team Owner or Member, or the
# project's administrator):
# - Environment Variables, "Enable access to System Environment Variables": must
#   stay on, or this script sees no VERCEL_ENV and builds everything.
# - Build and Deployment, Ignored Build Step: ignoreCommand replaces it, so read
#   it first and carry anything it does into the PR.
# - Redeploy with "Use project's Ignore Build Step" unchecked builds a skipped
#   preview. Without that access, push an empty commit.
#
# A skipped preview is also a skipped Vercel build: a dependency bump or a test
# change that breaks the build is first built by the production deployment after
# merge, so keep a build of the site in the repo's CI.
#
# To measure what the gate would skip on a repo before applying it, replay the
# repo's recent previews through it:
# - gh api --paginate "repos/OWNER/REPO/deployments?per_page=100" lists GitHub's
#   record of each deployment: its id, sha, environment and created_at. For
#   Vercel's deployments ref is the commit sha, not the branch. Keep the
#   project's preview environment, oldest first.
# - A deployment's branch is the head of the pull request that holds its commit:
#   gh api "repos/OWNER/REPO/commits/SHA/pulls" --jq '.[0].head.ref'. A preview
#   with no pull request has no branch here: count those apart.
# - gh api "repos/OWNER/REPO/deployments/ID/statuses" lists one deployment's
#   statuses: it built if one of them has the state "success".
# - A preview's VERCEL_GIT_PREVIOUS_SHA is the sha of the latest earlier preview
#   of its branch that built and that the replay did not skip, or empty.
# - Fetch its commit as Vercel clones it (git init, git fetch --depth=10 URL SHA,
#   git checkout FETCH_HEAD). In its Root Directory, run this script with the
#   arguments the PR will use, VERCEL_ENV=preview and that previous SHA.
# Count the builds (exit 1) and the skips (exit 0). Count apart the previews of
# dependabot/ branches, which the key turns off, and any commit that is gone.
#
# Reads VERCEL_ENV, VERCEL_TARGET_ENV and VERCEL_GIT_PREVIOUS_SHA, the commit of
# the branch's last successful deployment, which Vercel sets only when an
# Ignored Build Step is configured and leaves empty on a branch's first
# deployment.

NEVER_RENDERED=(
  '.github/**'
  'tests/**' 'test/**' 'e2e/**' 'cypress/**' 'playwright/**' '**/__tests__/**'
  '**/*.test.[jt]s' '**/*.test.[jt]sx' '**/*.test.[cm][jt]s'
  '**/*.spec.[jt]s' '**/*.spec.[jt]sx' '**/*.spec.[cm][jt]s'
  'vitest.config.*' 'jest.config.*' 'playwright.config.*' 'cypress.config.*'
)

build() { echo "vercel_preview_gate: build: $*"; exit 1; }
skip() { echo "vercel_preview_gate: skip: $*"; exit 0; }

[ "${VERCEL_ENV:-}" = preview ] || build "not a preview deployment (VERCEL_ENV=${VERCEL_ENV:-unset})"
[ "${VERCEL_TARGET_ENV:-preview}" = preview ] || build "the $VERCEL_TARGET_ENV environment"
base=${VERCEL_GIT_PREVIOUS_SHA:-}
[ -n "$base" ] || build "the branch's first deployment, with no earlier build to compare"
excludes=()
for glob in "${NEVER_RENDERED[@]}" "$@"; do
  excludes+=(":(top,exclude,glob)$glob")
done
# A page moved into docs/ is listed by its old path, which renders: git drops the
# excluded new path before it looks for renames (--no-renames is a second guard).
rendered=$(git diff --name-only --no-renames "$base" HEAD -- ':/' "${excludes[@]}" 2>/dev/null) ||
  build "cannot compare with ${base:0:12}"
nl=$'\n'
first=${rendered%%"$nl"*}
[ -z "$first" ] || build "$first can change what the site renders"
changed=$(git diff --name-only "$base" HEAD 2>/dev/null) || build "cannot compare with ${base:0:12}"
[ -n "$changed" ] || build "nothing changed since ${base:0:12}, so this is a redeploy"
# An empty commit is the way to ask for a preview this script would skip. Only a
# diff git reports (status 1) goes on to the skip: a parent it cannot read builds.
git diff --quiet HEAD^ HEAD 2>/dev/null
head_diff=$?
[ "$head_diff" -ne 0 ] || build "the head commit changes no file"
[ "$head_diff" -eq 1 ] || build "cannot compare the head commit with its parent"
skip "nothing changed since ${base:0:12} reaches the build"
