import json
from typing import Iterable, List, Optional

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    TextField,
)

from .core import PrimaryKeyField, _Model, now
from .library import Track
from .review_tasks import ReviewTask


class TrackMetadata(_Model):
    id = PrimaryKeyField()
    track = ForeignKeyField(Track, unique=True, backref="metadata")
    track_last_modification = IntegerField()
    language = CharField(max_length=16, null=True)
    mood_json = TextField(null=True)
    scene_json = TextField(null=True)
    tags_json = TextField(null=True)
    summary = TextField(null=True)
    energy = IntegerField(null=True)
    valence = IntegerField(null=True)
    danceability = IntegerField(null=True)
    confidence = FloatField(null=True)
    provider = CharField(max_length=64, null=True)
    model = CharField(max_length=128, null=True)
    source = CharField(max_length=64, null=True)
    raw_json = TextField(null=True)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)

    def save(self, *args, **kwargs):
        self.updated_at = now()
        return super().save(*args, **kwargs)

    def get_moods(self) -> List[str]:
        return _read_string_list(self.mood_json)

    def set_moods(self, values: Iterable[object]) -> None:
        self.mood_json = _write_string_list(values)

    def get_scenes(self) -> List[str]:
        return _read_string_list(self.scene_json)

    def set_scenes(self, values: Iterable[object]) -> None:
        self.scene_json = _write_string_list(values)

    def get_tags(self) -> List[str]:
        return _read_string_list(self.tags_json)

    def set_tags(self, values: Iterable[object]) -> None:
        self.tags_json = _write_string_list(values)

    class Meta:
        table_name = "track_metadata"
        indexes = (
            (("track",), True),
            (("provider",), False),
            (("updated_at",), False),
        )


class TrackMetadataEnrichmentTask(_Model):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_RETRY = "retry"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUSES = (
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_RETRY,
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_SKIPPED,
    )

    REASON_NEW_TRACK = "new_track"
    REASON_METADATA_MISSING = "metadata_missing"
    REASON_TAG_UPDATED = "tag_updated"
    REASON_MANUAL = "manual"
    REASON_MANUAL_FORCE = "manual_force"
    REASON_FAILED_RETRY = "failed_retry"
    REASON_PROVIDER_ERROR = "provider_error"
    REASON_PROVIDER_QUOTA = "provider_quota"
    REASON_INVALID_RESPONSE = "invalid_response"

    id = PrimaryKeyField()
    track = ForeignKeyField(Track, unique=True, backref="metadata_enrichment_task")
    status = CharField(max_length=32)
    reason = CharField(max_length=64)
    attempt_count = IntegerField(default=0)
    last_error = TextField(null=True)
    locked_at = DateTimeField(null=True)
    next_retry_at = DateTimeField(null=True)
    force = BooleanField(default=False)
    created_at = DateTimeField(default=now)
    updated_at = DateTimeField(default=now)
    completed_at = DateTimeField(null=True)

    def save(self, *args, **kwargs):
        self.updated_at = now()
        return super().save(*args, **kwargs)

    class Meta:
        table_name = "track_metadata_enrichment_task"
        indexes = (
            (("track",), True),
            (("status", "next_retry_at"), False),
            (("status", "locked_at"), False),
            (("updated_at",), False),
        )


def delete_track_metadata_for_tracks(track_ids: Iterable[object]) -> None:
    raw_ids = list(track_ids)
    if not raw_ids:
        return

    TrackMetadata.delete().where(TrackMetadata.track.in_(raw_ids)).execute()
    TrackMetadataEnrichmentTask.delete().where(
        TrackMetadataEnrichmentTask.track.in_(raw_ids)
    ).execute()
    entity_ids = [str(track_id) for track_id in raw_ids]
    ReviewTask.delete().where(
        ReviewTask.entity_type == "track",
        ReviewTask.entity_id.in_(entity_ids),
    ).execute()


def _read_string_list(raw_value: Optional[str]) -> List[str]:
    if not raw_value:
        return []
    try:
        values = json.loads(raw_value)
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _write_string_list(values: Iterable[object]) -> Optional[str]:
    clean_values = [str(value).strip() for value in values if str(value).strip()]
    return json.dumps(clean_values, ensure_ascii=False) if clean_values else None
