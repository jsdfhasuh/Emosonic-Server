from peewee import (
    BigIntegerField,
    CharField,
    DateTimeField,
    FloatField,
    IntegerField,
    TextField,
)

from .core import PrimaryKeyField, _Model, now


class EmoSessionQueue(_Model):
    id = PrimaryKeyField()
    session_id = CharField(128, unique=True)
    user_name = CharField(64)
    owner_client_id = CharField(128)
    queue_json = TextField()
    current_index = IntegerField(default=0)
    position_ms = IntegerField(default=0)
    version = IntegerField(default=1)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)


class EmoLocalQueue(_Model):
    id = PrimaryKeyField()
    session_id = CharField(128)
    owner_client_id = CharField(128)
    queue_json = TextField()
    current_index = IntegerField(default=0)
    position_ms = IntegerField(default=0)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = ((('session_id', 'owner_client_id'), True),)


class EmoPlaybackState(_Model):
    id = PrimaryKeyField()
    session_id = CharField(128)
    user_name = CharField(64)
    owner_client_id = CharField(128)
    state = CharField(32)
    track_id = CharField(128, null=True)
    position_ms = IntegerField(default=0)
    volume = IntegerField(null=True)
    playback_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = ((('session_id', 'owner_client_id'), True),)


class EmoPlaybackContext(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128, unique=True)
    user_name = CharField(64)
    authority_client_id = CharField(128, null=True)
    authority_device_session_id = CharField(128, null=True)
    origin_client_id = CharField(128, null=True)
    timeline_id = CharField(128, null=True)
    creation_fingerprint = CharField(64, null=True)
    lifecycle = CharField(16, default="active")
    queue_json = TextField()
    current_index = IntegerField(default=0)
    track_id = CharField(128, null=True)
    state = CharField(32, default="idle")
    position_ms = IntegerField(default=0)
    volume = IntegerField(null=True)
    queue_revision = IntegerField(default=1)
    control_version = IntegerField(default=1)
    version = IntegerField(default=1)
    epoch = IntegerField(default=1)
    playback_json = TextField(null=True)
    closed_at = DateTimeField(null=True)
    close_action = CharField(64, null=True)
    close_request_fingerprint = CharField(64, null=True)
    close_expected_epoch = IntegerField(null=True)
    close_base_version = IntegerField(null=True)
    closed_from_epoch = IntegerField(null=True)
    closed_from_version = IntegerField(null=True)
    final_epoch = IntegerField(null=True)
    final_version = IntegerField(null=True)
    final_queue_revision = IntegerField(null=True)
    final_control_version = IntegerField(null=True)
    close_outcome_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (
                (
                    'user_name',
                    'lifecycle',
                    'authority_client_id',
                    'authority_device_session_id',
                ),
                False,
            ),
            (('lifecycle', 'closed_at', 'playback_context_id'), False),
        )


class EmoDevicePlaybackState(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128)
    device_session_id = CharField(128)
    owner_client_id = CharField(128)
    user_name = CharField(64)
    state = CharField(32)
    track_id = CharField(128, null=True)
    position_ms = IntegerField(default=0)
    volume = IntegerField(null=True)
    is_authority = IntegerField(default=0)
    mode = CharField(32, default="normal")
    context_epoch = IntegerField(default=1)
    applied_control_version = IntegerField(default=0)
    client_seq = IntegerField(default=0)
    playback_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = ((('playback_context_id', 'owner_client_id'), True),)


class EmoPlaybackControlTransaction(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128)
    user_name = CharField(64)
    epoch = IntegerField()
    command_control_version = IntegerField()
    requesting_client_id = CharField(128)
    authority_client_id = CharField(128)
    authority_device_session_id = CharField(128)
    routed_connection_nonce = CharField(128)
    routed_connection_epoch = IntegerField(default=1)
    requesting_device_session_id = CharField(128, null=True)
    requesting_connection_nonce = CharField(128, null=True)
    requesting_connection_epoch = IntegerField(null=True)
    action = CharField(64)
    accepted_target_json = TextField()
    status = CharField(32, default="pending")
    error_code = CharField(64, null=True)
    depends_on_control_version = IntegerField(null=True)
    accepted_at_ms = BigIntegerField()
    execution_timeout_ms = IntegerField()
    watchdog_deadline_at_ms = BigIntegerField(null=True)
    effective_at_server_ms = BigIntegerField(null=True)
    execution_eligible_at_ms = BigIntegerField(null=True)
    error_message = TextField(null=True)
    applied_control_version = IntegerField(null=True)
    terminal_fingerprint = CharField(64, null=True)
    terminal_at_ms = BigIntegerField(null=True)
    reconciled_by_control_version = IntegerField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (
                (
                    'playback_context_id',
                    'epoch',
                    'command_control_version',
                ),
                True,
            ),
            (('status', 'watchdog_deadline_at_ms'), False),
            (
                (
                    'playback_context_id',
                    'epoch',
                    'status',
                    'command_control_version',
                ),
                False,
            ),
            (
                (
                    'requesting_client_id',
                    'requesting_device_session_id',
                    'requesting_connection_nonce',
                    'requesting_connection_epoch',
                    'status',
                ),
                False,
            ),
            (
                (
                    'authority_client_id',
                    'authority_device_session_id',
                    'routed_connection_nonce',
                    'routed_connection_epoch',
                    'status',
                ),
                False,
            ),
            (
                (
                    'playback_context_id',
                    'status',
                    'terminal_at_ms',
                    'epoch',
                    'command_control_version',
                ),
                False,
            ),
            (
                (
                    'playback_context_id',
                    'epoch',
                    'depends_on_control_version',
                ),
                False,
            ),
            (
                (
                    'playback_context_id',
                    'epoch',
                    'reconciled_by_control_version',
                ),
                False,
            ),
        )


