# Daily recommendation personalization

## Problem and scope

The daily playlist uses lifetime play counts without recency or language scoring.
It permits one artist to dominate and defaults to only 30% rotation against the
previous three days. Read-only investigation on 2026-11-12 found 41 K-Pop tracks
and 25 tracks by one artist in a 50-track playlist, despite recent Chinese plays.

Implement server-side ranking and selection changes only. Do not change playback
submission rules, metadata enrichment, schema, client APIs, strict-v2 behavior,
or an already generated daily playlist. No live configuration, database, service,
or playlist mutation is part of this change. Commit and push tested source work.

## Intended behavior

- Blend normalized lifetime play counts (30%) with the last seven days (70%).
  Recent plays decay with a three-day half-life. With no recent activity, use
  lifetime preferences. Keep every historically played ID excluded as before.
- Apply the same fractional weights to genre, artist, album, and semantic
  preferences; do not truncate fractional weights to integer play counts.
- Add a 20% language-match score, retaining a balanced total score of 100%.
  Normalize supported language aliases. Keep Mandarin and Cantonese distinct.
  Use trustworthy language metadata, or explicitly language-specific genre
  labels when metadata is absent. Never infer language from artist, title,
  generic Pop, K-Pop, or J-Pop labels. Unknown languages remain eligible.
- Target at most 10% of requested tracks per primary artist (five of 50).
  Relax the cap only as far as necessary to fill a genuinely small artist pool.
  Artist diversity takes priority when the rotation target is infeasible.
- Raise the default rotation target to 80% against the previous three days.
  Penalize repeated exposure on multiple recent days in the ranking, not just
  random tie-breaking. Preserve explicit rotation configuration overrides.
- Preserve deterministic ordering for the same inputs/day, negative feedback,
  all-history heard-track exclusion, same-day idempotency, and archive behavior.

## Implementation

1. Add bounded recent-history aggregation and fractional semantic/language
   preferences in the listening-profile module, preserving existing public
   listening-statistics responses.
2. Add a pure diversity/rotation selector with a minimal small-catalog fallback.
3. Integrate both into daily generation, update defaults and example settings,
   and add privacy-safe generation metrics for repeat/artist concentration.
4. Route short/feedback-filtered daily-list backfill through the same ranking and
   selection rules. Preserve current daily seed order where constraints allow;
   an existing seed heard after generation can remain, but newly backfilled
   tracks must not come from listening history. Intact playlists stay stable.
5. Add unit and database-backed regression tests plus API/daemon regressions.

## Verification and acceptance

- An old Korean history plus recent Chinese plays raises comparable Chinese
  candidates without deleting history or excluding other languages.
- Missing/low-confidence/unknown language metadata and fractional weights are
  handled deterministically without crashes or fabricated language labels.
- A sufficient candidate pool yields no more than five tracks per artist and
  at least 40 tracks outside the previous three daily playlists for size 50.
- Multiple recent exposures are penalized more than one exposure. Small pools
  still fill when possible, without duplicates or bypassing negative feedback.
- Run focused unittest modules and broader server tests without live DB access.
- After a separately authorized deployment, verify the next generated playlist
  through getRecommendedPlaylists on Android and Windows; collect the response
  and server generation/playlist_served logs. Already-created playlists remain
  unchanged. Real-device and live-runtime acceptance are pending until performed.

## Progress

- [x] Read current implementation and confirm clean source worktree.
- [x] Record design, scope, and acceptance criteria before implementation.
- [x] Implement recency, language, diversity, and rotation changes.
- [x] Run regression tests and review edge cases.
- [x] Record verification results and isolate the scoped source changes.

## Automated verification

Final affected-suite run on 2026-11-12: **209 tests passed**, no failures, errors,
or skips (41.526 seconds). Command, using the existing `supysonic` Conda Python:

