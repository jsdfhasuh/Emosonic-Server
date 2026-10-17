ALTER TABLE emo_playback_handoff
    ADD COLUMN source_device_session_id VARCHAR(128),
    ADD COLUMN source_connection_nonce VARCHAR(128),
    ADD COLUMN source_connection_epoch INTEGER,
    ADD COLUMN target_device_session_id VARCHAR(128),
    ADD COLUMN target_connection_nonce VARCHAR(128),
    ADD COLUMN target_connection_epoch INTEGER;
