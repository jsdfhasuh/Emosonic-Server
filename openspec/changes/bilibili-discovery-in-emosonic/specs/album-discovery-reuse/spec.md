# Album Discovery Reuse

## ADDED Requirements

### Requirement: Shared verified CN album delivery
Before dispatch, the system SHALL verify the CN song ID, artist, recording version and full album membership. Direct and related recordings SHALL pass the same inventory, feedback and order checks. Each new order SHALL use the existing Oracle whole-album workflow and shared quota. A Bilibili candidate MUST NOT create a legacy selection or placement.

#### Scenario: Direct and related candidates reach the same eligibility check
- **WHEN** a Bilibili source explicitly names a live track or an independently documented credit or style leads to a particular related recording
- **THEN** only a uniquely resolved, eligible target recording on a verified CN album MAY proceed to the existing order workflow. A foreign storefront ID, version mismatch, unresolved alternative or incomplete track list SHALL result in zero new orders. A non-CN version MUST NOT be used as fallback.

#### Scenario: Two sources request the same album
- **WHEN** Bilibili discovery and the existing album discovery select the same library/CN release, including different target songs
- **THEN** they SHALL share one persistent order and at most one new-album quota slot, including concurrent or repeated requests. Each source SHALL retain its own target song and eligibility evidence.

#### Scenario: Lost submission response
- **WHEN** Oracle may have accepted a request but the response is unavailable
- **THEN** the system SHALL reconcile via the original persistent order ID and existing control/closure semantics before any new order or quota release.

#### Scenario: Automatic downloads disabled
- **WHEN** either Bilibili switch (`enabled` or `auto_download`) is off, or the shared new-album quota is exhausted
- **THEN** the Bilibili source MUST NOT submit a new album. Accepted or uncertain orders SHALL remain reconcilable; the legacy source SHALL retain its own `aiDiscovery.autoSubmit` behavior.

#### Scenario: The two sources use the last slot
- **WHEN** the existing JSON sets `aiDiscovery.maxAlbumsPerDay` to 13 and 12 album slots for the library's Shanghai day are already occupied, and both sources request different new releases concurrently
- **THEN** at most one of those new albums SHALL acquire the final slot. A repeated association with an existing release SHALL NOT consume another slot. The configurable limit counts new albums across both sources, independent of numbers of clues, searches or target songs.

### Requirement: Verified library readiness
The system SHALL reuse the existing album reconciliation job and receipt/file/index verification for newly downloaded albums. Bilibili-only releases with a valid linked candidate SHALL be reconciled even without a legacy AlbumDiscoverySelection.

#### Scenario: Completed remote task without local indexing
- **WHEN** an order is accepted or the downloader reports completion but the complete expected album has not been verified in the local music library
- **THEN** no candidate from that downloaded album SHALL become ready, including when OpenWrt has received only 11 of 12 expected tracks, or has received all files but one expected track has no verified index entry.

#### Scenario: Shared release with multiple target songs
- **WHEN** the expected album has been verified and the release contains multiple Bilibili targets
- **THEN** each candidate SHALL use the verified Track UUID for its own Apple song ID, rather than all using the representative track.

#### Scenario: Existing ready release
- **WHEN** a Bilibili candidate links to a previously verified release
- **THEN** the service SHALL use current valid stored reconciliation evidence without downloading the album again or requiring a legacy selection.

#### Scenario: Verified target becomes available after today's playlist
- **WHEN** the existing reconciliation verifies every track and supplies a current local UUID for the target after the user's daily playlist was created
- **THEN** only the verified target SHALL become eligible as a Bilibili suggestion for a subsequent fresh daily playlist within the source window. Other album tracks MAY participate under ordinary library rules. The update MUST NOT rewrite the generated playlist, invent a play or reserve a fixed daily slot.
