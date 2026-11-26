CREATE TABLE IF NOT EXISTS album_discovery_preview (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    library_key VARCHAR(128) NOT NULL,
    day DATE NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    UNIQUE(user_id, library_key, day),
    FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS album_discovery_release (
    id UUID PRIMARY KEY,
    library_key VARCHAR(128) NOT NULL,
    album_id VARCHAR(64) NOT NULL,
    album_url TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    expected_tracks TEXT NOT NULL,
    reconciliation TEXT NOT NULL,
    ready_at TIMESTAMP,
    checked_at TIMESTAMP,
    oracle_status VARCHAR(32) NOT NULL,
    oracle_day DATE,
    oracle_detail TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    UNIQUE(library_key, album_id)
);

CREATE TABLE IF NOT EXISTS album_discovery_selection (
    id UUID PRIMARY KEY,
    release_id UUID NOT NULL,
    user_id UUID NOT NULL,
    representative_song_id VARCHAR(64) NOT NULL,
    representative_track_id UUID,
    evidence TEXT NOT NULL,
    priority INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    UNIQUE(release_id, user_id),
    FOREIGN KEY (release_id) REFERENCES album_discovery_release(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_track_id) REFERENCES track(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_album_discovery_selection_user ON album_discovery_selection (user_id);

CREATE TABLE IF NOT EXISTS album_discovery_placement (
    id UUID PRIMARY KEY,
    selection_id UUID NOT NULL UNIQUE,
    user_id UUID NOT NULL,
    playlist_id UUID NOT NULL,
    day DATE NOT NULL,
    track_id UUID NOT NULL,
    position INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    UNIQUE(user_id, day),
    FOREIGN KEY (selection_id) REFERENCES album_discovery_selection(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
