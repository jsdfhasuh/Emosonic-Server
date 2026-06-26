CREATE TABLE IF NOT EXISTS user_recommendation_feedback (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    song_id VARCHAR(128) NOT NULL,
    action VARCHAR(32) NOT NULL,
    scope VARCHAR(64) NOT NULL,
    source VARCHAR(64) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME,
    UNIQUE KEY index_user_recommendation_feedback_user_song_scope (user_id, song_id, scope)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_user_recommendation_feedback_user_scope_deleted ON user_recommendation_feedback(user_id, scope, deleted_at);
