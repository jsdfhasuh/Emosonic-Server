ALTER TABLE emo_playback_context
    ADD COLUMN close_action VARCHAR(64),
    ADD COLUMN close_request_fingerprint VARCHAR(64),
    ADD COLUMN close_expected_epoch INTEGER,
    ADD COLUMN close_base_version INTEGER,
    ADD COLUMN closed_from_epoch INTEGER,
    ADD COLUMN closed_from_version INTEGER,
    ADD COLUMN final_epoch INTEGER,
    ADD COLUMN final_version INTEGER,
    ADD COLUMN final_queue_revision INTEGER,
    ADD COLUMN final_control_version INTEGER,
    ADD COLUMN close_outcome_json TEXT;

ALTER TABLE emo_playback_control_transaction
    ADD COLUMN requesting_device_session_id VARCHAR(128),
    ADD COLUMN requesting_connection_nonce VARCHAR(128),
    ADD COLUMN requesting_connection_epoch INTEGER,
    ADD COLUMN effective_at_server_ms BIGINT,
    ADD COLUMN execution_eligible_at_ms BIGINT,
    ADD COLUMN error_message TEXT,
    ADD COLUMN reconciled_by_control_version INTEGER,
    MODIFY COLUMN accepted_at_ms BIGINT NOT NULL,
    MODIFY COLUMN watchdog_deadline_at_ms BIGINT NULL,
    MODIFY COLUMN terminal_at_ms BIGINT NULL;

ALTER TABLE emo_playback_control_transaction
    ADD KEY idx_emo_control_request_generation (
        requesting_client_id, requesting_device_session_id,
        requesting_connection_nonce, requesting_connection_epoch, status
    ),
    ADD KEY idx_emo_control_authority_generation (
        authority_client_id, authority_device_session_id,
        routed_connection_nonce, routed_connection_epoch, status
    );

CREATE TABLE emo_playback_control_reconciliation (
    id CHAR(32) PRIMARY KEY,
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
    server_updated_at_ms BIGINT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uniq_emo_reconcile_context_epoch_version (
        playback_context_id, epoch, reconciliation_control_version
    ),
    KEY idx_emo_reconcile_gap (
        playback_context_id, epoch, through_control_version
    )
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
