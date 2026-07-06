CREATE TABLE IF NOT EXISTS track_metadata (
    id UUID PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES track(id) ON DELETE CASCADE,
    track_last_modification INTEGER NOT NULL,
    language VARCHAR(16),
    mood_json TEXT,
    scene_json TEXT,
    tags_json TEXT,
    summary TEXT,
    energy INTEGER,
    valence INTEGER,
    danceability INTEGER,
    confidence DOUBLE PRECISION,
    provider VARCHAR(64),
    model VARCHAR(128),
    source VARCHAR(64),
    raw_json TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS index_track_metadata_track_id ON track_metadata(track_id);
CREATE INDEX IF NOT EXISTS index_track_metadata_provider ON track_metadata(provider);
CREATE INDEX IF NOT EXISTS index_track_metadata_updated_at ON track_metadata(updated_at);

CREATE TABLE IF NOT EXISTS track_metadata_enrichment_task (
    id UUID PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES track(id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    locked_at TIMESTAMP,
    next_retry_at TIMESTAMP,
    force BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS index_track_metadata_enrichment_task_track_id ON track_metadata_enrichment_task(track_id);
CREATE INDEX IF NOT EXISTS index_track_metadata_enrichment_task_status_next_retry_at ON track_metadata_enrichment_task(status, next_retry_at);
CREATE INDEX IF NOT EXISTS index_track_metadata_enrichment_task_status_locked_at ON track_metadata_enrichment_task(status, locked_at);
CREATE INDEX IF NOT EXISTS index_track_metadata_enrichment_task_updated_at ON track_metadata_enrichment_task(updated_at);
