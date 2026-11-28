from datetime import datetime, timezone

from peewee import CharField, DateField, DateTimeField, ForeignKeyField, IntegerField, TextField

from .album_discovery import AlbumDiscoveryRelease
from .core import PrimaryKeyField, _Model
from .library import Track
from .users import User


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class BilibiliDiscoveryRun(_Model):
    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="+", on_delete="CASCADE")
    source_day = DateField()
    status = CharField(max_length=32, default="fetch_pending")
    sync_confirmed_at = DateTimeField(null=True)
    snapshot_json = TextField(null=True)
    extract_offset = IntegerField(default=0)
    discovery_progress_json = TextField(default="{}")
    next_attempt_at = DateTimeField(null=True)
    last_error = CharField(max_length=128, default="")
    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    class Meta:
        indexes = ((("user", "source_day"), True),)


class BilibiliDiscoveryCandidate(_Model):
    id = PrimaryKeyField()
    run = ForeignKeyField(BilibiliDiscoveryRun, backref="candidates", on_delete="CASCADE")
    identity_key = CharField(max_length=128)
    title = CharField(max_length=256)
    artist = CharField(max_length=256)
    version = CharField(max_length=32)
    album_hint = CharField(max_length=256, default="")
    source_evidence_json = TextField(default="[]")
    status = CharField(max_length=32, default="pending")
    reason = CharField(max_length=128, default="")
    release = ForeignKeyField(AlbumDiscoveryRelease, null=True, backref="+", on_delete="SET NULL")
    apple_song_id = CharField(max_length=64, null=True)
    track = ForeignKeyField(Track, null=True, backref="+", on_delete="SET NULL")
    ready_at = DateTimeField(null=True)
    next_attempt_at = DateTimeField(null=True)
    created_at = DateTimeField(default=_utc_now)
    updated_at = DateTimeField(default=_utc_now)

    class Meta:
        indexes = (
            (("run", "identity_key"), True),
            (("status", "ready_at"), False),
        )
