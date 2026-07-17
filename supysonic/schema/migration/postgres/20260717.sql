ALTER TABLE emo_device_playback_state ADD COLUMN context_epoch INTEGER NOT NULL DEFAULT 1;
ALTER TABLE emo_device_playback_state ADD COLUMN applied_control_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE emo_device_playback_state ADD COLUMN client_seq INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS emo_playback_control_transaction (
    id UUID PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    epoch INTEGER NOT NULL,
    command_control_version INTEGER NOT NULL,
    requesting_client_id VARCHAR(128) NOT NULL,
    authority_client_id VARCHAR(128) NOT NULL,
    authority_device_session_id VARCHAR(128) NOT NULL,
    routed_connection_nonce VARCHAR(128) NOT NULL,
    routed_connection_epoch INTEGER NOT NULL DEFAULT 1,
    action VARCHAR(64) NOT NULL,
    accepted_target_json TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    error_code VARCHAR(64),
    depends_on_control_version INTEGER,
    accepted_at_ms BIGINT NOT NULL,
    execution_timeout_ms INTEGER NOT NULL,
    watchdog_deadline_at_ms BIGINT NOT NULL,
    applied_control_version INTEGER,
    terminal_fingerprint VARCHAR(64),
    terminal_at_ms BIGINT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    UNIQUE(playback_context_id, epoch, command_control_version)
);

CREATE INDEX IF NOT EXISTS idx_emo_control_pending_deadline
ON emo_playback_control_transaction (status, watchdog_deadline_at_ms);

CREATE INDEX IF NOT EXISTS idx_emo_control_context_status
ON emo_playback_control_transaction (
    playback_context_id,
    epoch,
    status,
    command_control_version
);

CREATE TABLE IF NOT EXISTS emo_playback_prepare_transaction (
    id UUID PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    epoch INTEGER NOT NULL,
    intent_id VARCHAR(128) NOT NULL,
    requesting_client_id VARCHAR(128) NOT NULL,
    authority_client_id VARCHAR(128) NOT NULL,
    authority_device_session_id VARCHAR(128) NOT NULL,
    routed_connection_nonce VARCHAR(128) NOT NULL,
    routed_connection_epoch INTEGER NOT NULL DEFAULT 1,
    request_fingerprint VARCHAR(64) NOT NULL,
    initial_queue_json TEXT,
    control_version INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'preparing',
    error_code VARCHAR(64),
    error_message TEXT,
    deadline_at_ms BIGINT NOT NULL,
    canonical_result_json TEXT,
    terminal_at_ms BIGINT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    UNIQUE(playback_context_id, epoch, intent_id)
);

CREATE INDEX IF NOT EXISTS idx_emo_prepare_context_status
ON emo_playback_prepare_transaction (playback_context_id, epoch, status);

CREATE INDEX IF NOT EXISTS idx_emo_prepare_pending_deadline
ON emo_playback_prepare_transaction (status, deadline_at_ms);

CREATE TABLE IF NOT EXISTS emo_playback_local_intent (
    id UUID PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    epoch INTEGER NOT NULL,
    intent_id VARCHAR(128) NOT NULL,
    authority_client_id VARCHAR(128) NOT NULL,
    authority_device_session_id VARCHAR(128) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    canonical_update_json TEXT NOT NULL,
    control_version INTEGER NOT NULL,
    superseded_through_control_version INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    UNIQUE(playback_context_id, epoch, intent_id)
);
