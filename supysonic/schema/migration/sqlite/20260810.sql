CREATE INDEX IF NOT EXISTS idx_emo_context_close_retention
ON emo_playback_context (lifecycle, closed_at, playback_context_id);

CREATE INDEX IF NOT EXISTS idx_emo_control_retention
ON emo_playback_control_transaction (
    playback_context_id,
    status,
    terminal_at_ms,
    epoch,
    command_control_version
);

CREATE INDEX IF NOT EXISTS idx_emo_control_dependency_ref
ON emo_playback_control_transaction (
    playback_context_id,
    epoch,
    depends_on_control_version
);

CREATE INDEX IF NOT EXISTS idx_emo_control_reconciliation_ref
ON emo_playback_control_transaction (
    playback_context_id,
    epoch,
    reconciled_by_control_version
);

CREATE INDEX IF NOT EXISTS idx_emo_reconcile_retention
ON emo_playback_control_reconciliation (
    playback_context_id,
    server_updated_at_ms,
    epoch,
    reconciliation_control_version
);

CREATE INDEX IF NOT EXISTS idx_emo_reconcile_trigger_ref
ON emo_playback_control_reconciliation (
    playback_context_id,
    epoch,
    trigger_command_control_version
);

CREATE INDEX IF NOT EXISTS idx_emo_local_intent_retention
ON emo_playback_local_intent (
    playback_context_id,
    created_at,
    epoch,
    control_version
);

CREATE INDEX IF NOT EXISTS idx_emo_core_recovery_retention
ON emo_core_startup_recovery (
    status,
    completed_at_ms,
    recovery_fingerprint
);
