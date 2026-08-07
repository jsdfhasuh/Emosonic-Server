ALTER TABLE emo_playback_context ADD COLUMN close_action VARCHAR(64);
ALTER TABLE emo_playback_context ADD COLUMN close_request_fingerprint VARCHAR(64);
ALTER TABLE emo_playback_context ADD COLUMN close_expected_epoch INTEGER;
ALTER TABLE emo_playback_context ADD COLUMN close_base_version INTEGER;
ALTER TABLE emo_playback_context ADD COLUMN closed_from_epoch INTEGER;
ALTER TABLE emo_playback_context ADD COLUMN closed_from_version INTEGER;
ALTER TABLE emo_playback_context ADD COLUMN final_epoch INTEGER;
ALTER TABLE emo_playback_context ADD COLUMN final_version INTEGER;
ALTER TABLE emo_playback_context ADD COLUMN final_queue_revision INTEGER;
ALTER TABLE emo_playback_context ADD COLUMN final_control_version INTEGER;
ALTER TABLE emo_playback_context ADD COLUMN close_outcome_json TEXT;

ALTER TABLE emo_playback_control_transaction
RENAME TO emo_playback_control_transaction_legacy_20260807;

CREATE TABLE emo_playback_control_transaction (
    id CHAR(36) PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    epoch INTEGER NOT NULL,
    command_control_version INTEGER NOT NULL,
    requesting_client_id VARCHAR(128) NOT NULL,
    authority_client_id VARCHAR(128) NOT NULL,
    authority_device_session_id VARCHAR(128) NOT NULL,
    routed_connection_nonce VARCHAR(128) NOT NULL,
    routed_connection_epoch INTEGER NOT NULL DEFAULT 1,
    requesting_device_session_id VARCHAR(128),
    requesting_connection_nonce VARCHAR(128),
    requesting_connection_epoch INTEGER,
    action VARCHAR(64) NOT NULL,
    accepted_target_json TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    error_code VARCHAR(64),
    depends_on_control_version INTEGER,
    accepted_at_ms INTEGER NOT NULL,
    execution_timeout_ms INTEGER NOT NULL,
    watchdog_deadline_at_ms INTEGER,
    effective_at_server_ms INTEGER,
    execution_eligible_at_ms INTEGER,
    error_message TEXT,
    applied_control_version INTEGER,
    terminal_fingerprint VARCHAR(64),
    terminal_at_ms INTEGER,
    reconciled_by_control_version INTEGER,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(playback_context_id, epoch, command_control_version)
);

INSERT INTO emo_playback_control_transaction (
    id,
    playback_context_id,
    user_name,
    epoch,
    command_control_version,
    requesting_client_id,
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    action,
    accepted_target_json,
    status,
    error_code,
    depends_on_control_version,
    accepted_at_ms,
    execution_timeout_ms,
    watchdog_deadline_at_ms,
    applied_control_version,
    terminal_fingerprint,
    terminal_at_ms,
    created_at,
    updated_at
)
SELECT
    id,
    playback_context_id,
    user_name,
    epoch,
    command_control_version,
    requesting_client_id,
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    action,
    accepted_target_json,
    status,
    error_code,
    depends_on_control_version,
    accepted_at_ms,
    execution_timeout_ms,
    watchdog_deadline_at_ms,
    applied_control_version,
    terminal_fingerprint,
    terminal_at_ms,
    created_at,
    updated_at
FROM emo_playback_control_transaction_legacy_20260807;

DROP TABLE emo_playback_control_transaction_legacy_20260807;

CREATE INDEX idx_emo_control_pending_deadline
ON emo_playback_control_transaction (status, watchdog_deadline_at_ms);

CREATE INDEX idx_emo_control_context_status
ON emo_playback_control_transaction (
    playback_context_id,
    epoch,
    status,
    command_control_version
);

CREATE INDEX idx_emo_control_request_generation
ON emo_playback_control_transaction (
    requesting_client_id,
    requesting_device_session_id,
    requesting_connection_nonce,
    requesting_connection_epoch,
    status
);

CREATE INDEX idx_emo_control_authority_generation
ON emo_playback_control_transaction (
    authority_client_id,
    authority_device_session_id,
    routed_connection_nonce,
    routed_connection_epoch,
    status
);

CREATE TABLE emo_playback_control_reconciliation (
    id CHAR(36) PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL,
    user_name VARCHAR(64) NOT NULL,
    epoch INTEGER NOT NULL,
    reconciliation_control_version INTEGER NOT NULL,
    from_applied_control_version INTEGER NOT NULL,
    through_control_version INTEGER NOT NULL,
    trigger_kind VARCHAR(64) NOT NULL,
    trigger_command_control_version INTEGER,
    actual_fact_fingerprint VARCHAR(64) NOT NULL,
    actual_fact_json TEXT NOT NULL,
    canonical_update_json TEXT NOT NULL,
    server_updated_at_ms INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(playback_context_id, epoch, reconciliation_control_version)
);

CREATE INDEX idx_emo_reconcile_gap
ON emo_playback_control_reconciliation (
    playback_context_id,
    epoch,
    through_control_version
);
