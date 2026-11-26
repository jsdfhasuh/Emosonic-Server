CREATE TABLE IF NOT EXISTS album_discovery_quota_lock (
    library_key VARCHAR(128) PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS album_discovery_order (
    id UUID PRIMARY KEY,
    library_key VARCHAR(128) NOT NULL,
    storefront VARCHAR(2) NOT NULL CHECK (storefront = 'cn'),
    album_id VARCHAR(64) NOT NULL,
    quota_date DATE NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('reserved', 'submitted', 'associated')),
    oracle_task_id VARCHAR(128),
    created_at TIMESTAMP NOT NULL,
    UNIQUE(library_key, storefront, album_id)
);
CREATE INDEX IF NOT EXISTS idx_album_discovery_order_quota
ON album_discovery_order (library_key, quota_date, status);
