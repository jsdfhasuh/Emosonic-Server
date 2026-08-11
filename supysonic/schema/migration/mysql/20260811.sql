CREATE TABLE IF NOT EXISTS emo_follow_safety_lease (
    id CHAR(32) PRIMARY KEY,
    user_name VARCHAR(64) NOT NULL,
    follower_client_id VARCHAR(128) NOT NULL,
    follower_device_session_id VARCHAR(128) NOT NULL,
    follower_connection_nonce VARCHAR(128) NOT NULL,
    follower_connection_epoch INTEGER NOT NULL DEFAULT 1,
    source_playback_context_id VARCHAR(128) NOT NULL,
    source_authority_client_id VARCHAR(128) NOT NULL,
    source_authority_device_session_id VARCHAR(128) NOT NULL,
    source_connection_nonce VARCHAR(128) NOT NULL,
    source_connection_epoch INTEGER NOT NULL DEFAULT 1,
    suspended_playback_context_id VARCHAR(128) NOT NULL,
    suspended_authority_client_id VARCHAR(128) NOT NULL,
    suspended_authority_device_session_id VARCHAR(128) NOT NULL,
    suspended_connection_nonce VARCHAR(128) NOT NULL,
    suspended_connection_epoch INTEGER NOT NULL DEFAULT 1,
    suspended_epoch INTEGER NOT NULL,
    suspended_version INTEGER NOT NULL,
    suspended_queue_revision INTEGER NOT NULL,
    suspended_control_version INTEGER NOT NULL,
    suspended_applied_control_version INTEGER NOT NULL,
    phase VARCHAR(32) NOT NULL DEFAULT 'active',
    follow_reconnect_grace_expires_at_ms BIGINT,
    source_recovery_deadline_at_ms BIGINT,
    lease_fingerprint VARCHAR(64) NOT NULL,
    start_request_fingerprint VARCHAR(64) NOT NULL,
    start_ack_json TEXT NOT NULL,
    stop_request_fingerprint VARCHAR(64),
    cleanup_fingerprint VARCHAR(64),
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uniq_emo_follow_lease_fingerprint (lease_fingerprint),
    UNIQUE KEY uniq_emo_follow_follower_pair (
        user_name, follower_client_id, follower_device_session_id
    ),
    UNIQUE KEY uniq_emo_follow_suspended_context (
        user_name, suspended_playback_context_id
    ),
    KEY idx_emo_follow_user_phase_deadline (
        user_name, phase, follow_reconnect_grace_expires_at_ms
    ),
    KEY idx_emo_follow_source_phase (source_playback_context_id, phase)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
