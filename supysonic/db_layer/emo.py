from peewee import CharField, DateTimeField, IntegerField, TextField

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
    action = CharField(64)
    accepted_target_json = TextField()
    status = CharField(32, default="pending")
    error_code = CharField(64, null=True)
    depends_on_control_version = IntegerField(null=True)
    accepted_at_ms = IntegerField()
    execution_timeout_ms = IntegerField()
    watchdog_deadline_at_ms = IntegerField()
    applied_control_version = IntegerField(null=True)
    terminal_fingerprint = CharField(64, null=True)
    terminal_at_ms = IntegerField(null=True)
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
        indexes = ((('playback_context_id', 'epoch', 'intent_id'), True),)


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
