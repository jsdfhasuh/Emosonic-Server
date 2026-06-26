ALTER TABLE user_recommendation_feedback ADD COLUMN target_type VARCHAR(32) NOT NULL DEFAULT 'song';
ALTER TABLE user_recommendation_feedback MODIFY song_id VARCHAR(128) NOT NULL;
ALTER TABLE user_recommendation_feedback ADD COLUMN target_id VARCHAR(128) NOT NULL DEFAULT '';
UPDATE user_recommendation_feedback SET target_id = song_id WHERE target_id = '';
DROP INDEX index_user_recommendation_feedback_user_song_scope ON user_recommendation_feedback;
CREATE UNIQUE INDEX index_user_recommendation_feedback_user_target_scope ON user_recommendation_feedback(user_id, target_type, target_id, scope);

CREATE TABLE IF NOT EXISTS recommendation_agent_session (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    message TEXT NOT NULL,
    reply TEXT NOT NULL,
    recommended_artists_json TEXT NOT NULL,
    context_summary_json TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    language VARCHAR(8) NOT NULL,
    created_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX index_recommendation_agent_session_user_created ON recommendation_agent_session(user_id, created_at);

CREATE TABLE IF NOT EXISTS recommendation_agent_cache (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL REFERENCES user(id),
    context_hash VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    language VARCHAR(8) NOT NULL,
    model VARCHAR(128) NOT NULL,
    payload_json TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE UNIQUE INDEX index_recommendation_agent_cache_user_context ON recommendation_agent_cache(user_id, context_hash);
CREATE INDEX index_recommendation_agent_cache_user_expires ON recommendation_agent_cache(user_id, expires_at);
