CREATE TABLE IF NOT EXISTS bilibili_discovery_run (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    source_day DATE NOT NULL,
    status VARCHAR(32) NOT NULL,
    sync_confirmed_at TIMESTAMP,
    snapshot_json TEXT,
    extract_offset INTEGER NOT NULL DEFAULT 0,
    discovery_progress_json TEXT NOT NULL,
    next_attempt_at TIMESTAMP,
    last_error VARCHAR(128) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    UNIQUE(user_id, source_day),
    FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bilibili_discovery_candidate (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    identity_key VARCHAR(128) NOT NULL,
    title VARCHAR(256) NOT NULL,
    artist VARCHAR(256) NOT NULL,
    version VARCHAR(32) NOT NULL,
    album_hint VARCHAR(256) NOT NULL,
    source_evidence_json TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(128) NOT NULL,
    release_id UUID,
    apple_song_id VARCHAR(64),
    track_id UUID,
    ready_at TIMESTAMP,
    next_attempt_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    UNIQUE(run_id, identity_key),
    FOREIGN KEY (run_id) REFERENCES bilibili_discovery_run(id) ON DELETE CASCADE,
    FOREIGN KEY (release_id) REFERENCES album_discovery_release(id) ON DELETE SET NULL,
    FOREIGN KEY (track_id) REFERENCES track(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_bilibili_candidate_release ON bilibili_discovery_candidate (release_id);
CREATE INDEX IF NOT EXISTS idx_bilibili_candidate_track ON bilibili_discovery_candidate (track_id);
CREATE INDEX IF NOT EXISTS idx_bilibili_candidate_ready ON bilibili_discovery_candidate (status, ready_at);
