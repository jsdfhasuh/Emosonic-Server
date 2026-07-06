from uuid import UUID

from peewee import CharField, DateTimeField, TextField

from .core import PrimaryKeyField, _Model, now


class ReviewTask(_Model):
    id = PrimaryKeyField()
    entity_type = CharField(max_length=32)
    entity_id = CharField(max_length=36)
    task_type = CharField(max_length=64)
    status = CharField(max_length=32)
    reason = CharField(max_length=64)
    pending_key = CharField(max_length=96, null=True)
    snapshot_json = TextField(null=True)
    created = DateTimeField(default=now)
    updated = DateTimeField(default=now)
    resolved_at = DateTimeField(null=True)
    expires_at = DateTimeField(null=True)

    def is_album_task(self):
        return self.entity_type == "album"

    def is_artist_task(self):
        return self.entity_type == "artist"

    def is_track_task(self):
        return self.entity_type == "track"

    def get_album(self):
        from .library import Album

        if not self.is_album_task():
            return None
        return Album.get_or_none(Album.id == self.entity_id)

    def get_artist(self):
        from .library import Artist

        if not self.is_artist_task():
            return None
        return Artist.get_or_none(Artist.id == self.entity_id)

    def get_track(self):
        from .library import Track

        if not self.is_track_task():
            return None
        return Track.get_or_none(Track.id == self.entity_id)

    @property
    def album(self):
        return self.get_album()

    @album.setter
    def album(self, value):
        self.entity_type = "album"
        self.entity_id = str(getattr(value, "id", value))

    @property
    def artist(self):
        return self.get_artist()

    @artist.setter
    def artist(self, value):
        self.entity_type = "artist"
        self.entity_id = str(getattr(value, "id", value))

    @property
    def track(self):
        return self.get_track()

    @track.setter
    def track(self, value):
        self.entity_type = "track"
        self.entity_id = str(getattr(value, "id", value))

    @property
    def album_id(self):
        if not self.is_album_task():
            return None
        return UUID(self.entity_id)

    @property
    def artist_id(self):
        if not self.is_artist_task():
            return None
        return UUID(self.entity_id)

    @property
    def track_id(self):
        if not self.is_track_task():
            return None
        return UUID(self.entity_id)

    def save(self, *args, **kwargs):
        if self.status == "pending" and self.entity_type and self.entity_id:
            if self.entity_type == "artist":
                self.pending_key = f"artist:{self.entity_id}:pending:{self.reason}"
            elif self.entity_type == "track":
                self.pending_key = f"track:{self.entity_id}:pending:{self.reason}"
            elif self.reason == "external_enrichment":
                self.pending_key = f"{self.entity_type}:{self.entity_id}:pending:{self.reason}"
            else:
                self.pending_key = f"{self.entity_type}:{self.entity_id}:pending"
        elif self.status != "pending":
            self.pending_key = None
        return super().save(*args, **kwargs)

    class Meta:
        table_name = "review_task"
        indexes = (
            (("entity_type", "entity_id", "status"), False),
            (("status", "created"), False),
            (("pending_key",), True),
        )


AlbumReviewTask = ReviewTask
