#!/bin/sh
set -eu

schema_root=/workspace/supysonic/schema
mysql="mariadb --host=$MYSQL_HOST --user=$MYSQL_USER --database=supysonic_clean --batch --skip-column-names"

$mysql < "$schema_root/mysql.sql"
clean_columns=$($mysql -e "
    SELECT count(*)
    FROM information_schema.columns
    WHERE table_schema = 'supysonic_clean'
      AND table_name = 'emo_playback_context'
      AND column_name IN (
        'authority_device_session_id', 'timeline_id',
        'creation_fingerprint', 'lifecycle', 'closed_at'
      )")
test "$clean_columns" = "5"

mariadb --host="$MYSQL_HOST" --user="$MYSQL_USER" \
    -e "CREATE DATABASE supysonic_upgrade CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
upgrade_mysql="mariadb --host=$MYSQL_HOST --user=$MYSQL_USER --database=supysonic_upgrade --batch --skip-column-names"
$upgrade_mysql <<'SQL'
CREATE TABLE emo_playback_context (
    id CHAR(36) PRIMARY KEY,
    playback_context_id VARCHAR(128) NOT NULL UNIQUE,
    user_name VARCHAR(64) NOT NULL,
    authority_client_id VARCHAR(128),
    origin_client_id VARCHAR(128),
    queue_json TEXT NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    track_id VARCHAR(128),
    state VARCHAR(32) NOT NULL DEFAULT 'stopped',
    position_ms INTEGER NOT NULL DEFAULT 0,
    volume INTEGER,
    queue_revision INTEGER NOT NULL DEFAULT 1,
    control_version INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 1,
    playback_json TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
INSERT INTO emo_playback_context (
    id, playback_context_id, user_name, authority_client_id,
    origin_client_id, queue_json, current_index, track_id,
    state, position_ms, queue_revision, control_version,
    version, epoch, created_at, updated_at
) VALUES (
    'context-row-1', 'context-1', 'alice', 'phone-1',
    'phone-1', '["song-2","song-1"]', 0, 'song-2',
    'closed', 1200, 0, 0, 0, 0,
    '2026-07-08 00:00:00', '2026-07-08 00:01:00'
);
SQL
$upgrade_mysql < "$schema_root/migration/mysql/20260712.sql"
upgrade_result=$($upgrade_mysql -e "
    SELECT CONCAT_WS('|',
        authority_device_session_id IS NULL,
        lifecycle, state, queue_revision, control_version, version, epoch,
        timeline_id, closed_at IS NOT NULL
    )
    FROM emo_playback_context
    WHERE playback_context_id = 'context-1'")
test "$upgrade_result" = "1|closed|stopped|1|1|1|1|playback:context-1|1"
