CREATE TABLE emo_core_startup_recovery (
    id CHAR(32) PRIMARY KEY,
    recovery_fingerprint VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'completed',
    started_at_ms BIGINT NOT NULL,
    completed_at_ms BIGINT NOT NULL,
    pending_count INTEGER NOT NULL DEFAULT 0,
    incomplete_generation_count INTEGER NOT NULL DEFAULT 0,
    recovered_root_count INTEGER NOT NULL DEFAULT 0,
    recovered_dependency_count INTEGER NOT NULL DEFAULT 0,
    outcome_fingerprint VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uniq_emo_core_recovery_fingerprint (recovery_fingerprint),
    KEY idx_emo_core_recovery_status_completed (status, completed_at_ms)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
