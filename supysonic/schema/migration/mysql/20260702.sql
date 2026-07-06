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
SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE UNIQUE INDEX index_track_metadata_track_id ON track_metadata(track_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
        AND table_name = 'track_metadata'
        AND index_name = 'index_track_metadata_track_id'
);
PREPARE supysonic_migration_stmt FROM @sql;
EXECUTE supysonic_migration_stmt;
DEALLOCATE PREPARE supysonic_migration_stmt;
SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX index_track_metadata_provider ON track_metadata(provider)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
        AND table_name = 'track_metadata'
        AND index_name = 'index_track_metadata_provider'
);
PREPARE supysonic_migration_stmt FROM @sql;
EXECUTE supysonic_migration_stmt;
DEALLOCATE PREPARE supysonic_migration_stmt;
SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX index_track_metadata_updated_at ON track_metadata(updated_at)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
        AND table_name = 'track_metadata'
        AND index_name = 'index_track_metadata_updated_at'
);
PREPARE supysonic_migration_stmt FROM @sql;
EXECUTE supysonic_migration_stmt;
DEALLOCATE PREPARE supysonic_migration_stmt;

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
SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE UNIQUE INDEX index_track_metadata_enrichment_task_track_id ON track_metadata_enrichment_task(track_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
        AND table_name = 'track_metadata_enrichment_task'
        AND index_name = 'index_track_metadata_enrichment_task_track_id'
);
PREPARE supysonic_migration_stmt FROM @sql;
EXECUTE supysonic_migration_stmt;
DEALLOCATE PREPARE supysonic_migration_stmt;
SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX index_track_metadata_enrichment_task_status_next_retry_at ON track_metadata_enrichment_task(status, next_retry_at)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
        AND table_name = 'track_metadata_enrichment_task'
        AND index_name = 'index_track_metadata_enrichment_task_status_next_retry_at'
);
PREPARE supysonic_migration_stmt FROM @sql;
EXECUTE supysonic_migration_stmt;
DEALLOCATE PREPARE supysonic_migration_stmt;
SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX index_track_metadata_enrichment_task_status_locked_at ON track_metadata_enrichment_task(status, locked_at)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
        AND table_name = 'track_metadata_enrichment_task'
        AND index_name = 'index_track_metadata_enrichment_task_status_locked_at'
);
PREPARE supysonic_migration_stmt FROM @sql;
EXECUTE supysonic_migration_stmt;
DEALLOCATE PREPARE supysonic_migration_stmt;
SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX index_track_metadata_enrichment_task_updated_at ON track_metadata_enrichment_task(updated_at)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
        AND table_name = 'track_metadata_enrichment_task'
        AND index_name = 'index_track_metadata_enrichment_task_updated_at'
);
PREPARE supysonic_migration_stmt FROM @sql;
EXECUTE supysonic_migration_stmt;
DEALLOCATE PREPARE supysonic_migration_stmt;
