ALTER TABLE emo_playback_context ADD COLUMN authority_device_session_id VARCHAR(128);
ALTER TABLE emo_playback_context ADD COLUMN timeline_id VARCHAR(128);
ALTER TABLE emo_playback_context ADD COLUMN creation_fingerprint VARCHAR(64);
ALTER TABLE emo_playback_context ADD COLUMN lifecycle VARCHAR(16) NOT NULL DEFAULT 'active';
ALTER TABLE emo_playback_context ADD COLUMN closed_at DATETIME;

UPDATE emo_playback_context
SET timeline_id = 'playback:' || playback_context_id
WHERE timeline_id IS NULL;

UPDATE emo_playback_context
SET epoch = CASE WHEN epoch < 1 THEN 1 ELSE epoch END,
    version = CASE WHEN version < 1 THEN 1 ELSE version END,
    queue_revision = CASE WHEN queue_revision < 1 THEN 1 ELSE queue_revision END,
    control_version = CASE WHEN control_version < 1 THEN 1 ELSE control_version END;

UPDATE emo_playback_context
SET lifecycle = 'closed',
    closed_at = updated_at,
    state = 'stopped'
WHERE state IN ('closed', 'expired');
