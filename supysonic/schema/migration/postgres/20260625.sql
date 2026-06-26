ALTER TABLE user_recommendation_feedback ADD COLUMN IF NOT EXISTS target_type VARCHAR(32) NOT NULL DEFAULT 'song';
ALTER TABLE user_recommendation_feedback ALTER COLUMN song_id TYPE VARCHAR(128);
ALTER TABLE user_recommendation_feedback ADD COLUMN IF NOT EXISTS target_id VARCHAR(128) NOT NULL DEFAULT '';
UPDATE user_recommendation_feedback SET target_id = song_id WHERE target_id = '';
DROP INDEX IF EXISTS index_user_recommendation_feedback_user_song_scope;
CREATE UNIQUE INDEX IF NOT EXISTS index_user_recommendation_feedback_user_target_scope ON user_recommendation_feedback(user_id, target_type, target_id, scope);

CREATE TABLE IF NOT EXISTS recommendation_agent_session (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES "user",
    message TEXT NOT NULL,
    reply TEXT NOT NULL,
    recommended_artists_json TEXT NOT NULL,
    context_summary_json TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    language VARCHAR(8) NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS index_recommendation_agent_session_user_created ON recommendation_agent_session(user_id, created_at);

CREATE TABLE IF NOT EXISTS recommendation_agent_cache (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES "user",
    context_hash VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    language VARCHAR(8) NOT NULL,
    model VARCHAR(128) NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS index_recommendation_agent_cache_user_context ON recommendation_agent_cache(user_id, context_hash);
CREATE INDEX IF NOT EXISTS index_recommendation_agent_cache_user_expires ON recommendation_agent_cache(user_id, expires_at);
