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

INSERT IGNORE INTO emo_playback_context (
    id,
    playback_context_id,
    user_name,
    authority_client_id,
    origin_client_id,
    queue_json,
    current_index,
    state,
    position_ms,
    queue_revision,
    control_version,
    version,
    epoch,
    created_at,
    updated_at
)
SELECT
    id,
    session_id,
    user_name,
    owner_client_id,
    owner_client_id,
    queue_json,
    current_index,
    'stopped',
    position_ms,
    version,
    version,
    version,
    1,
    created_at,
    updated_at
FROM emo_session_queue;

INSERT IGNORE INTO emo_playback_context (
    id,
    playback_context_id,
    user_name,
    authority_client_id,
    origin_client_id,
    queue_json,
    current_index,
    track_id,
    state,
    position_ms,
    volume,
    queue_revision,
    control_version,
    version,
    epoch,
    playback_json,
    created_at,
    updated_at
)
SELECT
    id,
    session_id,
    user_name,
    owner_client_id,
    owner_client_id,
    '[]',
    0,
    track_id,
    state,
    position_ms,
    volume,
    1,
    1,
    1,
    1,
    playback_json,
    created_at,
    updated_at
FROM emo_playback_state;

INSERT IGNORE INTO emo_device_playback_state (
    id,
    playback_context_id,
    device_session_id,
    owner_client_id,
    user_name,
    state,
    track_id,
    position_ms,
    volume,
    is_authority,
    mode,
    playback_json,
    created_at,
    updated_at
)
SELECT
    ps.id,
    ps.session_id,
    ps.session_id,
    ps.owner_client_id,
    ps.user_name,
    ps.state,
    ps.track_id,
    ps.position_ms,
    ps.volume,
    CASE WHEN pc.authority_client_id = ps.owner_client_id THEN 1 ELSE 0 END,
    'normal',
    ps.playback_json,
    ps.created_at,
    ps.updated_at
FROM emo_playback_state ps
LEFT JOIN emo_playback_context pc ON pc.playback_context_id = ps.session_id;
