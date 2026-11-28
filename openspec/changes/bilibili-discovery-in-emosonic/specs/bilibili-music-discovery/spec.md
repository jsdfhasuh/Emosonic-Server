# Bilibili Music Discovery

## ADDED Requirements

### Requirement: Daily discovery on the existing daemon
The system SHALL run Bilibili discovery using one additional existing Daemon Scheduler job and SHALL persist the Shanghai-local source day, daily snapshot and extraction progress in the EmoSonic database. It MUST NOT delay normal recommendation generation or create a separate scheduler service.

#### Scenario: Restart or repeated scheduler wake-up
- **WHEN** a completed input day is encountered again or an incomplete day is resumed
- **THEN** the system SHALL reuse the committed snapshot and processed batches, and SHALL NOT create a second run for the same user and source day.

#### Scenario: History source unavailable
- **WHEN** the source cannot be authenticated, the Bilibili provider result is missing or failed, or pagination is inconsistent
- **THEN** the run SHALL retain an incomplete state with a reason and SHALL NOT report an empty successful day.

#### Scenario: Yesterday's visible records
- **WHEN** the history API returns records for the requested Asia/Shanghai date
- **THEN** the service SHALL process the visible records with duplicate handling and SHALL NOT claim to recover views overwritten at the source.

#### Scenario: Valid empty day
- **WHEN** the authenticated Bilibili provider reports success and all pages consistently show no observations for Shanghai yesterday
- **THEN** the job SHALL record an empty completed run with zero model calls and zero new orders.

### Requirement: Evidence based music identification
The system SHALL reuse the existing budgeted LLM client to identify songs from bounded, untrusted history text, validate structured results, and verify music identity independently of the model before automated download. Source text SHALL NOT act as instructions. Credentials MUST NOT enter prompts or logs.

#### Scenario: Existing local recording
- **WHEN** a candidate has exactly one reliable playable local Track matching the identified song and version
- **THEN** the candidate SHALL use that Track without downloading or requiring other tracks of the existing album.

#### Scenario: Uncertain content
- **WHEN** a mix, generic title or suspected background song supplies insufficient evidence of a specific recording
- **THEN** the service SHALL record the direct match as unresolved and MUST NOT guess that song or its studio version. A separately supported musical clue MAY be considered by the bounded discovery rule below.

#### Scenario: Budget unavailable
- **WHEN** the existing paid-request budget or provider is unavailable
- **THEN** extraction SHALL retain resumable progress and MUST NOT switch to an unaccounted paid request.

### Requirement: Direct and related musical clues
For each accepted clue the system SHALL preserve the source item reference, evidence type and the evidence supporting it. It SHALL distinguish a recording explicitly identified in the source from another recording discovered through a confirmed credit or supported style. It MUST NOT record either type of Bilibili evidence as an EmoSonic play, favorite, completed listen or legacy album selection.

#### Scenario: A live recording is named
- **WHEN** a history item explicitly identifies a title, performing artist and live version and the only local hit is the studio take
- **THEN** the studio take SHALL NOT resolve the direct clue. A CN search MAY resolve the requested live recording if the version can be verified. An unidentified cover or a non-CN result SHALL NOT substitute for it.

#### Scenario: Discover through a credited artist
- **WHEN** a source names a collaboration and indexed credits or the CN catalog verify that relationship
- **THEN** the service MAY search for another recording by the credited artist within the design's search bounds. It SHALL preserve both the originating clue and the verified relationship, and identify the resulting recording as a related suggestion.

#### Scenario: Explore an adjacent style
- **WHEN** the source gives a supported style and at least two different artists among the user's repeated-play seeds support the same normalized style
- **THEN** the system MAY probe the local library or CN catalog for concrete recordings within per-user, per-Shanghai-source-day limits shared across batches and restarts. It SHALL cap styles and songs per style, deduplicate repeated hits and retain the style evidence. The genre string, free-form model suggestions, and unsupported versions MUST NOT create an order or a daily Track entry.
- **AND** without that listening support the service SHALL skip the style lookup.

#### Scenario: No documented evidence
- **WHEN** a non-music item or an untrusted title yields neither an identifiable song nor a supported musical clue
- **THEN** the job SHALL skip it with a reason. It SHALL issue no catalog expansion or download for that item.

### Requirement: Minimal persistence
The system SHALL use two new business models (Run and Candidate) and the existing database migration mechanism for SQLite, MySQL and PostgreSQL. Existing library, user, playlist and discovery data MUST be preserved.

#### Scenario: Upgrade from prior schema
- **WHEN** an existing supported database is upgraded
- **THEN** the two models SHALL be available with unique daily/candidate identities and nullable Track/release references, without changing existing rows' business meaning.
