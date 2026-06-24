import mimetypes
import os

from peewee import (
    AutoField,
    BlobField,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    MySQLDatabase,
    OperationalError,
    TextField,
    fn,
)

from .core import PathMixin, PrimaryKeyField, _Model, db, now
from ..tool import read_dict_from_json


def _path_tree_candidates(path: str):
    candidates = []
    for candidate in (os.path.normpath(path), os.path.abspath(path)):
        candidate = candidate.rstrip(os.sep) or os.sep
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _path_tree_condition(field, path: str):
    condition = None
    for base_path in _path_tree_candidates(path):
        if base_path == os.sep:
            path_condition = field.startswith(os.sep)
        else:
            path_condition = (field == base_path) | field.startswith(
                base_path + os.sep
            )
        condition = path_condition if condition is None else condition | path_condition

    return condition


class Image(_Model):
    """Store artist and album images."""

    id = AutoField()  # Auto-increment integer ID.
    path = CharField(4096)  # File path.
    # Related type: artist or album.
    image_type = CharField(max_length=10)
    # Related artist or album string ID.
    related_id = CharField(max_length=36)
    # Creation time.
    created = DateTimeField(default=now)


class Folder(PathMixin, _Model):
    id = AutoField()
    root = BooleanField()
    name = CharField()
    path = CharField(4096)  # unique
    _path_hash = BlobField(column_name="path_hash", unique=True)
    created = DateTimeField(default=now)
    cover_art = CharField(null=True)
    last_scan = IntegerField(default=0)

    parent = ForeignKeyField("self", null=True, backref="children")

    def as_subsonic_child(self, user):
        from .serializers import serialize_folder_child

        return serialize_folder_child(self, user)

    def as_subsonic_artist(self, user):  # "Artist" type in XSD
        from .serializers import serialize_folder_artist

        return serialize_folder_artist(self, user)

    def as_subsonic_directory(self, user, client):  # "Directory" type in XSD
        from .serializers import serialize_folder_directory

        return serialize_folder_directory(self, user, client)

    @classmethod
    def prune(cls):
        alias = cls.alias()
        query = cls.select(cls.id).where(
            ~cls.root,
            Track.select(fn.count("*")).where(Track.folder == cls.id) == 0,
            alias.select(fn.count("*")).where(alias.parent == cls.id) == 0,
        )
        total = 0
        while True:
            clone = query.clone()  # peewee caches results, clone to force refetch
            for f in clone:
                f.delete_instance(recursive=True)
                total += 1
            if not len(clone):
                return total

    def delete_hierarchy(self):
        if self.root:
            cond = Track.root_folder == self
        else:
            cond = _path_tree_condition(Track.path, self.path)

        return self.__delete_hierarchy(cond)

    def __delete_hierarchy(self, cond):
        from .annotations import (
            delete_folder_annotations,
            delete_track_annotations,
        )
        from .users import clear_last_play_for_tracks

        clear_last_play_for_tracks(cond)

        tracks = Track.select(Track.id).where(cond)
        delete_track_annotations(tracks)

        path_cond = _path_tree_condition(Folder.path, self.path)
        folders = Folder.select(Folder.id).where(path_cond)
        delete_folder_annotations(folders)

        deleted_tracks = Track.delete().where(cond).execute()

        # Ensure tracks in all child folders are deleted.
        # Collect all related folder IDs.
        folder_ids = [f.id for f in Folder.select(Folder.id).where(path_cond)]

        # Delete all tracks in the related folders.
        Track.delete().where(Track.folder_id.in_(folder_ids)).execute()

        # Delete folders after tracks.
        query = Folder.delete().where(path_cond)
        if isinstance(db.obj, MySQLDatabase):
            query = query.order_by(Folder.path.desc())
        query.execute()

        return deleted_tracks


class Artist(_Model):
    id = PrimaryKeyField()
    name = CharField()
    artist_info_json = CharField(4096, null=True)
    # Points an alias to the canonical artist name.
    real_artist = ForeignKeyField(
        "self", null=True, backref="aliases", on_delete="SET NULL"
    )

    def get_artist_name(self):
        if self.real_artist:
            return self.real_artist.name
        return self.name

    # Return the artist info dictionary for Subsonic responses.
    def as_subsonic_artist(self, user):
        from .serializers import serialize_artist

        return serialize_artist(self, user)

    def get_info(self):
        info = {
            "biography": "",
            "musicBrainzId": "",
            "lastFmUrl": "",
            "smallImageUrl": "",
            "mediumImageUrl": "",
            "largeImageUrl": "",
        }
        if self.artist_info_json:
            try:
                local_data = read_dict_from_json(self.artist_info_json)
                image_data = local_data.get("image")
                if not isinstance(image_data, dict):
                    image_data = {}
                info["biography"] = local_data.get("biography", "")
                info["musicBrainzId"] = local_data.get("musicBrainzId", "")
                info["lastFmUrl"] = local_data.get("lastFmUrl", "")
                info["smallImageUrl"] = image_data.get("small", "")
                info["mediumImageUrl"] = image_data.get("medium", "")
                info["largeImageUrl"] = image_data.get("large", "")
                return info
            except ValueError:
                return info
        info['cover_art'] = "ar-" + str(self.id)
        return info

    # Remove artists that are no longer referenced.
    @classmethod
    def prune(cls):
        from .annotations import delete_orphaned_artist_annotations

        # Collect all referenced artist IDs.
        album_artists = Album.select(Album.artist)
        track_artists = Track.select(Track.artist)
        album_multi_artists = AlbumArtist.select(AlbumArtist.artist_id)
        track_multi_artists = TrackArtist.select(TrackArtist.artist_id)
        delete_orphaned_artist_annotations(
            album_artists,
            track_artists,
            album_multi_artists,
            track_multi_artists,
        )

        # Delete artist records that are no longer referenced.
        return (
            cls.delete()
            .where(cls.real_artist.is_null())
            .where(
                cls.id.not_in(album_artists),
                cls.id.not_in(track_artists),
                cls.id.not_in(album_multi_artists),
            )
            .execute()
        )


