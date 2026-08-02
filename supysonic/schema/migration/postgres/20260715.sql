CREATE INDEX IF NOT EXISTS idx_emo_playback_context_binding
ON emo_playback_context (
    user_name,
    lifecycle,
    authority_client_id,
    authority_device_session_id
);
