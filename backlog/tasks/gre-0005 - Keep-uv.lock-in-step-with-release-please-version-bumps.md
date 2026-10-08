---
id: GRE-0005
title: Keep uv.lock in step with release-please version bumps
status: To Do
assignee: []
created_date: '2026-10-08 10:57'
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
- [ ] #1 A merged release PR leaves uv.lock matching every workspace member's version
- [ ] #2 just check on a fresh checkout after a release leaves the tree clean
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 just check
- [ ] #2 just package-all
<!-- DOD:END -->
