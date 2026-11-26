from peewee import (
    CharField,
    DateField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    TextField,
    UUIDField,
)

from .core import PrimaryKeyField, _Model, now
from .library import Track
from .users import User


class AlbumDiscoveryQuotaLock(_Model):
    library_key = CharField(max_length=128, primary_key=True)


class AlbumDiscoveryOrder(_Model):
    id = PrimaryKeyField()
    library_key = CharField(max_length=128)
    storefront = CharField(max_length=2)
    album_id = CharField(max_length=64)
    quota_date = DateField()
    status = CharField(max_length=16)
    oracle_task_id = CharField(max_length=128, null=True)
    created_at = DateTimeField(default=now)

    class Meta:
        indexes = (
            (("library_key", "storefront", "album_id"), True),
            (("library_key", "quota_date", "status"), False),
        )


class AlbumDiscoveryPreview(_Model):
    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="+", on_delete="CASCADE")
    library_key = CharField(max_length=128)
    day = DateField()
    payload = TextField()
    created_at = DateTimeField(default=now)

    class Meta:
        indexes = ((("user", "library_key", "day"), True),)


class AlbumDiscoveryRelease(_Model):
    id = PrimaryKeyField()
    library_key = CharField(max_length=128)
    album_id = CharField(max_length=64)
    album_url = TextField()
    title = TextField()
    artist = TextField()
    expected_tracks = TextField()
    reconciliation = TextField(default="{}")
    ready_at = DateTimeField(null=True)
    checked_at = DateTimeField(null=True)
    oracle_status = CharField(max_length=32, default="")
    oracle_day = DateField(null=True)
    oracle_detail = TextField(default="")
    created_at = DateTimeField(default=now)

    class Meta:
        indexes = ((("library_key", "album_id"), True),)


class AlbumDiscoverySelection(_Model):
    id = PrimaryKeyField()
    release = ForeignKeyField(AlbumDiscoveryRelease, backref="selections", on_delete="CASCADE")
    user = ForeignKeyField(User, backref="+", on_delete="CASCADE")
    representative_song_id = CharField(max_length=64)
    representative_track = ForeignKeyField(Track, null=True, backref="+", on_delete="SET NULL")
    evidence = TextField()
    priority = IntegerField()
    created_at = DateTimeField(default=now)

    class Meta:
        indexes = ((("release", "user"), True),)


class AlbumDiscoveryPlacement(_Model):
    id = PrimaryKeyField()
    selection = ForeignKeyField(AlbumDiscoverySelection, unique=True, backref="+", on_delete="CASCADE")
    user = ForeignKeyField(User, backref="+", on_delete="CASCADE")
    playlist_id = UUIDField()
    day = DateField()
    track_id = UUIDField()
    position = IntegerField()
    created_at = DateTimeField(default=now)

    class Meta:
        indexes = ((("user", "day"), True),)
