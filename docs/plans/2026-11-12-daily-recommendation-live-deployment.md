# Daily recommendation live deployment

## Authorized scope

The user authorized deploying the daily recommendation changes and replacing
their already-generated daily playlist on 2026-11-12. The runtime is OpenWrt,
accessed through the existing `openwrt` SSH alias. Do not use the local Docker
daemon as evidence of that runtime.

The live server was verified at metadata-enrichment commit
`a8b5fcfcdec3407470988f4d904c4c1f01aa024a`. That branch and recommendation commit
`e94c6224b5fcbc7edb1c5fcb61b3388c7040cff8` are siblings. Deploy a combined source
tree based on the live commit with the recommendation commit cherry-picked;
never replace the live metadata functionality with the older recommendation
branch alone. Preserve the existing runtime configuration and secret mounts.

## Deployment steps

1. Create an independent worktree and combined branch, leaving other worktrees
   and their uncommitted edits untouched.
2. Run focused recommendation, API, profile, and metadata regression tests.
   Record the combined source revision and inspect the image provenance before
   cutover. Do not equate build completion with runtime deployment.
3. Inspect the live container, storage mounts, service guard, and current
   deployment procedure. Save protected copies of the current container
   configuration, configuration file, and database backup before replacement.
   Keep credentials and backup contents out of repository files and logs.
4. Stage and verify the combined image, preserving the live container's runtime
   contract, metadata background behavior, mounts, limits, and service guard.
   Stop/recreate only the named Supysonic service during the final cutover.
5. Verify the running revision, HTTP/API response, daemon availability, schema,
   and retained listening history. Do not change other containers or playlists.

## Today's playlist replacement

- Resolve exactly one root-owned daily recommendation playlist for 2026-11-12,
  using the expected generated name and recognized recommendation comment.
  Abort if the date or match is ambiguous.
- Preserve a protected complete-row backup, including the original raw `tracks`
  field and checksum. Avoid `get_tracks()` during backup because it may remove
  missing track IDs and save the row.
- Compute a fresh list with the deployed ranking, recent-play weights, language
  preferences, feedback exclusions, artist cap, and recent daily exposure
  penalties. Do not seed selection with the old list.
- Do not call ordinary daily regeneration: its same-day guard reuses the old
  playlist, and its archive operation can mutate other rows first.
- Replace only `Playlist.tracks` in a transaction with a compare-and-swap guard
  matching the original row and raw track value. Require exactly one update.
  Preserve the playlist ID, name, creation time, comment, and public flag.
- Read back the stored order and the actual recommendation API response.

## Verification and rollback

- Verify 50 unique tracks, no historically played or blocked new candidates,
  artist concentration, effective rotation settings, recent-list overlap, and
  language distribution. Report any limited-catalog fallback accurately.
- Compare other playlist rows and playback-history state across the surgical
  write, allowing independently evidenced concurrent listening.
- Retain the old image/container definition and configuration for service
  rollback. Restore the old database snapshot only when required and after
  assessing newer legitimate writes; an image rollback alone does not undo
  database mutations.
- Playlist rollback restores only its backed-up raw track value, guarded by a
  compare-and-swap against the replacement value to avoid overwriting later
  user changes.
- Android and Windows real-device validation remains pending user validation.
  No client rebuild is needed. Refresh recommendations on both clients and
  verify the same playlist ID now serves the new song order without starting
  playback or changing the current playback queue.

## Execution status

- Combined branch prepared without conflicts.
- Automated combined-tree verification: 294 tests passed in 51.062 seconds on
  2026-11-12, with no failures, errors, or skips. This includes the 15 affected
  recommendation/profile/API/home-card/mood modules listed in the original
  recommendation plan plus `test_metadata_budget`, `test_metadata_pricing`,
  `test_track_metadata_background`, `test_track_metadata_budget_routing`, and
  `test_track_metadata_web`. The existing Conda `supysonic` Python was used.
- A full-suite rerun was not performed in this worktree; the focused result does
  not establish full-suite or real-device acceptance.
- Runtime deployment and playlist replacement: handled separately by the
  deployment operator; source preparation does not establish runtime success.
