CREATE TABLE IF NOT EXISTS album_discovery_preview (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL,
    library_key VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
    day DATE NOT NULL,
    payload MEDIUMTEXT NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(user_id, library_key, day),
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS album_discovery_release (
    id CHAR(32) PRIMARY KEY,
    library_key VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
    album_id VARCHAR(64) NOT NULL,
    album_url TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    expected_tracks MEDIUMTEXT NOT NULL,
    reconciliation MEDIUMTEXT NOT NULL,
    ready_at DATETIME,
    checked_at DATETIME,
    oracle_status VARCHAR(32) NOT NULL,
    oracle_day DATE,
    oracle_detail TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(library_key, album_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS album_discovery_selection (
    id CHAR(32) PRIMARY KEY,
    release_id CHAR(32) NOT NULL,
    user_id CHAR(32) NOT NULL,
    representative_song_id VARCHAR(64) NOT NULL,
    representative_track_id CHAR(32),
    evidence MEDIUMTEXT NOT NULL,
    priority INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(release_id, user_id),
    INDEX idx_album_discovery_selection_user (user_id),
    FOREIGN KEY (release_id) REFERENCES album_discovery_release(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_track_id) REFERENCES track(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS album_discovery_placement (
    id CHAR(32) PRIMARY KEY,
    selection_id CHAR(32) NOT NULL UNIQUE,
    user_id CHAR(32) NOT NULL,
    playlist_id CHAR(32) NOT NULL,
    day DATE NOT NULL,
    track_id CHAR(32) NOT NULL,
    position INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(user_id, day),
    FOREIGN KEY (selection_id) REFERENCES album_discovery_selection(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
