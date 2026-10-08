---
id: GRE-0005
title: Keep uv.lock in step with release-please version bumps
status: In Progress
assignee: []
created_date: '2026-10-08 10:57'
updated_date: '2026-10-08 11:35'
labels:
  - ci
dependencies: []
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Release PRs bump the version in each example's pyproject.toml but not the matching editable entry in uv.lock, so after a release any uv run re-locks and leaves uv.lock dirty (seen after adobe-aem 0.3.0 and generic-s3 0.2.0). Decide between a uv.lock updater in release-please (extra-files with a toml jsonpath, if the generic toml updater can address the array entry) or a step that re-locks on the release branch.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A merged release PR leaves uv.lock matching every workspace member's version
- [ ] #2 just check on a fresh checkout after a release leaves the tree clean
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 just check
- [ ] #2 just package-all
<!-- DOD:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Fixed in the 'bump workspace lock files with each release' commit: extra-files entries per package for uv.lock and package-lock.json, a just lint check that each package carries its entry, and just setup syncing with --locked. Updaters tested locally against the real lock files with release-please 17.6.0. AC 2 is proven only by the next real release PR: confirm its diff includes uv.lock, then close.
<!-- SECTION:NOTES:END -->