class EmoPlaybackControlReconciliation(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128)
    user_name = CharField(64)
    epoch = IntegerField()
    reconciliation_control_version = IntegerField()
    from_applied_control_version = IntegerField()
    through_control_version = IntegerField()
    trigger_kind = CharField(64)
    trigger_command_control_version = IntegerField(null=True)
    actual_fact_fingerprint = CharField(64)
    actual_fact_json = TextField()
    canonical_update_json = TextField()
    server_updated_at_ms = BigIntegerField()
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (
                (
                    'playback_context_id',
                    'epoch',
                    'reconciliation_control_version',
                ),
                True,
            ),
            (
                (
                    'playback_context_id',
                    'epoch',
                    'through_control_version',
                ),
                False,
            ),
            (
                (
                    'playback_context_id',
                    'server_updated_at_ms',
                    'epoch',
                    'reconciliation_control_version',
                ),
                False,
            ),
            (
                (
                    'playback_context_id',
                    'epoch',
                    'trigger_command_control_version',
                ),
                False,
            ),
        )


class EmoCoreStartupRecovery(_Model):
    id = PrimaryKeyField()
    recovery_fingerprint = CharField(64, unique=True)
    status = CharField(16, default="completed")
    started_at_ms = BigIntegerField()
    completed_at_ms = BigIntegerField()
    pending_count = IntegerField(default=0)
    incomplete_generation_count = IntegerField(default=0)
    recovered_root_count = IntegerField(default=0)
    recovered_dependency_count = IntegerField(default=0)
    outcome_fingerprint = CharField(64)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (('status', 'completed_at_ms'), False),
            (
                ('status', 'completed_at_ms', 'recovery_fingerprint'),
                False,
            ),
        )


class EmoFollowSafetyLease(_Model):
    id = PrimaryKeyField()
    user_name = CharField(64)
    follower_client_id = CharField(128)
    follower_device_session_id = CharField(128)
    follower_connection_nonce = CharField(128)
    follower_connection_epoch = IntegerField(default=1)
    source_playback_context_id = CharField(128)
    source_authority_client_id = CharField(128)
    source_authority_device_session_id = CharField(128)
    source_connection_nonce = CharField(128)
    source_connection_epoch = IntegerField(default=1)
    suspended_playback_context_id = CharField(128)
    suspended_authority_client_id = CharField(128)
    suspended_authority_device_session_id = CharField(128)
    suspended_connection_nonce = CharField(128)
    suspended_connection_epoch = IntegerField(default=1)
    suspended_epoch = IntegerField()
    suspended_version = IntegerField()
    suspended_queue_revision = IntegerField()
    suspended_control_version = IntegerField()
    suspended_applied_control_version = IntegerField()
    phase = CharField(32, default="active")
    follow_reconnect_grace_expires_at_ms = BigIntegerField(null=True)
    source_recovery_deadline_at_ms = BigIntegerField(null=True)
    lease_fingerprint = CharField(64, unique=True)
    start_request_fingerprint = CharField(64)
    start_ack_json = TextField()
    stop_request_fingerprint = CharField(64, null=True)
    cleanup_fingerprint = CharField(64, null=True)
    created_at_ms = BigIntegerField()
    updated_at_ms = BigIntegerField()
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (
                (
                    'user_name',
                    'follower_client_id',
                    'follower_device_session_id',
                ),
                True,
            ),
            (('user_name', 'suspended_playback_context_id'), True),
            (
                (
                    'user_name',
                    'phase',
                    'follow_reconnect_grace_expires_at_ms',
                ),
                False,
            ),
            (('source_playback_context_id', 'phase'), False),
        )


