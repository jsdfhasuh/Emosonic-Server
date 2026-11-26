CREATE TABLE IF NOT EXISTS album_discovery_quota_lock (
    library_key VARCHAR(128) PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE IF NOT EXISTS album_discovery_order (
    id CHAR(32) PRIMARY KEY,
    library_key VARCHAR(128) NOT NULL,
    storefront VARCHAR(2) NOT NULL CHECK (storefront = 'cn'),
    album_id VARCHAR(64) NOT NULL,
    quota_date DATE NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('reserved', 'submitted', 'associated')),
    oracle_task_id VARCHAR(128),
    created_at DATETIME NOT NULL,
    UNIQUE(library_key, storefront, album_id),
    INDEX idx_album_discovery_order_quota (library_key, quota_date, status)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
