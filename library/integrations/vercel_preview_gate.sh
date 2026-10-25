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
#    deploymentEnabled keeps Dependabot branches from deploying at all. Each
#    quoted argument names more paths that never reach the build, as a git
#    pathspec glob from the top of the repository: * stays inside one folder and
#    ** crosses folders (unlike the rollout check's docs-paths, where * crosses).
#    Name the repo's docs, and leave out any path the site renders.
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
# --no-renames: a page moved into docs/ is a page deleted, which renders.
rendered=$(git diff --name-only --no-renames "$base" HEAD -- ':/' "${excludes[@]}" 2>/dev/null) ||
  build "cannot compare with ${base:0:12}"
nl=$'\n'
first=${rendered%%"$nl"*}
[ -z "$first" ] || build "$first can change what the site renders"
changed=$(git diff --name-only "$base" HEAD 2>/dev/null) || build "cannot compare with ${base:0:12}"
[ -n "$changed" ] || build "nothing changed since ${base:0:12}, so this is a redeploy"
# An empty commit is the way to ask for a preview this script would skip.
git diff --quiet HEAD^ HEAD 2>/dev/null && build "the head commit changes no file"
skip "nothing changed since ${base:0:12} reaches the build"