class EmoPlaybackPrepareTransaction(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128)
    user_name = CharField(64)
    epoch = IntegerField()
    intent_id = CharField(128)
    requesting_client_id = CharField(128)
    authority_client_id = CharField(128)
    authority_device_session_id = CharField(128)
    routed_connection_nonce = CharField(128)
    routed_connection_epoch = IntegerField(default=1)
    request_fingerprint = CharField(64)
    initial_queue_json = TextField(null=True)
    control_version = IntegerField()
    status = CharField(32, default="preparing")
    error_code = CharField(64, null=True)
    error_message = TextField(null=True)
    deadline_at_ms = IntegerField()
    canonical_result_json = TextField(null=True)
    terminal_at_ms = IntegerField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (('playback_context_id', 'epoch', 'intent_id'), True),
            (('playback_context_id', 'epoch', 'status'), False),
            (('status', 'deadline_at_ms'), False),
        )


class EmoPlaybackLocalIntent(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128)
    user_name = CharField(64)
    epoch = IntegerField()
    intent_id = CharField(128)
    authority_client_id = CharField(128)
    authority_device_session_id = CharField(128)
    request_fingerprint = CharField(64)
    canonical_update_json = TextField()
    control_version = IntegerField()
    superseded_through_control_version = IntegerField()
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (('playback_context_id', 'epoch', 'intent_id'), True),
            (
                (
                    'playback_context_id',
                    'created_at',
                    'epoch',
                    'control_version',
                ),
                False,
            ),
        )


class EmoPlaybackHandoff(_Model):
    id = PrimaryKeyField()
    handoff_id = CharField(128, unique=True)
    request_id = CharField(128, null=True)
    playback_context_id = CharField(128)
    user_name = CharField(64)
    source_client_id = CharField(128)
    target_client_id = CharField(128)
    origin_client_id = CharField(128, null=True)
    status = CharField(32)
    base_control_version = IntegerField(default=0)
    snapshot_json = TextField(null=True)
    error_code = CharField(64, null=True)
    error_message = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)


class EmoBroadcast(_Model):
    id = PrimaryKeyField()
    broadcast_id = CharField(128, unique=True)
    user_name = CharField(64)
    playback_context_id = CharField(128)
    intent_id = CharField(128)
    owner_client_id = CharField(128)
    authority_client_id = CharField(128)
    authority_device_session_id = CharField(128)
    lifecycle_state = CharField(32, default="active")
    broadcast_revision = IntegerField(default=1)
    snapshot_json = TextField()
    authority_disconnect_deadline_ms = BigIntegerField(null=True)
    terminal_at_ms = BigIntegerField(null=True)
    full_expires_at_ms = BigIntegerField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (('user_name', 'playback_context_id', 'lifecycle_state'), False),
            (('lifecycle_state', 'authority_disconnect_deadline_ms'), False),
            (('lifecycle_state', 'terminal_at_ms'), False),
        )


class EmoBroadcastIntentOutcome(_Model):
    id = PrimaryKeyField()
    user_name = CharField(64)
    playback_context_id = CharField(128)
    owner_client_id = CharField(128)
    authority_client_id = CharField(128, null=True)
    authority_device_session_id = CharField(128, null=True)
    intent_id = CharField(128)
    request_fingerprint = CharField(64)
    broadcast_id = CharField(128)
    final_participants_json = TextField()
    skipped_client_ids_json = TextField()
    start_ack_json = TextField()
    terminal_broadcast_revision = IntegerField(null=True)
    stop_ack_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (
                (
                    'user_name',
                    'playback_context_id',
                    'owner_client_id',
                    'intent_id',
                ),
                True,
            ),
            (('playback_context_id', 'created_at'), False),
        )


class EmoBroadcastFence(_Model):
    id = PrimaryKeyField()
    resource_key = CharField(80, unique=True)
    broadcast_id = CharField(128)
    user_name = CharField(64)
    role = CharField(16)
    phase = CharField(32, default="nonterminal")
    playback_context_id = CharField(128, null=True)
    client_id = CharField(128, null=True)
    device_session_id = CharField(128, null=True)
    recovery_slot_reserved = IntegerField(default=0)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (('broadcast_id', 'role', 'phase'), False),
            (('user_name', 'phase', 'recovery_slot_reserved'), False),
        )


