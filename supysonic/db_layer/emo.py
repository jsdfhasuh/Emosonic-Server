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
    origin_client_id = CharField(128, null=True)
    queue_json = TextField()
    current_index = IntegerField(default=0)
    track_id = CharField(128, null=True)
    state = CharField(32, default="stopped")
    position_ms = IntegerField(default=0)
    volume = IntegerField(null=True)
    queue_revision = IntegerField(default=1)
    control_version = IntegerField(default=1)
    version = IntegerField(default=1)
    epoch = IntegerField(default=1)
    playback_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)


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
    playback_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    class Meta:
        indexes = ((('playback_context_id', 'owner_client_id'), True),)


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
