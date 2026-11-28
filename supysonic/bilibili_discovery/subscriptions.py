"""Subscribe a repeatedly watched performer through the Oracle China catalog."""

from datetime import timedelta
import hashlib
import json
import re
from typing import Iterable, List, Optional

from ..album_discovery_candidates import _DiscoveryPreferences
from ..album_discovery_catalog import normalize_music_name
from ..album_discovery_oracle import OracleAlbumClient, OracleSubscriptionUnavailable
from ..db import BilibiliDiscoveryRun as Run, Meta, User
from .history import HistoryObservation


class SubscriptionUnavailable(RuntimeError):
    """Retry the current task after a transient subscription service failure."""


def record_performance(progress: dict, performer: str, observation: HistoryObservation) -> None:
    if observation.business != "archive" or not (observation.bvid or observation.id):
        return
    # A single video can contribute many songs or parts; count it once per performer.
    video = "bv:" + observation.bvid if observation.bvid else "av:" + observation.id
    names = [performer]
    if performer in observation.title and re.search(r"合作|同台|合唱|联唱|聯唱", observation.title):
        names = re.split(r"[+＋、]", performer)
    for name in names:
        name = name.strip()
        key = normalize_music_name(name)
        if key:
            item = progress.setdefault("performers", {}).setdefault(key, {"name": name, "videos": []})
            if video not in item["videos"]:
                item["videos"].append(video)


def repeated_performer_tasks(recent: dict, earlier: Iterable[dict]) -> List[dict]:
    videos = {key: set(item["videos"]) for key, item in recent.items()}
    for previous in earlier:
        for key in videos.keys() & previous.keys():
            videos[key].update(previous[key]["videos"])
    return [
        {"kind": "subscribe_artist", "performer": recent[key]["name"], "distinct_videos": len(ids)}
        for key, ids in videos.items() if len(ids) >= 2
    ]


def build_subscription_plan(run: Run, progress: dict, window_days: int) -> List[dict]:
    recent = progress.get("performers", {})
    if not recent:
        return []
    first_day = run.source_day - timedelta(days=window_days - 1)
    earlier = []
    others = Run.select(Run.discovery_progress_json).where(
        Run.user == run.user_id, Run.source_day >= first_day, Run.source_day < run.source_day,
    )
    for row in others:
        stored = json.loads(row.discovery_progress_json)
        earlier.append(stored.get("performers", {}))
        # A rollout can seed dated evidence from a verified, read-only history replay.
        earlier.extend(performers for day, performers in stored.get("prior_performer_days", {}).items()
                       if first_day.isoformat() <= day < run.source_day.isoformat())
    return repeated_performer_tasks(recent, earlier)


def subscribe_performer(user: User, oracle: Optional[OracleAlbumClient], task: dict) -> str:
    if oracle is None:
        raise SubscriptionUnavailable("Oracle is unavailable")
    performer = task["performer"]
    name = normalize_music_name(performer)
    preferences = _DiscoveryPreferences(user)
    if name in preferences.hidden_artist_names:
        return "artist_subscription_hidden"
    try:
        matches = {row.artist_id: row for row in oracle.search_subscription_artists(performer)
                   if normalize_music_name(row.name) == name}
        if len(matches) != 1:
            return "artist_subscription_ambiguous" if matches else "artist_subscription_unverified"
        artist = next(iter(matches.values()))
        task["cn_artist_id"] = artist.artist_id
        if not preferences.artist_allowed(artist):
            return "artist_subscription_hidden"
        # A later manual unsubscribe stays effective across runs and service restarts.
        identity = f"{user.id}:{artist.artist_id}".encode()
        key = "bili-f:" + hashlib.sha256(identity).hexdigest()[:25]
        if Meta.get_or_none(Meta.key == key):
            return "artist_subscription_handled"
        outcome = oracle.ensure_artist_subscription(artist)
        Meta.get_or_create(key=key, defaults={"value": artist.artist_id})
        return outcome
    except OracleSubscriptionUnavailable as exc:
        raise SubscriptionUnavailable("Oracle artist subscription deferred") from exc