class EmoBroadcastParticipant(_Model):
    id = PrimaryKeyField()
    broadcast_id = CharField(128)
    user_name = CharField(64)
    client_id = CharField(128)
    device_session_id = CharField(128)
    suspended_playback_context_id = CharField(128)
    suspended_epoch = IntegerField()
    suspended_version = IntegerField()
    suspended_queue_revision = IntegerField()
    suspended_control_version = IntegerField()
    suspended_applied_control_version = IntegerField()
    restore_pending = IntegerField(default=0)
    terminal_confirmed = IntegerField(default=0)
    target_broadcast_revision = IntegerField(null=True)
    target_delivery_id = CharField(128, null=True)
    deadline_broadcast_revision = IntegerField(null=True)
    feedback_deadline_at_server_ms = BigIntegerField(null=True)
    sync_status = CharField(32, default="pending")
    applied_broadcast_revision = IntegerField(null=True)
    applied_queue_index = IntegerField(null=True)
    applied_track_id = CharField(128, null=True)
    applied_position_ms = BigIntegerField(null=True)
    applied_state = CharField(32, null=True)
    applied_playback_rate = FloatField(null=True)
    applied_at_server_ms = BigIntegerField(null=True)
    failed_broadcast_revision = IntegerField(null=True)
    failed_last_applied_broadcast_revision = IntegerField(null=True)
    failed_error_code = CharField(64, null=True)
    failed_error_message = TextField(null=True)
    timed_out_broadcast_revision = IntegerField(null=True)
    last_feedback_client_seq = IntegerField(null=True)
    last_feedback_at_ms = BigIntegerField(null=True)
    restore_completed = IntegerField(default=0)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (('broadcast_id', 'client_id', 'device_session_id'), True),
            (('user_name', 'restore_pending'), False),
            (('sync_status', 'feedback_deadline_at_server_ms'), False),
        )


class EmoBroadcastRevision(_Model):
    id = PrimaryKeyField()
    broadcast_id = CharField(128)
    broadcast_revision = IntegerField()
    snapshot_json = TextField()
    canonical_action = CharField(32)
    created_at_ms = BigIntegerField()
    created_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (('broadcast_id', 'broadcast_revision'), True),
            (('broadcast_id', 'created_at_ms'), False),
        )


class EmoBroadcastDelivery(_Model):
    id = PrimaryKeyField()
    delivery_id = CharField(128, unique=True)
    broadcast_id = CharField(128)
    broadcast_revision = IntegerField()
    client_id = CharField(128)
    device_session_id = CharField(128)
    action = CharField(32)
    effective_at_server_ms = BigIntegerField(null=True)
    server_time_ms = BigIntegerField(null=True)
    delivery_position_ms = BigIntegerField()
    payload_json = TextField()
    connection_nonce = CharField(128, null=True)
    is_current = IntegerField(default=1)
    delivery_status = CharField(32, default="pending")
    created_at_ms = BigIntegerField()
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (
                (
                    'broadcast_id',
                    'broadcast_revision',
                    'client_id',
                    'device_session_id',
                    'is_current',
                ),
                False,
            ),
            (('broadcast_id', 'delivery_status'), False),
        )


class EmoBroadcastFeedbackSettlement(_Model):
    id = PrimaryKeyField()
    playback_context_id = CharField(128)
    broadcast_id = CharField(128)
    client_id = CharField(128)
    device_session_id = CharField(128)
    connection_nonce = CharField(128)
    connection_epoch = IntegerField(default=1)
    client_seq = IntegerField()
    request_fingerprint = CharField(64)
    canonical_result_json = TextField()
    follow_up_delivery_id = CharField(128, null=True)
    created_at_ms = BigIntegerField()
    created_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (
                (
                    'playback_context_id',
                    'broadcast_id',
                    'client_id',
                    'device_session_id',
                    'connection_nonce',
                    'connection_epoch',
                    'client_seq',
                ),
                True,
            ),
            (
                (
                    'broadcast_id',
                    'client_id',
                    'device_session_id',
                    'created_at_ms',
                ),
                False,
            ),
        )


class EmoBroadcastTerminalRecovery(_Model):
    id = PrimaryKeyField()
    user_name = CharField(64)
    playback_context_id = CharField(128)
    broadcast_id = CharField(128)
    client_id = CharField(128)
    device_session_id = CharField(128)
    terminal_broadcast_revision = IntegerField()
    current_delivery_id = CharField(128, null=True)
    suspended_playback_context_id = CharField(128)
    suspended_epoch = IntegerField()
    suspended_version = IntegerField()
    suspended_queue_revision = IntegerField()
    suspended_control_version = IntegerField()
    suspended_applied_control_version = IntegerField()
    last_applied_broadcast_revision = IntegerField(null=True)
    terminal_queue_index = IntegerField()
    terminal_track_id = CharField(128)
    terminal_position_ms = BigIntegerField()
    terminal_playback_rate = FloatField()
    terminal_at_server_ms = BigIntegerField()
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (('user_name', 'client_id', 'device_session_id'), True),
            (('terminal_at_server_ms',), False),
        )