class Album(_Model):
    id = PrimaryKeyField()
    name = CharField()
    artist = ForeignKeyField(Artist, backref="albums")
    year = CharField(default=None, null=True)  # Album year.
    release_date = CharField(max_length=32, null=True)
    release_type = CharField(max_length=64, null=True)
    album_info_json = TextField(null=True)

    # Return album artists, including the main artist and collaborators.

    def get_all_artists(self):
        """Return all album artists, including the main artist and collaborators.

        Returns:
            Artists ordered by position.
        """
        # Query all AlbumArtist rows related to the current album.
        artist_relations = self.album_artists.order_by(AlbumArtist.position)

        # Extract artist objects from the relations.
        try:
            artists = [rel.artist_id for rel in artist_relations]
        except OperationalError as exc:
            if "album_artist" not in str(exc):
                raise
            artists = []

        return artists or [self.artist]

    def as_subsonic_album(self, user, server_type=None):  # "AlbumID3" type in XSD
        from .serializers import serialize_album

        return serialize_album(self, user, server_type)

    def sort_key(self):
        year = self.tracks.select(fn.min(Track.year)).scalar() or 9999
        return f"{year}{self.name.lower()}"

    @classmethod
    def prune(cls):
        from .annotations import delete_orphaned_album_annotations
        from .review_tasks import ReviewTask

        album_ids = [
            album_id for (album_id,) in Track.select(Track.album).distinct().tuples()
        ]
        album_id_strings = [str(album_id) for album_id in album_ids]
        delete_orphaned_album_annotations(album_ids)
        AlbumArtist.delete().where(AlbumArtist.album_id.not_in(album_ids)).execute()
        Image.delete().where(
            Image.image_type == "album",
            Image.related_id.not_in(album_id_strings),
        ).execute()
        ReviewTask.delete().where(
            ReviewTask.entity_type == "album",
            ReviewTask.status == "pending",
            ReviewTask.entity_id.not_in(album_id_strings),
        ).execute()
        return cls.delete().where(cls.id.not_in(album_ids)).execute()


class AlbumArtist(_Model):
    """Many-to-many relation between albums and artists."""

    id = AutoField()
    album_id = ForeignKeyField(Album, backref="album_artists", column_name="album_id")
    artist_id = ForeignKeyField(
        Artist, backref="artist_albums", column_name="artist_id"
    )
    position = IntegerField(default=0)  # Sort order; main artist is 1.

    class Meta:
        table_name = 'album_artist'
        indexes = (
            # Ensure each album-artist pair is unique.
            (('album', 'artist'), True),
        )


class Track(PathMixin, _Model):
    # Recursive deletion is handled in code.
    id = PrimaryKeyField()
    disc = IntegerField()
    number = IntegerField()
    title = CharField()
    year = IntegerField(null=True)
    genre = CharField(null=True)
    duration = IntegerField()
    has_art = BooleanField(default=False)

    album = ForeignKeyField(Album, backref="tracks")
    artist = ForeignKeyField(Artist, backref="tracks")

    bitrate = IntegerField()

    path = CharField(4096)  # unique
    _path_hash = BlobField(column_name="path_hash", unique=True)
    created = DateTimeField(default=now)
    last_modification = IntegerField()

    play_count = IntegerField(default=0)
    play_count_web = IntegerField(default=0)
    last_play = DateTimeField(null=True)

    root_folder = ForeignKeyField(Folder, backref="+")
    folder = ForeignKeyField(Folder, backref="tracks")

    def as_subsonic_child(self, user, prefs):
        from .serializers import serialize_track_child

        return serialize_track_child(self, user, prefs)

    @property
    def mimetype(self):
        return mimetypes.guess_type(self.path, False)[0] or "application/octet-stream"

    def duration_str(self):
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        ret = f"{m:02}:{s:02}"
        if h:
            ret = f"{h:02}:{ret}"
        return ret

    def suffix(self):
        return os.path.splitext(self.path)[1][1:].lower()

    def sort_key(self):
        return f"{self.album.artist.get_artist_name()}{self.album.name}{self.disc:02}{self.number:02}{self.title}".lower()


class TrackArtist(_Model):
    """Many-to-many relation between tracks and artists."""

    id = AutoField()
    track_id = ForeignKeyField(Track, backref="track_artists")
    artist_id = ForeignKeyField(Artist, backref="artist_tracks")
    position = IntegerField(default=0)  # Sort order; main artist is 1.

    class Meta:
        table_name = 'track_artist'
        indexes = (
            # Ensure each track-artist pair is unique.
            (('track', 'artist'), True),
        )
