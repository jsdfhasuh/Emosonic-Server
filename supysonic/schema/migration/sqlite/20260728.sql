ALTER TABLE emo_broadcast_intent_outcome
ADD COLUMN authority_client_id VARCHAR(128);
ALTER TABLE emo_broadcast_intent_outcome
ADD COLUMN authority_device_session_id VARCHAR(128);
