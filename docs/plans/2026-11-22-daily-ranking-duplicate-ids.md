# Daily ranking duplicate IDs

The live 2026-11-22 daily refresh rejected OpenAI rankings with repeated
candidate IDs and delayed affected accounts for six hours.

Keep the first occurrence of each valid ID in model order. Append omitted
tracks in the original local candidate order, then run the existing eligibility,
feedback, artist diversity and freshness checks. Cache the normalized ranking.
Preserve validation of the response shape, length, integer type and ID range.
Version the invalid-response cache key so earlier rejections do not block one
attempt under the corrected validator. Keep the same shared budget and lease.

Verify duplicate normalization, paid-call count, old and new cooldown behavior,
and existing selection safeguards. Deploy against the running gevent revision
with a rollback snapshot and verify automatic creation of today's missing
playlists using the usual background job.
