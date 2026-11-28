# Bilibili Recommendation Source

## ADDED Requirements

### Requirement: Merge verified recordings into the existing daily candidate pool
When a new AI daily playlist is eligible for the configured target user, the system SHALL merge only ready Bilibili recordings that resolve to current, playable, eligible local Track UUIDs in the same library. It SHALL apply the existing unplayed and negative-feedback filters. A verified recording MAY originate in a directly identified song or a supported artist/style exploration. An artist name, a genre, an Apple song ID or an Oracle receipt alone MUST NOT be ranked as a local Track.

#### Scenario: Full candidate pool
- **WHEN** the existing candidate capacity is K = min(150, 3 × requested playlist size) and K is positive
- **THEN** the system SHALL admit at most min(10, max(1, K // 10)) distinct additional UUIDs. It SHALL prefer the most recent ready time, then the most recent source day, then ascending Track UUID. It SHALL use unused capacity first, remove only unannotated tracks from the baseline tail to fit (admit fewer if necessary), and append admitted new tracks in that stable order.
- **AND** the remaining baseline tracks SHALL retain their relative order. A UUID already in the baseline SHALL carry at most one source annotation and SHALL NOT consume a new-track allowance. A zero capacity SHALL produce no injection.

#### Scenario: Multiple observations of one track
- **WHEN** several observations or routes link to the same UUID during its 14-day ready window
- **THEN** the merge SHALL include that UUID at most once using the same deterministic ordering and SHALL NOT treat repeated views as plays or as an independent score multiplier.

#### Scenario: Eligibility changes
- **WHEN** a recording is expired, no longer indexed or playable, already played, or excluded by current feedback
- **THEN** the system SHALL omit it from the source. Before persisting the playlist it SHALL recheck eligible Tracks and the current evidence for source-added recordings, including changes during the model request.

### Requirement: Rank the merged pool through the configured daily model
When source evidence is present, the system SHALL use the existing daily ranker, request-local IDs and ranking validation. It SHALL supply only provenance and age, and preserve freshness, diversity, feedback and local weights. The daily prompt MUST NOT contain viewing text, BVIDs, uploader names, paths or credentials. No source SHALL reserve a final slot.

#### Scenario: Sol ranking and explicit Luna configuration
- **WHEN** daily ranking uses the existing default gpt-6.2-sol
- **THEN** the input SHALL retain the available detailed candidate data and existing 7-, 30- and 90-day listening evidence, model-specific token preflight and shared budget. The compact route's 48 KiB limit SHALL NOT be applied to Sol.
- **WHEN** daily_model is explicitly gpt-6-luna
- **THEN** the compact input SHALL fit the existing 48 KiB bound. If the enhanced input cannot fit, the service SHALL use the original compact pool before making a paid request, preserving its usual error behavior. The source SHALL NOT switch models or trigger a second paid ranking.

#### Scenario: Selection may omit the entire source
- **WHEN** 150 eligible candidates are ranked for a 50-track playlist and every source-added recording ranks below the final selection
- **THEN** the selected playlist MAY contain zero source-added tracks. Genre labels SHALL NOT create mandatory representation or eligibility cutoffs. A Bilibili-only recording SHALL NOT trigger the existing fixed-position album placement. An independent legacy selection SHALL retain its own placement semantics.

#### Scenario: Cache and fallback
- **WHEN** an enhanced ranking is cached
- **THEN** its success key SHALL cover the configured model, effective prompt version, source-bearing context and ordered UUIDs. An identical request SHALL reuse the cached result under existing rules, and a changed source signal SHALL NOT reuse a stale ranking. The source MUST respect existing model-specific cooldown and budget accounting.
- **WHEN** the configured daily model fails
- **THEN** existing fallback or error handling SHALL apply. When local fallback is applicable it SHALL receive the original unmodified baseline and SHALL NOT trigger a second model request.

### Requirement: Independent daily refresh and verifiable rehearsal
The feature SHALL default off. A daily refresh SHALL proceed independently of the Bilibili scheduler, history provider, extraction and album delivery. Existing same-day playlists SHALL retain their identities and ordered entries.

#### Scenario: Late or absent source
- **WHEN** the source is disabled, no eligible signal exists, the account is not the target, or the existing daily AI path is disabled
- **THEN** original candidate selection, prompt, cache and fallback behavior SHALL be preserved. A ready recording that arrives after today's playlist was created MAY first be considered on a later newly generated day while still eligible.

#### Scenario: Isolated before-and-after candidate report
- **WHEN** the merged pool is rehearsed on a fixed user/day snapshot
- **THEN** the report SHALL distinguish baseline count, already-present UUIDs annotated, outside-pool UUIDs admitted, displaced baseline count, merged count, and final selections if ranking was actually run. It SHALL show artist/genre counts as descriptive library metadata, identify fixture versus real evidence, and verify unchanged production playlists and zero submitted orders for a preview. The baseline and merged pools SHALL be comparable without calling two models or requiring paid generation; a pool-only rehearsal SHALL label final selection and token use as unmeasured.
