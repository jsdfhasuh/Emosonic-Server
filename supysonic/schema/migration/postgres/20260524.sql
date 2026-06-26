CREATE TABLE IF NOT EXISTS user_recommendation_feedback (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES "user",
    song_id VARCHAR(128) NOT NULL,
    action VARCHAR(32) NOT NULL,
    scope VARCHAR(64) NOT NULL,
    source VARCHAR(64) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    deleted_at TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS index_user_recommendation_feedback_user_song_scope ON user_recommendation_feedback(user_id, song_id, scope);
CREATE INDEX IF NOT EXISTS index_user_recommendation_feedback_user_scope_deleted ON user_recommendation_feedback(user_id, scope, deleted_at);
