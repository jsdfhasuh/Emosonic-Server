from peewee import CharField, DateField, DateTimeField

from .core import PrimaryKeyField, _Model, now


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
