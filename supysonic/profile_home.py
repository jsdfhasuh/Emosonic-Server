"""Read-only listening summaries for the web profile."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from peewee import fn

from .db import Artist, Image, Track, User, User_Play_Activity, now
from .user_listening_profile import (
    RECOMMENDATION_LANGUAGE_ALIASES,
    build_track_count_listening_profile,
)


PROFILE_PERIODS = {"7": 7, "30": 30, "all": None}
LANGUAGE_LABELS = {
    "zh": ("Mandarin / Chinese", "国语"),
    "yue": ("Cantonese", "粤语"),
    "en": ("English", "英语"),
    "ko": ("Korean", "韩语"),
    "ja": ("Japanese", "日语"),
    "fr": ("French", "法语"),
    "es": ("Spanish", "西班牙语"),
    "de": ("German", "德语"),
    "other": ("Other", "其他"),
}


def _language_shares(raw_counts: dict[str, int]) -> list[dict]:
    counts = Counter()
    for language, plays in raw_counts.items():
        code = RECOMMENDATION_LANGUAGE_ALIASES.get(language, language)
        counts[code] += plays

    total = sum(counts.values())
    shares = []
    for code, plays in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]:
        label, label_zh = LANGUAGE_LABELS.get(code, (code, code))
        shares.append({
            "label": label, "label_zh": label_zh, "play_count": plays,
            "share": round(100 * plays / total, 1),
        })
    return shares


def build_profile_home(user: User, period: str, reference_time: datetime | None = None) -> dict:
    period = period if period in PROFILE_PERIODS else "7"
    reference_time = reference_time or now()
    activity = User_Play_Activity
    played = (activity.user == user) & (activity.time <= reference_time)
    days = PROFILE_PERIODS[period]
    if days is not None:
        played &= activity.time >= reference_time - timedelta(days=days)

    plays = fn.COUNT(activity.id)
    rows = list(
        activity.select(
            Track.id, Track.title, Track.album.alias("album_id"), Artist.name.alias("artist"),
            plays.alias("plays"), fn.MAX(activity.time).alias("last_play"),
        )
        .join(Track).join(Artist, on=(Track.artist == Artist.id))
        .where(played)
        .group_by(Track.id, Track.title, Track.album, Artist.name)
        .order_by(plays.desc(), Track.title, Track.id)
        .dicts()
    )
    track_counts = {row["id"]: row["plays"] for row in rows}
    play_count = sum(track_counts.values())
    metadata = build_track_count_listening_profile(track_counts)
    covered_plays = metadata["playCount"]

    artist_counts = Counter()
    for row in rows:
        artist_counts[row["artist"]] += row["plays"]
    top_artists = sorted(artist_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    top_tracks = rows[:5]
    top_album_ids = {str(track["album_id"]) for track in top_tracks}
    cover_album_ids = {
        image.related_id for image in Image.select(Image.related_id).where(
            Image.image_type == "album", Image.related_id.in_(top_album_ids),
        )
    } if top_album_ids else set()
    for track in top_tracks:
        track["has_art"] = str(track["album_id"]) in cover_album_ids

    return {
        "period": period,
        "play_count": play_count,
        "track_count": len(track_counts),
        "covered_plays": covered_plays,
        "coverage": round(100 * covered_plays / play_count) if play_count else 0,
        "languages": _language_shares(metadata["languageCounts"]),
        "moods": metadata["topMoods"],
        "scenes": metadata["topScenes"],
        "tags": metadata["topTags"],
        "energy": metadata["averageEnergy"],
        "danceability": metadata["averageDanceability"],
        "valence": metadata["averageValence"],
        "top_tracks": top_tracks,
        "top_artists": [{"name": name, "plays": plays} for name, plays in top_artists],
        "last_play": max((row["last_play"] for row in rows), default=None),
    }
