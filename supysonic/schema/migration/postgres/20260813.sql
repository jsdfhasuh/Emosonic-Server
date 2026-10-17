ALTER TABLE emo_playback_handoff
    ADD COLUMN context_epoch INTEGER,
    ADD COLUMN provisional_control_version INTEGER;

CREATE INDEX IF NOT EXISTS idx_emo_handoff_provisional_lane
ON emo_playback_handoff (
    playback_context_id,
    context_epoch,
    handoff_id
);
