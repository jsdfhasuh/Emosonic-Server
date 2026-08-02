CREATE TABLE IF NOT EXISTS emo_broadcast (
    id CHAR(32) PRIMARY KEY,
    broadcast_id VARCHAR(128) NOT NULL UNIQUE,
    user_name VARCHAR(64) NOT NULL,
    playback_context_id VARCHAR(128) NOT NULL,
    intent_id VARCHAR(128) NOT NULL,
    owner_client_id VARCHAR(128) NOT NULL,
    authority_client_id VARCHAR(128) NOT NULL,
    authority_device_session_id VARCHAR(128) NOT NULL,
    lifecycle_state VARCHAR(32) NOT NULL DEFAULT 'active',
    broadcast_revision INTEGER NOT NULL DEFAULT 1,
    snapshot_json TEXT NOT NULL,
    authority_disconnect_deadline_ms BIGINT,
    terminal_at_ms BIGINT,
    full_expires_at_ms BIGINT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_emo_broadcast_context_lifecycle
ON emo_broadcast (user_name, playback_context_id, lifecycle_state);
CREATE INDEX idx_emo_broadcast_disconnect_deadline
ON emo_broadcast (lifecycle_state, authority_disconnect_deadline_ms);
CREATE INDEX idx_emo_broadcast_terminal
ON emo_broadcast (lifecycle_state, terminal_at_ms);

CREATE TABLE IF NOT EXISTS emo_broadcast_intent_outcome (
    id CHAR(32) PRIMARY KEY,
    user_name VARCHAR(64) NOT NULL,
    playback_context_id VARCHAR(128) NOT NULL,
    owner_client_id VARCHAR(128) NOT NULL,
    intent_id VARCHAR(128) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    broadcast_id VARCHAR(128) NOT NULL,
    final_participants_json TEXT NOT NULL,
    skipped_client_ids_json TEXT NOT NULL,
    start_ack_json TEXT NOT NULL,
    terminal_broadcast_revision INTEGER,
    stop_ack_json TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(user_name, playback_context_id, owner_client_id, intent_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_emo_broadcast_intent_context
ON emo_broadcast_intent_outcome (playback_context_id, created_at);

CREATE TABLE IF NOT EXISTS emo_broadcast_fence (
    id CHAR(32) PRIMARY KEY,
    resource_key VARCHAR(80) NOT NULL UNIQUE,
    broadcast_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL,
    phase VARCHAR(32) NOT NULL DEFAULT 'nonterminal',
    playback_context_id VARCHAR(128),
    client_id VARCHAR(128),
    device_session_id VARCHAR(128),
    recovery_slot_reserved INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_emo_broadcast_fence_broadcast
ON emo_broadcast_fence (broadcast_id, role, phase);
CREATE INDEX idx_emo_broadcast_fence_recovery
ON emo_broadcast_fence (user_name, phase, recovery_slot_reserved);

CREATE TABLE IF NOT EXISTS emo_broadcast_participant (
    id CHAR(32) PRIMARY KEY,
    broadcast_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    client_id VARCHAR(128) NOT NULL,
    device_session_id VARCHAR(128) NOT NULL,
    suspended_playback_context_id VARCHAR(128) NOT NULL,
    suspended_epoch INTEGER NOT NULL,
    suspended_version INTEGER NOT NULL,
    suspended_queue_revision INTEGER NOT NULL,
    suspended_control_version INTEGER NOT NULL,
    suspended_applied_control_version INTEGER NOT NULL,
    restore_pending INTEGER NOT NULL DEFAULT 0,
    terminal_confirmed INTEGER NOT NULL DEFAULT 0,
    target_broadcast_revision INTEGER,
    target_delivery_id VARCHAR(128),
    deadline_broadcast_revision INTEGER,
    feedback_deadline_at_server_ms BIGINT,
    sync_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    applied_broadcast_revision INTEGER,
    applied_queue_index INTEGER,
    applied_track_id VARCHAR(128),
    applied_position_ms BIGINT,
    applied_state VARCHAR(32),
    applied_playback_rate DOUBLE,
    failed_broadcast_revision INTEGER,
    failed_last_applied_broadcast_revision INTEGER,
    failed_error_code VARCHAR(64),
    last_feedback_client_seq INTEGER,
    last_feedback_at_ms BIGINT,
    restore_completed INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(broadcast_id, client_id, device_session_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_emo_broadcast_participant_restore
ON emo_broadcast_participant (user_name, restore_pending);
CREATE INDEX idx_emo_broadcast_participant_deadline
ON emo_broadcast_participant (sync_status, feedback_deadline_at_server_ms);

CREATE TABLE IF NOT EXISTS emo_broadcast_revision (
    id CHAR(32) PRIMARY KEY,
    broadcast_id VARCHAR(128) NOT NULL,
    broadcast_revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    canonical_action VARCHAR(32) NOT NULL,
    created_at_ms BIGINT NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(broadcast_id, broadcast_revision)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_emo_broadcast_revision_created
ON emo_broadcast_revision (broadcast_id, created_at_ms);

CREATE TABLE IF NOT EXISTS emo_broadcast_delivery (
    id CHAR(32) PRIMARY KEY,
    delivery_id VARCHAR(128) NOT NULL UNIQUE,
    broadcast_id VARCHAR(128) NOT NULL,
    broadcast_revision INTEGER NOT NULL,
    client_id VARCHAR(128) NOT NULL,
    device_session_id VARCHAR(128) NOT NULL,
    action VARCHAR(32) NOT NULL,
    effective_at_server_ms BIGINT,
    server_time_ms BIGINT,
    delivery_position_ms BIGINT NOT NULL,
    payload_json TEXT NOT NULL,
    connection_nonce VARCHAR(128),
    is_current INTEGER NOT NULL DEFAULT 1,
    delivery_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    created_at_ms BIGINT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_emo_broadcast_delivery_current
ON emo_broadcast_delivery (
    broadcast_id, broadcast_revision, client_id, device_session_id, is_current
);
CREATE INDEX idx_emo_broadcast_delivery_status
ON emo_broadcast_delivery (broadcast_id, delivery_status);

CREATE TABLE IF NOT EXISTS emo_broadcast_feedback_settlement (
    id CHAR(32) PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL,
    broadcast_id VARCHAR(128) NOT NULL,
    client_id VARCHAR(128) NOT NULL,
    device_session_id VARCHAR(128) NOT NULL,
    connection_nonce VARCHAR(128) NOT NULL,
    connection_epoch INTEGER NOT NULL DEFAULT 1,
    client_seq INTEGER NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    canonical_result_json TEXT NOT NULL,
    follow_up_delivery_id VARCHAR(128),
    created_at_ms BIGINT NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(
        playback_context_id, broadcast_id, client_id, device_session_id,
        connection_nonce, connection_epoch, client_seq
    )
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_emo_broadcast_feedback_pair
ON emo_broadcast_feedback_settlement (
    broadcast_id, client_id, device_session_id, created_at_ms
);

CREATE TABLE IF NOT EXISTS emo_broadcast_terminal_recovery (
    id CHAR(32) PRIMARY KEY,
    user_name VARCHAR(64) NOT NULL,
    playback_context_id VARCHAR(128) NOT NULL,
    broadcast_id VARCHAR(128) NOT NULL,
    client_id VARCHAR(128) NOT NULL,
    device_session_id VARCHAR(128) NOT NULL,
    terminal_broadcast_revision INTEGER NOT NULL,
    current_delivery_id VARCHAR(128),
    suspended_playback_context_id VARCHAR(128) NOT NULL,
    suspended_epoch INTEGER NOT NULL,
    suspended_version INTEGER NOT NULL,
    suspended_queue_revision INTEGER NOT NULL,
    suspended_control_version INTEGER NOT NULL,
    suspended_applied_control_version INTEGER NOT NULL,
    last_applied_broadcast_revision INTEGER,
    terminal_queue_index INTEGER NOT NULL,
    terminal_track_id VARCHAR(128) NOT NULL,
    terminal_position_ms BIGINT NOT NULL,
    terminal_playback_rate DOUBLE NOT NULL,
    terminal_at_server_ms BIGINT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(user_name, client_id, device_session_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_emo_broadcast_recovery_terminal
ON emo_broadcast_terminal_recovery (terminal_at_server_ms);
