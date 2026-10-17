CREATE TABLE emo_core_startup_recovery (
    id CHAR(36) PRIMARY KEY,
    recovery_fingerprint VARCHAR(64) NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL DEFAULT 'completed',
    started_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER NOT NULL,
    pending_count INTEGER NOT NULL DEFAULT 0,
    incomplete_generation_count INTEGER NOT NULL DEFAULT 0,
    recovered_root_count INTEGER NOT NULL DEFAULT 0,
    recovered_dependency_count INTEGER NOT NULL DEFAULT 0,
    outcome_fingerprint VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE INDEX idx_emo_core_recovery_status_completed
ON emo_core_startup_recovery (status, completed_at_ms);
