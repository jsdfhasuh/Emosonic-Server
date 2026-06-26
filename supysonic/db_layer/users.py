from peewee import (
    BooleanField,
    CharField,
    CompositeKey,
    DateTimeField,
    FixedCharField,
    ForeignKeyField,
    IntegerField,
    TextField,
)

from .core import PrimaryKeyField, _Model, now
from .library import Track


class User(_Model):
    id = PrimaryKeyField()
    name = CharField(64, unique=True)
    mail = CharField(null=True)
    password = FixedCharField(40)
    salt = FixedCharField(6)

    admin = BooleanField(default=False)
    jukebox = BooleanField(default=False)

    lastfm_session = FixedCharField(32, null=True)
    lastfm_status = BooleanField(
        default=True
    )  # True: ok/unlinked, False: invalid session

    listenbrainz_session = FixedCharField(36, null=True)
    listenbrainz_status = BooleanField(
        default=True
    )  # True: ok/unlinked, False: invalid token

    last_play = ForeignKeyField(Track, null=True, backref="+")
    last_play_date = DateTimeField(null=True)

    def as_subsonic_user(self):
        from .serializers import serialize_user

        return serialize_user(self)


class User_Play_Activity(_Model):
    # record user play activity,every record is a play activity
    id = PrimaryKeyField()
    track = ForeignKeyField(Track, backref="play_activity_track")
    user = ForeignKeyField(User, backref="play_activity_user")
    time = DateTimeField(default=now)

    class Meta:
        table_name = "user_play_activity"


class UserRecommendationFeedback(_Model):
    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="recommendation_feedback")
    song_id = CharField(max_length=128)
    target_type = CharField(max_length=32, default="song")
    target_id = CharField(max_length=128)
    action = CharField(max_length=32)
    scope = CharField(max_length=64)
    source = CharField(max_length=64)
    reason = CharField(max_length=64)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)
    deleted_at = DateTimeField(null=True)

    class Meta:
        table_name = "user_recommendation_feedback"
        indexes = (
            (("user", "target_type", "target_id", "scope"), True),
            (("user", "scope", "deleted_at"), False),
        )


class RecommendationAgentSession(_Model):
    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="recommendation_agent_sessions")
    message = TextField()
    reply = TextField()
    recommended_artists_json = TextField()
    context_summary_json = TextField()
    model = CharField(max_length=128)
    language = CharField(max_length=8)
    created_at = DateTimeField(default=now)

    class Meta:
        table_name = "recommendation_agent_session"
        indexes = (
            (("user", "created_at"), False),
        )


class RecommendationAgentCache(_Model):
    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="recommendation_agent_cache_entries")
    context_hash = CharField(max_length=64)
    message = TextField()
    language = CharField(max_length=8)
    model = CharField(max_length=128)
    payload_json = TextField()
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)
    expires_at = DateTimeField()

    class Meta:
        table_name = "recommendation_agent_cache"
        indexes = (
            (("user", "context_hash"), True),
            (("user", "expires_at"), False),
        )


class ClientPrefs(_Model):
    user = ForeignKeyField(User, backref="clients")
    client_name = CharField(32)
    format = CharField(8, null=True)
    bitrate = IntegerField(null=True)

    class Meta:
        primary_key = CompositeKey("user", "client_name")


def clear_last_play_for_tracks(track_condition: object) -> None:
    users = User.select(User.id).join(Track).where(track_condition)
    User.update(last_play=None).where(User.id.in_(users)).execute()
