CREATE TABLE IF NOT EXISTS folder (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    root BOOLEAN NOT NULL,
    name VARCHAR(256) NOT NULL,
    path VARCHAR(4096) NOT NULL,
    path_hash BINARY(20) UNIQUE NOT NULL,
    created DATETIME NOT NULL,
    cover_art VARCHAR(256),
    last_scan INTEGER NOT NULL,
    parent_id INTEGER REFERENCES folder(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_folder_parent_id_fk ON folder(parent_id);

CREATE TABLE IF NOT EXISTS artist (
    id CHAR(32) PRIMARY KEY,
    name VARCHAR(256) NOT NULL,
    artist_info_json VARCHAR(4096) NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS album (
    id CHAR(32) PRIMARY KEY,
    name VARCHAR(256) NOT NULL,
    year VARCHAR(255) NULL,
    release_date VARCHAR(32) NULL,
    release_type VARCHAR(64) NULL,
    album_info_json TEXT NULL,
    artist_id CHAR(32) NOT NULL REFERENCES artist(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_album_artist_id_fk ON album(artist_id);

CREATE TABLE IF NOT EXISTS track (
    id CHAR(32) PRIMARY KEY,
    disc INTEGER NOT NULL,
    number INTEGER NOT NULL,
    title VARCHAR(256) NOT NULL,
    year INTEGER,
    genre VARCHAR(256),
    duration INTEGER NOT NULL,
    has_art BOOLEAN NOT NULL DEFAULT false,
    album_id CHAR(32) NOT NULL REFERENCES album(id),
    artist_id CHAR(32) NOT NULL REFERENCES artist(id),
    bitrate INTEGER NOT NULL,
    path VARCHAR(4096) NOT NULL,
    path_hash BINARY(20) UNIQUE NOT NULL,
    created DATETIME NOT NULL,
    last_modification INTEGER NOT NULL,
    play_count INTEGER NOT NULL,
    play_count_web INTEGER NOT NULL,
    last_play DATETIME,
    root_folder_id INTEGER NOT NULL REFERENCES folder(id),
    content_hash CHAR(40) NOT NULL,
    folder_id INTEGER NOT NULL REFERENCES folder(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_track_album_id_fk ON track(album_id);
CREATE INDEX index_track_artist_id_fk ON track(artist_id);
CREATE INDEX index_track_folder_id_fk ON track(folder_id);
CREATE INDEX index_track_root_folder_id_fk ON track(root_folder_id);

CREATE TABLE IF NOT EXISTS track_metadata (
    id CHAR(32) PRIMARY KEY,
    track_id CHAR(32) NOT NULL,
    track_last_modification INTEGER NOT NULL,
    language VARCHAR(16),
    mood_json TEXT,
    scene_json TEXT,
    tags_json TEXT,
    summary TEXT,
    energy INTEGER,
    valence INTEGER,
    danceability INTEGER,
    confidence DOUBLE,
    provider VARCHAR(64),
    model VARCHAR(128),
    source VARCHAR(64),
    raw_json TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY (track_id) REFERENCES track(id) ON DELETE CASCADE
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE UNIQUE INDEX index_track_metadata_track_id ON track_metadata(track_id);
CREATE INDEX index_track_metadata_provider ON track_metadata(provider);
CREATE INDEX index_track_metadata_updated_at ON track_metadata(updated_at);

CREATE TABLE IF NOT EXISTS track_metadata_enrichment_task (
    id CHAR(32) PRIMARY KEY,
    track_id CHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    locked_at DATETIME,
    next_retry_at DATETIME,
    `force` BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    completed_at DATETIME,
    FOREIGN KEY (track_id) REFERENCES track(id) ON DELETE CASCADE
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE UNIQUE INDEX index_track_metadata_enrichment_task_track_id ON track_metadata_enrichment_task(track_id);
CREATE INDEX index_track_metadata_enrichment_task_status_next_retry_at ON track_metadata_enrichment_task(status, next_retry_at);
CREATE INDEX index_track_metadata_enrichment_task_status_locked_at ON track_metadata_enrichment_task(status, locked_at);
CREATE INDEX index_track_metadata_enrichment_task_updated_at ON track_metadata_enrichment_task(updated_at);

CREATE TABLE IF NOT EXISTS user (
    id CHAR(32) PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    mail VARCHAR(256),
    password CHAR(40) NOT NULL,
    salt CHAR(6) NOT NULL,
    admin BOOLEAN NOT NULL,
    jukebox BOOLEAN NOT NULL,
    listenbrainz_session CHAR(36),
    listenbrainz_status BOOLEAN NOT NULL,
    lastfm_session CHAR(32),
    lastfm_status BOOLEAN NOT NULL,
    last_play_id CHAR(32) REFERENCES track(id),
    last_play_date DATETIME
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_user_last_play_id_fk ON user(last_play_id);

CREATE TABLE IF NOT EXISTS client_prefs (
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    client_name VARCHAR(32) NOT NULL,
    format VARCHAR(8),
    bitrate INTEGER,
    PRIMARY KEY (user_id, client_name)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_client_prefs_user_id_fk ON client_prefs(user_id);

CREATE TABLE IF NOT EXISTS user_recommendation_feedback (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    song_id VARCHAR(128) NOT NULL,
    target_type VARCHAR(32) NOT NULL DEFAULT 'song',
    target_id VARCHAR(128) NOT NULL,
    action VARCHAR(32) NOT NULL,
    scope VARCHAR(64) NOT NULL,
    source VARCHAR(64) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME,
    UNIQUE KEY index_user_recommendation_feedback_user_target_scope (user_id, target_type, target_id, scope)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_user_recommendation_feedback_user_scope_deleted ON user_recommendation_feedback(user_id, scope, deleted_at);

CREATE TABLE IF NOT EXISTS recommendation_agent_session (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    message TEXT NOT NULL,
    reply TEXT NOT NULL,
    recommended_artists_json TEXT NOT NULL,
    context_summary_json TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    language VARCHAR(8) NOT NULL,
    created_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_recommendation_agent_session_user_created ON recommendation_agent_session(user_id, created_at);

CREATE TABLE IF NOT EXISTS recommendation_agent_cache (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    context_hash VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    language VARCHAR(8) NOT NULL,
    model VARCHAR(128) NOT NULL,
    payload_json TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE UNIQUE INDEX index_recommendation_agent_cache_user_context ON recommendation_agent_cache(user_id, context_hash);
CREATE INDEX index_recommendation_agent_cache_user_expires ON recommendation_agent_cache(user_id, expires_at);

CREATE TABLE IF NOT EXISTS music_request (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    artist_name VARCHAR(256) NOT NULL,
    album_name VARCHAR(256),
    tracks_json TEXT,
    note TEXT,
    status VARCHAR(32) NOT NULL,
    status_note TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    resolved_at DATETIME
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_music_request_status_created_at ON music_request(status, created_at);
CREATE INDEX index_music_request_user_created_at ON music_request(user_id, created_at);
CREATE INDEX index_music_request_artist_album_status ON music_request(artist_name, album_name, status);

CREATE TABLE IF NOT EXISTS starred_folder (
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    starred_id INTEGER NOT NULL REFERENCES folder(id),
    date DATETIME NOT NULL,
    PRIMARY KEY (user_id, starred_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_starred_folder_user_id_fk ON starred_folder(user_id);
CREATE INDEX index_starred_folder_starred_id_fk ON starred_folder(starred_id);

CREATE TABLE IF NOT EXISTS starred_artist (
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    starred_id CHAR(32) NOT NULL REFERENCES artist(id),
    date DATETIME NOT NULL,
    PRIMARY KEY (user_id, starred_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_starred_artist_user_id_fk ON starred_artist(user_id);
CREATE INDEX index_starred_artist_starred_id_fk ON starred_artist(starred_id);

CREATE TABLE IF NOT EXISTS starred_album (
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    starred_id CHAR(32) NOT NULL REFERENCES album(id),
    date DATETIME NOT NULL,
    PRIMARY KEY (user_id, starred_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_starred_album_user_id_fk ON starred_album(user_id);
CREATE INDEX index_starred_album_starred_id_fk ON starred_album(starred_id);

CREATE TABLE IF NOT EXISTS starred_track (
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    starred_id CHAR(32) NOT NULL REFERENCES track(id),
    date DATETIME NOT NULL,
    PRIMARY KEY (user_id, starred_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_starred_track_user_id_fk ON starred_track(user_id);
CREATE INDEX index_starred_track_starred_id_fk ON starred_track(starred_id);

CREATE TABLE IF NOT EXISTS rating_folder (
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    rated_id INTEGER NOT NULL REFERENCES folder(id),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    PRIMARY KEY (user_id, rated_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_rating_folder_user_id_fk ON rating_folder(user_id);
CREATE INDEX index_rating_folder_rated_id_fk ON rating_folder(rated_id);

CREATE TABLE IF NOT EXISTS rating_track (
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    rated_id CHAR(32) NOT NULL REFERENCES track(id),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    PRIMARY KEY (user_id, rated_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_rating_track_user_id_fk ON rating_track(user_id);
CREATE INDEX index_rating_track_rated_id_fk ON rating_track(rated_id);

CREATE TABLE IF NOT EXISTS chat_message (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    time INTEGER NOT NULL,
    message VARCHAR(512) NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_chat_message_user_id_fk ON chat_message(user_id);

CREATE TABLE IF NOT EXISTS playlist (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    name VARCHAR(256) NOT NULL,
    comment VARCHAR(256),
    public BOOLEAN NOT NULL,
    created DATETIME NOT NULL,
    tracks TEXT
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_playlist_user_id_fk ON playlist(user_id);

CREATE TABLE IF NOT EXISTS emo_session_queue (
    id CHAR(32) PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL UNIQUE,
    user_name VARCHAR(64) NOT NULL,
    owner_client_id VARCHAR(128) NOT NULL,
    queue_json TEXT NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    position_ms INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS emo_local_queue (
    id CHAR(32) PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    owner_client_id VARCHAR(128) NOT NULL,
    queue_json TEXT NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    position_ms INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uniq_emo_local_queue_session_client (session_id, owner_client_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS emo_playback_state (
    id CHAR(32) PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    owner_client_id VARCHAR(128) NOT NULL,
    state VARCHAR(32) NOT NULL,
    track_id VARCHAR(128),
    position_ms INTEGER NOT NULL DEFAULT 0,
    volume INTEGER,
    playback_json TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uniq_emo_playback_state_session_client (session_id, owner_client_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS emo_playback_context (
    id CHAR(32) PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL UNIQUE,
    user_name VARCHAR(64) NOT NULL,
    authority_client_id VARCHAR(128),
    origin_client_id VARCHAR(128),
    queue_json TEXT NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    track_id VARCHAR(128),
    state VARCHAR(32) NOT NULL DEFAULT 'stopped',
    position_ms INTEGER NOT NULL DEFAULT 0,
    volume INTEGER,
    queue_revision INTEGER NOT NULL DEFAULT 1,
    control_version INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 1,
    playback_json TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS emo_device_playback_state (
    id CHAR(32) PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL,
    device_session_id VARCHAR(128) NOT NULL,
    owner_client_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL,
    track_id VARCHAR(128),
    position_ms INTEGER NOT NULL DEFAULT 0,
    volume INTEGER,
    is_authority INTEGER NOT NULL DEFAULT 0,
    mode VARCHAR(32) NOT NULL DEFAULT 'normal',
    playback_json TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uniq_emo_device_playback_context_client (playback_context_id, owner_client_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS emo_playback_handoff (
    id CHAR(32) PRIMARY KEY,
    handoff_id VARCHAR(128) NOT NULL UNIQUE,
    request_id VARCHAR(128),
    playback_context_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    source_client_id VARCHAR(128) NOT NULL,
    target_client_id VARCHAR(128) NOT NULL,
    origin_client_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    base_control_version INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT,
    error_code VARCHAR(64),
    error_message TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS shared_track_link (
    id CHAR(32) PRIMARY KEY,
    token VARCHAR(96) NOT NULL UNIQUE,
    track_id CHAR(32) NOT NULL REFERENCES track(id),
    created_by_id CHAR(32) NOT NULL REFERENCES user(id),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS review_task (
    id CHAR(32) PRIMARY KEY,
    entity_type VARCHAR(32) NOT NULL,
    entity_id CHAR(36) NOT NULL,
    task_type VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    pending_key VARCHAR(96),
    snapshot_json TEXT,
    created DATETIME NOT NULL,
    updated DATETIME NOT NULL,
    resolved_at DATETIME,
    expires_at DATETIME
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_review_task_entity_status ON review_task(entity_type, entity_id, status);
CREATE INDEX index_review_task_status_created ON review_task(status, created);
CREATE UNIQUE INDEX index_review_task_pending_key ON review_task(pending_key);

CREATE TABLE IF NOT EXISTS client_release (
    id CHAR(32) PRIMARY KEY,
    platform VARCHAR(16) NOT NULL,
    file_type VARCHAR(16) NOT NULL,
    build_name VARCHAR(64) NOT NULL,
    build_number INTEGER NOT NULL,
    version VARCHAR(80) NOT NULL,
    publish_mode VARCHAR(16) NOT NULL,
    file_name VARCHAR(256),
    file_path VARCHAR(4096),
    download_url VARCHAR(2048),
    file_size INTEGER,
    sha256 CHAR(64),
    release_notes TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created DATETIME NOT NULL,
    updated DATETIME NOT NULL,
    UNIQUE KEY index_client_release_platform_version (platform, build_name, build_number),
    INDEX index_client_release_platform_active (platform, active)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;


CREATE TABLE  album_artist (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    album_id CHAR(32) NOT NULL,
    artist_id CHAR(32) NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (album_id) REFERENCES album(id) ON DELETE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artist(id) ON DELETE CASCADE,
    UNIQUE KEY(album_id, artist_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;


CREATE TABLE  track_artist (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    track_id CHAR(32) NOT NULL,
    artist_id CHAR(32) NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (track_id) REFERENCES track(id) ON DELETE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artist(id) ON DELETE CASCADE,
    UNIQUE KEY(track_id, artist_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;



CREATE TABLE image (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    path VARCHAR(4096) NOT NULL,
    image_type VARCHAR(10) NOT NULL,
    related_id VARCHAR(36) NOT NULL,
    created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE user_play_activity (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    track_id CHAR(32) NOT NULL, 
    user_id CHAR(32) NOT NULL,
    time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, 
    CONSTRAINT fk_track FOREIGN KEY (track_id) REFERENCES track(id) ON DELETE CASCADE,
    CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
);DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE INDEX index_activity_user_id_fk ON user_play_activity(user_id);
CREATE INDEX index_activity_track_id_fk ON user_play_activity(track_id);
CREATE INDEX index_album_artist_album_id_fk ON album_artist(album_id);
CREATE INDEX index_album_artist_artist_id_fk ON album_artist(artist_id);
CREATE INDEX index_track_artist_track_id_fk ON track_artist(track_id);
CREATE INDEX index_track_artist_artist_id_fk ON track_artist(artist_id);
CREATE TABLE meta (
    `key` VARCHAR(32) PRIMARY KEY,
    value VARCHAR(256) NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS radio_station (
    id CHAR(32) PRIMARY KEY,
    stream_url VARCHAR(256) NOT NULL,
    name VARCHAR(256) NOT NULL,
    homepage_url VARCHAR(256),
    created DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