```text
python -m unittest tests.base.test_daily_recommendation_personalization tests.base.test_recommend tests.base.test_recommendation_preferences tests.base.test_recommendation_selection tests.base.test_user_listening_profile tests.base.test_recommend_api tests.base.test_recommendation_feedback tests.base.test_recommendation_agent_cache tests.base.test_recommendation_agent_session tests.base.test_daemon_recommend_refresh tests.base.test_home_smart_cards tests.base.test_mood_scene_playlists tests.base.test_daily_mood_scene_playlists tests.frontend.test_home_smart_cards tests.frontend.test_recommendations
```

Coverage includes four successive 50-track daily generations, language-only
ranking comparisons, large old-history versus recent-history changes, fractional
semantic weighting, per-user/window exposure isolation, dislikes/deletions and
API refill, preservation of current daily seeds, and small-catalog relaxation.
The pure selector also checks 324 artist-pool combinations inside its test cases.
Independent integration review found no remaining actionable findings.

Broader `python -m unittest` run: **2274 tests, 8 failures, 73 errors, 11 skips**
(516.244 seconds). This is not a full-suite pass. Observed failures are outside
the daily recommendation tests: Windows open-log/database-file cleanup errors
(`WinError 32`), short/long temporary-path comparisons, folder-hierarchy deletion
assertions, a Windows log-path formatting assertion, and an architecture check
against the concurrently added `metadata_budget.py`. The shared worktree was
being edited for separate metadata work during this run; it is not a clean
baseline comparison. These unrelated changes are excluded from this commit.

No live service restart, database mutation, daily-playlist replacement, or
Android/Windows device validation was performed. The source push uses a feature
branch; any automatically triggered CI/image build is separate from deployment.

## Manual acceptance (not run)

Run after a separately authorized server deployment and next daily generation.
No Android or Windows client rebuild is required for these server API changes.

1. On both Android and Windows, sign into the same account and refresh the
   discovery recommended list once. Record platform/version, local time, list
   date, returned song IDs/artists/languages, and expected versus actual counts.
   With a sufficient candidate pool, expect 50 tracks, at most five per primary
   artist, and at least 40 IDs absent from the preceding three daily playlists.
   Explicit rotation overrides and small-catalog relaxation must be noted.
2. Compare the following day's recommendation after naturally completing recent
   Chinese tracks. Expect Chinese-language candidates to receive a higher
   preference score; do not require all recommendations to be Chinese. Record
   actual songs heard, natural completion versus skipped playback, and whether
   the recently played songs have usable language metadata/genre labels.
3. On each platform, dislike one recommendation with one tap and refresh once.
   Expect it to remain absent, a suitable unheard replacement when available,
   no duplicate song IDs, and the artist limit to remain satisfied. Record
   whether one tap succeeded and whether any audio/track state unexpectedly
   changed. The change must not trigger playback or alter the current queue.
4. Refresh again without changing feedback. The intact stored daily list must
   not regenerate just because of a refresh. A daily list created before this
   deployment also stays unchanged; do not interpret that as failed ranking.
5. Collect bounded server logs containing `recommend_refresh_completed`,
   `event=daily_personalization`, `event=playlist_served`, and, for step 3,
   `event=feedback_updated`. New generation metrics include `track_count`,
   `fresh_count`, `recent_repeat_count`, `artist_count`, `max_artist_tracks`, and
   `rotation_target`. API logs include `backfilled_track_count`.
6. Return both platform logs and the observation table. Windows source is
   `C:\Users\jsdfhasuh\Documents\debug.log` (read-only). For Android, use the
   existing app log export or read-only ADB log collection after checking
   `adb devices -l`. Collect recommendation HTTP status/count evidence and
   `SCROBBLE_DURABLE` / successful submission or `SCROBBLE_PENDING` if emitted by
   that client version. Redact authentication query parameters and tokens.

Actual result: pending user validation on both platforms. These steps are
personal-lab acceptance, not production-readiness evidence.
