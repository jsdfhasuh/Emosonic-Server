CREATE TABLE IF NOT EXISTS emo_broadcast_recovery_abandon (
    id CHAR(36) PRIMARY KEY,
    user_name VARCHAR(64) NOT NULL,
    client_id VARCHAR(128) NOT NULL,
    device_session_id VARCHAR(128) NOT NULL,
    broadcast_id VARCHAR(128) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    terminal_broadcast_revision INTEGER NOT NULL,
    obligation_kind VARCHAR(16) NOT NULL,
    outcome_json TEXT NOT NULL,
    abandoned_at_ms INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(user_name, client_id, device_session_id)
);

CREATE INDEX IF NOT EXISTS idx_emo_broadcast_abandon_time
ON emo_broadcast_recovery_abandon (broadcast_id, abandoned_at_ms);

CREATE TABLE IF NOT EXISTS emo_permanent_device_decommission (
    id CHAR(36) PRIMARY KEY,
    user_name VARCHAR(64) NOT NULL,
    client_id VARCHAR(128) NOT NULL,
    device_session_id VARCHAR(128) NOT NULL,
    broadcast_id VARCHAR(128) NOT NULL,
    abandon_request_fingerprint VARCHAR(64) NOT NULL,
    decommissioned_at_ms INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(user_name, client_id, device_session_id)
);

CREATE INDEX IF NOT EXISTS idx_emo_permanent_decommission_time
ON emo_permanent_device_decommission (user_name, decommissioned_at_ms);
